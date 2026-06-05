from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from .models import User

logger = logging.getLogger(__name__)

SLACK_ID_PATTERN = re.compile(r'^[UW][A-Z0-9]{8,10}$')


@dataclass
class SlackUserRegistrationResult:
    user: User
    created: bool = False
    linked: bool = False


def ensure_slack_user(
    *,
    slack_id: str,
    email: str,
    first_name: str = "",
    last_name: str = "",
    avatar_url: Optional[str] = None,
) -> SlackUserRegistrationResult:
    normalized_email = str(email or "").strip().lower()
    if not slack_id or not normalized_email:
        raise ValueError("slack_id and email are required")

    user = User.objects.filter(slack_id=slack_id).first()
    if user:
        update_fields = []
        if user.email.lower() != normalized_email:
            user.email = normalized_email
            update_fields.append("email")
        if first_name and not user.first_name:
            user.first_name = first_name
            update_fields.append("first_name")
        if last_name and not user.last_name:
            user.last_name = last_name
            update_fields.append("last_name")
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
            update_fields.append("avatar_url")
        if update_fields:
            user.save(update_fields=update_fields)
        return SlackUserRegistrationResult(user=user, created=False, linked=False)

    user = User.objects.filter(email__iexact=normalized_email).first()
    if user:
        update_fields = []
        if user.slack_id != slack_id:
            user.slack_id = slack_id
            update_fields.append("slack_id")
        if first_name and not user.first_name:
            user.first_name = first_name
            update_fields.append("first_name")
        if last_name and not user.last_name:
            user.last_name = last_name
            update_fields.append("last_name")
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
            update_fields.append("avatar_url")
        if update_fields:
            user.save(update_fields=update_fields)
        return SlackUserRegistrationResult(user=user, created=False, linked=True)

    user = User.objects.create_user(
        email=normalized_email,
        role='participant',
        first_name=first_name,
        last_name=last_name,
        slack_id=slack_id,
    )
    user.is_active = True
    if avatar_url:
        user.avatar_url = avatar_url
    user.save()
    return SlackUserRegistrationResult(user=user, created=True, linked=False)


def validate_slack_id(slack_id: Optional[str]) -> Tuple[bool, str]:
    """Validate a Slack user ID's format. Returns (is_valid, error_message)."""
    if not slack_id:
        return False, "slack id is required"
    if slack_id == "system":
        return True, ""
    if not SLACK_ID_PATTERN.match(slack_id):
        return False, "Invalid Slack ID format"
    return True, ""


def _slack_profile(slack_user_id: str) -> Optional[dict]:
    """Fetch a Slack profile, returning None on any failure."""
    try:
        from integrations.services.slack import SlackService
        return SlackService.get_user_profile(slack_user_id)
    except Exception as exc:  # pragma: no cover - network / credential issues
        logger.warning("Slack profile lookup failed for %s: %s", slack_user_id, exc)
        return None


def resolve_existing_user(slack_user_id: str) -> Optional[User]:
    """Resolve a Slack ID to an *existing* Django user without creating one.

    Matches on ``slack_id`` first, then falls back to the email on the Slack
    profile (backfilling ``slack_id`` on the first match). Returns ``None`` when
    no existing account can be linked — callers should treat that as
    "not authorised".
    """
    if not slack_user_id:
        return None

    user = User.objects.filter(slack_id=slack_user_id).first()
    if user:
        return user

    profile = _slack_profile(slack_user_id)
    if not profile:
        return None

    email = profile.get('email')
    if not email:
        return None

    user = User.objects.filter(email__iexact=email).first()
    if user and not user.slack_id:
        user.slack_id = slack_user_id
        user.save(update_fields=['slack_id'])
    return user


def resolve_or_create_user(slack_user_id: str) -> Optional[User]:
    """Resolve a Slack ID to a Django user, creating one if needed.

    Used to attribute bot-authored content (for example a Roo announcement).
    Returns ``None`` only when the Slack profile cannot be fetched for a brand
    new Slack ID.
    """
    if not slack_user_id:
        return None

    user = User.objects.filter(slack_id=slack_user_id).first()
    if user:
        return user

    profile = _slack_profile(slack_user_id)
    if not profile:
        return None

    email = profile.get('email') or f"{slack_user_id}@slack.placeholder.com"
    real_name = (profile.get('real_name') or 'Unknown').strip() or 'Unknown'
    name_parts = real_name.split()

    result = ensure_slack_user(
        slack_id=slack_user_id,
        email=email,
        first_name=name_parts[0] if name_parts else 'Unknown',
        last_name=' '.join(name_parts[1:]) if len(name_parts) > 1 else '',
        avatar_url=profile.get('image_url'),
    )
    return result.user
