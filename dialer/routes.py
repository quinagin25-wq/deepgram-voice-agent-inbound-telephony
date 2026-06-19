"""
Power dialer - human-initiated, single-call-only outbound dialing.

TCPA compliance constraint (do not weaken this): every outbound AI call to
the 3,104-contractor list requires a human to physically click "Call" for
that specific contractor.

This module enforces:
- single-call lock (server-side)
- no batch dialing
- no concurrent outbound calls
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
# GLOBAL CALL LOCK (TCPA SAFETY)
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
# TWILIO CLIENT
# -----------------------------
def _get_twilio_client() -> TwilioClient:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials missing.")
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
# UI (OPERATOR MODE)
# -----------------------------
async def dialer_page(request: Request) -> HTMLResponse:
    business_entity = request.query_params.get("business_entity", "CO-003")

    page = max(1, int(request.query_params.get("page", "1")))
    page_size = min(200, max(10, int(request.query_params.get("page_size", "50"))))
    offset = (page - 1) * page_size

    exclude_statuses = ["booked", "declined"]

    total = await count_contractors(
        business_entity=business_entity,
        exclude_statuses=exclude_statuses,
    )

    contractors = await list_contractors(
        business_entity=business_entity,
        limit=page_size,
        offset=offset,
        exclude_statuses=exclude_statuses,
    )

    # LEFT QUEUE ROWS
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
                <button class="call-btn" onclick="event.stopPropagation(); dial('{phone}')">
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
    font-family: Arial;
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

button {{
    padding:10px;
    border:none;
    border-radius:8px;
    cursor:pointer;
    font-weight:bold;
}}

.call-btn {{
    background:#1D9E75;
}}

.actions {{
    display:flex;
    gap:10px;
    margin-top:15px;
}}

.call {{ background:#2ecc71; }}
.skip {{ background:#e67e22; }}
.next {{ background:#3498db; }}
.test {{ background:#e74c3c; }}

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

.box {{
    background:#101521;
    padding:12px;
    border-radius:8px;
    border:1px solid #2a2f3a;
}}
</style>
</head>

<body>

<!-- LEFT -->
<div class="left">
    <div style="padding:14px;border-bottom:1px solid #2a2f3a;">
        <b>Queue</b><br>
        <span style="color:#777;font-size:12px">{total} leads</span>
    </div>
    <table style="width:100%">
        {rows_html}
    </table>
</div>

<!-- RIGHT -->
<div class="right">

    <div class="top">
        <b>Operator Console</b>
        <span style="float:right;color:#777">Twilio Active</span>
    </div>

    <div class="card">
        <div id="leadName" style="font-size:20px;font-weight:bold;">
            Select a lead
        </div>
        <div id="leadPhone" style="color:#9bb3c9;margin-top:5px;"></div>
        <div id="leadStatus" style="margin-top:10px;color:#777;"></div>

        <div class="actions">
            <button class="call" onclick="callSelected()">Call</button>
            <button class="skip">Skip</button>
            <button class="next">Next</button>
            <button class="test" onclick="testCall()">Test AI Call</button>
        </div>
    </div>

    <div class="card">
        <b>Details</b>
        <div class="box" style="margin-top:10px;">
            Select a lead to view details.
        </div>
    </div>

</div>

<script>
let selectedPhone = null;

function selectLead(phone, name, status) {{
    selectedPhone = phone;
    document.getElementById("leadName").innerText = name;
    document.getElementById("leadPhone").innerText = phone;
    document.getElementById("leadStatus").innerText = "Status: " + status;
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
    if (!resp.ok) return alert(data.error || "Failed");

    alert("Test call started");
}}
</script>

</body>
</html>
"""

    return HTMLResponse(html)


# -----------------------------
# DIAL LOGIC (UNCHANGED)
# -----------------------------
async def dial(request: Request) -> JSONResponse:
    body = await request.json()
    phone = body.get("phone")
    mode = body.get("mode", "ai")
    is_test = body.get("is_test", False)

    if not phone:
        return JSONResponse({"error": "phone required"}, status_code=400)

    normalized = normalize_phone(phone)

    if mode == "human":
        await update_contractor_status(normalized, "CO-003", "dialed_manual")
        return JSONResponse({"success": True, "mode": "human", "phone": normalized})

    if _active_call_lock["in_progress"]:
        return JSONResponse({"error": "Call already in progress"}, status_code=409)

    _active_call_lock.update({
        "in_progress": True,
        "phone": normalized,
        "status": "initiating",
        "started_at": datetime.now(timezone.utc).isoformat()
    })

    try:
        client = _get_twilio_client()

        call = client.calls.create(
            to=normalized,
            from_=TWILIO_PHONE_NUMBER,
            url=_incoming_call_webhook_url(),
            status_callback=_status_callback_url(),
            status_callback_event=["initiated","ringing","completed"],
            status_callback_method="POST",
        )

        _active_call_lock["call_sid"] = call.sid

        if not is_test:
            await update_contractor_status(normalized, "CO-003", "dialed")

        return JSONResponse({"success": True, "call_sid": call.sid})

    except Exception as e:
        _active_call_lock["in_progress"] = False
        return JSONResponse({"error": str(e)}, status_code=500)


# -----------------------------
# LOCK STATUS (UNCHANGED IDEA)
# -----------------------------
async def dialer_lock_status(request: Request) -> JSONResponse:
    return JSONResponse(_active_call_lock)
