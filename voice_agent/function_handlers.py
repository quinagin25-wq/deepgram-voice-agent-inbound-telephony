"""
Function dispatch - routes agent function calls to the backend services.

Each function the agent can call (defined in agent_config.py) maps to a
method on a backend service. This module is the bridge between the voice
agent layer and the backend layer.

Two backends in play:
  - calendly_service: real Calendly Scheduling API (live bookings)
  - contractor_lookup: Supabase contractor record, for status updates

`contractor` is the record resolved by backend/contractor_lookup.py when
the call connected (see telephony/routes.py). It may be None if the caller
isn't on file (e.g. an inbound call from an unknown number) - functions
handle that case explicitly rather than assuming it's always present.
"""
import logging

logger = logging.getLogger(__name__)


async def dispatch_function(name: str, args: dict, contractor: dict = None) -> dict:
    """Dispatch a function call to the appropriate backend handler.

    Args:
        name: Function name (matches names in agent_config.FUNCTIONS)
        args: Parsed arguments from the LLM
        contractor: The contractor record for this call, if one was matched
            by phone number lookup. None for unknown callers.

    Returns:
        Result dict that gets sent back to the agent as context for its next response.
    """
    from backend.calendly_service import calendly_service
    from backend.contractor_lookup import get_effective_email, update_contractor_status

    if name == "check_availability":
        return await calendly_service.get_available_slots(date=args.get("date"))

    elif name == "book_meeting":
        if not contractor:
            # Shouldn't normally happen - Maya only offers to book once she
            # knows who she's talking to - but fail safely if it does.
            return {
                "success": False,
                "error": "I don't have your contact info pulled up to book this. Let me have someone follow up by phone instead.",
            }

        # Trust the looked-up/verified email by default. Only use what the
        # LLM passed if it's explicitly a correction the contractor gave
        # live on the call (signaled by the email_was_corrected flag).
        if args.get("email_was_corrected") and args.get("email"):
            email_to_use = args["email"]
            # Persist the correction so future calls don't need to ask again.
            await update_contractor_status(
                phone=contractor["phone"],
                business_entity=contractor.get("business_entity", "CO-003"),
                status=contractor.get("status", "not_called"),  # don't clobber status here
                corrected_email=email_to_use,
            )
        else:
            email_to_use = get_effective_email(contractor)

        if not email_to_use:
            return {
                "success": False,
                "error": "I don't have an email on file to send the invite to. Ask the contractor for one before booking.",
            }

        result = await calendly_service.book_appointment(
            contractor_name=contractor.get("owner_name") or args.get("contractor_name", "there"),
            contractor_email=email_to_use,
            start_time=args["start_time"],
        )

        # Update contractor status on success so the dialer/CRM reflects it.
        if result.get("success"):
            await update_contractor_status(
                phone=contractor["phone"],
                business_entity=contractor.get("business_entity", "CO-003"),
                status="booked",
                call_notes=f"Booked via Maya for {args['start_time']}",
            )

        return result

    elif name == "end_call":
        reason = args.get("reason", "customer_goodbye")
        logger.info(f"Call ending: {reason}")

        # Log the outcome to the contractor record, if we have one, so the
        # dialer's status column reflects what actually happened.
        if contractor and reason in ("not_interested", "no_answer", "callback_requested"):
            status_map = {
                "not_interested": "declined",
                "no_answer": "no_answer",
                "callback_requested": "callback_requested",
            }
            await update_contractor_status(
                phone=contractor["phone"],
                business_entity=contractor.get("business_entity", "CO-003"),
                status=status_map[reason],
            )

        return {"status": "call_ended", "reason": reason}

    elif name == "transfer_call":
        reason = args.get("reason", "available_now")
        logger.info(f"Transfer requested: {reason}")
        return {"status": "transfer_initiated", "reason": reason}

    else:
        logger.warning(f"Unknown function: {name}")
        return {"error": f"Unknown function: {name}"}
