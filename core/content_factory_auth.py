from __future__ import annotations

from django.utils import timezone


CONTENT_FACTORY_GITHUB_TOKEN_EXPIRY_BUFFER = timezone.timedelta(minutes=5)


def content_factory_github_connection_state(config) -> str:
    if not config or not str(getattr(config, "github_token_encrypted", "") or "").strip():
        return "auth_required"

    expires_at = getattr(config, "github_token_expires_at", None)
    if expires_at and timezone.now() >= (expires_at - CONTENT_FACTORY_GITHUB_TOKEN_EXPIRY_BUFFER):
        return "auth_required"

    if str(getattr(config, "github_repo", "") or "").strip():
        return "connected"
    return "repo_selection_required"
