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
import asyncio
import logging
from datetime import datetime, timezone
from html import escape as html_escape

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
from backend.contractor_lookup import list_contractors, count_contractors, normalize_phone, update_contractor_status

logger = logging.getLogger(__name__)

# Server-side lock - the actual TCPA-safety mechanism. True while any
# outbound call placed through this dialer is in progress. Also doubles as
# the live call-state store the dialer page polls to show real status
# (ringing/in-progress/voicemail/ended) instead of a static "Calling..." label.
_active_call_lock = {
    "in_progress": False,
    "phone": None,
    "call_sid": None,
    "status": None,  # Twilio's CallStatus: queued/ringing/in-progress/completed/busy/no-answer/failed/canceled
    "answered_by": None,  # AMD result: human/machine_start/machine_end_beep/etc, when available
    "started_at": None,
}


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
    """Serve the contractor list with Call buttons, paginated.

    Pagination via ?page=N (1-indexed) and ?page_size=N (default 50, max 200).
    Reads directly from Supabase on every page load - no caching, so status
    is always current.
    """
    business_entity = request.query_params.get("business_entity", "CO-003")
    page = max(1, int(request.query_params.get("page", "1")))
    page_size = min(200, max(10, int(request.query_params.get("page_size", "50"))))
    q = request.query_params.get("q", "").strip()
    offset = (page - 1) * page_size

    exclude_statuses = ["booked", "declined"]

    total = await count_contractors(business_entity=business_entity, exclude_statuses=exclude_statuses, search=q or None)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)  # clamp if someone requests a page beyond the end
    offset = (page - 1) * page_size

    contractors = await list_contractors(
        business_entity=business_entity,
        limit=page_size,
        offset=offset,
        exclude_statuses=exclude_statuses,
        search=q or None,
    )

    rows_html = ""
    for c in contractors:
        email = c.get("corrected_email") or c.get("email") or ""
        row_status = c.get("status") or "not_called"
        row_notes = html_escape(c.get("call_notes") or "", quote=True)
        row_callback = html_escape(c.get("callback_at") or "", quote=True)
        rows_html += f"""
        <tr data-phone="{c['phone']}" data-status="{row_status}" data-notes="{row_notes}" data-callback-at="{row_callback}">
            <td>{c.get('owner_name') or ''}</td>
            <td>{c.get('business_name') or ''}</td>
            <td>{c['phone']}</td>
            <td>{email}</td>
            <td class="status">{row_status}</td>
            <td>
                <button class="call-btn" onclick="dial('{c['phone']}', '{business_entity}', this)">Call</button>
                <button class="notes-btn" onclick="openNotes(this)">Notes</button>
            </td>
        </tr>"""

    def page_link(target_page: int, label: str, disabled: bool) -> str:
        if disabled:
            return f'<span class="page-btn disabled">{label}</span>'
        q_param = f"&q={html_escape(q, quote=True)}" if q else ""
        return f'<a class="page-btn" href="/dialer?business_entity={business_entity}&page_size={page_size}&page={target_page}{q_param}">{label}</a>'

    pagination_html = f"""
    <div class="pagination">
        {page_link(1, '« First', page <= 1)}
        {page_link(page - 1, '‹ Prev', page <= 1)}
        <span class="page-info">Page {page} of {total_pages} &nbsp;({total} contractors)</span>
        {page_link(page + 1, 'Next ›', page >= total_pages)}
        {page_link(total_pages, 'Last »', page >= total_pages)}
    </div>"""

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
  .notes-btn {{ background: transparent; color: #888; border: 1px solid #444; padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 12px; margin-left: 6px; }}
  .notes-btn:hover {{ color: #1D9E75; border-color: #1D9E75; }}
  .search-box {{ background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 8px 14px; font-size: 14px; flex: 1; max-width: 400px; }}
  .search-box:focus {{ outline: none; border-color: #1D9E75; }}
  .search-clear {{ color: #888; font-size: 13px; text-decoration: none; padding: 4px 8px; }}
  .search-clear:hover {{ color: #fff; }}
  .status {{ text-transform: capitalize; }}
  #lock-banner {{ display: none; background: #5a3b00; color: #ffd27a; padding: 10px 16px; border-radius: 6px; margin-bottom: 16px; align-items: center; justify-content: space-between; }}
  #lock-banner.active {{ display: flex; }}
  #end-call-btn {{ background: #c0392b; color: #fff; border: none; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-weight: 600; }}
  #end-call-btn:hover {{ background: #e74c3c; }}
  .pagination {{ display: flex; align-items: center; gap: 12px; margin: 20px 0; flex-wrap: wrap; }}
  .page-btn {{ color: #1D9E75; text-decoration: none; padding: 6px 12px; border: 1px solid #1D9E75; border-radius: 6px; }}
  .page-btn:hover {{ background: #1D9E75; color: #111; }}
  .page-btn.disabled {{ color: #555; border-color: #333; cursor: not-allowed; }}
  .page-info {{ color: #ccc; }}
  .test-dial-box {{ background: #1a1a1a; border: 1px solid #1D9E75; border-radius: 8px; padding: 16px; margin-bottom: 16px; }}
  .test-dial-box input {{ background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 8px 12px; margin: 0 10px; width: 200px; }}
  .disp-btn {{ background: #222; color: #ccc; border: 1px solid #444; padding: 8px 18px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; }}
  .disp-btn:hover {{ background: #333; color: #fff; }}
  .disp-btn.selected {{ background: #1D9E75; color: #111; border-color: #1D9E75; }}
  #disposition-panel {{ display: none; position: fixed; bottom: 0; left: 0; right: 0; background: #1a1a1a; border-top: 2px solid #1D9E75; padding: 20px 32px; z-index: 100; box-shadow: 0 -4px 24px rgba(0,0,0,0.5); }}
  #disp-notes {{ flex: 1; background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 8px 12px; resize: vertical; min-height: 60px; font-size: 13px; font-family: inherit; }}
  #callback-at-input {{ background: #222; color: #fff; border: 1px solid #444; border-radius: 6px; padding: 6px 10px; margin-left: 8px; color-scheme: dark; }}
  #disp-save-btn {{ background: #1D9E75; color: #111; border: none; padding: 10px 24px; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 14px; }}
  #disp-save-btn:disabled {{ background: #555; color: #999; cursor: not-allowed; }}
  #disp-skip-btn {{ background: transparent; color: #999; border: 1px solid #444; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-size: 13px; }}
</style>
</head>
<body>
  <h1>craftd Power Dialer — {business_entity}</h1>
  <div class="mode-toggle">
    <label><input type="checkbox" id="humanMode"> Human mode (I'll dial manually from TextNow/Google Voice)</label>
  </div>
  <div class="test-dial-box">
    <strong>Test call</strong> (doesn't touch any contractor data, won't use a real name/business in Maya's greeting):
    <input type="tel" id="testPhoneInput" placeholder="+19195551234" />
    <button class="call-btn" onclick="dialTest()">Call this number</button>
  </div>
  <div id="lock-banner">
    <span id="lock-status-text">A call is currently in progress.</span>
    <button id="end-call-btn" onclick="endCall()">End Call</button>
  </div>
  <form method="get" action="/dialer" style="display:flex; gap:8px; margin:16px 0; align-items:center;">
    <input type="hidden" name="business_entity" value="{business_entity}" />
    <input type="hidden" name="page_size" value="{page_size}" />
    <input class="search-box" type="text" name="q" value="{html_escape(q, quote=True)}" placeholder="Search by name, business, or phone..." />
    <button type="submit" class="call-btn">Search</button>
    {f'<a class="search-clear" href="/dialer?business_entity={business_entity}&page_size={page_size}">✕ Clear</a>' if q else ''}
  </form>
  {pagination_html}
  <table>
    <tr><th>Owner</th><th>Business</th><th>Phone</th><th>Email</th><th>Status</th><th></th></tr>
    {rows_html}
  </table>
  {pagination_html}

<script>
const BUSINESS_ENTITY = '{business_entity}';

let _dispPhone = null;
let _dispBusiness = null;
let _dispStatus = null;

const DISPOSITION_STATUSES = ['callback_requested', 'booked', 'declined', 'wrong_number', 'dnc'];

function showDispositionPanel(phone, ownerName, businessName, businessEntity, existingStatus, existingNotes, existingCallbackAt) {{
    _dispPhone = phone;
    _dispBusiness = businessEntity;
    _dispStatus = null;
    document.getElementById('disp-name').innerText = ownerName || businessName || '';
    document.getElementById('disp-phone').innerText = phone;
    document.getElementById('disp-notes').value = existingNotes || '';
    document.getElementById('callback-at-input').value = '';
    document.getElementById('callback-row').style.display = 'none';
    document.getElementById('disp-save-btn').disabled = true;
    document.querySelectorAll('.disp-btn').forEach(b => b.classList.remove('selected'));

    if (existingStatus && DISPOSITION_STATUSES.includes(existingStatus)) {{
        setDisposition(existingStatus);
    }}

    if (existingCallbackAt) {{
        try {{
            const dt = new Date(existingCallbackAt);
            const local = new Date(dt.getTime() - dt.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
            document.getElementById('callback-at-input').value = local;
        }} catch(e) {{}}
    }}

    document.getElementById('disposition-panel').style.display = 'block';
}}

function openNotes(btn) {{
    const row = btn.closest('tr');
    const phone = row.dataset.phone;
    const existingStatus = row.dataset.status || '';
    const existingNotes = row.dataset.notes || '';
    const existingCallbackAt = row.dataset.callbackAt || '';
    const ownerName = row.cells[0].innerText;
    const businessName = row.cells[1].innerText;
    showDispositionPanel(phone, ownerName, businessName, BUSINESS_ENTITY, existingStatus, existingNotes, existingCallbackAt);
}}

function setDisposition(status) {{
    _dispStatus = status;
    document.querySelectorAll('.disp-btn').forEach(b => {{
        b.classList.toggle('selected', b.dataset.status === status);
    }});
    document.getElementById('disp-save-btn').disabled = false;
    document.getElementById('callback-row').style.display = status === 'callback_requested' ? 'block' : 'none';
}}

async function saveDisposition() {{
    if (!_dispStatus) return;
    const notes = document.getElementById('disp-notes').value.trim() || null;
    const callbackAt = document.getElementById('callback-at-input').value || null;
    const body = {{ phone: _dispPhone, business_entity: _dispBusiness, status: _dispStatus }};
    if (notes) body.notes = notes;
    if (callbackAt) body.callback_at = callbackAt;
    try {{
        const resp = await fetch('/dialer/dispose', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(body)
        }});
        const data = await resp.json();
        if (!resp.ok) {{
            alert(data.error || 'Could not save disposition.');
            return;
        }}
        const row = document.querySelector('tr[data-phone="' + _dispPhone + '"]');
        if (row) row.querySelector('.status').innerText = _dispStatus.replace(/_/g, ' ');
        dismissDisposition();
    }} catch(e) {{
        alert('Request failed: ' + e);
    }}
}}

function dismissDisposition() {{
    document.getElementById('disposition-panel').style.display = 'none';
    _dispPhone = null;
    _dispBusiness = null;
    _dispStatus = null;
}}

const STATUS_LABELS = {{
    'initiating': 'Placing call...',
    'initiated': 'Call initiated...',
    'ringing': 'Ringing...',
    'in-progress': 'Connected - call in progress',
    'completed': 'Call ended',
    'busy': 'Line busy',
    'no-answer': 'No answer',
    'failed': 'Call failed',
    'canceled': 'Call canceled',
    'transferring': 'Transferring to rep...',
    'transferring_rep_dialing': 'Dialing rep now...',
    'transfer_failed': 'Transfer failed - call may still be live',
}};

async function pollCallStatus() {{
    try {{
        const resp = await fetch('/dialer/lock-status');
        const data = await resp.json();
        const banner = document.getElementById('lock-banner');
        const statusText = document.getElementById('lock-status-text');

        if (data.in_progress) {{
            banner.classList.add('active');
            const label = STATUS_LABELS[data.status] || data.status || 'In progress...';
            statusText.innerText = label + (data.phone ? ' (' + data.phone + ')' : '');
        }} else {{
            banner.classList.remove('active');
        }}
    }} catch (e) {{
        // Silent fail on a poll - not worth alerting the user over a missed poll
    }}
}}
setInterval(pollCallStatus, 2000);
pollCallStatus();

async function endCall() {{
    if (!confirm('End the current call now?')) return;
    try {{
        const resp = await fetch('/dialer/end-call', {{ method: 'POST' }});
        const data = await resp.json();
        if (!resp.ok) {{
            alert(data.error || 'Could not end call.');
            return;
        }}
        pollCallStatus();
    }} catch (e) {{
        alert('Request failed: ' + e);
    }}
}}

async function dialTest() {{
    const phone = document.getElementById('testPhoneInput').value.trim();
    if (!phone) {{
        alert('Enter a phone number first.');
        return;
    }}

    try {{
        const resp = await fetch('/dialer/dial', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{ phone: phone, mode: 'ai', is_test: true }})
        }});
        const data = await resp.json();

        if (!resp.ok) {{
            alert(data.error || 'Call could not be placed.');
            return;
        }}

        alert('Calling ' + phone + ' now - answer your phone.');
    }} catch (e) {{
        alert('Request failed: ' + e);
    }}
}}

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
        const ownerName = row.cells[0].innerText;
        const businessName = row.cells[1].innerText;
        showDispositionPanel(phone, ownerName, businessName, businessEntity);

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

  <div id="disposition-panel">
    <div style="max-width:960px; margin:0 auto;">
      <div style="display:flex; align-items:center; gap:16px; margin-bottom:14px;">
        <span style="color:#1D9E75; font-weight:700; font-size:15px;">Disposition</span>
        <span id="disp-name" style="color:#fff; font-size:15px;"></span>
        <span id="disp-phone" style="color:#999; font-size:13px;"></span>
      </div>
      <div style="display:flex; gap:10px; margin-bottom:14px; flex-wrap:wrap;">
        <button class="disp-btn" onclick="setDisposition('callback_requested')" data-status="callback_requested">Interested</button>
        <button class="disp-btn" onclick="setDisposition('booked')" data-status="booked">Booked</button>
        <button class="disp-btn" onclick="setDisposition('declined')" data-status="declined">Not Interested</button>
        <button class="disp-btn" onclick="setDisposition('wrong_number')" data-status="wrong_number">Wrong Number</button>
        <button class="disp-btn" onclick="setDisposition('dnc')" data-status="dnc">DNC</button>
      </div>
      <div id="callback-row" style="display:none; margin-bottom:12px;">
        <label style="color:#ccc; font-size:13px;">Callback date/time:</label>
        <input type="datetime-local" id="callback-at-input" />
      </div>
      <div style="display:flex; gap:12px; align-items:flex-start;">
        <textarea id="disp-notes" placeholder="Call notes (optional)..."></textarea>
        <div style="display:flex; flex-direction:column; gap:8px;">
          <button id="disp-save-btn" onclick="saveDisposition()" disabled>Save</button>
          <button id="disp-skip-btn" onclick="dismissDisposition()">Skip</button>
        </div>
      </div>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


async def dial(request: Request) -> JSONResponse:
    """Place exactly one outbound call (AI mode) or mark one contractor as
    manually dialed (human mode). Enforces the single-call-at-a-time lock
    in AI mode.

    is_test=true skips all contractor status updates - used for the manual
    test-dial box (calling your own number to hear Maya live) so testing
    never touches real contractor data.
    """
    body = await request.json()
    phone = body.get("phone")
    business_entity = body.get("business_entity", "CO-003")
    mode = body.get("mode", "ai")
    is_test = bool(body.get("is_test", False))

    if not phone:
        return JSONResponse({"error": "phone is required"}, status_code=400)

    normalized = normalize_phone(phone)

    if mode == "human":
        if not is_test:
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
    _active_call_lock["call_sid"] = None
    _active_call_lock["status"] = "initiating"
    _active_call_lock["answered_by"] = None
    _active_call_lock["started_at"] = datetime.now(timezone.utc).isoformat()

    try:
        twilio_client = _get_twilio_client()
        call = twilio_client.calls.create(
            to=normalized,
            from_=TWILIO_PHONE_NUMBER,
            url=_incoming_call_webhook_url(),
            status_callback=_status_callback_url(),
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
        _active_call_lock["call_sid"] = call.sid
        if not is_test:
            await update_contractor_status(phone=normalized, business_entity=business_entity, status="dialed")
        logger.info(f"[DIALER] Outbound call placed to {normalized}, CallSid={call.sid} (test={is_test})")
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
    """Twilio hits this on every call state change (initiated, ringing,
    answered, completed). We track every update, not just the terminal one -
    the dialer page polls /dialer/lock-status to show live progress
    (ringing -> in-progress -> ended).

    The lock itself (in_progress) only releases on a terminal status - that's
    still what enforces single-call-at-a-time, unaffected by the extra
    intermediate updates.
    """
    form = await request.form()
    call_status = form.get("CallStatus")
    call_sid = form.get("CallSid")

    logger.info(f"[DIALER] Call status callback: CallSid={call_sid} status={call_status}")

    # Only update if this callback matches the call we're currently tracking -
    # guards against a stale/late callback from a previous call overwriting
    # the state of a new one (unlikely given the lock, but cheap to check).
    if _active_call_lock.get("call_sid") in (None, call_sid):
        _active_call_lock["call_sid"] = call_sid
        _active_call_lock["status"] = call_status

    if call_status in ("completed", "failed", "busy", "no-answer", "canceled"):
        _active_call_lock["in_progress"] = False
        _active_call_lock["phone"] = None
        _active_call_lock["call_sid"] = None
        _active_call_lock["status"] = None
        _active_call_lock["answered_by"] = None
        _active_call_lock["started_at"] = None

    return JSONResponse({"ok": True})


async def end_call(request: Request) -> JSONResponse:
    """Manually hang up the currently active outbound call. Lets Capital
    bail out of a stuck or unwanted live call from the dialer page instead
    of waiting for it to end naturally.
    """
    call_sid = _active_call_lock.get("call_sid")
    if not call_sid:
        return JSONResponse({"error": "No active call to end."}, status_code=404)

    try:
        twilio_client = _get_twilio_client()
        await asyncio.to_thread(twilio_client.calls(call_sid).update, status="completed")
        logger.info(f"[DIALER] Manually ended call {call_sid}")
        return JSONResponse({"success": True, "call_sid": call_sid})
    except Exception as e:
        logger.error(f"[DIALER] Failed to end call {call_sid}: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def dialer_lock_status(request: Request) -> JSONResponse:
    """Lightweight endpoint the dialer page can poll to know if it's safe
    to enable the next Call button."""
    return JSONResponse(_active_call_lock)


async def dispose(request: Request) -> JSONResponse:
    """Save a call disposition (and optional notes/callback_at) for a contractor.
    Called from the disposition panel after a call ends or is skipped.
    """
    body = await request.json()
    phone = body.get("phone")
    business_entity = body.get("business_entity", "CO-003")
    status = body.get("status")
    notes = body.get("notes") or None
    callback_at = body.get("callback_at") or None

    if not phone or not status:
        return JSONResponse({"error": "phone and status are required"}, status_code=400)

    valid_statuses = {"booked", "declined", "callback_requested", "wrong_number", "dnc"}
    if status not in valid_statuses:
        return JSONResponse({"error": f"invalid status: {status}"}, status_code=400)

    success = await update_contractor_status(
        phone=phone,
        business_entity=business_entity,
        status=status,
        call_notes=notes,
        callback_at=callback_at,
    )
    if not success:
        return JSONResponse({"error": "Failed to save disposition."}, status_code=500)
    logger.info(f"[DIALER] Disposition saved: {phone} → {status}")
    return JSONResponse({"success": True})
