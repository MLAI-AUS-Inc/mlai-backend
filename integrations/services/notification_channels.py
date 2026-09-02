"""User-facing lifecycle for research notification channels.

Owns connect/verify/disable flows for the three channel types:
- email: activated from the authenticated user's account email
- whatsapp: 6-digit OTP sent via an approved Twilio authentication Content template
- slack: auto-linked from the signed-in user (login already proves the email)

Depends one-directionally on notification_adapters for transport.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone

from core.actor_ids import is_internal_actor_id
from content_factory.models import (
    NotificationChannel,
    NotificationChannelType,
    NotificationConsentState,
    ResearchAutomation,
    ResearchAutomationStatus,
)
from integrations.services.notification_adapters import (
    _send_email,
    pause_automations_if_no_active_channels,
    send_whatsapp_template,
)
from integrations.services.slack import SlackService
from core.slack_founder_links import (
    ConflictingSlackFounderLinkError,
    assign_direct_slack_identity,
)


logger = logging.getLogger(__name__)

OTP_LENGTH = 6
OTP_TTL_SECONDS = 10 * 60
OTP_MAX_ATTEMPTS = 5
OTP_RESEND_COOLDOWN_SECONDS = 60
OTP_MAX_SENDS_PER_WINDOW = 8  # rolling 24h, shared by email + whatsapp sends
EMAIL_VERIFY_SALT = "content-factory-notification-channel-verify"
DEFAULT_EMAIL_VERIFY_MAX_AGE_SECONDS = 3 * 24 * 60 * 60

VERIFICATION_FIELDS = [
    "verification_code_hash",
    "verification_expires_at",
    "verification_attempts",
    "verification_last_sent_at",
    "verification_send_count",
]


class ChannelActionError(Exception):
    """Raised by channel lifecycle actions; maps onto an HTTP error response."""

    def __init__(
        self,
        code: str,
        detail: str,
        http_status: int = 400,
        extra: Optional[dict[str, Any]] = None,
    ):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.http_status = http_status
        self.extra = extra or {}


def _hash_verification_code(channel: NotificationChannel, code: str) -> str:
    message = f"{channel.id}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _generate_otp() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(OTP_LENGTH))


def serialize_channel(
    channel: NotificationChannel,
    *,
    primary_channel_id=None,
    now=None,
) -> dict[str, Any]:
    now = now or timezone.now()
    pending = None
    if (
        channel.consent_state == NotificationConsentState.PENDING
        and channel.verification_last_sent_at
    ):
        resend_available_at = channel.verification_last_sent_at + timedelta(
            seconds=OTP_RESEND_COOLDOWN_SECONDS
        )
        pending = {
            "expiresAt": (
                channel.verification_expires_at.isoformat()
                if channel.verification_expires_at
                else None
            ),
            "resendAvailableAt": resend_available_at.isoformat(),
            "attemptsRemaining": (
                max(0, OTP_MAX_ATTEMPTS - int(channel.verification_attempts or 0))
                if channel.verification_code_hash
                else None
            ),
        }
    return {
        "id": str(channel.id),
        "channelType": channel.channel_type,
        "routeId": channel.route_id,
        "displayName": channel.display_name,
        "consentState": channel.consent_state,
        "deliveryEnabled": bool(channel.delivery_enabled),
        "verifiedAt": channel.verified_at.isoformat() if channel.verified_at else None,
        "isPrimary": primary_channel_id is not None
        and channel.id == primary_channel_id,
        "pendingVerification": pending,
    }


def serialize_automation(
    automation: Optional[ResearchAutomation],
) -> Optional[dict[str, Any]]:
    if automation is None:
        return None
    return {
        "id": str(automation.id),
        "status": automation.status,
        "timezone": automation.timezone,
        "frequencyPerDay": automation.frequency_per_day,
        "localSendTimes": list(automation.local_send_times or []),
        "enabled": automation.status == ResearchAutomationStatus.ACTIVE,
    }


def list_org_channels(organization) -> list[NotificationChannel]:
    return list(
        NotificationChannel.objects.filter(organization=organization).order_by(
            "channel_type", "created_at"
        )
    )


def _active_org_channels(organization) -> list[NotificationChannel]:
    return [
        channel
        for channel in list_org_channels(organization)
        if channel.consent_state == NotificationConsentState.ACTIVE
    ]


def _activate_channel(channel: NotificationChannel) -> NotificationChannel:
    channel.consent_state = NotificationConsentState.ACTIVE
    # A freshly (re)connected channel opts back into delivery. This is the only
    # transition into ACTIVE, so it also resets a channel that was unchecked and
    # then removed/re-added — it comes back selected rather than silently muted.
    channel.delivery_enabled = True
    channel.verified_at = timezone.now()
    channel.opted_out_at = None
    channel.verification_code_hash = ""
    channel.verification_expires_at = None
    channel.verification_attempts = 0
    channel.save(
        update_fields=[
            "consent_state",
            "delivery_enabled",
            "verified_at",
            "opted_out_at",
            "verification_code_hash",
            "verification_expires_at",
            "verification_attempts",
            "updated_at",
        ]
    )
    return channel


def _prepare_pending_channel(
    *,
    organization,
    user,
    channel_type: str,
    route_id: str,
    display_name: str = "",
) -> NotificationChannel:
    """Create or reset a channel for verification without downgrading ACTIVE ones."""
    channel = NotificationChannel.objects.filter(
        organization=organization,
        channel_type=channel_type,
        route_id=route_id,
    ).first()
    if channel and channel.consent_state == NotificationConsentState.ACTIVE:
        if user is not None and channel.user_id is None:
            channel.user = user
            channel.save(update_fields=["user", "updated_at"])
        return channel
    if channel is None:
        channel = NotificationChannel(
            organization=organization,
            channel_type=channel_type,
            route_id=route_id,
        )
    channel.user = user
    if display_name:
        channel.display_name = display_name
    channel.consent_state = NotificationConsentState.PENDING
    channel.opted_out_at = None
    channel.save()
    return channel


def _enforce_send_limits(channel: NotificationChannel, now) -> None:
    last_sent = channel.verification_last_sent_at
    if last_sent:
        elapsed = (now - last_sent).total_seconds()
        if elapsed < OTP_RESEND_COOLDOWN_SECONDS:
            raise ChannelActionError(
                "resend_cooldown",
                "Please wait a minute before requesting another code.",
                http_status=429,
                extra={
                    "retry_after_seconds": int(OTP_RESEND_COOLDOWN_SECONDS - elapsed)
                    or 1
                },
            )
        if elapsed > 24 * 60 * 60:
            channel.verification_send_count = 0
    if int(channel.verification_send_count or 0) >= OTP_MAX_SENDS_PER_WINDOW:
        raise ChannelActionError(
            "send_limit_reached",
            "Too many verification messages were sent in the last day. Try again later.",
            http_status=429,
        )


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def initiate_email_channel(
    *, organization, user, route_id: str = ""
) -> NotificationChannel:
    account_email = str(getattr(user, "email", "") or "").strip().lower()
    requested_email = str(route_id or "").strip().lower() or account_email
    if not account_email or "@" not in account_email:
        raise ChannelActionError(
            "invalid_email", "Your account needs a valid email address."
        )
    if requested_email != account_email:
        raise ChannelActionError(
            "email_must_match_account",
            "Daily reminder email must match your signed-in account email.",
        )
    channel = _prepare_pending_channel(
        organization=organization,
        user=user,
        channel_type=NotificationChannelType.EMAIL,
        route_id=account_email,
        display_name=account_email,
    )
    if channel.consent_state != NotificationConsentState.ACTIVE:
        _activate_channel(channel)
    return channel


def build_email_verification_url(channel: NotificationChannel) -> str:
    base_url = str(getattr(settings, "DEFAULT_BACKEND_URL", "") or "").rstrip("/")
    token = signing.dumps(
        {"channel_id": str(channel.id), "route_id": channel.route_id},
        salt=EMAIL_VERIFY_SALT,
    )
    path = reverse("content_factory_notification_channel_verify")
    return f"{base_url}{path}?{urlencode({'token': token})}"


def send_email_verification(channel: NotificationChannel) -> None:
    now = timezone.now()
    _enforce_send_limits(channel, now)
    channel.verification_last_sent_at = now
    channel.verification_send_count = int(channel.verification_send_count or 0) + 1
    channel.save(
        update_fields=[
            "verification_last_sent_at",
            "verification_send_count",
            "updated_at",
        ]
    )

    verify_url = build_email_verification_url(channel)
    text = (
        "Confirm this email address to receive your daily article topic suggestions.\n\n"
        f"Confirm: {verify_url}\n\n"
        "If you didn't request this, you can ignore this email."
    )
    html_body = (
        "<p>Confirm this email address to receive your daily article topic suggestions.</p>"
        f'<p><a href="{verify_url}">Confirm this email</a></p>'
        "<p>If you didn't request this, you can ignore this email.</p>"
    )
    success, _message_id, response_payload = _send_email(
        channel,
        subject="Confirm your notification email",
        text=text,
        html_body=html_body,
        idempotency_key=f"channel-verify:{channel.id}:{channel.verification_send_count}",
    )
    if not success:
        error = (
            response_payload.get("error")
            or response_payload.get("message")
            or response_payload
        )
        if str(error) == "email_not_configured" or "is not configured" in str(error):
            raise ChannelActionError(
                "email_not_configured",
                "Email sending is not configured.",
                http_status=503,
            )
        raise ChannelActionError(
            "send_failed",
            "Could not send the verification email. Try again shortly.",
            http_status=502,
            extra={"provider_error": str(error)},
        )


def handle_email_verification_token(token: str) -> NotificationChannel:
    max_age = int(
        getattr(
            settings,
            "NOTIFICATION_CHANNEL_VERIFY_MAX_AGE_SECONDS",
            DEFAULT_EMAIL_VERIFY_MAX_AGE_SECONDS,
        )
    )
    payload = signing.loads(token, salt=EMAIL_VERIFY_SALT, max_age=max_age)
    channel = NotificationChannel.objects.filter(
        id=payload.get("channel_id"),
        channel_type=NotificationChannelType.EMAIL,
    ).first()
    if not channel or channel.route_id != payload.get("route_id"):
        raise ChannelActionError(
            "invalid_token", "This verification link is no longer valid."
        )
    if channel.consent_state != NotificationConsentState.ACTIVE:
        _activate_channel(channel)
    return channel


# ---------------------------------------------------------------------------
# WhatsApp
# ---------------------------------------------------------------------------


def normalize_e164(value: str) -> str:
    text = re.sub(r"[\s\-().]", "", str(value or ""))
    if text.startswith("00"):
        text = "+" + text[2:]
    if not text.startswith("+"):
        raise ChannelActionError(
            "invalid_phone",
            "Enter the number in international format, e.g. +61400000000.",
        )
    digits = text[1:]
    if not digits.isdigit() or not (8 <= len(digits) <= 15):
        raise ChannelActionError(
            "invalid_phone",
            "Enter the number in international format, e.g. +61400000000.",
        )
    return "+" + digits


def initiate_whatsapp_channel(*, organization, user, phone: str) -> NotificationChannel:
    route_id = normalize_e164(phone)
    return _prepare_pending_channel(
        organization=organization,
        user=user,
        channel_type=NotificationChannelType.WHATSAPP,
        route_id=route_id,
        display_name=route_id,
    )


def send_whatsapp_otp(channel: NotificationChannel) -> dict[str, Any]:
    content_sid = str(
        getattr(settings, "TWILIO_WHATSAPP_OTP_CONTENT_SID", "") or ""
    ).strip()
    if not content_sid:
        # An approved WhatsApp authentication template is mandatory: free-form
        # text never delivers without an open 24h service window.
        raise ChannelActionError(
            "whatsapp_not_configured",
            "WhatsApp verification is not configured yet.",
            http_status=503,
        )
    now = timezone.now()
    _enforce_send_limits(channel, now)
    code = _generate_otp()
    channel.verification_code_hash = _hash_verification_code(channel, code)
    channel.verification_expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
    channel.verification_attempts = 0
    channel.verification_last_sent_at = now
    channel.verification_send_count = int(channel.verification_send_count or 0) + 1
    channel.save(update_fields=VERIFICATION_FIELDS + ["updated_at"])

    # Twilio authentication Content template contract: one variable (the code);
    # the copy-code button reuses the same variable server-side.
    success, _provider_id, response_payload = send_whatsapp_template(
        channel.route_id,
        content_sid=content_sid,
        content_variables={"1": code},
    )
    if not success:
        channel.verification_code_hash = ""
        channel.verification_expires_at = None
        channel.save(
            update_fields=[
                "verification_code_hash",
                "verification_expires_at",
                "updated_at",
            ]
        )
        raise ChannelActionError(
            "send_failed",
            "Could not send the WhatsApp verification code. Check the number and try again.",
            http_status=502,
            extra={
                "provider_error": str(response_payload.get("error") or response_payload)
            },
        )
    return {"expires_at": channel.verification_expires_at}


def verify_whatsapp_otp(channel: NotificationChannel, code: str) -> NotificationChannel:
    now = timezone.now()
    candidate = str(code or "").strip()
    if not channel.verification_code_hash:
        raise ChannelActionError(
            "no_pending_code", "No verification code is pending. Request a new one."
        )
    if channel.verification_expires_at and channel.verification_expires_at < now:
        raise ChannelActionError("expired", "That code has expired. Request a new one.")
    if int(channel.verification_attempts or 0) >= OTP_MAX_ATTEMPTS:
        raise ChannelActionError(
            "too_many_attempts", "Too many incorrect attempts. Request a new code."
        )
    if not hmac.compare_digest(
        _hash_verification_code(channel, candidate), channel.verification_code_hash
    ):
        channel.verification_attempts = int(channel.verification_attempts or 0) + 1
        if channel.verification_attempts >= OTP_MAX_ATTEMPTS:
            channel.verification_code_hash = ""
            channel.verification_expires_at = None
            channel.save(
                update_fields=[
                    "verification_attempts",
                    "verification_code_hash",
                    "verification_expires_at",
                    "updated_at",
                ]
            )
            raise ChannelActionError(
                "too_many_attempts", "Too many incorrect attempts. Request a new code."
            )
        channel.save(update_fields=["verification_attempts", "updated_at"])
        raise ChannelActionError("invalid_code", "That code is incorrect.")
    return _activate_channel(channel)


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def link_slack_channel(*, organization, user, config=None) -> NotificationChannel:
    user_slack_id = str(getattr(user, "slack_id", "") or "").strip()
    slack_id = "" if is_internal_actor_id(user_slack_id) else user_slack_id
    slack_id_source = "user" if slack_id else ""
    profile = None
    if not slack_id and config is not None:
        configured_slack_id = str(
            getattr(config, "connected_slack_user_id", "") or ""
        ).strip()
        if not is_internal_actor_id(configured_slack_id):
            slack_id = configured_slack_id
            slack_id_source = "config"
    if not slack_id and user is not None and getattr(user, "email", ""):
        profile = SlackService.lookup_user_by_email(user.email)
        slack_id = str((profile or {}).get("slack_id") or "").strip()
        if slack_id:
            slack_id_source = "email_lookup"
    if not slack_id:
        raise ChannelActionError(
            "slack_user_not_found",
            "We couldn't find your Slack account in the workspace. Join the workspace with this email, then try again.",
        )

    identity_assignment_succeeded = slack_id_source == "user"
    if user is not None and (
        not getattr(user, "slack_id", None)
        or is_internal_actor_id(getattr(user, "slack_id", None))
    ):
        try:
            user = assign_direct_slack_identity(
                user,
                slack_id,
                allow_reassignment=is_internal_actor_id(
                    getattr(user, "slack_id", None)
                ),
            )
            identity_assignment_succeeded = True
        except (ConflictingSlackFounderLinkError, IntegrityError):
            # Explicit account links and existing identity ownership must stay
            # unchanged. A pre-existing notification route may remain usable,
            # but an email-discovered identity must fail closed rather than
            # retargeting migration-preserved Content Factory ownership.
            if slack_id_source == "email_lookup":
                raise ChannelActionError(
                    "slack_identity_conflict",
                    "That Slack account is already connected elsewhere. Contact MLAI support before changing this connection.",
                    http_status=409,
                )
    if (
        config is not None
        and not getattr(config, "connected_slack_user_id", None)
        and identity_assignment_succeeded
    ):
        config.connected_slack_user_id = slack_id
        config.save(update_fields=["connected_slack_user_id", "updated_at"])

    display_name = str(
        (profile or {}).get("real_name")
        or (profile or {}).get("display_name")
        or getattr(user, "full_name", "")
        or ""
    )
    channel = _prepare_pending_channel(
        organization=organization,
        user=user,
        channel_type=NotificationChannelType.SLACK,
        route_id=slack_id,
        display_name=display_name,
    )
    was_active = channel.consent_state == NotificationConsentState.ACTIVE
    sent, _message_ts = SlackService.send_dm(
        slack_id,
        "You're set up to receive daily article topic suggestions here. "
        "You can manage notification channels from your marketing dashboard.",
    )
    if not sent:
        if not was_active:
            channel.refresh_from_db()
        raise ChannelActionError(
            "send_failed",
            "We found your Slack account but couldn't send a verification message. Try again shortly.",
            http_status=502,
        )
    if not was_active:
        _activate_channel(channel)
    return channel


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def deactivate_channel(
    channel: NotificationChannel,
    *,
    state: str = NotificationConsentState.REVOKED,
) -> NotificationChannel:
    """Revoke a channel. Channels are never hard-deleted: deliveries and
    automations reference them with PROTECT."""
    channel.consent_state = state
    channel.opted_out_at = timezone.now()
    channel.verification_code_hash = ""
    channel.verification_expires_at = None
    channel.verification_attempts = 0
    channel.save(
        update_fields=[
            "consent_state",
            "opted_out_at",
            "verification_code_hash",
            "verification_expires_at",
            "verification_attempts",
            "updated_at",
        ]
    )
    return channel


def ensure_research_automation_for_org(
    *,
    organization,
    user=None,
    timezone_name: str = "",
    enabled: bool = True,
    config=None,
) -> tuple[Optional[ResearchAutomation], list[NotificationChannel]]:
    """Ensure the org has one ResearchAutomation backed by its active channels.

    If no channel is active yet, attempts a Slack auto-link from the signed-in
    user. Returns (None, []) when no channel can be established — callers keep
    the legacy daily-discovery path as fallback in that case.
    """
    from integrations.services.research_automations import _coerce_timezone

    channels = _active_org_channels(organization)
    if not channels:
        try:
            channels = [
                link_slack_channel(organization=organization, user=user, config=config)
            ]
        except ChannelActionError:
            return None, []

    automation = (
        ResearchAutomation.objects.filter(organization=organization)
        .order_by("created_at")
        .first()
    )
    status = (
        ResearchAutomationStatus.ACTIVE if enabled else ResearchAutomationStatus.PAUSED
    )
    if automation is None:
        primary = next(
            (
                channel
                for channel in channels
                if channel.channel_type == NotificationChannelType.SLACK
            ),
            channels[0],
        )
        automation = ResearchAutomation.objects.create(
            organization=organization,
            user=user,
            notification_channel=primary,
            timezone=(
                _coerce_timezone(timezone_name)
                if timezone_name
                else "Australia/Melbourne"
            ),
            frequency_per_day=1,
            local_send_times=["08:00"],
            status=status,
        )
        return automation, channels

    update_fields = []
    if automation.status != status:
        automation.status = status
        update_fields.append("status")
    if timezone_name:
        coerced = _coerce_timezone(timezone_name)
        if automation.timezone != coerced:
            automation.timezone = coerced
            update_fields.append("timezone")
    if user is not None and automation.user_id is None:
        automation.user = user
        update_fields.append("user")
    if automation.notification_channel.consent_state != NotificationConsentState.ACTIVE:
        automation.notification_channel = channels[0]
        update_fields.append("notification_channel")
    if update_fields:
        automation.save(update_fields=update_fields + ["updated_at"])
    return automation, channels
