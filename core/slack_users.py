from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from django.db import IntegrityError, transaction

from .models import User
from .slack_founder_links import (
    ConflictingSlackFounderLinkError,
    assign_direct_slack_identity,
    user_participates_in_slack_founder_link,
)

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
        if (
            user.email.lower() != normalized_email
            and not user_participates_in_slack_founder_link(user)
        ):
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
    if user and user.slack_id != slack_id:
        try:
            user = assign_direct_slack_identity(user, slack_id)
        except ConflictingSlackFounderLinkError:
            # Preserve the explicit account boundary. Roo may still register
            # this Slack identity as a separate placeholder user.
            normalized_email = f"{slack_id}@slack.placeholder.com"
            user = None
    if user:
        update_fields = []
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


def resolve_existing_user_from_profile(
    *,
    slack_user_id: str,
    profile: dict,
) -> Optional[User]:
    """Link a verified Slack profile to an existing MLAI user.

    The caller owns fetching the profile from Slack. This helper never creates
    a user and never moves an MLAI account from one Slack identity to another.
    Concurrent attempts are serialized on the matching user row; the unique
    ``slack_id`` constraint remains the final guard against conflicting links.
    """
    requested_slack_id = str(slack_user_id or "").strip()
    if not requested_slack_id or not isinstance(profile, dict):
        return None

    profile_slack_id = str(profile.get("slack_id") or "").strip()
    if profile_slack_id and profile_slack_id != requested_slack_id:
        logger.warning(
            "Refusing Slack profile mismatch: requested=%s returned=%s",
            requested_slack_id,
            profile_slack_id,
        )
        return None
    if profile.get("is_bot") or profile.get("deleted"):
        return None

    normalized_email = User.objects.normalize_email(profile.get("email"))
    if not normalized_email:
        return None

    with transaction.atomic():
        already_linked = (
            User.objects.select_for_update()
            .filter(slack_id=requested_slack_id)
            .first()
        )
        if already_linked:
            return already_linked

        user = (
            User.objects.select_for_update()
            .filter(email__iexact=normalized_email)
            .first()
        )
        if not user:
            return None

        existing_slack_id = str(user.slack_id or "").strip()
        if existing_slack_id and existing_slack_id != requested_slack_id:
            logger.warning(
                "Refusing to relink MLAI user %s from Slack %s to %s",
                user.pk,
                existing_slack_id,
                requested_slack_id,
            )
            return None

        if existing_slack_id == requested_slack_id:
            return user

        try:
            # Keep the assignment in a savepoint so a concurrent unique-key
            # winner can be inspected without poisoning this transaction.
            with transaction.atomic():
                user = assign_direct_slack_identity(user, requested_slack_id)
        except ConflictingSlackFounderLinkError:
            return None
        except IntegrityError:
            winner = User.objects.filter(slack_id=requested_slack_id).first()
            if winner and winner.pk == user.pk:
                return winner
            return None
        return user


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

    return resolve_existing_user_from_profile(
        slack_user_id=slack_user_id,
        profile=profile,
    )


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
