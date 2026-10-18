"""Backend reader for component-only original-publisher-v2 evidence.

These parsers do not enroll targets, accept CI readiness or create operations.
The future executing pipeline/Ready protocol is separate and remains disabled.
"""
from functools import lru_cache

from . import publisher_contract_v2 as protocol
from .website_publishing_inputs import _public_tree, _shape, _schema, validate_candidate as native_validate


def wire_validate(value, document):
    """Use the receiver's strict native wire parser without coercion/defaults."""
    _public_tree(value)
    if not _shape(value, document, document):
        raise ValueError("publisher_wire_invalid")


@lru_cache(maxsize=1)
def schemas():
    """Return the exact source-pinned schemas for this pending protocol."""
    return protocol.candidate_schema(_schema()), protocol.receipt_schema()


def arguments():
    return {"native_validate": native_validate, "wire_validate": wire_validate, "native_schema": _schema()}


def validate_candidate(value):
    """Read a proposed contract without promoting it to publishing authority."""
    return protocol.validate_candidate(value, **arguments())


def validate_component(value, *, candidate, origin, original_binding, trusted_profile):
    """Check independently reserved scope/profile; keep pipeline authority false."""
    return protocol.validate_receipt(value, candidate=candidate, origin=origin,
        original_binding=original_binding, trusted_profile=trusted_profile, **arguments())
