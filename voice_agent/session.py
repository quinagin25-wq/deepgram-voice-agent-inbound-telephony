"""
VoiceAgentSession - manages a single Deepgram Voice Agent connection for one phone call.

This is the core of the audio pipeline.  It bridges two WebSocket connections:

  Twilio WebSocket  ←→  VoiceAgentSession  ←→  Deepgram Voice Agent API

Audio flow:
  1. Twilio sends mulaw audio as base64 JSON → we decode → send raw bytes to Deepgram
  2. Deepgram sends raw mulaw bytes back   → we encode to base64 → send JSON to Twilio

The session also handles:
  - Barge-in (sending Twilio "clear" events when the user starts speaking)
  - Function call dispatch (routing to backend/scheduling_service.py)
  - Transcript logging
  - Lifecycle management (connect, run, cleanup)

"""
import asyncio
import base64
import json
import logging

from starlette.websockets import WebSocket

from deepgram import AsyncDeepgramClient
from deepgram.core.pydantic_utilities import parse_obj_as
from deepgram.agent.v1 import (
    AgentV1SettingsApplied,
    AgentV1FunctionCallRequest,
    AgentV1ConversationText,
    AgentV1UserStartedSpeaking,
    AgentV1AgentAudioDone,
    AgentV1Error,
    AgentV1Warning,
    AgentV1SendFunctionCallResponse,
)
from deepgram.agent.v1.socket_client import V1SocketClientResponse

from voice_agent.agent_config import get_agent_config

logger = logging.getLogger(__name__)


class VoiceAgentSession:
    """Manages one Deepgram Voice Agent session for the lifetime of a phone call."""

    def __init__(self, twilio_ws: WebSocket, call_sid: str, stream_sid: str, contractor: dict = None):
        self.twilio_ws = twilio_ws
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        self.contractor = contractor  # dict from contractor_lookup, or None if unknown caller

        self._client = None
        self._connection = None
        self._context_manager = None

        self._settings_applied = asyncio.Event()
        self._cleanup_done = False

        self._listen_task = None
        self._audio_task = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Connect to Deepgram Voice Agent API, configure, and start processing audio."""
        logger.info(f"[SESSION:{self.call_sid}] Connecting to Deepgram Voice Agent API")

        self._client = AsyncDeepgramClient()
        self._context_manager = self._client.agent.v1.connect()
        self._connection = await self._context_manager.__aenter__()

        self._listen_task = asyncio.create_task(self._listen_loop())

        config = get_agent_config(contractor=self.contractor)
        await self._connection.send_settings(config)

        try:
            await asyncio.wait_for(self._settings_applied.wait(), timeout=5.0)
            logger.info(f"[SESSION:{self.call_sid}] Settings applied - ready for audio")
        except asyncio.TimeoutError:
            logger.error(f"[SESSION:{self.call_sid}] Timeout waiting for settings to be applied")
            raise

    async def run(self):
        """Forward audio from Twilio to Deepgram until the call ends."""
        self._audio_task = asyncio.create_task(self._forward_twilio_audio())

        done, pending = await asyncio.wait(
            [self._audio_task, self._listen_task],
            return_when=asyncio.FIRST_COMPLETED,
        )

        for task in pending:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        logger.info(f"[SESSION:{self.call_sid}] Call ended")

    async def cleanup(self):
        """Release all resources. Safe to call multiple times."""
        if self._cleanup_done:
            return
        self._cleanup_done = True

        logger.info(f"[SESSION:{self.call_sid}] Cleaning up")

        for task in [self._audio_task, self._listen_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._context_manager:
            try:
                await self._context_manager.__aexit__(None, None, None)
            except Exception as e:
                logger.debug(f"[SESSION:{self.call_sid}] Error during Deepgram cleanup: {e}")

        self._connection = None
        self._client = None
        logger.info(f"[SESSION:{self.call_sid}] Cleanup complete")

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _listen_loop(self):
        """Read messages from Deepgram, skipping any the SDK can't parse."""
        try:
            async for raw_message in self._connection._websocket:
                try:
                    if isinstance(raw_message, bytes):
                        parsed = raw_message
                    else:
                        json_data = json.loads(raw_message)
                        parsed = parse_obj_as(V1SocketClientResponse, json_data)
                except Exception:
                    msg_type = json_data.get("type", "unknown") if isinstance(raw_message, str) else "binary"
                    logger.debug(f"[SESSION:{self.call_sid}] Skipping unrecognized message type: {msg_type}")
                    continue

                if isinstance(parsed, AgentV1SettingsApplied):
                    self._settings_applied.set()
                else:
                    await self._handle_message(parsed)
        except Exception as e:
            logger.info(f"[SESSION:{self.call_sid}] Deepgram listen loop ended: {e}")
        finally:
            logger.info(f"[SESSION:{self.call_sid}] Deepgram connection closed")

    async def _handle_message(self, message):
        """Process a single message from the Deepgram Voice Agent."""
        try:
            if isinstance(message, bytes):
                audio_b64 = base64.b64encode(message).decode("utf-8")
                await self.twilio_ws.send_json({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": audio_b64},
                })

            elif isinstance(message, AgentV1FunctionCallRequest):
                await self._handle_function_call(message)

            elif isinstance(message, AgentV1ConversationText):
                logger.info(f"[SESSION:{self.call_sid}] {message.role.upper()}: {message.content}")

            elif isinstance(message, AgentV1UserStartedSpeaking):
                logger.info(f"[SESSION:{self.call_sid}] User started speaking")
                await self.twilio_ws.send_json({
                    "event": "clear",
                    "streamSid": self.stream_sid,
                })

            elif isinstance(message, AgentV1AgentAudioDone):
                logger.debug(f"[SESSION:{self.call_sid}] Agent finished speaking")

            elif isinstance(message, AgentV1Error):
                logger.error(f"[SESSION:{self.call_sid}] Agent error: {message.description}")
            elif isinstance(message, AgentV1Warning):
                logger.warning(f"[SESSION:{self.call_sid}] Agent warning: {message.description}")

        except Exception as e:
            logger.error(f"[SESSION:{self.call_sid}] Error handling message: {e}")

    # ------------------------------------------------------------------
    # Function calls
    # ------------------------------------------------------------------

    async def _handle_function_call(self, event: AgentV1FunctionCallRequest):
        """Dispatch a function call from the agent to the backend service."""
        if not event.functions:
            return

        func = event.functions[0]
        function_name = func.name
        call_id = func.id
        args = json.loads(func.arguments) if func.arguments else {}

        logger.info(f"[SESSION:{self.call_sid}] Function call: {function_name}({args})")

        try:
            from voice_agent.function_handlers import dispatch_function
            result = await dispatch_function(function_name, args, contractor=self.contractor)
            logger.info(f"[SESSION:{self.call_sid}] Function result: {function_name} → {json.dumps(result)}")
        except Exception as e:
            logger.error(f"[SESSION:{self.call_sid}] Function error: {function_name} → {e}")
            result = {"error": str(e)}

        response = AgentV1SendFunctionCallResponse(
            type="FunctionCallResponse",
            name=function_name,
            content=json.dumps(result),
            id=call_id,
        )
        await self._connection.send_function_call_response(response)

        if function_name == "end_call":
            asyncio.create_task(self._end_call_after_delay())

        if function_name == "transfer_call":
            # Log immediately, before the delay - so "nothing happened" and
            # "transfer hasn't started yet" are distinguishable in logs and
            # in the dialer UI. Previously the first log line came AFTER a
            # 2s sleep, which made a working-but-slow transfer look
            # identical to a broken one to anyone watching logs or the
            # dashboard in real time.
            logger.info(f"[SESSION:{self.call_sid}] Transfer requested - starting transfer sequence")
            self._set_dialer_status("transferring")
            asyncio.create_task(self._transfer_call_after_delay())

    # ------------------------------------------------------------------
    # Call termination
    # ------------------------------------------------------------------

    async def _end_call_after_delay(self):
        """Wait for goodbye audio then hang up."""
        await asyncio.sleep(3)

        logger.info(f"[SESSION:{self.call_sid}] Hanging up call")

        from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
        if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            try:
                from twilio.rest import Client
                client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
                await asyncio.to_thread(
                    client.calls(self.call_sid).update,
                    status="completed",
                )
                logger.info(f"[SESSION:{self.call_sid}] Twilio call completed")
            except Exception as e:
                logger.error(f"[SESSION:{self.call_sid}] Failed to complete Twilio call: {e}")

        try:
            await self.twilio_ws.close()
        except Exception:
            pass

    async def _transfer_call_after_delay(self):
        """Redirect the live call to a rep via Twilio's REST API.

        Sequence:
          1. Brief pause so Maya's "let me connect you" line finishes
             playing before we yank the call out of the media stream.
          2. Build TwiML that dials the rep, with a timeout + fallback so
             an unanswered/busy rep doesn't strand the caller in dead air.
          3. PATCH the live call (client.calls(sid).update(twiml=...)).
             This is the correct mechanism, but it must be applied to a
             call currently in <Connect><Stream> - see note below.
          4. Only close OUR websocket after we've confirmed the Twilio
             update call returned successfully. Closing first created a
             race in the old code: Twilio could end up processing "stream
             closed" before or instead of "switch to this TwiML", so the
             dial to the rep sometimes never happened at all.

        On any failure (misconfiguration, Twilio error, timeout), we log
        loudly with ERROR level and update the dialer's visible status so
        a human watching the dashboard sees "transfer_failed" instead of
        silence - the original bug report was specifically that nothing
        observable happened, which led an operator to manually hang up
        mid-transfer.
        """
        # Long enough for "Perfect, let me connect you..." to finish
        # playing over the Twilio media stream, short enough that it
        # doesn't look stalled on the dialer dashboard.
        await asyncio.sleep(1.5)

        from config import TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, REP_PHONE_NUMBER, SERVER_EXTERNAL_URL, WEBHOOK_SECRET

        missing = [
            name for name, val in [
                ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
                ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
                ("REP_PHONE_NUMBER", REP_PHONE_NUMBER),
            ] if not val
        ]
        if missing:
            logger.error(
                f"[SESSION:{self.call_sid}] TRANSFER FAILED - missing config: {', '.join(missing)}. "
                f"Set these in the environment before transfer_call can work."
            )
            self._set_dialer_status("transfer_failed")
            await self._close_twilio_ws()
            return

        # Fallback TwiML if the rep doesn't pick up - read by Twilio's
        # action callback instead of leaving the caller connected to a
        # ringing-forever Dial. Without this, a no-answer/busy rep means
        # the caller just hears ringing until Twilio's own hard timeout.
        host = (SERVER_EXTERNAL_URL or "").replace("https://", "").replace("http://", "").rstrip("/")
        fallback_path = f"/transfer-fallback/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/transfer-fallback"
        fallback_url = f"https://{host}{fallback_path}" if host else None

        action_attr = f' action="{fallback_url}" method="POST"' if fallback_url else ""
        twiml = (
            f'<Response><Dial timeout="20"{action_attr}>{REP_PHONE_NUMBER}</Dial></Response>'
        )

        from twilio.rest import Client
        from twilio.base.exceptions import TwilioRestException

        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

        try:
            logger.info(f"[SESSION:{self.call_sid}] Sending Twilio redirect to dial {REP_PHONE_NUMBER}")
            result = await asyncio.to_thread(
                client.calls(self.call_sid).update,
                twiml=twiml,
            )
            logger.info(
                f"[SESSION:{self.call_sid}] Twilio accepted transfer redirect "
                f"(call status now: {getattr(result, 'status', 'unknown')})"
            )
            self._set_dialer_status("transferring_rep_dialing")
        except TwilioRestException as e:
            # Common causes: call already ended (caller hung up first),
            # or the call is no longer in a state that accepts redirects.
            logger.error(
                f"[SESSION:{self.call_sid}] TRANSFER FAILED - Twilio rejected the "
                f"redirect (code={e.code}, status={e.status}): {e.msg}"
            )
            self._set_dialer_status("transfer_failed")
            await self._close_twilio_ws()
            return
        except Exception as e:
            logger.error(f"[SESSION:{self.call_sid}] TRANSFER FAILED - unexpected error: {e}")
            self._set_dialer_status("transfer_failed")
            await self._close_twilio_ws()
            return

        # Only close our side of the media stream now that Twilio has
        # confirmed it accepted the new TwiML. Twilio itself will tear
        # down the <Connect><Stream> as part of switching to <Dial>; we
        # don't need to race it by closing first.
        await self._close_twilio_ws()

    def _set_dialer_status(self, status: str):
        """Best-effort update to the dialer's polled call-state dict, so a
        human watching the dashboard sees real-time transfer progress
        instead of an unexplained gap. Safe no-op if the dialer module
        isn't loaded or the call_sid doesn't match what the dialer thinks
        is active (e.g. local/dev test calls)."""
        try:
            from dialer.routes import _active_call_lock
            if _active_call_lock.get("call_sid") == self.call_sid:
                _active_call_lock["status"] = status
        except Exception as e:
            logger.debug(f"[SESSION:{self.call_sid}] Could not update dialer status: {e}")

    async def _close_twilio_ws(self):
        try:
            await self.twilio_ws.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Audio forwarding
    # ------------------------------------------------------------------

    async def _forward_twilio_audio(self):
        """Read Twilio WebSocket messages and forward audio to Deepgram."""
        try:
            while True:
                message = await self.twilio_ws.receive_text()
                data = json.loads(message)

                if data.get("event") == "media":
                    payload = data["media"]["payload"]
                    audio_bytes = base64.b64decode(payload)
                    if self._connection:
                        await self._connection.send_media(audio_bytes)

                elif data.get("event") == "stop":
                    logger.info(f"[SESSION:{self.call_sid}] Twilio stream stopped")
                    break

        except Exception as e:
            logger.info(f"[SESSION:{self.call_sid}] Twilio WebSocket closed: {e}")
