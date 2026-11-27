"""Immutable personal review identity used by sign-in and the points ledger."""

from uuid import NAMESPACE_URL, UUID, uuid5


def workspace_id(profile_id):
    """Derive a stable personal workspace without creating a startup or membership."""
    return str(uuid5(NAMESPACE_URL, f"https://mlai.au/security/personal/{UUID(str(profile_id))}"))


def personal_binding(binding):
    """The personal protocol uses the same v5 UUID for its subject and workspace."""
    subject = binding.get("mlai_company_id")
    return bool(subject and subject == binding.get("workspace_id") and UUID(subject).version == 5)
