"""
Cal.com scheduling service — replaces Calendly for CO-003 craftd bookings.

Uses Cal.com API v2. Bookings made here are real — Cal.com sends the
contractor an actual calendar invite + confirmation email.

Auth: API key (CALCOM_API_KEY). Set in Railway environment variables.
Event type: CALCOM_EVENT_TYPE_ID — the numeric ID from the Cal.com event
type URL (e.g. cal.com/event-types/6072650 → 6072650).
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config import CALCOM_API_KEY, CALCOM_EVENT_TYPE_ID

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cal.com/v2"


class CalService:
    """Real scheduling backend backed by Cal.com API v2."""

    def _headers(self) -> dict:
        if not CALCOM_API_KEY:
            raise RuntimeError(
                "CALCOM_API_KEY is not set. Add it to Railway environment variables."
            )
        return {
            "Authorization": f"Bearer {CALCOM_API_KEY}",
            "Content-Type": "application/json",
            "cal-api-version": "2024-09-04",
        }

    async def get_available_slots(self, date: Optional[str] = None) -> dict:
        """Get available time slots for the craftd discovery call event type.

        Returns up to 5 upcoming slots within the next 7 days, optionally
        filtered to a specific date if the contractor named one.
        """
        if not CALCOM_EVENT_TYPE_ID:
            logger.error("[CALCOM] CALCOM_EVENT_TYPE_ID is not set")
            return {
                "available_slots": [],
                "message": "I'm having trouble pulling up the calendar right now. Let me have our rep reach out to you directly to find a time.",
            }

        now = datetime.now(timezone.utc)
        start = now + timedelta(minutes=15)
        end = now + timedelta(days=7)

        params = {
            "eventTypeId": CALCOM_EVENT_TYPE_ID,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeZone": "America/New_York",
        }

        try:
            resp = requests.get(
                f"{BASE_URL}/slots",
                headers=self._headers(),
                params=params,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"[CALCOM] Availability request failed: {e}")
            return {
                "available_slots": [],
                "message": "I'm having trouble pulling up the calendar right now. Let me have our rep reach out to you directly to find a time.",
            }

        if resp.status_code != 200:
            logger.error(f"[CALCOM] Availability lookup failed: {resp.status_code} {resp.text}")
            return {
                "available_slots": [],
                "message": "I'm having trouble pulling up the calendar right now. Let me have our rep reach out to you directly to find a time.",
            }

        data = resp.json()
        logger.info(f"[CALCOM] Raw slots response: {data}")

        # Cal.com v2 slots response shape:
        # { "status": "success", "data": { "slots": { "2026-06-23": [{"time": "...", "slotUtcOffsetMinutes": ...}] } } }
        # OR flat: { "status": "success", "data": [ {"time": "..."}, ... ] }
        raw_data = data.get("data", {})

        all_slots = []

        if isinstance(raw_data, dict):
            # Nested by date: { "slots": { "YYYY-MM-DD": [...] } } or just { "YYYY-MM-DD": [...] }
            slots_by_date = raw_data.get("slots", raw_data)
            for day, day_slots in sorted(slots_by_date.items()):
                if date and day != date:
                    continue
                if isinstance(day_slots, list):
                    for slot in day_slots:
                        t = slot.get("time") or slot.get("start_time") or slot.get("startTime")
                        if t:
                            all_slots.append({"start_time": t, "date": day})
        elif isinstance(raw_data, list):
            # Flat list of slots
            for slot in raw_data:
                t = slot.get("time") or slot.get("start_time") or slot.get("startTime")
                if t:
                    if date and not t.startswith(date):
                        continue
                    all_slots.append({"start_time": t})

        if not all_slots:
            logger.warning(f"[CALCOM] No slots found in response. Raw data: {raw_data}")
            return {
                "available_slots": [],
                "message": "I'm not seeing any open times in the next week. Let me have our rep reach out to you directly to find a time that works.",
            }

        # Cap at 5 — nobody wants 20 times read aloud on a phone call
        all_slots = all_slots[:5]

        return {
            "available_slots": [
                {
                    "start_time": s["start_time"],
                    "status": "available",
                }
                for s in all_slots
            ],
            "total_available": len(all_slots),
            "note": "Times are in America/New_York. Speak them naturally, e.g. 'Monday at 2pm'.",
        }

    async def book_appointment(
        self,
        contractor_name: str,
        contractor_email: str,
        start_time: str,
    ) -> dict:
        """Book a real Cal.com meeting. Sends a calendar invite + confirmation
        email to the contractor and adds it to the host calendar automatically.
        """
        if not CALCOM_EVENT_TYPE_ID:
            logger.error("[CALCOM] CALCOM_EVENT_TYPE_ID is not set")
            return {"success": False, "error": "Booking system is not configured. Have our rep reach out directly."}

        name_parts = contractor_name.strip().split(" ", 1)
        first_name = name_parts[0]
        last_name = name_parts[1] if len(name_parts) > 1 else ""

        payload = {
            "eventTypeId": int(CALCOM_EVENT_TYPE_ID),
            "start": start_time,
            "attendee": {
                "name": contractor_name,
                "email": contractor_email,
                "timeZone": "America/New_York",
                "language": "en",
            },
            "metadata": {
                "source": "maya-voice-agent",
                "firstName": first_name,
                "lastName": last_name,
            },
        }

        try:
            resp = requests.post(
                f"{BASE_URL}/bookings",
                headers=self._headers(),
                json=payload,
                timeout=10,
            )
        except Exception as e:
            logger.error(f"[CALCOM] Booking request failed: {e}")
            return {"success": False, "error": "Booking system is not responding. Have our rep reach out directly."}

        if resp.status_code not in (200, 201):
            logger.error(f"[CALCOM] Booking failed: {resp.status_code} {resp.text}")
            return {
                "success": False,
                "error": "That time didn't go through — it may have just been taken. Let's try another time.",
            }

        result = resp.json().get("data", {})
        logger.info(f"[CALCOM] Booked: {contractor_name} ({contractor_email}) at {start_time}")

        return {
            "success": True,
            "confirmation": f"Booked for {contractor_name} at {start_time}. A calendar invite and confirmation email are on their way.",
            "booking_uid": result.get("uid"),
        }


# Singleton
cal_service = CalService()
