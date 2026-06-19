"""
Agent configuration - defines the voice agent's personality, capabilities, and audio settings.

This configures Deepgram's Voice Agent API with:
  - Audio encoding (mulaw 8kHz for Twilio compatibility)
  - Speech-to-text (Deepgram Flux)
  - LLM (configurable, defaults to gpt-4o-mini)
  - Text-to-speech (Deepgram Aura)
  - System prompt (dental office receptionist)
  - Function definitions (scheduling operations)

To customize the agent's behavior, modify the SYSTEM_PROMPT and FUNCTIONS below.
To swap the LLM or voice, change LLM_MODEL / VOICE_MODEL in your .env file.
"""
from datetime import date

from config import VOICE_MODEL, LLM_MODEL
from deepgram.agent.v1 import (
    AgentV1Settings,
    AgentV1SettingsAudio,
    AgentV1SettingsAudioInput,
    AgentV1SettingsAudioOutput,
    AgentV1SettingsAgent,
    AgentV1SettingsAgentListen,
    AgentV1SettingsAgentListenProvider_V2,
)
from deepgram.types.think_settings_v1 import ThinkSettingsV1
from deepgram.types.think_settings_v1provider import ThinkSettingsV1Provider_OpenAi
from deepgram.types.think_settings_v1functions_item import ThinkSettingsV1FunctionsItem
from deepgram.types.speak_settings_v1 import SpeakSettingsV1
from deepgram.types.speak_settings_v1provider import SpeakSettingsV1Provider_Deepgram

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
# This prompt follows voice-specific best practices from docs/PROMPT_GUIDE.md.
# Key rules: short turns, plain language, no markdown, confirm-then-act for
# function calls.

_TODAY = date.today()
_TODAY_STR = _TODAY.strftime("%A, %B %-d, %Y")  # e.g. "Monday, February 24, 2026"

BASE_SYSTEM_PROMPT = """You are Maya, an outbound sales representative calling on behalf of craftd — a done-for-you website and local SEO service built for contractors and tradespeople in North Carolina.

Your goal is to find out if the contractor can talk for 30 minutes right now — if so, transfer them to a live rep immediately. If they can't talk now, book a real meeting on the calendar for the soonest time that works for them instead.

VOICE FORMATTING RULES:
- Use only plain conversational language
- NO markdown, emojis, or special formatting
- Keep responses to 1-2 sentences per turn
- Never pressure. If they say no, thank them and end politely.
- Only book a meeting when the contractor gives explicit verbal agreement to a specific time.
- If they ask about price say: Our rep will walk you through everything on the call, plans start under a hundred dollars.

FLOW:
After the user responds to the greeting, say: We help contractors in NC get found online without the tech headache. Do you currently have a website for your business?

If NO website: That's exactly who we work with. We build and manage everything for you.
If YES website: Got it, we also help contractors who have a site but aren't getting leads from it.

Either way, once they show interest, find out if they can talk right now versus needing to book something for later: ask something like "Do you have about 30 minutes free right now to walk through it, or would it be easier to grab a time later today or this week?"

If they can talk NOW: say "Perfect, let me connect you with someone on our team right now" and call transfer_call. Do not call check_availability or book_meeting in this path — the live transfer is the goal.
If they CANNOT talk now, or prefer to schedule: use check_availability and offer the soonest realistic times, then use book_meeting once they agree to one. Default to finding the soonest available slot rather than pushing far out, unless the contractor specifically asks for a different day or time.
If not interested in either right now: thank them and end the call gracefully.

EMAIL VERIFICATION (required before booking):
{email_verification_block}

COMPLIANCE:
Never claim to be human if asked.
End call professionally if hostile or unresponsive after two attempts."""

_EMAIL_VERIFICATION_WITH_RECORD = """You already have an email on file for this contractor: {email}. Before booking, read it back naturally and confirm it's still correct — for example: "I have your email as {email}, is that still the best one to send the invite to?" If they confirm, proceed with that email. If they give a different one, use that corrected email for the booking instead, and set email_was_corrected to true when you call book_meeting."""

_EMAIL_VERIFICATION_NO_RECORD = """You do not have an email on file for this contractor. Before booking, ask for their email directly and read it back to confirm you have it right before calling book_meeting. Set email_was_corrected to true and pass the email they give you."""


def _build_system_prompt(contractor: dict = None) -> str:
    if contractor and (contractor.get("email") or contractor.get("corrected_email")):
        from backend.contractor_lookup import get_effective_email
        email = get_effective_email(contractor)
        email_block = _EMAIL_VERIFICATION_WITH_RECORD.format(email=email)
    else:
        email_block = _EMAIL_VERIFICATION_NO_RECORD

    return BASE_SYSTEM_PROMPT.format(email_verification_block=email_block)


def _build_greeting(contractor: dict = None) -> str:
    """Personalized greeting when we know who we're calling, generic fallback otherwise."""
    if contractor and contractor.get("owner_name"):
        owner_name = contractor["owner_name"].split(" ")[0]  # first name only, sounds natural
        business_name = contractor.get("business_name")
        if business_name:
            return f"Hi, can I speak with {owner_name}? This is Maya, an AI assistant calling from craftd about {business_name}."
        return f"Hi, is this {owner_name}? This is Maya, an AI assistant calling from craftd."

    return "Hi, this is Maya calling from craftd — I'm an AI assistant. Is this a good time for a quick second?"

# ---------------------------------------------------------------------------
# Function definitions
# ---------------------------------------------------------------------------
# Each function maps to a method in backend/scheduling_service.py.
# See docs/FUNCTION_GUIDE.md for definition best practices.

FUNCTIONS = [
    ThinkSettingsV1FunctionsItem(
        name="check_availability",
        description="""Check real open times on the calendar for the craftd walkthrough call.

Call this before offering any specific times to the contractor. Returns up to 5 upcoming open slots within the next 7 days. If the contractor names a specific date, pass it to filter — otherwise omit it for a general overview.

This is a read-only lookup — no confirmation needed before calling. You MUST call this before book_meeting; book_meeting requires a start_time that came from this function's results.""",
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to check in YYYY-MM-DD format, if the contractor named a specific day. Omit for a general next-7-days overview."
                }
            },
            "required": []
        }
    ),
    ThinkSettingsV1FunctionsItem(
        name="book_meeting",
        description="""Book a real meeting on the calendar. This sends the contractor an actual calendar invite and confirmation email - it is a live booking, not a note.

IMPORTANT: Before calling this function, you MUST:
1. Call check_availability and offer the contractor one of the real returned times
2. Verify the contractor's email out loud per the EMAIL VERIFICATION instructions in your prompt
3. WAIT for the contractor to explicitly agree to both the time and the email

The start_time you pass MUST be one of the exact start_time values returned by check_availability - do not estimate or convert it yourself.""",
        parameters={
            "type": "object",
            "properties": {
                "start_time": {
                    "type": "string",
                    "description": "The exact start_time value (ISO8601 UTC) from check_availability results. Do NOT construct this yourself."
                },
                "email": {
                    "type": "string",
                    "description": "Only include this if the contractor gave you an email different from the one on file. Omit entirely if they confirmed the email already on file."
                },
                "email_was_corrected": {
                    "type": "boolean",
                    "description": "Set to true only if the contractor gave a new/different email than what you had on file or if none was on file and they gave you one now."
                }
            },
            "required": ["start_time"]
        }
    ),
    ThinkSettingsV1FunctionsItem(
        name="end_call",
        description="""End the phone call gracefully.

Call this after:
- The contractor says goodbye
- The conversation has naturally concluded
- You've said your closing remarks

Say goodbye FIRST, then call this function. Do not generate text after calling it.""",
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the call is ending",
                    "enum": ["meeting_booked", "not_interested", "no_answer", "callback_requested", "customer_goodbye", "no_action_needed"]
                }
            },
            "required": ["reason"]
        }
    ),
  ThinkSettingsV1FunctionsItem(
        name="transfer_call",
        description="""Transfer the call to a live sales representative.

Call this when the contractor confirms they can talk right now for about 30 minutes, or explicitly asks to speak with a real person immediately.
Say "Perfect, let me connect you with someone on our team right now." FIRST, then call this function.

Note: once this is called, you will not learn whether the rep actually answered - the call hands off completely. Only use this when the contractor has agreed to talk now.""",
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the call is being transferred",
                    "enum": ["available_now", "requested_rep"]
                }
            },
            "required": ["reason"]
        }
    ),
]


# ---------------------------------------------------------------------------
# Build the settings message
# ---------------------------------------------------------------------------

def get_agent_config(contractor: dict = None) -> AgentV1Settings:
    """Build the Voice Agent settings message for Deepgram.

    This is sent once per call when the Deepgram connection is established.
    It configures STT, LLM, TTS, and the agent's prompt and tools.

    contractor: record from backend/contractor_lookup.py if the call was
    matched to a known contractor by phone number. None for unknown callers
    (falls back to a generic, non-personalized greeting/prompt).
    """
    return AgentV1Settings(
        type="Settings",
        audio=AgentV1SettingsAudio(
            input=AgentV1SettingsAudioInput(
                encoding="mulaw",
                sample_rate=8000,
            ),
            output=AgentV1SettingsAudioOutput(
                encoding="mulaw",
                sample_rate=8000,
                container="none",
            ),
        ),
        agent=AgentV1SettingsAgent(
            listen=AgentV1SettingsAgentListen(
                provider=AgentV1SettingsAgentListenProvider_V2(
                    version="v2",
                    type="deepgram",
                    model="flux-general-en",
                ),
            ),
            think=ThinkSettingsV1(
                provider=ThinkSettingsV1Provider_OpenAi(
                    type="open_ai",
                    model=LLM_MODEL,
                ),
                prompt=_build_system_prompt(contractor),
                functions=FUNCTIONS,
            ),
            speak=SpeakSettingsV1(
                provider=SpeakSettingsV1Provider_Deepgram(
                    type="deepgram",
                    model=VOICE_MODEL,
                ),
            ),
            greeting=_build_greeting(contractor),
        ),
    )
