"""
Function dispatch - routes agent function calls to the backend scheduling service.
Each function the agent can call (defined in agent_config.py) maps to a method
on the scheduling service.  This module is the bridge between the voice agent
layer and the backend layer.
To swap the mock backend for a real API, you only need to change the imports
and method calls here - the voice agent layer doesn't know or care whether
the backend is in-memory or a remote HTTP service.
"""
import logging

logger = logging.getLogger(__name__)


async def dispatch_function(name: str, args: dict) -> dict:
    """Dispatch a function call to the appropriate backend handler.
    Args:
        name: Function name (matches names in agent_config.FUNCTIONS)
        args: Parsed arguments from the LLM
    Returns:
        Result dict that gets sent back to the agent as context for its next response.
    """
    from backend.scheduling_service import scheduling_service

    if name == "check_available_slots":
        return await scheduling_service.get_available_slots(
            date=args.get("date"),
            provider=args.get("provider"),
        )
    elif name == "book_appointment":
        return await scheduling_service.book_appointment(
            patient_name=args["patient_name"],
            patient_phone=args["patient_phone"],
            slot_id=args["slot_id"],
        )
    elif name == "check_appointment":
        return await scheduling_service.check_appointment(
            patient_name=args.get("patient_name"),
            patient_phone=args.get("patient_phone"),
        )
    elif name == "cancel_appointment":
        return await scheduling_service.cancel_appointment(
            appointment_id=args["appointment_id"],
        )
    elif name == "end_call":
        reason = args.get("reason", "customer_goodbye")
        logger.info(f"Call ending: {reason}")
        return {"status": "call_ended", "reason": reason}
    elif name == "transfer_call":
        reason = args.get("reason", "interested_in_service")
        logger.info(f"Transfer requested: {reason}")
        return {"status": "transfer_initiated", "reason": reason}
    else:
        logger.warning(f"Unknown function: {name}")
        return {"error": f"Unknown function: {name}"}
