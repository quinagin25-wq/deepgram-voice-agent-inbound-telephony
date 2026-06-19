"""
Telephony routes - handles Twilio webhook and audio stream.

Two endpoints:

  POST /incoming-call
    Twilio hits this when someone calls your phone number (inbound), or
    when an outbound call we initiated connects (the dialer points
    Twilio's outbound call at this same webhook). Returns TwiML that
    tells Twilio to open a WebSocket audio stream back to our /twilio
    endpoint.

  WS /twilio
    Receives the audio stream from Twilio (or from dev_client.py in local mode).
    Creates a VoiceAgentSession and bridges audio to/from Deepgram.

The server doesn't know or care whether the WebSocket connection comes from
a real Twilio call or from dev_client.py - both send identical messages.

Contractor lookup:
  Before the WebSocket stream starts, we look up the contractor by phone
  number in Supabase (backend/contractor_lookup.py) and stash the result
  in `pending_contractors`, keyed by CallSid. When the WebSocket's "start"
  event arrives with the matching CallSid, VoiceAgentSession picks up that
  context so Maya knows who she's talking to (owner_name, business_name,
  email) before the conversation begins.

  Direction matters for which number is the contractor's:
    - Inbound call (contractor called us): contractor's number is `From`
    - Outbound call (dialer called them):   contractor's number is `To`

Security (when deployed via setup.py):
  - WEBHOOK_SECRET: path token on both endpoints - return 404 on mismatch
  - TWILIO_AUTH_TOKEN: Twilio request signature validation on /incoming-call
"""
import json
import logging

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from starlette.websockets import WebSocket

from config import SERVER_EXTERNAL_URL, TWILIO_AUTH_TOKEN, WEBHOOK_SECRET
from voice_agent.session import VoiceAgentSession
from backend.contractor_lookup import get_contractor_by_phone

logger = logging.getLogger(__name__)

if TWILIO_AUTH_TOKEN:
    from twilio.request_validator import RequestValidator
    _twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
else:
    _twilio_validator = None


def _check_webhook_secret(path_params: dict) -> bool:
    """Return True if the request passes the webhook secret check.

    If WEBHOOK_SECRET is not set, all requests pass (local dev mode).
    If set, the token path parameter must match exactly.
    """
    if not WEBHOOK_SECRET:
        return True
    token = path_params.get("token", "")
    return token == WEBHOOK_SECRET

# Active sessions, keyed by call_sid.  Used for monitoring and cleanup.
active_sessions: dict[str, VoiceAgentSession] = {}

# Contractor records resolved during the /incoming-call webhook, keyed by
# CallSid, waiting to be picked up once the matching WebSocket "start"
# event arrives. Entries are popped (not just read) once consumed, so this
# never grows unbounded.
pending_contractors: dict[str, dict] = {}


async def incoming_call(request: Request) -> Response:
    """Handle Twilio webhook for inbound calls.

    When someone calls your Twilio number, Twilio makes an HTTP POST here.
    We respond with TwiML that tells Twilio to open a bidirectional audio
    stream back to our /twilio WebSocket endpoint.

    The SERVER_EXTERNAL_URL env var controls the WebSocket URL in the TwiML.
    If it's not set, we fall back to the Host header (works for local dev
    with dev_client.py, but not for real Twilio calls - Twilio can't reach
    localhost).
    """
    # Check webhook secret (path token)
    if not _check_webhook_secret(request.path_params):
        return Response(status_code=404)

    # Always parse form data - we need it for the contractor lookup
    # regardless of whether signature validation is on.
    form_data = await request.form()
    params = dict(form_data)

    # Validate Twilio request signature (only when TWILIO_AUTH_TOKEN is set)
    if _twilio_validator:
        url = str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")
        logger.info(f"[TELEPHONY] Signature validation - url={url} signature={signature[:20] + '...' if signature else 'MISSING'}")
        if not _twilio_validator.validate(url, params, signature):
            logger.warning("[TELEPHONY] Invalid Twilio signature - rejecting request")
            return Response(status_code=404)

    # Resolve the contractor's number based on call direction.
    # Outbound calls (placed by the dialer) report direction starting with
    # "outbound"; the contractor is the number we called (`To`).
    # Inbound calls: the contractor is whoever called us (`From`).
    call_sid = params.get("CallSid", "unknown")
    direction = params.get("Direction", "inbound")
    contractor_number = params.get("To") if direction.startswith("outbound") else params.get("From")

    if contractor_number:
        contractor = await get_contractor_by_phone(contractor_number)
        if contractor:
            pending_contractors[call_sid] = contractor
            logger.info(
                f"[TELEPHONY] CallSid={call_sid} matched contractor "
                f"{contractor.get('owner_name')} / {contractor.get('business_name')}"
            )
        else:
            logger.info(f"[TELEPHONY] CallSid={call_sid} - no contractor record for {contractor_number}")

    # Use configured external URL, or fall back to the request's Host header.
    if SERVER_EXTERNAL_URL:
        # Strip protocol prefix - TwiML needs a bare hostname for wss://
        host = SERVER_EXTERNAL_URL.replace("https://", "").replace("http://", "").rstrip("/")
    else:
        host = request.headers.get("host", "localhost:8080")

    # Build the WebSocket URL - include the webhook secret token if configured
    ws_path = "/twilio"
    if WEBHOOK_SECRET:
        ws_path = f"/twilio/{WEBHOOK_SECRET}"

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}{ws_path}" />
    </Connect>
</Response>"""

    logger.info(f"[TELEPHONY] Incoming call - streaming to wss://{host}{ws_path}")

    return Response(content=twiml, media_type="application/xml")


async def twilio_websocket(websocket: WebSocket):
    """Handle a Twilio audio stream (or dev_client.py mock stream).

    Protocol:
      1. Twilio opens the WebSocket and sends a "connected" event
      2. Twilio sends a "start" event with callSid and streamSid
      3. Twilio sends "media" events with base64-encoded mulaw audio
      4. We send "media" events back with agent audio
      5. Twilio sends a "stop" event when the call ends

    This handler creates a VoiceAgentSession and delegates all audio
    processing to it.  The session handles the Deepgram connection,
    audio bridging, function calls, and cleanup.
    """
    # Check webhook secret (path token)
    if not _check_webhook_secret(websocket.path_params):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info("[TELEPHONY] WebSocket connected")

    call_sid = None
    stream_sid = None
    session = None

    try:
        # Wait for the Twilio "start" event to get call metadata.
        while True:
            message = await websocket.receive_text()
            data = json.loads(message)

            if data.get("event") == "start":
                call_sid = data["start"].get("callSid", "unknown")
                stream_sid = data["start"].get("streamSid", "unknown")
                logger.info(f"[TELEPHONY] Call started - callSid={call_sid}")
                break
            elif data.get("event") == "connected":
                # Twilio sends this first, before "start".  Nothing to do.
                continue

        # Pick up the contractor record resolved during /incoming-call, if any.
        # pop() so this dict never grows unbounded across calls.
        contractor = pending_contractors.pop(call_sid, None)
        if contractor:
            logger.info(f"[TELEPHONY] CallSid={call_sid} - using contractor context for {contractor.get('owner_name')}")
        else:
            logger.info(f"[TELEPHONY] CallSid={call_sid} - no contractor context, Maya will use generic greeting")

        # Create and start the voice agent session.
        session = VoiceAgentSession(websocket, call_sid, stream_sid, contractor=contractor)
        active_sessions[call_sid] = session

        await session.start()
        await session.run()

    except Exception as e:
        logger.error(f"[TELEPHONY] Error in call {call_sid}: {e}")
    finally:
        if session:
            await session.cleanup()
        if call_sid and call_sid in active_sessions:
            del active_sessions[call_sid]
        logger.info(f"[TELEPHONY] Call {call_sid} ended")


# Starlette routes - imported by main.py.
# Each endpoint has two route entries: with and without a {token} path param.
# When WEBHOOK_SECRET is set, only requests with the correct token are accepted.
# When WEBHOOK_SECRET is not set, both routes work (local dev mode).
telephony_routes = [
    Route("/incoming-call/{token:path}", incoming_call, methods=["POST"]),
    Route("/incoming-call", incoming_call, methods=["POST"]),
]
