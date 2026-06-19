"""
Contractor lookup - reads contractor records from Supabase by phone number.

This is what lets Maya know who she's calling before the conversation
starts: owner_name, business_name, and email (for Calendly booking
verification). Used by telephony/routes.py when a call connects, and
will also be used by the power dialer to initiate calls with the right
metadata.

Uses the Supabase service-role key, not the anon key - this is trusted
backend code, not a public client. Safe to use even before RLS is
enabled on the contractors table; becomes the same code path once RLS
is turned on later (no changes needed).
"""
import logging
from typing import Optional

from supabase import create_client, Client

from config import SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY

logger = logging.getLogger(__name__)

_client: Optional[Client] = None


def _get_client() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment variables."
            )
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    return _client


def get_client() -> Client:
    """Public accessor for the shared Supabase client. Used by modules
    outside this file (e.g. backend/admin_import.py) that need direct
    table access beyond the helper functions defined here."""
    return _get_client()


def normalize_phone(phone: str) -> str:
    """Ensure a phone number is in E.164 format (+1XXXXXXXXXX) for lookup.

    Twilio call params are usually already E.164, but this guards against
    stray formatting differences (spaces, dashes, missing +1).
    """
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits}"


async def list_contractors(business_entity: str = "CO-003", limit: int = 50, offset: int = 0, exclude_statuses: Optional[list] = None) -> list:
    """List contractors for a business entity, optionally excluding certain
    statuses (e.g. already-booked or declined contractors the dialer
    shouldn't show). Used by the power dialer page. offset enables pagination.
    """
    exclude_statuses = exclude_statuses or []
    client = _get_client()
    query = client.table("contractors").select("*").eq("business_entity", business_entity)
    for status in exclude_statuses:
        query = query.neq("status", status)
    resp = query.order("status").order("business_name").range(offset, offset + limit - 1).execute()
    return resp.data or []


async def count_contractors(business_entity: str = "CO-003", exclude_statuses: Optional[list] = None) -> int:
    """Total count of contractors for a business entity, with the same status
    filtering as list_contractors. Used to compute total pages for the dialer.
    """
    exclude_statuses = exclude_statuses or []
    client = _get_client()
    query = client.table("contractors").select("id", count="exact").eq("business_entity", business_entity)
    for status in exclude_statuses:
        query = query.neq("status", status)
    resp = query.limit(1).execute()
    return resp.count or 0


async def get_contractor_by_phone(phone: str, business_entity: str = "CO-003") -> Optional[dict]:
    """Look up a contractor record by phone number, scoped to a business entity.

    Returns None if no record is found - callers should handle this
    gracefully (e.g. inbound calls from numbers not on any outreach list).
    """
    normalized = normalize_phone(phone)

    try:
        client = _get_client()
        resp = (
            client.table("contractors")
            .select("*")
            .eq("phone", normalized)
            .eq("business_entity", business_entity)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.error(f"[CONTRACTOR_LOOKUP] Supabase query failed for {normalized}: {e}")
        return None

    if not resp.data:
        logger.info(f"[CONTRACTOR_LOOKUP] No record found for {normalized} ({business_entity})")
        return None

    record = resp.data[0]
    logger.info(
        f"[CONTRACTOR_LOOKUP] Found {record.get('owner_name')} / {record.get('business_name')} for {normalized}"
    )
    return record


def get_effective_email(record: dict) -> Optional[str]:
    """Return the email to actually use: corrected_email if present, else email."""
    return record.get("corrected_email") or record.get("email")


async def update_contractor_status(
    phone: str,
    business_entity: str,
    status: str,
    call_notes: Optional[str] = None,
    corrected_email: Optional[str] = None,
) -> bool:
    """Update a contractor's status after a call. Used by Maya's end-of-call
    handling and by the dialer when a call is initiated.

    Never overwrites `email` - corrections always go into `corrected_email`.
    """
    normalized = normalize_phone(phone)

    update_data = {"status": status}
    if call_notes:
        update_data["call_notes"] = call_notes
    if corrected_email:
        update_data["corrected_email"] = corrected_email

    try:
        client = _get_client()
        client.table("contractors").update(update_data).eq("phone", normalized).eq(
            "business_entity", business_entity
        ).execute()
        return True
    except Exception as e:
        logger.error(f"[CONTRACTOR_LOOKUP] Failed to update status for {normalized}: {e}")
        return False
