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

# Eastern time offset (UTC-4 during EDT, UTC-5 during EST)
# Using pytz would be cleaner but this avoids an extra dep
ET_OFFSET = -4  # EDT (summer)

DAY_NAMES = {0: "Monday", 1: "Tuesday", 2: "Wednesday",
             3: "Thursday", 4: "Friday", 5: "Saturday", 6: "Sunday"}

MONTH_NAMES = {1: "January", 2: "February", 3: "March", 4: "April",
               5: "May", 6: "June", 7: "July", 8: "August",
               9: "September", 10: "October", 11: "November", 12: "December"}


def _format_slot_for_voice(iso_time: str) -> str:
    """Convert ISO8601 time to a natural spoken string, e.g. 'Monday June 23rd at 9am'."""
    try:
        # Parse the time string — handle offset formats like -04:00
        dt = datetime.fromisoformat(iso_time)
        # Day name
        day_name = DAY_NAMES[dt.weekday()]
        month_name = MONTH_NAMES[dt.month]
        day_num = dt.day
        suffix = "th" if 11 <= day_num <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day_num % 10, "th")
        # Hour
        hour = dt.hour
        minute = dt.minute
        if hour == 0:
            time_str = "12am" if minute == 0 else f"12:{minute:02d}am"
        elif hour < 12:
            time_str = f"{hour}am" if minute == 0 else f"{hour}:{minute:02d}am"
        elif hour == 12:
            time_str = "12pm" if minute == 0 else f"12:{minute:02d}pm"
        else:
            h = hour - 12
            time_str = f"{h}pm" if minute == 0 else f"{h}:{minute:02d}pm"
        return f"{day_name} {month_name} {day_num}{suffix} at {time_str}"
    except Exception:
        return iso_time  # fallback to raw if parsing fails


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

        Returns up to 5 upcoming slots within the next 7 days.

        NOTE: We intentionally ignore the `date` filter arg from the LLM.
        The LLM frequently hallucinates wrong years when constructing a date
        string, which causes all slots to be filtered out. Instead we always
        return the next 5 available slots across the whole 7-day window and
        let Maya present them by their full spoken name (day + date + time)
        so there's no ambiguity about which day is being offered.
        """
        if not CALCOM_EVENT_TYPE_ID:
            logger.error("[CALCOM] CALCOM_EVENT_TYPE_ID is not set")
            return {
                "available_slots": [],
                "message": "I'm having trouble pulling up the calendar right now. Can I get your name and best callback number so our rep can reach out to find a time that works?",
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
                "message": "I'm having trouble pulling up the calendar right now. Can I get your name and best callback number so our rep can reach out to find a time that works?",
            }

        if resp.status_code != 200:
            logger.error(f"[CALCOM] Availability lookup failed: {resp.status_code} {resp.text}")
            return {
                "available_slots": [],
                "message": "I'm having trouble pulling up the calendar right now. Can I get your name and best callback number so our rep can reach out to find a time that works?",
            }

        data = resp.json()
        raw_data = data.get("data", {})

        all_slots = []

        if isinstance(raw_data, dict):
            slots_by_date = raw_data.get("slots", raw_data)
            for day, day_slots in sorted(slots_by_date.items()):
                if isinstance(day_slots, list):
                    for slot in day_slots:
                        t = slot.get("start") or slot.get("time") or slot.get("start_time") or slot.get("startTime")
                        if t:
                            all_slots.append({"start_time": t, "date": day})
        elif isinstance(raw_data, list):
            for slot in raw_data:
                t = slot.get("start") or slot.get("time") or slot.get("start_time") or slot.get("startTime")
                if t:
                    all_slots.append({"start_time": t})

        if not all_slots:
            logger.warning(f"[CALCOM] No slots parsed from response.")
            return {
                "available_slots": [],
                "message": "I'm not seeing any open times in the next week. Can I get your name and best callback number so our rep can reach out directly?",
            }

        # Cap at 5
        all_slots = all_slots[:5]
        logger.info(f"[CALCOM] Returning {len(all_slots)} slots")

        return {
            "available_slots": [
                {
                    "start_time": s["start_time"],
                    "spoken_label": _format_slot_for_voice(s["start_time"]),
                    "status": "available",
                }
                for s in all_slots
            ],
            "total_available": len(all_slots),
            "instructions": "Use the spoken_label field when reading times aloud — it includes the full day name and date so there is no ambiguity. Example: 'I have Monday June 23rd at 9am available — does that work for you?' When booking, pass the start_time field (not the spoken_label) to book_meeting.",
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
            return {"success": False, "error": "Booking system is not configured. Get the contractor's name and callback number for our rep to follow up."}

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
            return {"success": False, "error": "Booking system is not responding. Get the contractor's name and callback number for our rep to follow up."}

        if resp.status_code not in (200, 201):
            logger.error(f"[CALCOM] Booking failed: {resp.status_code} {resp.text}")
            return {
                "success": False,
                "error": "That time didn't go through. Try offering the next available slot from the list, or get the contractor's callback number for our rep.",
            }

        result = resp.json().get("data", {})
        logger.info(f"[CALCOM] Booked: {contractor_name} ({contractor_email}) at {start_time}")

        return {
            "success": True,
            "confirmation": f"Booked for {contractor_name} at {start_time}. A calendar invite and confirmation email are on their way to {contractor_email}.",
            "booking_uid": result.get("uid"),
        }


# Singleton
cal_service = CalService()
