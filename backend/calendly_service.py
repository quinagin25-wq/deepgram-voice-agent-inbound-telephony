"""
Calendly scheduling service - replaces the mock dental-office scheduling_service.

This is the REAL backend Maya uses to check availability and book live
meetings on Capital's Calendly account. Bookings made here are real -
Calendly sends the contractor an actual calendar invite + confirmation
(and a Zoom link, if the event type has Zoom attached as the location).

Auth: Personal Access Token (CALENDLY_API_KEY). Capital is the host/owner
of the Calendly account, so a PAT is sufficient - no OAuth app needed.
(Calendly's own docs confirm PAT is valid for an account owner using the
API for their own organization; OAuth is only required for apps booking
on behalf of OTHER people's Calendly accounts.)

Event type resolution: CALENDLY_EVENT_TYPE_URI should be the full
event_type URI (e.g. "https://api.calendly.com/event_types/<uuid>").
If only the slug is known, call resolve_event_type_uri() once at startup
to look it up from the user's event type list and cache it.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config import CALENDLY_API_KEY, CALENDLY_EVENT_TYPE_URI

logger = logging.getLogger(__name__)

BASE_URL = "https://api.calendly.com"

# Slug from the existing craftd booking link, used to auto-resolve the
# event_type URI if CALENDLY_EVENT_TYPE_URI isn't set directly.
# https://calendly.com/craftd26/craftd-free-strategy-call-clone-clone
FALLBACK_SLUG = "craftd-free-strategy-call-clone-clone"


class CalendlyService:
    """Real scheduling backend backed by the Calendly Scheduling API."""

    def __init__(self):
        self._event_type_uri: Optional[str] = CALENDLY_EVENT_TYPE_URI
        self._user_uri: Optional[str] = None
        self._resolved = False

    # ------------------------------------------------------------------
    # Setup / resolution
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        if not CALENDLY_API_KEY:
            raise RuntimeError(
                "CALENDLY_API_KEY is not set. Add it to Railway environment variables."
            )
        return {
            "Authorization": f"Bearer {CALENDLY_API_KEY}",
            "Content-Type": "application/json",
        }

    async def _ensure_resolved(self):
        """Resolve user URI and (if needed) event type URI. Runs once, lazily."""
        if self._resolved:
            return

        resp = requests.get(f"{BASE_URL}/users/me", headers=self._headers())
        resp.raise_for_status()
        user = resp.json()["resource"]
        self._user_uri = user["uri"]
        logger.info(f"[CALENDLY] Resolved user URI: {self._user_uri}")

        if not self._event_type_uri:
            logger.info(f"[CALENDLY] No CALENDLY_EVENT_TYPE_URI set - resolving from slug '{FALLBACK_SLUG}'")
            self._event_type_uri = await self._resolve_event_type_by_slug(FALLBACK_SLUG)
            logger.info(f"[CALENDLY] Resolved event type URI: {self._event_type_uri}")

        self._resolved = True

    async def _resolve_event_type_by_slug(self, slug: str) -> str:
        """Look up an event type URI by matching its scheduling_url slug."""
        params = {"user": self._user_uri, "count": 100}
        url = f"{BASE_URL}/event_types"

        while url:
            resp = requests.get(url, headers=self._headers(), params=params)
            resp.raise_for_status()
            data = resp.json()

            for et in data["collection"]:
                et_slug = et["scheduling_url"].rstrip("/").split("/")[-1]
                if et_slug == slug:
                    return et["uri"]

            url = data.get("pagination", {}).get("next_page")
            params = None  # next_page already includes query params

        raise ValueError(f"No Calendly event type found matching slug '{slug}'")

    # ------------------------------------------------------------------
    # Public API - mirrors the shape of the old scheduling_service so
    # function_handlers.py changes are minimal.
    # ------------------------------------------------------------------

    async def get_available_slots(self, date: Optional[str] = None) -> dict:
        """Get available time slots for the craftd walkthrough event type.

        Calendly's available-times endpoint only returns up to 7 days per
        request, so we default to 'now through 7 days out' and let the
        agent re-call this if the contractor wants something further out.
        """
        await self._ensure_resolved()

        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=15)  # small buffer, can't book in the past
        end = now + timedelta(days=7)

        params = {
            "event_type": self._event_type_uri,
            "start_time": start.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "end_time": end.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        }

        resp = requests.get(
            f"{BASE_URL}/event_type_available_times",
            headers=self._headers(),
            params=params,
        )

        if resp.status_code != 200:
            logger.error(f"[CALENDLY] Availability lookup failed: {resp.status_code} {resp.text}")
            return {
                "available_slots": [],
                "message": "I'm having trouble pulling up availability right now.",
            }

        slots = resp.json().get("collection", [])

        # Optionally filter to a specific date if the contractor named one
        if date:
            slots = [s for s in slots if s["start_time"].startswith(date)]

        if not slots:
            return {
                "available_slots": [],
                "message": "No open times found in the next week. Offer to follow up by text instead.",
            }

        # Cap at 5 for a voice conversation - nobody wants 20 times read aloud
        slots = slots[:5]

        return {
            "available_slots": [
                {
                    "start_time": s["start_time"],  # ISO8601 UTC, e.g. 2026-06-22T18:30:00.000000Z
                    "status": s.get("status", "available"),
                }
                for s in slots
            ],
            "total_available": len(slots),
            "note": "start_time values are in UTC. Convert to the contractor's local time before speaking it aloud.",
        }

    async def book_appointment(
        self,
        contractor_name: str,
        contractor_email: str,
        start_time: str,
    ) -> dict:
        """Book a real Calendly meeting. This sends a real calendar invite
        + confirmation email to the contractor, and creates the event on
        Capital's calendar too - standard Calendly behavior for any booking
        made through this endpoint.
        """
        await self._ensure_resolved()

        name_parts = contractor_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        payload = {
            "event_type": self._event_type_uri,
            "start_time": start_time,
            "invitee": {
                "name": contractor_name,
                "first_name": first_name,
                "last_name": last_name,
                "email": contractor_email,
                "timezone": "America/New_York",
            },
        }

        resp = requests.post(
            f"{BASE_URL}/invitees",
            headers=self._headers(),
            json=payload,
        )

        if resp.status_code not in (200, 201):
            logger.error(f"[CALENDLY] Booking failed: {resp.status_code} {resp.text}")
            return {
                "success": False,
                "error": "That time didn't go through - it may have just been taken. Let's try another time.",
            }

        result = resp.json().get("resource", {})
        logger.info(f"[CALENDLY] Booked: {contractor_name} ({contractor_email}) at {start_time}")

        return {
            "success": True,
            "confirmation": f"Booked for {contractor_name} at {start_time} (UTC). A calendar invite and confirmation email are on their way.",
            "reschedule_url": result.get("reschedule_url"),
            "cancel_url": result.get("cancel_url"),
        }


# Singleton - mirrors the pattern of the old scheduling_service.
calendly_service = CalendlyService()
