from __future__ import annotations

import hashlib
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import SlackFounderAccountLink, SlackFounderLinkRequest, User

logger = logging.getLogger(__name__)
LINK_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class SlackFounderLinkError(Exception):
    code = "invalid_token"
    status_code = 400
    default_message = "This account-linking request is invalid."

    def __init__(self, message: str | None = None):
        super().__init__(message or self.default_message)


class ExpiredSlackFounderLinkError(SlackFounderLinkError):
    code = "expired_token"
    status_code = 410
    default_message = "This account-linking request has expired. Ask Roo for a new link."


class UsedSlackFounderLinkError(SlackFounderLinkError):
    code = "token_already_used"
    status_code = 409
    default_message = "This account-linking request has already been used."


class ConflictingSlackFounderLinkError(SlackFounderLinkError):
    code = "link_conflict"
    status_code = 409
    default_message = (
        "One of these accounts is already connected to a different account. "
        "Contact MLAI support to change an existing connection."
    )


@dataclass(frozen=True)
class SlackFounderLinkPreview:
    request: SlackFounderLinkRequest
    status: str
    link: SlackFounderAccountLink | None


def digest_link_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _link_request_for_token(
    token: str,
    *,
    for_update: bool,
) -> SlackFounderLinkRequest:
    normalized_token = str(token or "").strip()
    if not LINK_TOKEN_PATTERN.fullmatch(normalized_token):
        raise SlackFounderLinkError()

    queryset = SlackFounderLinkRequest.objects.select_related("slack_user")
    if for_update:
        queryset = queryset.select_for_update()

    request = queryset.filter(token_digest=digest_link_token(normalized_token)).first()
    if request is None or request.invalidated_at is not None:
        raise SlackFounderLinkError()
    if request.consumed_at is not None:
        raise UsedSlackFounderLinkError()
    if request.expires_at <= timezone.now():
        raise ExpiredSlackFounderLinkError()
    return request


def _link_status(
    *,
    slack_user: User,
    founder_user: User,
    for_update: bool,
) -> tuple[str, SlackFounderAccountLink | None]:
    user_ids = {slack_user.pk, founder_user.pk}
    links = SlackFounderAccountLink.objects.filter(
        Q(slack_user_id__in=user_ids) | Q(founder_user_id__in=user_ids)
    )
    if for_update:
        links = links.select_for_update()
    existing_links = list(links)

    same_link = next(
        (
            link
            for link in existing_links
            if link.slack_user_id == slack_user.pk
            and link.founder_user_id == founder_user.pk
        ),
        None,
    )
    if same_link is not None:
        return "already_linked", same_link

    if existing_links:
        raise ConflictingSlackFounderLinkError()

    # A direct Slack identity is existing verified ownership. Linking a
    # different Slack identity to it would allow two Slack users to claim the
    # same Founder Tools eligibility.
    if founder_user.slack_id and founder_user.slack_id != slack_user.slack_id:
        raise ConflictingSlackFounderLinkError()

    return "ready", None


@transaction.atomic
def create_slack_founder_link_request(
    slack_user: User,
) -> tuple[SlackFounderLinkRequest, str]:
    now = timezone.now()
    locked_user = User.objects.select_for_update().get(pk=slack_user.pk)

    SlackFounderLinkRequest.objects.filter(
        slack_user=locked_user,
        consumed_at__isnull=True,
        invalidated_at__isnull=True,
    ).update(invalidated_at=now, updated_at=now)

    raw_token = secrets.token_urlsafe(32)
    request = SlackFounderLinkRequest.objects.create(
        slack_user=locked_user,
        token_digest=digest_link_token(raw_token),
        expires_at=now
        + timedelta(seconds=settings.ROO_FOUNDER_LINK_TTL_SECONDS),
    )
    logger.info(
        "slack_founder_link_started slack_user_pk=%s request_pk=%s",
        locked_user.pk,
        request.pk,
    )
    return request, raw_token


def preview_slack_founder_link(
    token: str,
    *,
    founder_user: User,
) -> SlackFounderLinkPreview:
    request = _link_request_for_token(token, for_update=False)
    status, link = _link_status(
        slack_user=request.slack_user,
        founder_user=founder_user,
        for_update=False,
    )
    return SlackFounderLinkPreview(request=request, status=status, link=link)


@transaction.atomic
def complete_slack_founder_link(
    token: str,
    *,
    founder_user: User,
) -> tuple[SlackFounderAccountLink, bool]:
    request = _link_request_for_token(token, for_update=True)

    # Lock both identities in a stable order before evaluating uniqueness.
    user_ids = sorted({request.slack_user_id, founder_user.pk})
    locked_users = {
        user.pk: user
        for user in User.objects.select_for_update().filter(pk__in=user_ids)
    }
    slack_user = locked_users[request.slack_user_id]
    locked_founder_user = locked_users[founder_user.pk]

    status, link = _link_status(
        slack_user=slack_user,
        founder_user=locked_founder_user,
        for_update=True,
    )
    created = status != "already_linked"
    if created:
        link = SlackFounderAccountLink.objects.create(
            slack_user=slack_user,
            founder_user=locked_founder_user,
        )

    request.consumed_at = timezone.now()
    request.save(update_fields=["consumed_at", "updated_at"])
    logger.info(
        "slack_founder_link_completed slack_user_pk=%s founder_user_pk=%s created=%s",
        slack_user.pk,
        locked_founder_user.pk,
        created,
    )
    return link, created


def founder_tools_account_linked(user: User) -> bool:
    return SlackFounderAccountLink.objects.filter(slack_user=user).exists()


def founder_account_connection_status(founder_user: User) -> dict:
    """Return the authenticated founder's Roo connection without exposing IDs."""
    link = (
        SlackFounderAccountLink.objects.select_related("slack_user")
        .filter(founder_user=founder_user)
        .first()
    )
    if link is not None:
        return {
            "status": "connected",
            "connection_type": "explicit",
            "slack_display_name": (
                link.slack_user.full_name or "Your Roo Slack account"
            ),
            "verified_at": link.verified_at.isoformat(),
        }

    # Same-account users were already verified when their Slack identity was
    # attached to this user, so they do not need an additional explicit link.
    if founder_user.slack_id:
        return {
            "status": "connected",
            "connection_type": "direct",
            "slack_display_name": (
                founder_user.full_name or "Your Roo Slack account"
            ),
            "verified_at": None,
        }

    return {
        "status": "not_connected",
        "connection_type": None,
        "slack_display_name": None,
        "verified_at": None,
    }


def coworking_eligibility_user(user: User) -> User:
    link = (
        SlackFounderAccountLink.objects.select_related("founder_user")
        .filter(slack_user=user)
        .first()
    )
    return link.founder_user if link else user
