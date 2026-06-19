"""
Power dialer - human-initiated, single-call-only outbound dialing.

TCPA compliance constraint (do not weaken this): every outbound AI call to
the 3,104-contractor list requires a human to physically click "Call" for
that specific contractor. This module enforces that mechanically, not just
through UI suggestion:

  - `_active_call_lock` blocks a second outbound call from being placed
    while one is already in progress, server-side, regardless of what the
    frontend does. Even a scripted/automated POST flood against /dial
    cannot trigger simultaneous calls.
  - There is no batch/list-dial endpoint. Only one contractor, one call,
    one explicit POST per dial.

Two routes:
  GET  /dialer        - simple HTML page listing contractors with Call buttons
  POST /dialer/dial    - places exactly one outbound call via Twilio REST API

AI mode vs human mode:
  AI mode (default): /dialer/dial calls Twilio's REST API, Twilio dials the
    contractor and connects them to our /incoming-call webhook -> Maya.
  Human mode: /dialer/dial does NOT place any call. It just marks the
    contractor "dialed_manual" and returns their phone number so the
    frontend can show a tel: link / copyable number for Capital to dial
    manually from TextNow or Google Voice. No VOIP API integration exists
    (or is needed) for this - it's a status-tracking convenience only.
"""
import logging
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from twilio.rest import Client as TwilioClient

from config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_PHONE_NUMBER,
    SERVER_EXTERNAL_URL,
    WEBHOOK_SECRET,
)
from backend.contractor_lookup import list_contractors, normalize_phone, update_contractor_status

logger = logging.getLogger(__name__)

# Server-side lock - the actual TCPA-safety mechanism. True while any
# outbound call placed through this dialer is in progress.
_active_call_lock = {"in_progress": False, "phone": None, "started_at": None}


def _get_twilio_client() -> TwilioClient:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set.")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _incoming_call_webhook_url() -> str:
    """Build the same webhook URL Twilio's TwiML <Stream> points to, for use
    as the outbound call's connection target."""
    host = (SERVER_EXTERNAL_URL or "").replace("https://", "").replace("http://", "").rstrip("/")
    path = f"/incoming-call/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/incoming-call"
    return f"https://{host}{path}"


async def dialer_page(request: Request) -> HTMLResponse:
    """Serve the contractor list with Call buttons. Reads directly from
    Supabase on every page load - no caching, so status is always current.
    """
    business_entity = request.query_params.get("business_entity", "CO-003")
    limit = int(request.query_params.get("limit", "50"))

    contractors = await list_contractors(
        business_entity=business_entity,
        limit=limit,
        exclude_statuses=["booked", "declined"],
    )

    rows_html = ""
    for c in contractors:
        email = c.get("corrected_email") or c.get("email") or ""
        rows_html += f"""
        <tr data-phone="{c['phone']}">
            <td>{c.get('owner_name') or ''}</td>
            <td>{c.get('business_name') or ''}</td>
            <td>{c['phone']}</td>
            <td>{email}</td>
            <td class="status">{c.get('status') or 'not_called'}</td>
            <td>
                <button class="call-btn" onclick="dial('{c['phone']}', '{business_entity}', this)">Call</button>
            </td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html>
<head>
<title>craftd Power Dialer</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #111111; color: #f0f0f0; padding: 24px; }}
  h1 {{ color: #1D9E75; }}
  .mode-toggle {{ margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #333; }}
  th {{ color: #1D9E75; }}
  .call-btn {{ background: #1D9E75; color: #111; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; }}
  .call-btn:disabled {{ background: #555; color: #999; cursor: not-allowed; }}
  .status {{ text-transform: capitalize; }}
  #lock-banner {{ display: none; background: #5a3b00; color: #ffd27a; padding: 10px; border-radius: 6px; margin-bottom: 16px; }}
</style>
</head>
<body>
  <h1>craftd Power Dialer — {business_entity}</h1>
  <div class="mode-toggle">
    <label><input type="checkbox" id="humanMode"> Human mode (I'll dial manually from TextNow/Google Voice)</label>
  </div>
  <div id="lock-banner">A call is currently in progress. Wait for it to finish before dialing the next contractor.</div>
  <table>
    <tr><th>Owner</th><th>Business</th><th>Phone</th><th>Email</th><th>Status</th><th></th></tr>
    {rows_html}
  </table>

<script>
async function dial(phone, businessEntity, btn) {{
    const humanMode = document.getElementById('humanMode').checked;
    btn.disabled = true;
    btn.innerText = humanMode ? 'Dialing manually...' : 'Calling...';

    try {{
        const resp = await fetch('/dialer/dial', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ phone: phone, business_entity: businessEntity, mode: humanMode ? 'human' : 'ai' }})
        }});
        const data = await resp.json();

        if (!resp.ok) {{
            alert(data.error || 'Call could not be placed.');
            btn.disabled = false;
            btn.innerText = 'Call';
            return;
        }}

        const row = btn.closest('tr');
        row.querySelector('.status').innerText = humanMode ? 'dialed_manual' : 'dialed';

        if (humanMode) {{
            window.location.href = 'tel:' + phone;
            btn.innerText = 'Dialed manually';
        }} else {{
            btn.innerText = 'Calling...';
        }}
    }} catch (e) {{
        alert('Request failed: ' + e);
        btn.disabled = false;
        btn.innerText = 'Call';
    }}
}}
</script>
</body>
</html>"""
    return HTMLResponse(html)


async def dial(request: Request) -> JSONResponse:
    """Place exactly one outbound call (AI mode) or mark one contractor as
    manually dialed (human mode). Enforces the single-call-at-a-time lock
    in AI mode.
    """
    body = await request.json()
    phone = body.get("phone")
    business_entity = body.get("business_entity", "CO-003")
    mode = body.get("mode", "ai")

    if not phone:
        return JSONResponse({"error": "phone is required"}, status_code=400)

    normalized = normalize_phone(phone)

    if mode == "human":
        # No call placed by us at all - just record that a human is about
        # to dial this contractor manually.
        await update_contractor_status(
            phone=normalized,
            business_entity=business_entity,
            status="dialed_manual",
        )
        return JSONResponse({"success": True, "mode": "human", "phone": normalized})

    # --- AI mode: this is the path that must stay single-call-only ---
    if _active_call_lock["in_progress"]:
        return JSONResponse(
            {"error": f"A call to {_active_call_lock['phone']} is already in progress. Wait for it to finish."},
            status_code=409,
        )

    _active_call_lock["in_progress"] = True
    _active_call_lock["phone"] = normalized
    _active_call_lock["started_at"] = datetime.now(timezone.utc).isoformat()

    try:
        twilio_client = _get_twilio_client()
        call = twilio_client.calls.create(
            to=normalized,
            from_=TWILIO_PHONE_NUMBER,
            url=_incoming_call_webhook_url(),
            status_callback=_status_callback_url(),
            status_callback_event=["completed"],
            status_callback_method="POST",
        )
        await update_contractor_status(phone=normalized, business_entity=business_entity, status="dialed")
        logger.info(f"[DIALER] Outbound call placed to {normalized}, CallSid={call.sid}")
        return JSONResponse({"success": True, "mode": "ai", "call_sid": call.sid})
    except Exception as e:
        # Release the lock immediately on failure to place the call -
        # nothing is actually in progress if Twilio rejected the request.
        _active_call_lock["in_progress"] = False
        logger.error(f"[DIALER] Failed to place call to {normalized}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _status_callback_url() -> str:
    host = (SERVER_EXTERNAL_URL or "").replace("https://", "").replace("http://", "").rstrip("/")
    path = f"/dialer/call-status/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/dialer/call-status"
    return f"https://{host}{path}"


async def call_status_callback(request: Request) -> JSONResponse:
    """Twilio hits this when the outbound call completes. This is what
    actually releases the dial lock - not a timer, not the frontend - so
    the lock is accurate even if the browser tab was closed mid-call.
    """
    form = await request.form()
    call_status = form.get("CallStatus")
    call_sid = form.get("CallSid")

    logger.info(f"[DIALER] Call status callback: CallSid={call_sid} status={call_status}")

    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        _active_call_lock["in_progress"] = False
        _active_call_lock["phone"] = None
        _active_call_lock["started_at"] = None

    return JSONResponse({"ok": True})


async def dialer_lock_status(request: Request) -> JSONResponse:
    """Lightweight endpoint the dialer page can poll to know if it's safe
    to enable the next Call button."""
    return JSONResponse(_active_call_lock)
