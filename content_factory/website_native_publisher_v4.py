"""Read hosted native publisher setup evidence without promoting any target."""
from . import native_publisher_contract_v4 as protocol
from .website_publisher_inputs_v2 import validate_candidate,wire_validate


def validate_native_setup(value,*,candidate,records,identity,supported_profile,original_binding,current_binding,origin):
    """Require independently saved context/profile and server-authenticated origin.

    This function does not authenticate transport, reserve missing original
    facts, select a profile, write a target or grant publication/Ready authority.
    Each expected argument must come from owned server state, never the receipt.
    """
    return protocol.validate_receipt(value,candidate=candidate,records=records,identity=identity,profile=supported_profile,
        original_binding=original_binding,current_binding=current_binding,origin=origin,
        candidate_validate=validate_candidate,wire_validate=wire_validate)
