"""Server-owned fixture enrollment for B1; worker payloads cannot opt in."""

from contextlib import contextmanager
from contextvars import ContextVar

_organization = ContextVar("articles_failure_organization", default=None)


@contextmanager
def failure_scope(organization_id):
    """Bind an authenticated resource's saved organization, without a lookup."""
    token = _organization.set(organization_id)
    try:
        yield
    finally:
        _organization.reset(token)


def taxonomy_enabled(organization_id=None, *, observe=False):
    """Require both the deployment switch and an exact server-owned allowlist."""
    from django.conf import settings
    if not settings.configured:
        return False
    organization_id = organization_id if organization_id is not None else _organization.get()
    name = "ARTICLES_FAILURE_TAXONOMY_OBSERVE_ENABLED" if observe else "ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED"
    return bool(getattr(settings, name, False) and organization_id is not None
                and str(organization_id) in getattr(settings, "ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS", ()))
