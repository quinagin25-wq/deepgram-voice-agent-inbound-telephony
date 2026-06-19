"""
Telephony Voice Agent - Entry Point

Starts a Starlette web server that handles:
  - POST /incoming-call  → Twilio webhook (returns TwiML)
  - WS   /twilio         → Twilio audio stream (or dev_client.py)

Usage:
  python main.py

For local development without Twilio:
  Terminal 1:  python main.py
  Terminal 2:  python dev_client.py
"""
import logging

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.responses import PlainTextResponse

from config import SERVER_HOST, SERVER_PORT, SERVER_EXTERNAL_URL, DEEPGRAM_API_KEY
from telephony.routes import incoming_call, twilio_websocket
from dialer.routes import dialer_page, dial, call_status_callback, dialer_lock_status
from backend.admin_import import import_contractors

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def dashboard(request):
    return PlainTextResponse(
        "Telephony Voice Agent is running.\n"
        "Call your Twilio number or use `python dev_client.py` to test locally."
    )


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = Starlette(
    routes=[
        Route("/incoming-call/{token:path}", incoming_call, methods=["POST"]),
        Route("/incoming-call", incoming_call, methods=["POST"]),
        WebSocketRoute("/twilio/{token:path}", twilio_websocket),
        WebSocketRoute("/twilio", twilio_websocket),
        Route("/dialer", dialer_page, methods=["GET"]),
        Route("/dialer/dial", dial, methods=["POST"]),
        Route("/dialer/call-status/{token:path}", call_status_callback, methods=["POST"]),
        Route("/dialer/call-status", call_status_callback, methods=["POST"]),
        Route("/dialer/lock-status", dialer_lock_status, methods=["GET"]),
        Route("/admin/import-contractors", import_contractors, methods=["GET"]),
        Route("/", dashboard),
    ],
)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logger.info(f"Deepgram API key: {'configured' if DEEPGRAM_API_KEY else 'MISSING'}")
    if SERVER_EXTERNAL_URL:
        logger.info(f"External URL: {SERVER_EXTERNAL_URL}")
        logger.info(f"Twilio webhook: {SERVER_EXTERNAL_URL}/incoming-call")
    else:
        logger.info("Running in local-only mode (no SERVER_EXTERNAL_URL set)")
        logger.info("Use dev_client.py to test - no Twilio or tunnel needed")

    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=int(SERVER_PORT),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
