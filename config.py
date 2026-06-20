"""
Configuration - environment variable management and validation.
All configuration is loaded from environment variables (via .env file).
Only DEEPGRAM_API_KEY is required. Everything else has sensible defaults
or is optional depending on your setup:
  Local dev:    Just DEEPGRAM_API_KEY
  Telephony:    + SERVER_EXTERNAL_URL (set by setup.py or manually)
  Production:   Same as telephony, deployed behind a real domain
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Required
# ---------------------------------------------------------------------------
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
SERVER_HOST = os.getenv("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.getenv("SERVER_PORT", "8080"))
SERVER_EXTERNAL_URL = os.getenv("SERVER_EXTERNAL_URL")

# ---------------------------------------------------------------------------
# Voice Agent
# ---------------------------------------------------------------------------
VOICE_MODEL = os.getenv("VOICE_MODEL", "aura-2-thalia-en")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# ---------------------------------------------------------------------------
# Twilio
# ---------------------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
REP_PHONE_NUMBER = os.getenv("REP_PHONE_NUMBER")

# ---------------------------------------------------------------------------
# Cal.com
# ---------------------------------------------------------------------------
CALCOM_API_KEY = os.getenv("CALCOM_API_KEY")
CALCOM_EVENT_TYPE_ID = os.getenv("CALCOM_EVENT_TYPE_ID")

# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
if not DEEPGRAM_API_KEY:
    raise ValueError(
        "Missing required environment variable: DEEPGRAM_API_KEY\n"
        "Get a free key at https://console.deepgram.com"
    )
