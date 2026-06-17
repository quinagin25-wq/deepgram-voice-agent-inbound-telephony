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

SYSTEM_PROMPT = """You are Maya, an outbound sales representative calling on behalf of craftd — a done-for-you website and local SEO service built for contractors and tradespeople in North Carolina.

Your goal is to get the contractor interested and transfer them to a live rep for a 30-minute Zoom walkthrough. If they're not available now, book a callback.

VOICE FORMATTING RULES:
- Use only plain conversational language
- NO markdown, emojis, or special formatting
- Keep responses to 1-2 sentences per turn
- Never pressure. If they say no, thank them and end politely.
- Only transfer when the contractor gives explicit verbal agreement.
- If they ask about price say: Our rep will walk you through everything on the call, plans start under a hundred dollars.

FLOW:
After the user responds to the greeting, say: We help contractors in NC get found online without the tech headache. Do you currently have a website for your business?

If NO website: That's exactly who we work with. We build and manage everything for you. Would you be open to a free 30-minute call to see what it looks like for your trade?
If YES website: Got it, we also help contractors who have a site but aren't getting leads from it. Same question, open to a quick call?
If YES to call: say Perfect, let me connect you with someone on our team right now. Then end the call gracefully.
If not available: ask what day works better and note the callback.

COMPLIANCE:
Never claim to be human if asked.
End call professionally if hostile or unresponsive after two attempts."""

GREETING = "Hi, this is Maya calling from craftd — I'm an AI assistant. Is this a good time for a quick second?" 

# ---------------------------------------------------------------------------
# Function definitions
# ---------------------------------------------------------------------------
# Each function maps to a method in backend/scheduling_service.py.
# See docs/FUNCTION_GUIDE.md for definition best practices.

FUNCTIONS = [
    ThinkSettingsV1FunctionsItem(
        name="check_available_slots",
        description="""Check available appointment slots. Call this when a patient asks about availability.

You can optionally filter by date and/or provider. If the patient doesn't specify, omit both parameters to get a general overview of upcoming availability — do NOT call this once per day.

IMPORTANT: You must call this function before booking. The results include slot_id values that are required for book_appointment.

This is a read-only lookup — no confirmation needed before calling.""",
        parameters={
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date to check in YYYY-MM-DD format. If the patient says 'Monday' or 'next week', convert to a specific date."
                },
                "provider": {
                    "type": "string",
                    "description": "Provider name to filter by (e.g., 'Dr. Chen', 'Lisa Thompson'). Omit to see all providers."
                }
            },
            "required": []
        }
    ),
    ThinkSettingsV1FunctionsItem(
        name="book_appointment",
        description="""Book an appointment for a patient.

IMPORTANT: Before calling this function, you MUST:
1. Call check_available_slots to get available slots with their slot_id values
2. Confirm the appointment details with the patient (date, time, provider, service type)
3. Collect the patient's name and phone number
4. WAIT for the patient to say "yes" or confirm

Only call this after the patient has explicitly agreed to the booking. The slot_id must come from a check_available_slots result.""",
        parameters={
            "type": "object",
            "properties": {
                "patient_name": {
                    "type": "string",
                    "description": "Full name of the patient"
                },
                "patient_phone": {
                    "type": "string",
                    "description": "Patient phone number"
                },
                "slot_id": {
                    "type": "string",
                    "description": "The slot_id value from check_available_slots results (e.g. 'slot-a1b2c3d4'). You MUST call check_available_slots first and use the exact slot_id from the results. Do NOT use a date string."
                }
            },
            "required": ["patient_name", "patient_phone", "slot_id"]
        }
    ),
    ThinkSettingsV1FunctionsItem(
        name="check_appointment",
        description="""Look up a patient's existing appointment. Call this when a patient asks about an appointment they already have.

Provide either the patient's name or phone number (or both). This is a read-only lookup — no confirmation needed.""",
        parameters={
            "type": "object",
            "properties": {
                "patient_name": {
                    "type": "string",
                    "description": "Patient name to search for"
                },
                "patient_phone": {
                    "type": "string",
                    "description": "Patient phone number to search for"
                }
            },
            "required": []
        }
    ),
    ThinkSettingsV1FunctionsItem(
        name="cancel_appointment",
        description="""Cancel an existing appointment.

IMPORTANT: Before calling this function, you MUST:
1. Look up the appointment first using check_appointment
2. Confirm with the patient: "I can cancel your [service] appointment on [date] at [time] with [provider]. Are you sure?"
3. WAIT for the patient to confirm

Only call this after the patient has explicitly confirmed they want to cancel.""",
        parameters={
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "string",
                    "description": "The appointment ID from check_appointment results"
                }
            },
            "required": ["appointment_id"]
        }
    ),
    ThinkSettingsV1FunctionsItem(
        name="end_call",
        description="""End the phone call gracefully.

Call this after:
- The patient says goodbye
- The conversation has naturally concluded
- You've said your closing remarks

Say goodbye FIRST, then call this function. Do not generate text after calling it.""",
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the call is ending",
                    "enum": ["appointment_booked", "customer_goodbye", "no_action_needed"]
                }
            },
            "required": ["reason"]
        }
    ),
  ThinkSettingsV1FunctionsItem(
        name="transfer_call",
        description="""Transfer the call to a live sales representative.

Call this when the contractor explicitly agrees to speak with someone on the team.
Say "Perfect, let me connect you with someone on our team right now." FIRST, then call this function.""",
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the call is being transferred",
                    "enum": ["interested_in_service", "requested_rep"]
                }
            },
            "required": ["reason"]
        }
    ),
]


# ---------------------------------------------------------------------------
# Build the settings message
# ---------------------------------------------------------------------------

def get_agent_config() -> AgentV1Settings:
    """Build the Voice Agent settings message for Deepgram.

    This is sent once per call when the Deepgram connection is established.
    It configures STT, LLM, TTS, and the agent's prompt and tools.
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
                prompt=SYSTEM_PROMPT,
                functions=FUNCTIONS,
            ),
            speak=SpeakSettingsV1(
                provider=SpeakSettingsV1Provider_Deepgram(
                    type="deepgram",
                    model=VOICE_MODEL,
                ),
            ),
            greeting=GREETING,
        ),
    )
