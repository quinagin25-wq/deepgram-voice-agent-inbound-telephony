"""
One-time admin import route - imports the contractor CSV (bundled in the
repo at backend/data/craftd_call_log.csv) into Supabase.

This exists because Capital has no local way to run a Python script
directly - the CSV gets uploaded to the repo via GitHub's file upload
(not pasted, to avoid the truncation issues hit with large text pastes
elsewhere tonight), and this route runs the same import logic that
scripts/import_contractors.py implements, triggered by a single browser
visit instead of a terminal command.

Safety: this route checks Supabase for any existing CO-003 contractor
rows first. If the table already has CO-003 records, it refuses to run
again unless ?force=true is passed - this prevents an accidental double
import if the route gets visited twice (e.g. a page refresh).
"""
import csv
import io
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from backend.contractor_lookup import get_client, normalize_phone

logger = logging.getLogger(__name__)

BUSINESS_ENTITY = "CO-003"
BATCH_SIZE = 500
CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "craftd_call_log.csv")


def _map_status(status_raw: str, substatus_raw: str) -> tuple:
    status_raw = (status_raw or "").strip().lower()
    substatus_raw = (substatus_raw or "").strip().lower()

    has_website = "has site" in substatus_raw

    if status_raw != "called":
        return "not_called", has_website

    if "no answer" in substatus_raw:
        return "no_answer", has_website
    if "call back" in substatus_raw or "callback" in substatus_raw:
        return "callback_requested", has_website
    if "not interested" in substatus_raw:
        return "declined", has_website
    if "booked" in substatus_raw:
        return "booked", has_website
    if "may not qualify" in substatus_raw:
        return "declined", has_website

    return "no_answer", has_website


def _load_and_dedupe(csv_path: str) -> list:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        rows = list(reader)

    seen = {}
    skipped = 0

    for r in rows:
        if not r or not r[0].strip():
            continue
        phone = normalize_phone(r[0].strip())
        owner_name = r[1].strip()
        business_name = r[2].strip()
        email = r[3].strip() or None
        county = r[5].strip() if len(r) > 5 else None
        status_raw = r[6] if len(r) > 6 else ""
        substatus_raw = r[7] if len(r) > 7 else ""
        notes = r[8].strip() if len(r) > 8 and r[8].strip() else None

        status, has_website = _map_status(status_raw, substatus_raw)

        key = (phone, business_name.lower())
        if key in seen:
            skipped += 1
            continue

        seen[key] = {
            "business_entity": BUSINESS_ENTITY,
            "phone": phone,
            "owner_name": owner_name,
            "business_name": business_name,
            "email": email,
            "status": status,
            "has_website": has_website,
            "call_notes": notes,
            "source_market": county or "NC",
        }

    return list(seen.values()), skipped, len(rows)


async def import_contractors(request: Request) -> JSONResponse:
    force = request.query_params.get("force", "false").lower() == "true"

    if not os.path.exists(CSV_PATH):
        return JSONResponse(
            {"error": f"CSV not found at {CSV_PATH}. Upload it via GitHub first."},
            status_code=404,
        )

    client = get_client()

    if not force:
        existing = (
            client.table("contractors")
            .select("id", count="exact")
            .eq("business_entity", BUSINESS_ENTITY)
            .limit(1)
            .execute()
        )
        if existing.count and existing.count > 0:
            return JSONResponse(
                {
                    "error": f"CO-003 already has {existing.count} contractor rows. "
                    "Refusing to re-import without ?force=true to avoid duplicating data.",
                },
                status_code=409,
            )

    try:
        records, skipped_dupes, total_rows = _load_and_dedupe(CSV_PATH)
    except Exception as e:
        logger.error(f"[IMPORT] Failed to parse CSV: {e}")
        return JSONResponse({"error": f"CSV parse error: {e}"}, status_code=500)

    imported = 0
    errors = []

    for i in range(0, len(records), BATCH_SIZE):
        batch = records[i:i + BATCH_SIZE]
        try:
            client.table("contractors").upsert(
                batch,
                on_conflict="business_entity,phone,business_name",
            ).execute()
            imported += len(batch)
        except Exception as e:
            errors.append(f"Batch at row {i}: {e}")
            logger.error(f"[IMPORT] Batch error at {i}: {e}")

    status_summary = {}
    for r in records:
        status_summary[r["status"]] = status_summary.get(r["status"], 0) + 1

    logger.info(f"[IMPORT] Imported {imported}/{len(records)} contractors for {BUSINESS_ENTITY}")

    return JSONResponse({
        "success": len(errors) == 0,
        "total_csv_rows": total_rows,
        "exact_duplicates_skipped": skipped_dupes,
        "unique_records": len(records),
        "imported": imported,
        "status_breakdown": status_summary,
        "errors": errors,
    })
