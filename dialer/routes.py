"""
Power dialer - human-initiated, single-call-only outbound dialing.

TCPA compliance constraint (do not weaken this): every outbound AI call to
the contractor list requires a human click per call.
"""

import asyncio
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

from backend.contractor_lookup import (
    list_contractors,
    count_contractors,
    normalize_phone,
    update_contractor_status,
)

logger = logging.getLogger(__name__)

# -----------------------------
# CALL LOCK
# -----------------------------
_active_call_lock = {
    "in_progress": False,
    "phone": None,
    "call_sid": None,
    "status": None,
    "answered_by": None,
    "started_at": None,
}

# -----------------------------
# TWILIO
# -----------------------------
def _get_twilio_client() -> TwilioClient:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Missing Twilio credentials.")
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def _incoming_call_webhook_url() -> str:
    host = (SERVER_EXTERNAL_URL or "").replace("https://", "").replace("http://", "").rstrip("/")
    path = f"/incoming-call/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/incoming-call"
    return f"https://{host}{path}"


def _status_callback_url() -> str:
    host = (SERVER_EXTERNAL_URL or "").replace("https://", "").replace("http://", "").rstrip("/")
    path = f"/dialer/call-status/{WEBHOOK_SECRET}" if WEBHOOK_SECRET else "/dialer/call-status"
    return f"https://{host}{path}"


# -----------------------------
# UI
# -----------------------------
async def dialer_page(request: Request) -> HTMLResponse:
    business_entity = request.query_params.get("business_entity", "CO-003")

    page = max(1, int(request.query_params.get("page", "1")))
    page_size = min(200, max(10, int(request.query_params.get("page_size", "50"))))
    offset = (page - 1) * page_size

    total = await count_contractors(business_entity=business_entity)

    contractors = await list_contractors(
        business_entity=business_entity,
        limit=page_size,
        offset=offset,
    )

    rows_html = ""
    for c in contractors:
        phone = c["phone"]
        name = c.get("owner_name") or "Unknown"
        status = c.get("status") or "not_called"

        rows_html += f"""
        <tr onclick="selectLead('{phone}', '{name}', '{status}')">
            <td>
                <div><b>{name}</b></div>
                <div style="font-size:11px;color:#777">{phone}</div>
            </td>
            <td style="text-align:right">
                <button onclick="event.stopPropagation(); dial('{phone}')" style="
                    background:#1D9E75;border:none;padding:6px 10px;
                    border-radius:6px;cursor:pointer;font-weight:bold;">
                    Call
                </button>
            </td>
        </tr>
        """

    html = f"""
<!DOCTYPE html>
<html>
<head>
<title>Operator Dialer</title>

<style>
body {{
    margin:0;
    font-family:Arial;
    background:#0f1115;
    color:#e6e6e6;
    display:flex;
    height:100vh;
}}

.left {{
    width:320px;
    background:#151922;
    border-right:1px solid #2a2f3a;
    overflow:auto;
}}

.right {{
    flex:1;
    display:flex;
    flex-direction:column;
}}

.top {{
    padding:14px;
    background:#121621;
    border-bottom:1px solid #2a2f3a;
}}

.card {{
    margin:20px;
    padding:20px;
    background:#161b26;
    border:1px solid #2a2f3a;
    border-radius:10px;
}}

tr {{
    cursor:pointer;
}}

td {{
    padding:10px;
    border-bottom:1px solid #222836;
}}

tr:hover {{
    background:#1c2230;
}}
</style>
</head>

<body>

<div class="left">
    <div style="padding:14px;border-bottom:1px solid #2a2f3a;">
        <b>Queue</b><br>
        <span style="color:#777;font-size:12px">{total} leads</span>
    </div>

    <table style="width:100%">
        {rows_html}
    </table>
</div>

<div class="right">

    <div class="top">
        <b>Operator Console</b>
        <span style="float:right;color:#777">Twilio Active</span>
    </div>

    <div class="card">
        <div id="name" style="font-size:20px;font-weight:bold;">Select a lead</div>
        <div id="phone" style="color:#9bb3c9;margin-top:5px;"></div>
        <div id="status" style="margin-top:10px;color:#777;"></div>

        <div style="margin-top:15px;display:flex;gap:10px;">
            <button onclick="callSelected()" style="background:#2ecc71;padding:10px;border:none;border-radius:8px;font-weight:bold;">Call</button>
            <button style="background:#e67e22;padding:10px;border:none;border-radius:8px;font-weight:bold;">Skip</button>
            <button onclick="testCall()" style="background:#e74c3c;padding:10px;border:none;border-radius:8px;font-weight:bold;">Test</button>
        </div>
    </div>

</div>

<script>
let selectedPhone = null;

function selectLead(phone, name, status) {{
    selectedPhone = phone;
    document.getElementById("name").innerText = name;
    document.getElementById("phone").innerText = phone;
    document.getElementById("status").innerText = "Status: " + status;
}}

async function callSelected() {{
    if (!selectedPhone) return alert("Select a lead first");

    const resp = await fetch("/dialer/dial", {{
        method:"POST",
        headers:{{"Content-Type":"application/json"}},
        body: JSON.stringify({{ phone: selectedPhone, mode:"ai" }})
    }});

    const data = await resp.json();
    if (!resp.ok) return alert(data.error || "Call failed");

    alert("Calling " + selectedPhone);
}}

async function testCall() {{
    const phone = prompt("Enter test number:");
    if (!phone) return;

    const resp = await fetch("/dialer/dial", {{
        method:"POST",
        headers:{{"Content-Type":"application/json"}},
        body: JSON.stringify({{ phone, mode:"ai", is_test:true }})
    }});

    const data = await resp.json();
    if (!resp.ok) return alert(data.error);

    alert("Test call started");
}}
</script>

</body>
</html>
"""

    return HTMLResponse(html)


# -----------------------------
# DIAL
# -----------------------------
async def dial(request: Request) -> JSONResponse:
    body = await request.json()
    phone = body.get("phone")
    mode = body.get("mode", "ai")
    is_test = body.get("is_test", False)

    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    phone = normalize_phone(phone)

    if mode == "human":
        await update_contractor_status(phone, "CO-003", "dialed_manual")
        return JSONResponse({"success": True, "mode": "human"})

    if _active_call_lock["in_progress"]:
        return JSONResponse({"error": "Call already in progress"}, status_code=409)

    _active_call_lock.update({
        "in_progress": True,
        "phone": phone,
        "status": "initiating",
        "started_at": datetime.now(timezone.utc).isoformat()
    })

    try:
        client = _get_twilio_client()

        call = client.calls.create(
            to=phone,
            from_=TWILIO_PHONE_NUMBER,
            url=_incoming_call_webhook_url(),
            status_callback=_status_callback_url(),
            status_callback_event=["initiated", "ringing", "completed"],
            status_callback_method="POST",
        )

        _active_call_lock["call_sid"] = call.sid

        if not is_test:
            await update_contractor_status(phone, "CO-003", "dialed")

        return JSONResponse({"success": True, "call_sid": call.sid})

    except Exception as e:
        _active_call_lock["in_progress"] = False
        return JSONResponse({"error": str(e)}, status_code=500)


# -----------------------------
# STATUS CALLBACK (RESTORED)
# -----------------------------
async def call_status_callback(request: Request) -> JSONResponse:
    form = await request.form()
    status = form.get("CallStatus")
    sid = form.get("CallSid")

    if _active_call_lock.get("call_sid") in (None, sid):
        _active_call_lock["call_sid"] = sid
        _active_call_lock["status"] = status

    if status in ("completed", "failed", "busy", "no-answer", "canceled"):
        _active_call_lock.update({
            "in_progress": False,
            "phone": None,
            "call_sid": None,
            "status": None,
            "answered_by": None,
            "started_at": None,
        })

    return JSONResponse({"ok": True})


# -----------------------------
# END CALL (RESTORED)
# -----------------------------
async def end_call(request: Request) -> JSONResponse:
    sid = _active_call_lock.get("call_sid")

    if not sid:
        return JSONResponse({"error": "No active call"}, status_code=404)

    try:
        client = _get_twilio_client()
        await asyncio.to_thread(client.calls(sid).update, status="completed")

        _active_call_lock["in_progress"] = False
        return JSONResponse({"success": True})

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# -----------------------------
# LOCK STATUS
# -----------------------------
async def dialer_lock_status(request: Request) -> JSONResponse:
    return JSONResponse(_active_call_lock)
