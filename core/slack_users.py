from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import User


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
