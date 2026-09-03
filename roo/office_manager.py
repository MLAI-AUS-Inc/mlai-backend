"""Office Manager of the Day scheduling, claiming, and Slack delivery."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from core.models import User
from integrations.services.slack import SlackService

from .models import (
    CoworkingBooking,
    OfficeManagerAssignment,
    OfficeManagerClaimAttempt,
    OfficeManagerDay,
)
from .permissions import InsufficientBalanceError
from .services import CoworkingService, PointsService

logger = logging.getLogger(__name__)

OFFICE_MANAGER_ACTION_ID = "office_manager_volunteer_today"
MAX_OFFICE_MANAGER_GENERATION = (2**31) - 1
NO_FOOD_REMINDER = "Reminder: no food is permitted in the coworking space."
COWORKING_SELF_BOOK_REMINDER = (
    "Using the coworking space today? Please book yourself by asking "
    "`@Roo book me in today`."
)
OFFICE_MANAGER_BOOKING_RESPONSIBILITY = (
    "Please remind anyone using the coworking space to book themselves "
    "through Roo for today."
)


def _coworking_self_book_reminder(day: date) -> str:
    return (
        f"Using the coworking space on {day.isoformat()}? Please book yourself "
        "by asking `@Roo book me in`."
    )


def _office_manager_booking_responsibility(day: date) -> str:
    return (
        "Make sure everyone using the coworking space on "
        f"{day.isoformat()} books through Roo."
    )


class OfficeManagerConfigurationError(RuntimeError):
    pass


class OfficeManagerDeliveryCoordinateUnknown(RuntimeError):
    """Slack may have accepted a post but omitted its recovery coordinate."""


class OfficeManagerClaimError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        assignee_slack_user_id: str = "",
        attempt_id: uuid.UUID | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.assignee_slack_user_id = assignee_slack_user_id
        self.attempt_id = attempt_id


class _OfficeManagerAttemptLostRace(RuntimeError):
    """Force the current mutation transaction to roll back and replay."""

    def __init__(self, attempt_id: uuid.UUID):
        super().__init__(str(attempt_id))
        self.attempt_id = attempt_id


@dataclass(frozen=True)
class OfficeManagerClaimResult:
    assignment: OfficeManagerAssignment
    booking: CoworkingBooking
    status: str
    existing_booking_converted: bool
    attempt_id: uuid.UUID | None = None
    replayed: bool = False


TERMINAL_CLAIM_OUTCOMES = {
    "member_not_eligible",
    "office_manager_day_not_found",
    "already_claimed",
    "claim_closed",
    "refund_unavailable",
    "announcement_superseded",
}


def _office_manager_enabled() -> bool:
    return bool(getattr(settings, "OFFICE_MANAGER_ENABLED", False))


def _office_manager_slack_token() -> str:
    token = str(
        getattr(settings, "OFFICE_MANAGER_SLACK_BOT_TOKEN", "") or ""
    ).strip()
    if not token:
        raise OfficeManagerConfigurationError(
            "The Public Roo Slack bot token is not configured"
        )
    return token


def _office_manager_slack_client():
    return SlackService.get_client(bot_token=_office_manager_slack_token())


def _timezone() -> ZoneInfo:
    return ZoneInfo(
        str(getattr(settings, "OFFICE_MANAGER_TIMEZONE", "Australia/Melbourne"))
    )


def _local_now(now: datetime | None = None) -> datetime:
    current = now or timezone.now()
    if timezone.is_naive(current):
        current = timezone.make_aware(current, _timezone())
    return current.astimezone(_timezone())


def _setting_time(hour_name: str, minute_name: str, default_hour: int, default_minute: int) -> time:
    return time(
        hour=int(getattr(settings, hour_name, default_hour)),
        minute=int(getattr(settings, minute_name, default_minute)),
    )


def _claim_cutoff(day: date) -> datetime:
    cutoff_time = _setting_time(
        "OFFICE_MANAGER_CLAIM_CUTOFF_HOUR",
        "OFFICE_MANAGER_CLAIM_CUTOFF_MINUTE",
        10,
        0,
    )
    return datetime.combine(day, cutoff_time, tzinfo=_timezone())


def _configured_weekdays() -> set[int]:
    raw = getattr(settings, "OFFICE_MANAGER_WEEKDAYS", "0,1,2,3,4")
    if isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = str(raw).split(",")
    weekdays = set()
    invalid_values = []
    for value in values:
        cleaned = str(value).strip()
        if not cleaned:
            continue
        try:
            weekday = int(cleaned)
        except (TypeError, ValueError):
            invalid_values.append(cleaned)
            continue
        if 0 <= weekday <= 6:
            weekdays.add(weekday)
        else:
            invalid_values.append(cleaned)
    if invalid_values:
        raise ValueError(
            "OFFICE_MANAGER_WEEKDAYS must contain only integers from 0 to 6"
        )
    return weekdays


def _format_local_time(value: datetime) -> str:
    local_value = value.astimezone(_timezone())
    hour = local_value.hour % 12 or 12
    suffix = "AM" if local_value.hour < 12 else "PM"
    return f"{hour}:{local_value.minute:02d} {suffix}"


def _slack_client_msg_id(kind: str, object_id: object) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"https://mlai.au/roo/office-manager/{kind}/{object_id}",
        )
    )


def _announcement_text(day: OfficeManagerDay) -> str:
    day_label = day.date.isoformat()
    if day.status == "claimed":
        assignment = day.assignments.filter(status="active").select_related("user").first()
        mention = (
            f"<@{assignment.user.slack_id}>"
            if assignment and assignment.user.slack_id
            else "A member"
        )
        return (
            f"Office Manager for {day_label}: {mention}. "
            "Roo booked them in without deducting Roo points. "
            f"{_coworking_self_book_reminder(day.date)}"
        )
    if day.status == "closed":
        return (
            f"The Office Manager volunteer window for {day_label} is closed. "
            f"{_coworking_self_book_reminder(day.date)}"
        )
    return (
        f"Volunteer to be Office Manager for {day_label}. "
        "Roo will book the selected member in without deducting Roo points. "
        f"{_coworking_self_book_reminder(day.date)}"
    )


def _announcement_blocks(day: OfficeManagerDay) -> list[dict]:
    heading = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": f"*Office Manager — {day.date.isoformat()}*",
        },
    }
    if day.status == "claimed":
        assignment = day.assignments.filter(status="active").select_related("user").first()
        mention = (
            f"<@{assignment.user.slack_id}>"
            if assignment and assignment.user.slack_id
            else "A member"
        )
        return [
            heading,
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{mention} is Office Manager for {day.date.isoformat()}.\n"
                        "They have been booked in without deducting Roo points."
                        f"\n\n{_coworking_self_book_reminder(day.date)}"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": NO_FOOD_REMINDER}],
            },
        ]
    if day.status == "closed":
        return [
            heading,
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"The volunteer window for {day.date.isoformat()} is closed."
                        f"\n\n{_coworking_self_book_reminder(day.date)}"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": NO_FOOD_REMINDER}],
            },
        ]
    return [
        heading,
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "Volunteer to help welcome members, help people get settled, "
                    "and reset the space before leaving.\n\n"
                    f"Roo will book the selected member in for {day.date.isoformat()} without "
                    "deducting Roo points. No channel or thread reply is needed."
                    f"\n\n{_coworking_self_book_reminder(day.date)}"
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Volunteer before {_format_local_time(day.claim_cutoff_at)}. "
                        f"{NO_FOOD_REMINDER}"
                    ),
                }
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": OFFICE_MANAGER_ACTION_ID,
                    "text": {
                        "type": "plain_text",
                        "text": f"Volunteer for {day.date.isoformat()}",
                    },
                    "style": "primary",
                    "value": json.dumps(
                        {
                            "date": day.date.isoformat(),
                            "generation": day.generation,
                        },
                        separators=(",", ":"),
                    ),
                }
            ],
        },
    ]


def _winner_dm_text(assignment: OfficeManagerAssignment) -> str:
    day_label = assignment.day.date.isoformat()
    refund_line = ""
    if assignment.points_refunded:
        refund_line = (
            f"\nRoo returned the {assignment.points_refunded} Roo points "
            f"previously charged for the {day_label} booking."
        )
    return (
        f"*You are the Office Manager for {day_label}.*\n\n"
        "Roo booked you in without deducting Roo points."
        f"{refund_line}\n\n"
        f"Responsibilities for {day_label}:\n"
        "- Welcome new members and visitors.\n"
        "- Help people get settled and onboarded.\n"
        f"- {_office_manager_booking_responsibility(assignment.day.date)}\n"
        "- Reset and tidy the space before leaving.\n\n"
        f"{NO_FOOD_REMINDER}"
    )


def _winner_channel_announcement_text(
    assignment: OfficeManagerAssignment,
) -> str:
    mention = (
        f"<@{assignment.user.slack_id}>"
        if assignment.user.slack_id
        else "A member"
    )
    return (
        f"{mention} is *Office Manager for {assignment.day.date.isoformat()}*.\n"
        "Roo booked them in without deducting Roo points. Please say hello "
        "and reach out if you need help getting settled.\n\n"
        f"{_coworking_self_book_reminder(assignment.day.date)}\n\n"
        f"{NO_FOOD_REMINDER}"
    )


def _winner_channel_client_msg_id(
    assignment: OfficeManagerAssignment,
) -> str:
    winning_attempt_id = (
        assignment.claim_attempts.order_by("created_at", "attempt_id")
        .values_list("attempt_id", flat=True)
        .first()
    )
    return _slack_client_msg_id(
        "winner",
        winning_attempt_id or assignment.id,
    )


def _relinquished_winner_channel_text(
    assignment: OfficeManagerAssignment,
) -> str:
    mention = (
        f"<@{assignment.user.slack_id}>"
        if assignment.user.slack_id
        else "The previously selected member"
    )
    return (
        f"{mention} is no longer *Office Manager for "
        f"{assignment.day.date.isoformat()}*. "
        "Check Roo's daily Office Manager announcement for the current "
        "assignment or volunteer availability.\n\n"
        f"{_coworking_self_book_reminder(assignment.day.date)}\n\n"
        f"{NO_FOOD_REMINDER}"
    )


def _end_of_day_dm_text(assignment: OfficeManagerAssignment) -> str:
    return (
        "Office Manager reminder for "
        f"{assignment.day.date.isoformat()}: before you leave, please reset and tidy the "
        f"coworking space.\n\n{NO_FOOD_REMINDER}"
    )


def _private_correction_text(assignment: OfficeManagerAssignment) -> str:
    state = (
        "has been cancelled"
        if assignment.status == "relinquished"
        else "is no longer current"
    )
    return (
        "Your Office Manager assignment for "
        f"{assignment.day.date.isoformat()} {state}. Please ignore "
        "any earlier winner or end-of-day message for that date."
    )


def _expired_winner_channel_text(
    assignment: OfficeManagerAssignment,
) -> str:
    return (
        "The Office Manager assignment notice for "
        f"{assignment.day.date.isoformat()} is no longer current. "
        "Check Roo's latest daily Office Manager announcement for current "
        "volunteer availability."
    )


def _safe_slack_error(exc: Exception) -> str:
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error") or "slack_api_error")
    return exc.__class__.__name__


DELIVERY_LEASE_PREFIX = "office-manager-delivery-lease:"
RETRACTION_LEASE_PREFIX = "office-manager-retraction-lease:"
MESSAGE_UPDATE_LEASE_PREFIX = "office-manager-message-update-lease:"
EXPIRED_DELIVERY_ERROR = "expired_after_local_date_rollover"
CLOSED_DELIVERY_ERROR = "expired_after_volunteer_window_closed"
RELINQUISHED_DELIVERY_ERROR = "assignment_relinquished_before_delivery"
RETRYABLE_DELIVERY_STATUSES = ("pending", "sending", "failed", "unknown")
TERMINAL_ASSIGNMENT_DELIVERY_ERRORS = (
    EXPIRED_DELIVERY_ERROR,
    RELINQUISHED_DELIVERY_ERROR,
)
PERMANENT_SLACK_ERRORS = {
    "account_inactive",
    "channel_not_found",
    "invalid_auth",
    "missing_scope",
    "not_allowed_token_type",
    "not_in_channel",
    "token_revoked",
    "user_not_found",
    "users_not_found",
}
TRANSIENT_SLACK_ERRORS = {
    "fatal_error",
    "internal_error",
    "ratelimited",
    "rate_limited",
    "request_timeout",
    "service_unavailable",
}
TERMINAL_DELIVERY_ERROR_PREFIXES = ("permanent:", "exhausted:")


def _retryable_delivery_q(*, status_field: str, error_field: str) -> Q:
    """Select retry states without letting terminal rows starve the sweep."""
    query = Q(**{f"{status_field}__in": RETRYABLE_DELIVERY_STATUSES})
    query &= ~Q(**{f"{error_field}__in": TERMINAL_ASSIGNMENT_DELIVERY_ERRORS})
    for prefix in TERMINAL_DELIVERY_ERROR_PREFIXES:
        query &= ~Q(**{f"{error_field}__startswith": prefix})
    return query


def _delivery_lease_token() -> str:
    """Encode an owner and timestamp in an existing per-delivery field."""
    return (
        f"{DELIVERY_LEASE_PREFIX}{uuid.uuid4().hex}:"
        f"{timezone.now().timestamp():.6f}"
    )


def _retraction_lease_token() -> str:
    return f"{RETRACTION_LEASE_PREFIX}{uuid.uuid4().hex}"


def _message_update_retry_count(value: str) -> int:
    raw = str(value or "")
    try:
        if raw.startswith(MESSAGE_UPDATE_LEASE_PREFIX):
            return max(0, int(raw.rsplit(":", 2)[-2]))
        if raw.startswith("message_update:retry:"):
            return max(0, int(raw.split(":", 3)[2]))
    except (TypeError, ValueError, IndexError):
        return 0
    return 0


def _message_update_lease_token(attempt_count: int) -> str:
    return (
        f"{MESSAGE_UPDATE_LEASE_PREFIX}{uuid.uuid4().hex}:"
        f"{max(0, int(attempt_count))}:"
        f"{timezone.now().timestamp():.6f}"
    )


def _message_update_lease_is_live(value: str) -> bool:
    if not str(value or "").startswith(MESSAGE_UPDATE_LEASE_PREFIX):
        return False
    try:
        acquired_at = datetime.fromtimestamp(
            float(str(value).rsplit(":", 1)[1]),
            tz=timezone.get_current_timezone(),
        )
    except (TypeError, ValueError, OverflowError):
        return False
    return timezone.now() - acquired_at < timedelta(
        seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
    )


def _message_update_attempt_is_due(day: OfficeManagerDay) -> bool:
    if _message_update_lease_is_live(day.announcement_last_error):
        return False
    return (
        day.announcement_next_attempt_at is None
        or day.announcement_next_attempt_at <= timezone.now()
    )


def _slack_failure_is_transient(exc: Exception) -> bool:
    if isinstance(exc, OfficeManagerConfigurationError):
        return False
    if not isinstance(exc, SlackApiError):
        return True
    error = _safe_slack_error(exc)
    response = exc.response
    status_code = int(getattr(response, "status_code", 0) or 0)
    if error in PERMANENT_SLACK_ERRORS:
        return False
    # Slack can accept a deterministic client_msg_id and then answer a retry
    # with duplicate_message before the accepted post is visible in history.
    # Public posts need another bounded recovery pass to discover their ts.
    if error == "duplicate_message":
        return True
    if error in TRANSIENT_SLACK_ERRORS:
        return True
    return status_code in {408, 429} or status_code >= 500


def _find_message_ts_by_client_msg_id(
    slack_client,
    *,
    channel_id: str,
    client_msg_id: str,
    oldest: datetime,
) -> str:
    """Resolve a response-loss post without creating another public message."""
    cursor = ""
    for _ in range(5):
        kwargs = {
            "channel": channel_id,
            "oldest": f"{max(0.0, oldest.timestamp() - 300):.6f}",
            "inclusive": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        response = slack_client.conversations_history(**kwargs)
        if response.get("ok", True) is False:
            raise SlackApiError("conversations.history failed", response)
        raw_messages = response.get("messages")
        messages = raw_messages if isinstance(raw_messages, (list, tuple)) else []
        for message in messages:
            if str(message.get("client_msg_id") or "") == client_msg_id:
                return str(message.get("ts") or "")
        metadata = response.get("response_metadata")
        cursor = (
            str(metadata.get("next_cursor") or "").strip()
            if isinstance(metadata, dict)
            else ""
        )
        if not cursor:
            break
    return ""


def _delivery_lease_is_live(
    *,
    status: str,
    error_value: str,
    legacy_updated_at: datetime,
) -> bool:
    """Recognise both fenced leases and legacy pre-upgrade sending rows."""
    if status != "sending":
        return False
    acquired_at = legacy_updated_at
    cleaned_error = str(error_value or "")
    if cleaned_error.startswith(DELIVERY_LEASE_PREFIX):
        try:
            acquired_at = datetime.fromtimestamp(
                float(cleaned_error.rsplit(":", 1)[1]),
                tz=ZoneInfo("UTC"),
            )
        except (TypeError, ValueError, OverflowError):
            acquired_at = legacy_updated_at
    return acquired_at >= timezone.now() - timedelta(
        seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
    )


def _delivery_error_is_terminal(error_value: str) -> bool:
    cleaned = str(error_value or "")
    return cleaned in {
        EXPIRED_DELIVERY_ERROR,
        CLOSED_DELIVERY_ERROR,
        RELINQUISHED_DELIVERY_ERROR,
    } or cleaned.startswith(TERMINAL_DELIVERY_ERROR_PREFIXES)


def _delivery_may_have_been_accepted(
    *,
    message_ts: str,
    status: str,
    error_value: str,
) -> bool:
    """Conservatively identify a Slack post that cancellation must correct."""
    return bool(
        message_ts
        or status in {"sending", "sent", "unknown"}
        or (
            status == "failed"
            and str(error_value or "").startswith("exhausted:")
        )
    )


def _delivery_attempt_is_due(
    *,
    status: str,
    error_value: str,
    next_attempt_at: datetime | None,
    attempt_count: int,
    legacy_updated_at: datetime,
) -> bool:
    if status == "sent" or _delivery_error_is_terminal(error_value):
        return False
    if attempt_count >= OfficeManagerService.DELIVERY_MAX_ATTEMPTS:
        return False
    if next_attempt_at and next_attempt_at > timezone.now():
        return False
    return not _delivery_lease_is_live(
        status=status,
        error_value=error_value,
        legacy_updated_at=legacy_updated_at,
    )


def _coordinate_recovery_is_due(
    *,
    status: str,
    error_value: str,
    next_attempt_at: datetime | None,
    attempt_count: int,
    legacy_updated_at: datetime,
) -> bool:
    """Allow one final lookup after the public-post retry budget is spent."""
    if status not in {"sending", "unknown"}:
        return False
    if _delivery_error_is_terminal(error_value):
        return False
    if attempt_count > OfficeManagerService.DELIVERY_MAX_ATTEMPTS:
        return False
    if next_attempt_at and next_attempt_at > timezone.now():
        return False
    return not _delivery_lease_is_live(
        status=status,
        error_value=error_value,
        legacy_updated_at=legacy_updated_at,
    )


def _finish_delivery_failure(
    model,
    object_id: int,
    *,
    status_field: str,
    error_field: str,
    attempt_count_field: str,
    next_attempt_field: str,
    lease_token: str,
    exc: Exception,
    uncertain: bool,
    preserve_coordinate_recovery_at_exhaustion: bool = False,
) -> bool:
    """Fence a worker's failure so it cannot overwrite a replacement."""
    attempt_count = (
        model.objects.filter(pk=object_id)
        .values_list(attempt_count_field, flat=True)
        .first()
        or 0
    )
    transient = uncertain or _slack_failure_is_transient(exc)
    preserve_coordinate_recovery = bool(
        uncertain
        and preserve_coordinate_recovery_at_exhaustion
        and attempt_count >= OfficeManagerService.DELIVERY_MAX_ATTEMPTS
    )
    exhausted = (
        not transient
        or (
            attempt_count >= OfficeManagerService.DELIVERY_MAX_ATTEMPTS
            and not preserve_coordinate_recovery
        )
    )
    error = _safe_slack_error(exc)
    if preserve_coordinate_recovery:
        error = f"coordinate_recovery_required:{error}"
    elif exhausted:
        error = (
            f"exhausted:{error}"
            if transient
            else f"permanent:{error}"
        )
    backoff_seconds = min(
        OfficeManagerService.DELIVERY_RETRY_BASE_SECONDS
        * (2 ** max(attempt_count - 1, 0)),
        OfficeManagerService.DELIVERY_RETRY_MAX_SECONDS,
    )
    updated = model.objects.filter(
        pk=object_id,
        **{
            status_field: "sending",
            error_field: lease_token,
        },
    ).update(
        **{
            status_field: (
                "failed" if exhausted or not uncertain else "unknown"
            ),
            error_field: error,
            next_attempt_field: (
                None
                if exhausted
                else timezone.now() + timedelta(seconds=backoff_seconds)
            ),
            "updated_at": timezone.now(),
        }
    )
    return updated == 1


def _terminalize_expired_max_delivery_leases() -> None:
    """Make a crashed final attempt visible instead of stranding it forever."""
    lease_cutoff = timezone.now() - timedelta(
        seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
    )
    for (
        status_field,
        error_field,
        attempt_count_field,
        next_attempt_field,
    ) in (
        (
            "winner_dm_status",
            "winner_dm_last_error",
            "winner_dm_attempt_count",
            "winner_dm_next_attempt_at",
        ),
        (
            "end_of_day_reminder_status",
            "end_of_day_reminder_last_error",
            "end_of_day_reminder_attempt_count",
            "end_of_day_reminder_next_attempt_at",
        ),
        (
            "private_correction_status",
            "private_correction_last_error",
            "private_correction_attempt_count",
            "private_correction_next_attempt_at",
        ),
    ):
        OfficeManagerAssignment.objects.filter(
            **{
                status_field: "sending",
                f"{attempt_count_field}__gte": (
                    OfficeManagerService.DELIVERY_MAX_ATTEMPTS
                ),
                "updated_at__lt": lease_cutoff,
            }
        ).update(
            **{
                status_field: "failed",
                error_field: "exhausted:worker_lease_expired",
                next_attempt_field: None,
                "updated_at": timezone.now(),
            }
        )


def _terminalize_delivery_lease(
    model,
    object_id: int,
    *,
    status_field: str,
    error_field: str,
    next_attempt_field: str,
    lease_token: str,
    error: str,
) -> bool:
    """Terminalize only the exact delivery lease that crossed a hard fence."""
    return (
        model.objects.filter(
            pk=object_id,
            **{status_field: "sending", error_field: lease_token},
        ).update(
            **{
                status_field: "failed",
                error_field: error,
                next_attempt_field: None,
                "updated_at": timezone.now(),
            }
        )
        == 1
    )


def _open_dm_channel(slack_client, slack_user_id: str) -> str:
    response = slack_client.conversations_open(users=[slack_user_id])
    channel_id = str((response.get("channel") or {}).get("id") or "")
    if not response.get("ok", True) or not channel_id:
        raise RuntimeError("conversations_open_failed")
    return channel_id


class OfficeManagerService:
    DELIVERY_LEASE_SECONDS = 300
    DELIVERY_MAX_ATTEMPTS = 5
    DELIVERY_RETRY_BASE_SECONDS = 30
    DELIVERY_RETRY_MAX_SECONDS = 900
    PENDING_RETRACTION_SWEEP_LIMIT = 100
    PENDING_DELIVERY_SWEEP_LIMIT = 100

    @staticmethod
    def has_pending_committed_delivery_work() -> bool:
        """Return whether disabled-mode recovery still needs Slack access."""
        if OfficeManagerAssignment.objects.filter(
            Q(winner_channel_retraction_pending=True)
            | _retryable_delivery_q(
                status_field="winner_channel_announcement_status",
                error_field="winner_channel_announcement_last_error",
            )
            | _retryable_delivery_q(
                status_field="winner_dm_status",
                error_field="winner_dm_last_error",
            )
            | _retryable_delivery_q(
                status_field="end_of_day_reminder_status",
                error_field="end_of_day_reminder_last_error",
            )
            | (
                Q(private_correction_pending=True)
                & _retryable_delivery_q(
                    status_field="private_correction_status",
                    error_field="private_correction_last_error",
                )
            )
        ).exists():
            return True
        return OfficeManagerDay.objects.filter(
            Q(announcement_status__in=("sending", "unknown"))
            | Q(slack_message_ts__isnull=False, message_update_pending=True)
        ).exists()
    RETRACTION_MAX_ATTEMPTS = 5
    RETRACTION_RETRY_BASE_SECONDS = 30
    RETRACTION_RETRY_MAX_SECONDS = 900

    @staticmethod
    def resolve_member(slack_user_id: str) -> User:
        cleaned = str(slack_user_id or "").strip()
        if not cleaned:
            raise OfficeManagerClaimError("member_not_eligible", "Slack user is required")

        existing = User.objects.filter(slack_id=cleaned).first()
        if existing:
            if not existing.is_active:
                raise OfficeManagerClaimError(
                    "member_not_eligible",
                    "This member account is inactive",
                )

        try:
            slack_client = _office_manager_slack_client()
        except OfficeManagerConfigurationError as exc:
            raise OfficeManagerClaimError(
                "slack_profile_unavailable",
                "Roo could not verify this Slack member",
            ) from exc
        profile = SlackService.get_user_profile(cleaned, client=slack_client)
        if profile is None:
            raise OfficeManagerClaimError(
                "slack_profile_unavailable",
                "Roo could not verify this Slack member",
            )
        if (
            profile.get("is_bot")
            or profile.get("deleted")
            or profile.get("is_restricted")
            or profile.get("is_ultra_restricted")
        ):
            raise OfficeManagerClaimError(
                "member_not_eligible",
                "Only active MLAI workspace members can volunteer",
            )
        if existing:
            return existing

        display_name = " ".join(
            str(
                profile.get("real_name")
                or profile.get("display_name")
                or profile.get("name")
                or "Slack Member"
            ).split()
        )
        first_name, _, last_name = display_name.partition(" ")
        generated_email = f"slack+{cleaned.lower()}@users.mlai.internal"
        profile_email = User.objects.normalize_email(
            profile.get("email") or generated_email
        )

        def resolve_locked_candidates(candidates: list[User]) -> User | None:
            by_slack = next(
                (candidate for candidate in candidates if candidate.slack_id == cleaned),
                None,
            )
            by_email = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.email.lower() == profile_email.lower()
                ),
                None,
            )
            if by_slack and by_email and by_slack.pk != by_email.pk:
                raise OfficeManagerClaimError(
                    "member_not_eligible",
                    "This Slack identity conflicts with an existing MLAI account",
                )

            user = by_slack or by_email
            if user is None:
                return None
            if by_email and by_email.slack_id not in {None, "", cleaned}:
                raise OfficeManagerClaimError(
                    "member_not_eligible",
                    "This MLAI account is already linked to another Slack member",
                )
            if not user.is_active:
                raise OfficeManagerClaimError(
                    "member_not_eligible",
                    "This member account is inactive",
                )

            update_fields = []
            if not user.slack_id:
                user.slack_id = cleaned
                update_fields.append("slack_id")
            if first_name and not user.first_name:
                user.first_name = first_name
                update_fields.append("first_name")
            if last_name and not user.last_name:
                user.last_name = last_name
                update_fields.append("last_name")
            if profile.get("image_url") and not user.avatar_url:
                user.avatar_url = profile["image_url"]
                update_fields.append("avatar_url")
            if update_fields:
                user.save(update_fields=update_fields)
            return user

        identity_query = Q(slack_id=cleaned) | Q(email__iexact=profile_email)
        with transaction.atomic():
            candidates = list(
                User.objects.select_for_update()
                .filter(identity_query)
                .order_by("pk")
            )
            user = resolve_locked_candidates(candidates)
            if user is not None:
                return user

            try:
                with transaction.atomic():
                    return User.objects.create_user(
                        email=profile_email,
                        slack_id=cleaned,
                        first_name=first_name,
                        last_name=last_name,
                        avatar_url=profile.get("image_url"),
                    )
            except IntegrityError:
                candidates = list(
                    User.objects.select_for_update()
                    .filter(identity_query)
                    .order_by("pk")
                )
                user = resolve_locked_candidates(candidates)
                if user is not None:
                    return user
                raise OfficeManagerClaimError(
                    "member_not_eligible",
                    "Roo could not safely link this Slack member",
                )

    @staticmethod
    def _recover_existing_claim(
        *,
        slack_user_id: str,
        booking_date: date,
    ) -> OfficeManagerClaimResult | None:
        """Recover a committed claim before applying gates for a new claim.

        A Roo retry can arrive after midnight, after the rollout flag is disabled,
        or after member-profile lookup becomes unavailable. Those changes must not
        hide a result that this endpoint already committed for the same Slack actor.
        """
        cleaned_slack_user_id = str(slack_user_id or "").strip()
        if not cleaned_slack_user_id:
            return None

        with transaction.atomic():
            day = (
                OfficeManagerDay.objects.select_for_update()
                .filter(date=booking_date)
                .first()
            )
            if day is None:
                return None
            active_assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .filter(
                    day=day,
                    status="active",
                    user__slack_id=cleaned_slack_user_id,
                )
                .select_related("booking")
                .first()
            )
            if active_assignment is None:
                return None
            return OfficeManagerClaimResult(
                assignment=active_assignment,
                booking=active_assignment.booking,
                status="already_claimed_by_you",
                existing_booking_converted=bool(
                    active_assignment.booking.ledger_entry_id
                ),
            )

    @staticmethod
    def _normalize_attempt_id(value: uuid.UUID | str) -> uuid.UUID:
        try:
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError) as exc:
            raise OfficeManagerClaimError(
                "invalid_request",
                "attempt_id must be a canonical UUID",
            ) from exc

    @staticmethod
    def _assert_attempt_payload(
        attempt: OfficeManagerClaimAttempt,
        *,
        slack_user_id: str,
        booking_date: date,
        generation: int,
    ) -> None:
        if (
            attempt.slack_user_id != str(slack_user_id or "").strip()
            or attempt.booking_date != booking_date
            or attempt.generation != generation
        ):
            raise OfficeManagerClaimError(
                "attempt_payload_conflict",
                "attempt_id is already bound to a different claim payload",
                attempt_id=attempt.attempt_id,
            )

    @staticmethod
    def _replay_attempt(
        attempt: OfficeManagerClaimAttempt,
        *,
        slack_user_id: str,
        booking_date: date,
        generation: int,
    ) -> OfficeManagerClaimResult:
        OfficeManagerService._assert_attempt_payload(
            attempt,
            slack_user_id=slack_user_id,
            booking_date=booking_date,
            generation=generation,
        )
        if attempt.outcome in {"claimed", "already_claimed_by_you"}:
            if attempt.assignment_id is None:
                raise OfficeManagerClaimError(
                    "attempt_unavailable",
                    "The stored claim result is incomplete",
                    attempt_id=attempt.attempt_id,
                )
            assignment = (
                OfficeManagerAssignment.objects.select_related("booking")
                .get(pk=attempt.assignment_id)
            )
            return OfficeManagerClaimResult(
                assignment=assignment,
                booking=assignment.booking,
                status=attempt.outcome,
                existing_booking_converted=(
                    attempt.existing_booking_converted
                ),
                attempt_id=attempt.attempt_id,
                replayed=True,
            )
        raise OfficeManagerClaimError(
            attempt.outcome,
            attempt.message or "Office Manager claim rejected",
            assignee_slack_user_id=attempt.assignee_slack_user_id,
            attempt_id=attempt.attempt_id,
        )

    @staticmethod
    def _persist_attempt(
        *,
        attempt_id: uuid.UUID,
        slack_user_id: str,
        booking_date: date,
        generation: int,
        outcome: str,
        message: str = "",
        assignee_slack_user_id: str = "",
        assignment: OfficeManagerAssignment | None = None,
        existing_booking_converted: bool = False,
        points_refunded: int = 0,
    ) -> tuple[OfficeManagerClaimAttempt, bool]:
        try:
            with transaction.atomic():
                return (
                    OfficeManagerClaimAttempt.objects.create(
                        attempt_id=attempt_id,
                        slack_user_id=str(slack_user_id or "").strip(),
                        booking_date=booking_date,
                        generation=generation,
                        outcome=outcome,
                        message=message,
                        assignee_slack_user_id=assignee_slack_user_id,
                        assignment=assignment,
                        existing_booking_converted=existing_booking_converted,
                        points_refunded=points_refunded,
                    ),
                    True,
                )
        except IntegrityError:
            return (
                OfficeManagerClaimAttempt.objects.select_related(
                    "assignment__booking"
                ).get(pk=attempt_id),
                False,
            )

    @staticmethod
    @transaction.atomic
    def _persist_terminal_attempt(
        *,
        attempt_id: uuid.UUID,
        slack_user_id: str,
        booking_date: date,
        generation: int,
        outcome: str,
        message: str,
        assignee_slack_user_id: str = "",
    ) -> tuple[OfficeManagerClaimAttempt, bool]:
        """Persist a rejection under the same day lifecycle fence as cancel."""
        CoworkingService._lock_booking_date(booking_date)
        day = (
            OfficeManagerDay.objects.select_for_update()
            .filter(date=booking_date)
            .first()
        )
        lifecycle_superseded = False
        if day is not None:
            lifecycle_superseded = day.generation != generation
            if not lifecycle_superseded:
                lifecycle_superseded = (
                    day.assignments.filter(status="relinquished").exists()
                    and not day.assignments.filter(status="active").exists()
                )
        if lifecycle_superseded:
            outcome = "attempt_superseded"
            message = (
                "This Office Manager claim attempt was superseded by "
                "cancellation and cannot be replayed"
            )
            assignee_slack_user_id = ""
        return OfficeManagerService._persist_attempt(
            attempt_id=attempt_id,
            slack_user_id=slack_user_id,
            booking_date=booking_date,
            generation=generation,
            outcome=outcome,
            message=message,
            assignee_slack_user_id=assignee_slack_user_id,
        )

    @staticmethod
    def claim(
        *,
        slack_user_id: str,
        booking_date: date,
        attempt_id: uuid.UUID | str | None = None,
        generation: int = 1,
        now: datetime | None = None,
    ) -> OfficeManagerClaimResult:
        """Claim once per Roo-owned attempt identity.

        Service callers that omit ``attempt_id`` retain the legacy natural-key
        recovery used by older internal tests.  The HTTP contract always
        supplies an attempt id and therefore never conflates a later lifecycle
        with a retry of an earlier one.
        """
        legacy_natural_key = attempt_id is None
        if isinstance(generation, bool):
            raise OfficeManagerClaimError(
                "invalid_request",
                "generation must be a positive integer",
            )
        try:
            normalized_generation = int(generation)
        except (TypeError, ValueError) as exc:
            raise OfficeManagerClaimError(
                "invalid_request",
                "generation must be a positive integer",
            ) from exc
        if (
            normalized_generation < 1
            or normalized_generation > MAX_OFFICE_MANAGER_GENERATION
            or str(generation).strip() != str(normalized_generation)
        ):
            raise OfficeManagerClaimError(
                "invalid_request",
                "generation must be a canonical positive integer",
            )
        normalized_attempt_id = (
            uuid.uuid4()
            if legacy_natural_key
            else OfficeManagerService._normalize_attempt_id(attempt_id)
        )
        existing_attempt = (
            OfficeManagerClaimAttempt.objects.select_related(
                "assignment__booking"
            )
            .filter(pk=normalized_attempt_id)
            .first()
        )
        if existing_attempt is not None:
            return OfficeManagerService._replay_attempt(
                existing_attempt,
                slack_user_id=slack_user_id,
                booking_date=booking_date,
                generation=normalized_generation,
            )

        if legacy_natural_key:
            existing_claim = OfficeManagerService._recover_existing_claim(
                slack_user_id=slack_user_id,
                booking_date=booking_date,
            )
            if existing_claim is not None:
                return existing_claim

        try:
            return OfficeManagerService._claim_new_attempt(
                slack_user_id=slack_user_id,
                booking_date=booking_date,
                attempt_id=normalized_attempt_id,
                generation=normalized_generation,
                now=now,
            )
        except _OfficeManagerAttemptLostRace as exc:
            attempt = OfficeManagerClaimAttempt.objects.select_related(
                "assignment__booking"
            ).get(pk=exc.attempt_id)
            return OfficeManagerService._replay_attempt(
                attempt,
                slack_user_id=slack_user_id,
                booking_date=booking_date,
                generation=normalized_generation,
            )
        except OfficeManagerClaimError as exc:
            if legacy_natural_key or exc.code not in TERMINAL_CLAIM_OUTCOMES:
                raise
            attempt, created = OfficeManagerService._persist_terminal_attempt(
                attempt_id=normalized_attempt_id,
                slack_user_id=slack_user_id,
                booking_date=booking_date,
                generation=normalized_generation,
                outcome=exc.code,
                message=str(exc),
                assignee_slack_user_id=exc.assignee_slack_user_id,
            )
            if not created:
                return OfficeManagerService._replay_attempt(
                    attempt,
                    slack_user_id=slack_user_id,
                    booking_date=booking_date,
                    generation=normalized_generation,
                )
            raise OfficeManagerClaimError(
                attempt.outcome,
                attempt.message,
                assignee_slack_user_id=attempt.assignee_slack_user_id,
                attempt_id=attempt.attempt_id,
            ) from exc

    @staticmethod
    def _claim_new_attempt(
        *,
        slack_user_id: str,
        booking_date: date,
        attempt_id: uuid.UUID,
        generation: int,
        now: datetime | None = None,
    ) -> OfficeManagerClaimResult:

        if not _office_manager_enabled():
            raise OfficeManagerClaimError(
                "feature_disabled",
                "Office Manager volunteering is currently unavailable",
            )
        local_now = _local_now(now)
        if booking_date != local_now.date():
            raise OfficeManagerClaimError(
                "claim_closed",
                "Office Manager volunteering is only available for today",
            )

        user = OfficeManagerService.resolve_member(slack_user_id)
        local_now = _local_now(now)
        if booking_date != local_now.date():
            raise OfficeManagerClaimError(
                "claim_closed",
                "Office Manager volunteering is only available for today",
            )
        with transaction.atomic():
            # All booking mutations acquire user -> date -> booking/account.
            # Taking the principal first avoids a cycle with cancellation and
            # identity merge when a paid booking is converted concurrently.
            user = User.objects.select_for_update().get(pk=user.pk)
            if (
                not user.is_active
                or str(user.slack_id or "").strip()
                != str(slack_user_id or "").strip()
            ):
                raise OfficeManagerClaimError(
                    "member_not_eligible",
                    "Only active linked MLAI members can volunteer",
                )
            CoworkingService._lock_booking_date(booking_date)
            try:
                day = OfficeManagerDay.objects.select_for_update().get(date=booking_date)
            except OfficeManagerDay.DoesNotExist as exc:
                raise OfficeManagerClaimError(
                    "office_manager_day_not_found",
                    "Today's Office Manager announcement is not available",
                ) from exc

            local_now = _local_now(now)
            if booking_date != local_now.date():
                raise OfficeManagerClaimError(
                    "claim_closed",
                    "Office Manager volunteering is only available for today",
                )
            if (
                day.status == "closed"
                or local_now >= day.claim_cutoff_at.astimezone(_timezone())
            ):
                raise OfficeManagerClaimError(
                    "claim_closed",
                    "The Office Manager volunteer window is closed",
                )

            if day.generation != generation:
                raise OfficeManagerClaimError(
                    "announcement_superseded",
                    "This Office Manager announcement has been superseded; use the current button",
                )

            active_assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .filter(day=day, status="active")
                .select_related("user", "booking")
                .first()
            )
            if active_assignment is not None:
                if active_assignment.user_id == user.id:
                    attempt, created = OfficeManagerService._persist_attempt(
                        attempt_id=attempt_id,
                        slack_user_id=slack_user_id,
                        booking_date=booking_date,
                        generation=generation,
                        outcome="already_claimed_by_you",
                        assignment=active_assignment,
                        existing_booking_converted=bool(
                            active_assignment.booking.ledger_entry_id
                        ),
                        points_refunded=active_assignment.points_refunded,
                    )
                    if not created:
                        raise _OfficeManagerAttemptLostRace(attempt.attempt_id)
                    return OfficeManagerClaimResult(
                        assignment=active_assignment,
                        booking=active_assignment.booking,
                        status="already_claimed_by_you",
                        existing_booking_converted=bool(
                            active_assignment.booking.ledger_entry_id
                        ),
                        attempt_id=attempt_id,
                    )
                raise OfficeManagerClaimError(
                    "already_claimed",
                    "Another member has already been selected",
                    assignee_slack_user_id=active_assignment.user.slack_id or "",
                )

            if CoworkingService.get_capacity(booking_date) <= 0:
                raise OfficeManagerClaimError(
                    "claim_closed",
                    "Coworking is closed today",
                )

            booking = (
                CoworkingBooking.objects.select_for_update()
                .filter(user=user, date=booking_date, status="booked")
                .first()
            )
            existing_booking_converted = booking is not None
            points_refunded = 0
            purchased_points_refunded_microroo = 0
            refund_ledger = None

            if booking is None:
                booking = CoworkingBooking.objects.create(
                    user=user,
                    date=booking_date,
                    status="booked",
                    points_cost=0,
                    booking_source="office_manager",
                    slack_channel_id=day.slack_channel_id,
                )
            else:
                points_refunded = max(0, int(booking.points_cost))
                if points_refunded:
                    try:
                        purchased_points_refunded_microroo = (
                            CoworkingService.validated_booking_debit_provenance(
                                booking
                            )
                        )
                    except ValueError as exc:
                        raise OfficeManagerClaimError(
                            "refund_unavailable",
                            str(exc),
                        ) from exc
                    refund_ledger, _ = PointsService.refund(
                        user=user,
                        delta=points_refunded,
                        source="COWORKING",
                        description=f"Office Manager booking refund for {booking_date}",
                        created_by_slack_id=str(slack_user_id).strip(),
                        idempotency_key=f"office_manager_refund:{day.id}:{booking.id}",
                        reference_type="OFFICE_MANAGER_ASSIGNMENT",
                        reference_id=str(day.id),
                        purchased_delta_microroo=(
                            purchased_points_refunded_microroo
                        ),
                        reverse_lifetime_spent=True,
                    )
                    booking.refund_ledger_entry = refund_ledger
                if booking.original_points_cost is None:
                    booking.original_points_cost = points_refunded
                booking.points_cost = 0
                booking.booking_source = "office_manager"
                booking.slack_channel_id = booking.slack_channel_id or day.slack_channel_id
                booking.save(
                    update_fields=[
                        "points_cost",
                        "booking_source",
                        "original_points_cost",
                        "refund_ledger_entry",
                        "slack_channel_id",
                    ]
                )

            # Capacity, booking, ledger, and account acquisition can all wait.
            # Re-sample the authoritative clock at the last point before any
            # durable Office Manager result is created; a rejection rolls the
            # tentative booking/refund changes above back atomically.
            local_now = _local_now(now)
            if booking_date != local_now.date() or (
                local_now >= day.claim_cutoff_at.astimezone(_timezone())
            ):
                raise OfficeManagerClaimError(
                    "claim_closed",
                    "The Office Manager volunteer window is closed",
                )

            assignment = OfficeManagerAssignment.objects.create(
                day=day,
                user=user,
                booking=booking,
                status="active",
                points_refunded=points_refunded,
                purchased_points_refunded_microroo=(
                    purchased_points_refunded_microroo
                ),
                refund_ledger_entry=refund_ledger,
            )
            attempt, created = OfficeManagerService._persist_attempt(
                attempt_id=attempt_id,
                slack_user_id=slack_user_id,
                booking_date=booking_date,
                generation=generation,
                outcome="claimed",
                assignment=assignment,
                existing_booking_converted=existing_booking_converted,
                points_refunded=points_refunded,
            )
            if not created:
                raise _OfficeManagerAttemptLostRace(attempt.attempt_id)
            day.status = "claimed"
            day.closed_at = None
            day.message_update_pending = True
            day.save(
                update_fields=[
                    "status",
                    "closed_at",
                    "message_update_pending",
                    "updated_at",
                ]
            )

        return OfficeManagerClaimResult(
            assignment=assignment,
            booking=booking,
            status="claimed",
            existing_booking_converted=existing_booking_converted,
            attempt_id=attempt_id,
        )

    @staticmethod
    @transaction.atomic
    def relinquish_for_booking(
        booking: CoworkingBooking,
        *,
        requester_slack_id: str,
        locked_day: OfficeManagerDay | None = None,
        now: datetime | None = None,
    ) -> tuple[bool, int | None, int | None]:
        assignment_day_id = (
            OfficeManagerAssignment.objects.filter(
                booking=booking,
                status="active",
            )
            .values_list("day_id", flat=True)
            .first()
        )
        if assignment_day_id is None:
            return False, None, None

        day = locked_day
        if day is None:
            day = OfficeManagerDay.objects.select_for_update().get(
                pk=assignment_day_id
            )
        elif day.pk != assignment_day_id:
            raise ValueError("Office Manager day lock does not match booking")

        assignment = (
            OfficeManagerAssignment.objects.select_for_update(of=("self",))
            .filter(booking=booking, day=day, status="active")
            .first()
        )
        if assignment is None:
            return False, None, None

        if assignment.points_refunded and not assignment.refund_reversal_ledger_entry_id:
            refund_ledger = assignment.refund_ledger_entry
            expected_refund_microroo = PointsService.roo_to_microroo(
                assignment.points_refunded
            )
            purchased_refund_microroo = (
                assignment.purchased_points_refunded_microroo
            )
            original_debit = booking.ledger_entry
            booking_purchased_microroo = (
                booking.purchased_points_cost_microroo
            )
            if (
                booking.original_points_cost != assignment.points_refunded
                or original_debit is None
                or original_debit.user_id != booking.user_id
                or original_debit.kind != "SPEND"
                or original_debit.source != "COWORKING"
                or original_debit.delta_microroo != -expected_refund_microroo
                or original_debit.reference_type != "COWORKING_BOOKING"
                or not CoworkingService.booking_debit_reference_matches(
                    booking,
                    original_debit.reference_id,
                )
                or booking_purchased_microroo is None
                or booking_purchased_microroo != purchased_refund_microroo
                or refund_ledger is None
                or refund_ledger.user_id != booking.user_id
                or refund_ledger.kind != "REFUND"
                or refund_ledger.source != "COWORKING"
                or refund_ledger.delta_microroo != expected_refund_microroo
                or refund_ledger.reference_type
                != "OFFICE_MANAGER_ASSIGNMENT"
                or refund_ledger.reference_id != str(day.id)
                or purchased_refund_microroo is None
                or not 0
                <= purchased_refund_microroo
                <= expected_refund_microroo
            ):
                raise ValueError(
                    "The Office Manager refund cannot be safely reversed "
                    "because its authoritative ledger entry is unavailable"
                )
            try:
                reversal_ledger, _ = PointsService.spend(
                    user=booking.user,
                    delta=assignment.points_refunded,
                    source="COWORKING",
                    description=(
                        "Reversal of Office Manager refund for "
                        f"{booking.date}"
                    ),
                    created_by_slack_id=requester_slack_id,
                    idempotency_key=(
                        f"office_manager_refund_reversal:{assignment.id}"
                    ),
                    reference_type="OFFICE_MANAGER_REFUND_REVERSAL",
                    reference_id=str(assignment.id),
                    purchased_delta_microroo=(
                        purchased_refund_microroo
                    ),
                )
            except InsufficientBalanceError as exc:
                raise ValueError(
                    "The Office Manager booking cannot be cancelled because "
                    "the previously refunded Roo points are no longer available"
                ) from exc
            assignment.refund_reversal_ledger_entry = reversal_ledger
            booking.points_cost = (
                booking.original_points_cost or assignment.points_refunded
            )
            booking.booking_source = "points"
            booking.save(update_fields=["points_cost", "booking_source"])

        local_now = _local_now(now)
        private_delivery_may_have_happened = (
            _delivery_may_have_been_accepted(
                message_ts=assignment.winner_dm_message_ts,
                status=assignment.winner_dm_status,
                error_value=assignment.winner_dm_last_error,
            )
            or _delivery_may_have_been_accepted(
                message_ts=assignment.end_of_day_reminder_message_ts,
                status=assignment.end_of_day_reminder_status,
                error_value=assignment.end_of_day_reminder_last_error,
            )
        )
        assignment.status = "relinquished"
        assignment.relinquished_at = timezone.now()
        assignment.winner_channel_retraction_pending = (
            _delivery_may_have_been_accepted(
                message_ts=assignment.winner_channel_message_ts,
                status=assignment.winner_channel_announcement_status,
                error_value=assignment.winner_channel_announcement_last_error,
            )
        )
        assignment.winner_channel_retraction_status = (
            "pending"
            if assignment.winner_channel_retraction_pending
            else "not_required"
        )
        assignment.winner_channel_retraction_lease_token = ""
        assignment.winner_channel_retraction_next_attempt_at = (
            timezone.now()
            if assignment.winner_channel_retraction_pending
            else None
        )
        if private_delivery_may_have_happened:
            assignment.private_correction_pending = True
            assignment.private_correction_status = "pending"
            assignment.private_correction_last_error = ""
            assignment.private_correction_next_attempt_at = timezone.now()
        assignment.save(
            update_fields=[
                "status",
                "relinquished_at",
                "refund_reversal_ledger_entry",
                "winner_channel_retraction_pending",
                "winner_channel_retraction_status",
                "winner_channel_retraction_lease_token",
                "winner_channel_retraction_next_attempt_at",
                "private_correction_pending",
                "private_correction_status",
                "private_correction_last_error",
                "private_correction_next_attempt_at",
                "updated_at",
            ]
        )
        reopened = (
            day.date == local_now.date()
            and local_now < day.claim_cutoff_at.astimezone(_timezone())
        )
        attempts_to_supersede = OfficeManagerClaimAttempt.objects.filter(
            booking_date=day.date,
            generation=day.generation,
        )
        attempts_to_supersede.exclude(outcome="attempt_superseded").update(
            outcome="attempt_superseded",
            message=(
                "This Office Manager claim attempt was superseded by "
                "cancellation and cannot be replayed"
            ),
            superseded_at=timezone.now(),
        )
        day.status = "open" if reopened else "closed"
        if reopened:
            # Fence every button from the lifecycle that was just cancelled,
            # including clicks which had not reached the backend yet.
            day.generation += 1
        day.closed_at = None if reopened else timezone.now()
        day.message_update_pending = True
        day.save(
            update_fields=[
                "status",
                "generation",
                "closed_at",
                "message_update_pending",
                "updated_at",
            ]
        )
        return reopened, day.id, assignment.id

    @staticmethod
    def recover_announcement_coordinates(day_id: int) -> bool | None:
        """Return True when a response-loss daily post is found, None if absent."""
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            day = OfficeManagerDay.objects.select_for_update().get(pk=day_id)
            if day.slack_message_ts:
                return True
            if not _coordinate_recovery_is_due(
                status=day.announcement_status,
                error_value=day.announcement_last_error,
                next_attempt_at=day.announcement_next_attempt_at,
                attempt_count=day.announcement_attempt_count,
                legacy_updated_at=day.updated_at,
            ):
                return False
            day.announcement_status = "sending"
            if (
                day.announcement_attempt_count
                < OfficeManagerService.DELIVERY_MAX_ATTEMPTS
            ):
                day.announcement_attempt_count += 1
            day.announcement_last_error = lease_token
            day.announcement_next_attempt_at = None
            day.save(update_fields=[
                "announcement_status",
                "announcement_attempt_count",
                "announcement_last_error",
                "announcement_next_attempt_at",
                "updated_at",
            ])
        try:
            slack_client = _office_manager_slack_client()
            message_ts = _find_message_ts_by_client_msg_id(
                slack_client,
                channel_id=day.slack_channel_id,
                client_msg_id=_slack_client_msg_id("daily", day.id),
                oldest=day.created_at,
            )
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerDay,
                day_id,
                status_field="announcement_status",
                error_field="announcement_last_error",
                attempt_count_field="announcement_attempt_count",
                next_attempt_field="announcement_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=_slack_failure_is_transient(exc),
            )
            return False
        if not message_ts:
            exhausted = (
                day.announcement_attempt_count
                >= OfficeManagerService.DELIVERY_MAX_ATTEMPTS
            )
            OfficeManagerDay.objects.filter(
                pk=day_id,
                announcement_status="sending",
                announcement_last_error=lease_token,
            ).update(
                announcement_status=("failed" if exhausted else "pending"),
                announcement_last_error=(
                    "exhausted:coordinate_recovery:not_found"
                    if exhausted
                    else "coordinate_recovery:not_found"
                ),
                announcement_next_attempt_at=(
                    None
                    if exhausted
                    else timezone.now()
                    + timedelta(seconds=OfficeManagerService.DELIVERY_RETRY_BASE_SECONDS)
                ),
                updated_at=timezone.now(),
            )
            return False
        updated = OfficeManagerDay.objects.filter(
            pk=day_id,
            slack_message_ts="",
            announcement_status="sending",
            announcement_last_error=lease_token,
        ).update(
            slack_message_ts=message_ts,
            announcement_status="sent",
            announcement_last_error="",
            announcement_next_attempt_at=None,
            announced_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return bool(updated) or OfficeManagerDay.objects.filter(
            pk=day_id,
            slack_message_ts=message_ts,
        ).exists()

    @staticmethod
    def recover_winner_channel_coordinates(
        assignment_id: int,
        *,
        for_retraction: bool = False,
    ) -> bool | None:
        """Resolve an accepted winner post after its Slack response was lost.

        A cancellation owns a separate bounded retraction budget. It must be
        allowed to locate a possibly accepted winner post even after the
        original posting worker exhausted its own coordinate-recovery budget.
        """
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("day")
                .get(pk=assignment_id)
            )
            if assignment.winner_channel_message_ts:
                return True
            ordinary_recovery_due = _coordinate_recovery_is_due(
                status=assignment.winner_channel_announcement_status,
                error_value=assignment.winner_channel_announcement_last_error,
                next_attempt_at=(
                    assignment.winner_channel_announcement_next_attempt_at
                ),
                attempt_count=(
                    assignment.winner_channel_announcement_attempt_count
                ),
                legacy_updated_at=assignment.updated_at,
            )
            retraction_recovery_due = bool(
                for_retraction
                and _delivery_may_have_been_accepted(
                    message_ts="",
                    status=assignment.winner_channel_announcement_status,
                    error_value=assignment.winner_channel_announcement_last_error,
                )
                and assignment.winner_channel_announcement_last_error
                not in TERMINAL_ASSIGNMENT_DELIVERY_ERRORS
                and not str(
                    assignment.winner_channel_announcement_last_error or ""
                ).startswith("permanent:")
                and not _delivery_lease_is_live(
                    status=assignment.winner_channel_announcement_status,
                    error_value=assignment.winner_channel_announcement_last_error,
                    legacy_updated_at=assignment.updated_at,
                )
            )
            if not ordinary_recovery_due and not retraction_recovery_due:
                return False
            assignment.winner_channel_announcement_status = "sending"
            if (
                assignment.winner_channel_announcement_attempt_count
                < OfficeManagerService.DELIVERY_MAX_ATTEMPTS
            ):
                assignment.winner_channel_announcement_attempt_count += 1
            assignment.winner_channel_announcement_last_error = lease_token
            assignment.winner_channel_announcement_next_attempt_at = None
            assignment.save(update_fields=[
                "winner_channel_announcement_status",
                "winner_channel_announcement_attempt_count",
                "winner_channel_announcement_last_error",
                "winner_channel_announcement_next_attempt_at",
                "updated_at",
            ])
        try:
            slack_client = _office_manager_slack_client()
            message_ts = _find_message_ts_by_client_msg_id(
                slack_client,
                channel_id=assignment.day.slack_channel_id,
                client_msg_id=_winner_channel_client_msg_id(assignment),
                oldest=assignment.claimed_at,
            )
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_channel_announcement_status",
                error_field="winner_channel_announcement_last_error",
                attempt_count_field="winner_channel_announcement_attempt_count",
                next_attempt_field="winner_channel_announcement_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=_slack_failure_is_transient(exc),
            )
            return False
        if not message_ts:
            exhausted = (
                assignment.winner_channel_announcement_attempt_count
                >= OfficeManagerService.DELIVERY_MAX_ATTEMPTS
            )
            OfficeManagerAssignment.objects.filter(
                pk=assignment_id,
                winner_channel_announcement_status="sending",
                winner_channel_announcement_last_error=lease_token,
            ).update(
                winner_channel_announcement_status=(
                    "failed" if exhausted else "pending"
                ),
                winner_channel_announcement_last_error=(
                    "exhausted:coordinate_recovery:not_found"
                    if exhausted
                    else "coordinate_recovery:not_found"
                ),
                winner_channel_announcement_next_attempt_at=(None if exhausted else timezone.now() + timedelta(seconds=OfficeManagerService.DELIVERY_RETRY_BASE_SECONDS)),
                updated_at=timezone.now(),
            )
            return False
        updated = OfficeManagerAssignment.objects.filter(
            pk=assignment_id,
            winner_channel_message_ts="",
            winner_channel_announcement_status="sending",
            winner_channel_announcement_last_error=lease_token,
        ).update(
            winner_channel_message_ts=message_ts,
            winner_channel_announcement_status="sent",
            winner_channel_announcement_last_error="",
            winner_channel_announcement_next_attempt_at=None,
            winner_channel_announcement_sent_at=timezone.now(),
            updated_at=timezone.now(),
        )
        return bool(updated) or OfficeManagerAssignment.objects.filter(
            pk=assignment_id,
            winner_channel_message_ts=message_ts,
        ).exists()

    @staticmethod
    def post_announcement(day_id: int, *, now: datetime | None = None) -> bool:
        existing = OfficeManagerDay.objects.get(pk=day_id)
        if (
            not existing.slack_message_ts
            and existing.announcement_status in {"sending", "unknown"}
            and _coordinate_recovery_is_due(
                status=existing.announcement_status,
                error_value=existing.announcement_last_error,
                next_attempt_at=existing.announcement_next_attempt_at,
                attempt_count=existing.announcement_attempt_count,
                legacy_updated_at=existing.updated_at,
            )
        ):
            recovered = OfficeManagerService.recover_announcement_coordinates(
                day_id
            )
            if recovered is not None:
                return recovered
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            day = OfficeManagerDay.objects.select_for_update().get(pk=day_id)
            if day.announcement_status == "sent":
                return True
            if not _delivery_attempt_is_due(
                status=day.announcement_status,
                error_value=day.announcement_last_error,
                next_attempt_at=day.announcement_next_attempt_at,
                attempt_count=day.announcement_attempt_count,
                legacy_updated_at=day.updated_at,
            ):
                return False
            day.announcement_status = "sending"
            day.announcement_attempt_count += 1
            day.announcement_last_error = lease_token
            day.announcement_next_attempt_at = None
            day.save(
                update_fields=[
                    "announcement_status",
                    "announcement_attempt_count",
                    "announcement_last_error",
                    "announcement_next_attempt_at",
                    "updated_at",
                ]
            )

        try:
            # The lease is durable; Slack I/O must not hold the day row lock.
            # Re-check the fence immediately before publishing and finalize only
            # if this worker still owns it.
            day = OfficeManagerDay.objects.get(pk=day_id)
            if (
                day.announcement_status != "sending"
                or day.announcement_last_error != lease_token
            ):
                return day.announcement_status == "sent"
            local_now = _local_now(now)
            cutoff_passed = local_now >= day.claim_cutoff_at.astimezone(
                _timezone()
            )
            if day.date != local_now.date() or day.status == "closed" or (
                day.status == "open" and cutoff_passed
            ):
                _terminalize_delivery_lease(
                    OfficeManagerDay,
                    day_id,
                    status_field="announcement_status",
                    error_field="announcement_last_error",
                    next_attempt_field="announcement_next_attempt_at",
                    lease_token=lease_token,
                    error=(
                        EXPIRED_DELIVERY_ERROR
                        if day.date != local_now.date()
                        else CLOSED_DELIVERY_ERROR
                    ),
                )
                return False
            rendered_text = _announcement_text(day)
            rendered_blocks = _announcement_blocks(day)
            response = _office_manager_slack_client().chat_postMessage(
                channel=day.slack_channel_id,
                text=rendered_text,
                blocks=rendered_blocks,
                client_msg_id=_slack_client_msg_id("daily", day.id),
                unfurl_links=False,
                unfurl_media=False,
            )
            message_ts = str(response.get("ts") or "")
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
            if not message_ts:
                raise OfficeManagerDeliveryCoordinateUnknown(
                    "chat.postMessage accepted without ts"
                )
            post_send_local_now = _local_now(now)
            needs_reconcile = False
            with transaction.atomic():
                current_day = OfficeManagerDay.objects.select_for_update().get(
                    pk=day_id
                )
                if (
                    current_day.announcement_status != "sending"
                    or current_day.announcement_last_error != lease_token
                ):
                    return current_day.announcement_status == "sent"
                crossed_delivery_boundary = (
                    current_day.date != post_send_local_now.date()
                    or (
                        current_day.status == "open"
                        and post_send_local_now
                        >= current_day.claim_cutoff_at.astimezone(_timezone())
                    )
                )
                if crossed_delivery_boundary and current_day.status == "open":
                    current_day.status = "closed"
                    current_day.closed_at = timezone.now()
                needs_reconcile = crossed_delivery_boundary or (
                    _announcement_text(current_day) != rendered_text
                    or _announcement_blocks(current_day) != rendered_blocks
                )
                current_day.announcement_status = "sent"
                current_day.slack_message_ts = message_ts
                current_day.announced_at = timezone.now()
                current_day.announcement_last_error = ""
                current_day.announcement_next_attempt_at = None
                current_day.message_update_pending = needs_reconcile
                current_day.save(
                    update_fields=[
                        "announcement_status",
                        "slack_message_ts",
                        "announced_at",
                        "announcement_last_error",
                        "announcement_next_attempt_at",
                        "message_update_pending",
                        "status",
                        "closed_at",
                        "updated_at",
                    ]
                )
        except OfficeManagerConfigurationError as exc:
            _finish_delivery_failure(
                OfficeManagerDay,
                day_id,
                status_field="announcement_status",
                error_field="announcement_last_error",
                attempt_count_field="announcement_attempt_count",
                next_attempt_field="announcement_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=False,
            )
            return False
        except SlackApiError as exc:
            _finish_delivery_failure(
                OfficeManagerDay,
                day_id,
                status_field="announcement_status",
                error_field="announcement_last_error",
                attempt_count_field="announcement_attempt_count",
                next_attempt_field="announcement_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=_slack_failure_is_transient(exc),
                preserve_coordinate_recovery_at_exhaustion=True,
            )
            return False
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerDay,
                day_id,
                status_field="announcement_status",
                error_field="announcement_last_error",
                attempt_count_field="announcement_attempt_count",
                next_attempt_field="announcement_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=True,
                preserve_coordinate_recovery_at_exhaustion=True,
            )
            return False
        if needs_reconcile:
            return OfficeManagerService.reconcile_message(day_id)
        return True

    @staticmethod
    def reconcile_message(
        day_id: int,
        *,
        _allow_immediate_followup: bool = True,
    ) -> bool:
        with transaction.atomic():
            day = OfficeManagerDay.objects.select_for_update().get(pk=day_id)
            if not day.slack_message_ts:
                return False
            if not _message_update_attempt_is_due(day):
                return False
            attempt_count = _message_update_retry_count(
                day.announcement_last_error
            ) + 1
            lease_token = _message_update_lease_token(attempt_count)
            day.announcement_last_error = lease_token
            day.announcement_next_attempt_at = None
            day.message_update_pending = True
            day.save(
                update_fields=[
                    "announcement_last_error",
                    "announcement_next_attempt_at",
                    "message_update_pending",
                    "updated_at",
                ]
            )
            rendered_text = _announcement_text(day)
            rendered_blocks = _announcement_blocks(day)
            channel_id = day.slack_channel_id
            message_ts = day.slack_message_ts
        try:
            slack_client = _office_manager_slack_client()
            success = SlackService.update_message(
                channel_id,
                message_ts,
                rendered_text,
                blocks=rendered_blocks,
                client=slack_client,
                raise_errors=True,
            )
            failure = None
        except Exception as exc:
            success = False
            failure = exc
        with transaction.atomic():
            current_day = OfficeManagerDay.objects.select_for_update().get(
                pk=day_id
            )
            if current_day.announcement_last_error != lease_token:
                return False
            state_is_current = (
                _announcement_text(current_day) == rendered_text
                and _announcement_blocks(current_day) == rendered_blocks
            )
            if success and state_is_current:
                current_day.message_update_pending = False
                current_day.announcement_last_error = ""
                current_day.announcement_next_attempt_at = None
            elif success:
                # A newer state won while Slack was in flight. Give that state
                # a fresh bounded generation instead of publishing stale data.
                current_day.message_update_pending = True
                current_day.announcement_last_error = (
                    "message_update:retry:0:state_changed"
                )
                current_day.announcement_next_attempt_at = timezone.now()
            else:
                error = _safe_slack_error(failure) if failure else "unknown"
                transient = bool(
                    failure is not None and _slack_failure_is_transient(failure)
                )
                exhausted = (
                    attempt_count >= OfficeManagerService.DELIVERY_MAX_ATTEMPTS
                )
                if not transient:
                    current_day.message_update_pending = False
                    current_day.announcement_last_error = (
                        f"permanent:message_update:{error}"
                    )
                    current_day.announcement_next_attempt_at = None
                elif exhausted:
                    current_day.message_update_pending = False
                    current_day.announcement_last_error = (
                        f"exhausted:message_update:{error}"
                    )
                    current_day.announcement_next_attempt_at = None
                else:
                    current_day.message_update_pending = True
                    current_day.announcement_last_error = (
                        f"message_update:retry:{attempt_count}:{error}"
                    )
                    backoff_seconds = min(
                        OfficeManagerService.DELIVERY_RETRY_BASE_SECONDS
                        * (2 ** max(attempt_count - 1, 0)),
                        OfficeManagerService.DELIVERY_RETRY_MAX_SECONDS,
                    )
                    current_day.announcement_next_attempt_at = (
                        timezone.now() + timedelta(seconds=backoff_seconds)
                    )
            current_day.save(
                update_fields=[
                    "message_update_pending",
                    "announcement_last_error",
                    "announcement_next_attempt_at",
                    "updated_at",
                ]
            )
        if success and not state_is_current:
            if _allow_immediate_followup:
                return OfficeManagerService.reconcile_message(
                    day_id,
                    _allow_immediate_followup=False,
                )
            return False
        return success

    @staticmethod
    def unresolved_message_update_dead_letters() -> list[dict]:
        """Return content-free terminal update failures for scheduler health."""
        rows = OfficeManagerDay.objects.filter(
            Q(announcement_last_error__startswith="permanent:message_update:")
            | Q(announcement_last_error__startswith="exhausted:message_update:")
        ).values("id", "date", "announcement_last_error")
        return [
            {
                "day_id": row["id"],
                "date": row["date"].isoformat(),
                "error": row["announcement_last_error"],
            }
            for row in rows
        ]

    @staticmethod
    def unresolved_announcement_dead_letters() -> list[dict]:
        """Return terminal daily-announcement post failures for alerting."""
        terminal = Q()
        for prefix in TERMINAL_DELIVERY_ERROR_PREFIXES:
            terminal |= Q(announcement_last_error__startswith=prefix)
        rows = (
            OfficeManagerDay.objects.filter(terminal)
            .exclude(
                Q(
                    announcement_last_error__startswith=(
                        "permanent:message_update:"
                    )
                )
                | Q(
                    announcement_last_error__startswith=(
                        "exhausted:message_update:"
                    )
                )
            )
            .order_by("date", "pk")
            .values("id", "date", "announcement_last_error")[
                : OfficeManagerService.PENDING_DELIVERY_SWEEP_LIMIT
            ]
        )
        return [
            {
                "day_id": row["id"],
                "date": row["date"].isoformat(),
                "error": row["announcement_last_error"],
            }
            for row in rows
        ]

    @staticmethod
    def unresolved_assignment_delivery_dead_letters() -> list[dict]:
        """Return terminal winner/reminder delivery failures for alerting."""
        terminal = Q()
        error_fields = (
            "winner_channel_announcement_last_error",
            "winner_dm_last_error",
            "end_of_day_reminder_last_error",
            "private_correction_last_error",
        )
        for error_field in error_fields:
            for prefix in TERMINAL_DELIVERY_ERROR_PREFIXES:
                terminal |= Q(**{f"{error_field}__startswith": prefix})
        rows = (
            OfficeManagerAssignment.objects.filter(terminal)
            .select_related("day")
            .order_by("updated_at", "pk")
            .values(
                "id",
                "day__date",
                *error_fields,
            )[: OfficeManagerService.PENDING_DELIVERY_SWEEP_LIMIT]
        )
        return [
            {
                "assignment_id": row["id"],
                "date": row["day__date"].isoformat(),
                "winner_channel_error": row[
                    "winner_channel_announcement_last_error"
                ],
                "winner_dm_error": row["winner_dm_last_error"],
                "end_of_day_error": row[
                    "end_of_day_reminder_last_error"
                ],
                "private_correction_error": row[
                    "private_correction_last_error"
                ],
            }
            for row in rows
        ]

    @staticmethod
    def deliver_winner_channel_announcement(
        assignment_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        existing = OfficeManagerAssignment.objects.get(pk=assignment_id)
        if (
            not existing.winner_channel_message_ts
            and existing.winner_channel_announcement_status
            in {"sending", "unknown"}
            and _coordinate_recovery_is_due(
                status=existing.winner_channel_announcement_status,
                error_value=existing.winner_channel_announcement_last_error,
                next_attempt_at=(
                    existing.winner_channel_announcement_next_attempt_at
                ),
                attempt_count=(
                    existing.winner_channel_announcement_attempt_count
                ),
                legacy_updated_at=existing.updated_at,
            )
        ):
            recovered = (
                OfficeManagerService.recover_winner_channel_coordinates(
                    assignment_id
                )
            )
            if recovered is not None:
                return recovered
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("day", "user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                return False
            if assignment.winner_channel_announcement_status == "sent":
                return True
            if not _delivery_attempt_is_due(
                status=assignment.winner_channel_announcement_status,
                error_value=assignment.winner_channel_announcement_last_error,
                next_attempt_at=(
                    assignment.winner_channel_announcement_next_attempt_at
                ),
                attempt_count=(
                    assignment.winner_channel_announcement_attempt_count
                ),
                legacy_updated_at=assignment.updated_at,
            ):
                return False
            assignment.winner_channel_announcement_status = "sending"
            assignment.winner_channel_announcement_last_error = lease_token
            assignment.winner_channel_announcement_attempt_count += 1
            assignment.winner_channel_announcement_next_attempt_at = None
            assignment.save(
                update_fields=[
                    "winner_channel_announcement_status",
                    "winner_channel_announcement_last_error",
                    "winner_channel_announcement_attempt_count",
                    "winner_channel_announcement_next_attempt_at",
                    "updated_at",
                ]
            )

        try:
            assignment = (
                OfficeManagerAssignment.objects.select_related("day", "user")
                .get(pk=assignment_id)
            )
            if (
                assignment.winner_channel_announcement_status != "sending"
                or assignment.winner_channel_announcement_last_error
                != lease_token
            ):
                return assignment.winner_channel_announcement_status == "sent"
            if assignment.status != "active":
                OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    winner_channel_announcement_status="sending",
                    winner_channel_announcement_last_error=lease_token,
                ).update(
                    winner_channel_announcement_status="failed",
                    winner_channel_announcement_last_error=(
                        RELINQUISHED_DELIVERY_ERROR
                    ),
                    winner_channel_announcement_next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                return False
            if assignment.day.date != _local_now(now).date():
                _terminalize_delivery_lease(
                    OfficeManagerAssignment,
                    assignment_id,
                    status_field="winner_channel_announcement_status",
                    error_field="winner_channel_announcement_last_error",
                    next_attempt_field=(
                        "winner_channel_announcement_next_attempt_at"
                    ),
                    lease_token=lease_token,
                    error=EXPIRED_DELIVERY_ERROR,
                )
                return False
            response = _office_manager_slack_client().chat_postMessage(
                channel=assignment.day.slack_channel_id,
                text=_winner_channel_announcement_text(assignment),
                client_msg_id=_winner_channel_client_msg_id(assignment),
                unfurl_links=False,
                unfurl_media=False,
            )
            message_ts = str(response.get("ts") or "")
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
            if not message_ts:
                raise OfficeManagerDeliveryCoordinateUnknown(
                    "chat.postMessage accepted without ts"
                )
            post_send_local_date = _local_now(now).date()
            with transaction.atomic():
                assignment = (
                    OfficeManagerAssignment.objects.select_for_update(of=("self",))
                    .select_related("day", "user")
                    .get(pk=assignment_id)
                )
                if (
                    assignment.winner_channel_announcement_status != "sending"
                    or assignment.winner_channel_announcement_last_error
                    != lease_token
                ):
                    return (
                        assignment.winner_channel_announcement_status == "sent"
                    )
                assignment.winner_channel_announcement_status = "sent"
                assignment.winner_channel_announcement_sent_at = timezone.now()
                assignment.winner_channel_message_ts = message_ts
                assignment.winner_channel_announcement_last_error = ""
                assignment.winner_channel_announcement_next_attempt_at = None
                if (
                    assignment.status != "active"
                    or assignment.day.date != post_send_local_date
                ):
                    assignment.winner_channel_retraction_pending = True
                    assignment.winner_channel_retraction_status = "pending"
                    assignment.winner_channel_retraction_next_attempt_at = (
                        timezone.now()
                    )
                assignment.save(
                    update_fields=[
                        "winner_channel_announcement_status",
                        "winner_channel_announcement_sent_at",
                        "winner_channel_message_ts",
                        "winner_channel_announcement_last_error",
                        "winner_channel_announcement_next_attempt_at",
                        "winner_channel_retraction_pending",
                        "winner_channel_retraction_status",
                        "winner_channel_retraction_next_attempt_at",
                        "updated_at",
                    ]
                )
        except OfficeManagerConfigurationError as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_channel_announcement_status",
                error_field="winner_channel_announcement_last_error",
                attempt_count_field=(
                    "winner_channel_announcement_attempt_count"
                ),
                next_attempt_field=(
                    "winner_channel_announcement_next_attempt_at"
                ),
                lease_token=lease_token,
                exc=exc,
                uncertain=False,
            )
            return False
        except SlackApiError as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_channel_announcement_status",
                error_field="winner_channel_announcement_last_error",
                attempt_count_field=(
                    "winner_channel_announcement_attempt_count"
                ),
                next_attempt_field=(
                    "winner_channel_announcement_next_attempt_at"
                ),
                lease_token=lease_token,
                exc=exc,
                uncertain=_slack_failure_is_transient(exc),
                preserve_coordinate_recovery_at_exhaustion=True,
            )
            return False
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_channel_announcement_status",
                error_field="winner_channel_announcement_last_error",
                attempt_count_field=(
                    "winner_channel_announcement_attempt_count"
                ),
                next_attempt_field=(
                    "winner_channel_announcement_next_attempt_at"
                ),
                lease_token=lease_token,
                exc=exc,
                uncertain=True,
                preserve_coordinate_recovery_at_exhaustion=True,
            )
            return False

        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("day", "user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                assignment.winner_channel_retraction_pending = True
            assignment.save(
                update_fields=[
                    "winner_channel_retraction_pending",
                    "updated_at",
                ]
            )
            should_retract = assignment.winner_channel_retraction_pending

        if should_retract:
            return OfficeManagerService.retract_winner_channel_announcement(
                assignment_id
            )
        return True

    @staticmethod
    def retract_winner_channel_announcement(assignment_id: int) -> bool:
        lease_token = _retraction_lease_token()
        now = timezone.now()
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("day", "user")
                .get(pk=assignment_id)
            )
            if not assignment.winner_channel_retraction_pending:
                return assignment.winner_channel_retraction_status in {
                    "not_required",
                    "sent",
                }
            if (
                assignment.winner_channel_retraction_next_attempt_at
                and assignment.winner_channel_retraction_next_attempt_at > now
            ):
                return False
            if (
                assignment.winner_channel_retraction_status == "sending"
                and assignment.updated_at
                >= now
                - timedelta(
                    seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
                )
            ):
                return False
            if (
                assignment.winner_channel_retraction_attempt_count
                >= OfficeManagerService.RETRACTION_MAX_ATTEMPTS
            ):
                assignment.winner_channel_retraction_pending = False
                assignment.winner_channel_retraction_status = "exhausted"
                assignment.winner_channel_retraction_last_error = (
                    assignment.winner_channel_retraction_last_error
                    or "retry_budget_exhausted"
                )
                assignment.winner_channel_retraction_lease_token = ""
                assignment.save(
                    update_fields=[
                        "winner_channel_retraction_pending",
                        "winner_channel_retraction_status",
                        "winner_channel_retraction_last_error",
                        "winner_channel_retraction_lease_token",
                        "updated_at",
                    ]
                )
                return False
            assignment.winner_channel_retraction_status = "sending"
            assignment.winner_channel_retraction_lease_token = lease_token
            assignment.winner_channel_retraction_attempt_count += 1
            assignment.winner_channel_retraction_next_attempt_at = None
            assignment.save(
                update_fields=[
                    "winner_channel_retraction_status",
                    "winner_channel_retraction_lease_token",
                    "winner_channel_retraction_attempt_count",
                    "winner_channel_retraction_next_attempt_at",
                    "updated_at",
                ]
            )
            channel_id = assignment.day.slack_channel_id
            message_ts = assignment.winner_channel_message_ts

        try:
            # Never create or re-create a winner announcement merely to retract
            # it. Recover an accepted response-loss post by its deterministic
            # client_msg_id before deciding that an operator must intervene.
            slack_client = _office_manager_slack_client()
            if not message_ts:
                recovered = (
                    OfficeManagerService.recover_winner_channel_coordinates(
                        assignment_id,
                        for_retraction=True,
                    )
                )
                if recovered is False:
                    raise RuntimeError("winner_message_coordinate_lookup_failed")
                if recovered is None:
                    raise RuntimeError("winner_message_coordinates_not_observable")
                message_ts = str(
                    OfficeManagerAssignment.objects.filter(pk=assignment_id)
                    .values_list("winner_channel_message_ts", flat=True)
                    .get()
                )

            # Re-read the authoritative day and assignment after leasing and
            # immediately before publishing. The copy deliberately makes no
            # stale "open again" claim if a replacement has since won.
            with transaction.atomic():
                current = (
                    OfficeManagerAssignment.objects.select_for_update(of=("self",))
                    .select_related("day", "user")
                    .get(pk=assignment_id)
                )
                if (
                    current.winner_channel_retraction_status != "sending"
                    or current.winner_channel_retraction_lease_token
                    != lease_token
                ):
                    return current.winner_channel_retraction_status == "sent"
                current_local_date = _local_now().date()
                if (
                    current.status != "relinquished"
                    and current.day.date == current_local_date
                ):
                    current.winner_channel_retraction_pending = False
                    current.winner_channel_retraction_status = "not_required"
                    current.winner_channel_retraction_lease_token = ""
                    current.save(
                        update_fields=[
                            "winner_channel_retraction_pending",
                            "winner_channel_retraction_status",
                            "winner_channel_retraction_lease_token",
                            "updated_at",
                        ]
                    )
                    return True
                text = (
                    _relinquished_winner_channel_text(current)
                    if current.status == "relinquished"
                    else _expired_winner_channel_text(current)
                )

            response = slack_client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=text,
            )
            if not response.get("ok", True):
                raise SlackApiError("chat.update failed", response)
        except Exception as exc:
            error = _safe_slack_error(exc)
            if error == "message_not_found":
                updated = OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    winner_channel_retraction_status="sending",
                    winner_channel_retraction_lease_token=lease_token,
                ).update(
                    winner_channel_retraction_pending=False,
                    winner_channel_retraction_status="sent",
                    winner_channel_retraction_last_error="",
                    winner_channel_retraction_lease_token="",
                    winner_channel_retraction_next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                return updated == 1

            transient = _slack_failure_is_transient(exc)
            attempt_count = (
                OfficeManagerAssignment.objects.filter(pk=assignment_id)
                .values_list(
                    "winner_channel_retraction_attempt_count",
                    flat=True,
                )
                .first()
                or 0
            )
            exhausted = (
                not transient
                or attempt_count
                >= OfficeManagerService.RETRACTION_MAX_ATTEMPTS
            )
            backoff_seconds = min(
                OfficeManagerService.RETRACTION_RETRY_BASE_SECONDS
                * (2 ** max(attempt_count - 1, 0)),
                OfficeManagerService.RETRACTION_RETRY_MAX_SECONDS,
            )
            OfficeManagerAssignment.objects.filter(
                pk=assignment_id,
                winner_channel_retraction_status="sending",
                winner_channel_retraction_lease_token=lease_token,
            ).update(
                winner_channel_retraction_pending=not exhausted,
                winner_channel_retraction_status=(
                    "exhausted" if exhausted else "failed"
                ),
                winner_channel_retraction_last_error=error,
                winner_channel_retraction_lease_token="",
                winner_channel_retraction_next_attempt_at=(
                    None
                    if exhausted
                    else timezone.now() + timedelta(seconds=backoff_seconds)
                ),
                updated_at=timezone.now(),
            )
            return False

        updated = OfficeManagerAssignment.objects.filter(
            pk=assignment_id,
            winner_channel_retraction_status="sending",
            winner_channel_retraction_lease_token=lease_token,
        ).update(
            winner_channel_retraction_pending=False,
            winner_channel_retraction_status="sent",
            winner_channel_retraction_last_error="",
            winner_channel_retraction_lease_token="",
            winner_channel_retraction_next_attempt_at=None,
            updated_at=timezone.now(),
        )
        return updated == 1

    @staticmethod
    def retry_pending_winner_retractions(
        *,
        limit: int | None = None,
    ) -> list[bool]:
        sweep_limit = (
            OfficeManagerService.PENDING_RETRACTION_SWEEP_LIMIT
            if limit is None
            else max(0, int(limit))
        )
        lease_cutoff = timezone.now() - timedelta(
            seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
        )
        OfficeManagerAssignment.objects.filter(
            winner_channel_retraction_pending=True,
            winner_channel_retraction_status="sending",
            winner_channel_retraction_attempt_count__gte=(
                OfficeManagerService.RETRACTION_MAX_ATTEMPTS
            ),
            updated_at__lt=lease_cutoff,
        ).update(
            winner_channel_retraction_pending=False,
            winner_channel_retraction_status="exhausted",
            winner_channel_retraction_last_error="worker_lease_expired",
            winner_channel_retraction_lease_token="",
            winner_channel_retraction_next_attempt_at=None,
            updated_at=timezone.now(),
        )
        assignment_ids = list(
            OfficeManagerAssignment.objects.filter(
                winner_channel_retraction_pending=True,
                winner_channel_retraction_attempt_count__lt=(
                    OfficeManagerService.RETRACTION_MAX_ATTEMPTS
                ),
            )
            .filter(
                Q(winner_channel_retraction_next_attempt_at__isnull=True)
                | Q(winner_channel_retraction_next_attempt_at__lte=timezone.now())
            )
            .exclude(
                winner_channel_retraction_status="sending",
                updated_at__gte=lease_cutoff,
            )
            .order_by("updated_at", "pk")
            .values_list("pk", flat=True)[:sweep_limit]
        )
        return [
            OfficeManagerService.retract_winner_channel_announcement(
                assignment_id
            )
            for assignment_id in assignment_ids
        ]

    @staticmethod
    def unresolved_winner_retraction_dead_letters() -> list[dict]:
        """Keep terminal public-message repair failures visible every tick."""
        return list(
            OfficeManagerAssignment.objects.filter(
                winner_channel_retraction_status="exhausted",
            )
            .order_by("updated_at", "pk")
            .values(
                "id",
                "day_id",
                "winner_channel_retraction_last_error",
                "winner_channel_retraction_attempt_count",
            )[: OfficeManagerService.PENDING_RETRACTION_SWEEP_LIMIT]
        )

    @staticmethod
    def retry_pending_deliveries(
        *,
        now: datetime | None = None,
        limit: int | None = None,
        allow_new_announcements: bool = True,
    ) -> dict[str, dict[int, bool]]:
        """Recover or explicitly expire durable deliveries across date gates."""
        local_now = _local_now(now)
        local_date = local_now.date()
        sweep_limit = (
            OfficeManagerService.PENDING_DELIVERY_SWEEP_LIMIT
            if limit is None
            else max(0, int(limit))
        )
        recovered: dict[str, dict[int, bool]] = {
            "announcement": {},
            "message_update": {},
            "winner_channel": {},
            "winner_dm": {},
            "end_of_day": {},
            "private_correction": {},
        }
        if sweep_limit == 0:
            return recovered
        _terminalize_expired_max_delivery_leases()

        announcement_time = _setting_time(
            "OFFICE_MANAGER_ANNOUNCEMENT_HOUR",
            "OFFICE_MANAGER_ANNOUNCEMENT_MINUTE",
            8,
            30,
        )
        day_candidates = (
            OfficeManagerDay.objects.filter(
                date__lte=local_date,
            )
            .filter(
                _retryable_delivery_q(
                    status_field="announcement_status",
                    error_field="announcement_last_error",
                )
            )
            .exclude(announcement_last_error=CLOSED_DELIVERY_ERROR)
            .order_by("date", "pk")
            .values_list("pk", flat=True)
        )
        day_ids = []
        for day_id in day_candidates.iterator(chunk_size=max(100, sweep_limit)):
            candidate = OfficeManagerDay.objects.get(pk=day_id)
            announcement_due = _delivery_attempt_is_due(
                status=candidate.announcement_status,
                error_value=candidate.announcement_last_error,
                next_attempt_at=candidate.announcement_next_attempt_at,
                attempt_count=candidate.announcement_attempt_count,
                legacy_updated_at=candidate.updated_at,
            ) or (
                not candidate.slack_message_ts
                and _coordinate_recovery_is_due(
                    status=candidate.announcement_status,
                    error_value=candidate.announcement_last_error,
                    next_attempt_at=candidate.announcement_next_attempt_at,
                    attempt_count=candidate.announcement_attempt_count,
                    legacy_updated_at=candidate.updated_at,
                )
            )
            if not announcement_due:
                continue
            day_ids.append(day_id)
            if len(day_ids) >= sweep_limit:
                break
        for day_id in day_ids:
            day = OfficeManagerDay.objects.get(pk=day_id)
            announcement_due = _delivery_attempt_is_due(
                status=day.announcement_status,
                error_value=day.announcement_last_error,
                next_attempt_at=day.announcement_next_attempt_at,
                attempt_count=day.announcement_attempt_count,
                legacy_updated_at=day.updated_at,
            ) or (
                not day.slack_message_ts
                and _coordinate_recovery_is_due(
                    status=day.announcement_status,
                    error_value=day.announcement_last_error,
                    next_attempt_at=day.announcement_next_attempt_at,
                    attempt_count=day.announcement_attempt_count,
                    legacy_updated_at=day.updated_at,
                )
            )
            if not announcement_due:
                # Another request/worker owns a live lease, or this row is in
                # backoff. It is not a failed delivery and must not make the
                # shared scheduler exit nonzero.
                continue
            if day.date < local_date:
                if (
                    not day.slack_message_ts
                    and day.announcement_status in {"sending", "unknown"}
                ):
                    coordinate_recovery = (
                        OfficeManagerService.recover_announcement_coordinates(
                            day_id
                        )
                    )
                    if coordinate_recovery is False:
                        recovered["announcement"][day_id] = False
                        day.refresh_from_db()
                        if (
                            day.announcement_status in {"sending", "unknown"}
                            or _delivery_error_is_terminal(
                                day.announcement_last_error
                            )
                        ):
                            continue
                    if coordinate_recovery is True:
                        OfficeManagerDay.objects.filter(pk=day_id).update(
                            status="closed",
                            closed_at=timezone.now(),
                            message_update_pending=True,
                            updated_at=timezone.now(),
                        )
                        recovered["announcement"][day_id] = (
                            OfficeManagerService.reconcile_message(day_id)
                        )
                        continue
                OfficeManagerDay.objects.filter(
                    pk=day_id,
                    announcement_status__in=RETRYABLE_DELIVERY_STATUSES,
                ).update(
                    announcement_status="failed",
                    announcement_last_error=EXPIRED_DELIVERY_ERROR,
                    updated_at=timezone.now(),
                )
                recovered["announcement"][day_id] = False
                continue
            if day.date > local_date:
                continue
            cutoff_passed = local_now >= day.claim_cutoff_at.astimezone(
                _timezone()
            )
            if day.status == "closed" or (
                day.status == "open" and cutoff_passed
            ):
                OfficeManagerDay.objects.filter(
                    pk=day_id,
                    announcement_status__in=RETRYABLE_DELIVERY_STATUSES,
                ).update(
                    announcement_status="failed",
                    announcement_last_error=CLOSED_DELIVERY_ERROR,
                    updated_at=timezone.now(),
                )
                recovered["announcement"][day_id] = False
                continue
            if local_now.time().replace(tzinfo=None) >= announcement_time:
                if allow_new_announcements:
                    recovered["announcement"][day_id] = (
                        OfficeManagerService.post_announcement(
                            day_id,
                            now=now,
                        )
                    )

        update_candidates = (
            OfficeManagerDay.objects.filter(
                date__lte=local_date,
                announcement_status="sent",
                message_update_pending=True,
            )
            .exclude(slack_message_ts="")
            .exclude(
                Q(
                    announcement_last_error__startswith=(
                        "permanent:message_update:"
                    )
                )
                | Q(
                    announcement_last_error__startswith=(
                        "exhausted:message_update:"
                    )
                )
            )
            .order_by("date", "pk")
        )
        if allow_new_announcements:
            # Enabled-mode processing below owns the current-day update and
            # preserves its public scheduler result contract. This sweep is
            # specifically the rollback/disabled recovery path.
            update_candidates = OfficeManagerDay.objects.none()
        updated_count = 0
        for candidate in update_candidates.iterator(
            chunk_size=max(100, sweep_limit)
        ):
            if not _message_update_attempt_is_due(candidate):
                continue
            recovered["message_update"][candidate.pk] = (
                OfficeManagerService.reconcile_message(candidate.pk)
            )
            updated_count += 1
            if updated_count >= sweep_limit:
                break

        reminder_time = _setting_time(
            "OFFICE_MANAGER_END_OF_DAY_REMINDER_HOUR",
            "OFFICE_MANAGER_END_OF_DAY_REMINDER_MINUTE",
            16,
            30,
        )
        delivery_fields = (
            (
                "winner_channel_announcement_status",
                "winner_channel_announcement_last_error",
                "winner_channel_announcement_next_attempt_at",
                "winner_channel_announcement_attempt_count",
                "winner_channel",
            ),
            (
                "winner_dm_status",
                "winner_dm_last_error",
                "winner_dm_next_attempt_at",
                "winner_dm_attempt_count",
                "winner_dm",
            ),
            (
                "end_of_day_reminder_status",
                "end_of_day_reminder_last_error",
                "end_of_day_reminder_next_attempt_at",
                "end_of_day_reminder_attempt_count",
                "end_of_day",
            ),
        )
        assignment_candidates = (
            OfficeManagerAssignment.objects.filter(
                day__date__lte=local_date,
            )
            .filter(
                _retryable_delivery_q(
                    status_field="winner_channel_announcement_status",
                    error_field="winner_channel_announcement_last_error",
                )
                | _retryable_delivery_q(
                    status_field="winner_dm_status",
                    error_field="winner_dm_last_error",
                )
                | _retryable_delivery_q(
                    status_field="end_of_day_reminder_status",
                    error_field="end_of_day_reminder_last_error",
                )
            )
            .select_related("day")
            .order_by("day__date", "pk")
        )
        assignments = []
        for candidate in assignment_candidates.iterator(
            chunk_size=max(100, sweep_limit)
        ):
            terminal_transition = (
                candidate.day.date < local_date
                or candidate.status != "active"
            )
            has_due_delivery = False
            for (
                status_field,
                error_field,
                next_attempt_field,
                attempt_count_field,
                result_key,
            ) in delivery_fields:
                if (
                    result_key == "end_of_day"
                    and not terminal_transition
                    and local_now.time().replace(tzinfo=None) < reminder_time
                ):
                    continue
                if getattr(candidate, status_field) not in (
                    RETRYABLE_DELIVERY_STATUSES
                ):
                    continue
                delivery_due = _delivery_attempt_is_due(
                    status=getattr(candidate, status_field),
                    error_value=getattr(candidate, error_field),
                    next_attempt_at=getattr(candidate, next_attempt_field),
                    attempt_count=getattr(candidate, attempt_count_field),
                    legacy_updated_at=candidate.updated_at,
                )
                if (
                    result_key == "winner_channel"
                    and not candidate.winner_channel_message_ts
                ):
                    delivery_due = delivery_due or _coordinate_recovery_is_due(
                        status=getattr(candidate, status_field),
                        error_value=getattr(candidate, error_field),
                        next_attempt_at=getattr(candidate, next_attempt_field),
                        attempt_count=getattr(candidate, attempt_count_field),
                        legacy_updated_at=candidate.updated_at,
                    )
                if delivery_due:
                    has_due_delivery = True
                    break
            if not has_due_delivery:
                continue
            assignments.append(candidate)
            if len(assignments) >= sweep_limit:
                break
        for assignment in assignments:
            defer_winner_terminal = False
            if (
                assignment.day.date < local_date
                and not assignment.winner_channel_message_ts
                and assignment.winner_channel_announcement_status
                in {"sending", "unknown"}
                and _coordinate_recovery_is_due(
                    status=assignment.winner_channel_announcement_status,
                    error_value=(
                        assignment.winner_channel_announcement_last_error
                    ),
                    next_attempt_at=(
                        assignment.winner_channel_announcement_next_attempt_at
                    ),
                    attempt_count=(
                        assignment.winner_channel_announcement_attempt_count
                    ),
                    legacy_updated_at=assignment.updated_at,
                )
            ):
                coordinate_recovery = (
                    OfficeManagerService.recover_winner_channel_coordinates(
                        assignment.pk
                    )
                )
                if coordinate_recovery is False:
                    assignment.refresh_from_db()
                    # Preserve a durable unknown state when Slack history was
                    # unavailable. A definitive not-found becomes pending and
                    # may be expired immediately because no public post exists.
                    defer_winner_terminal = (
                        assignment.winner_channel_announcement_status
                        in {"sending", "unknown"}
                        or _delivery_error_is_terminal(
                            assignment.winner_channel_announcement_last_error
                        )
                    )
                elif coordinate_recovery is True:
                    assignment.refresh_from_db()
                    if assignment.winner_channel_retraction_pending:
                        recovered["winner_channel"][assignment.pk] = (
                            OfficeManagerService.retract_winner_channel_announcement(
                                assignment.pk
                            )
                        )
                        assignment.refresh_from_db()
            terminal_error = ""
            if assignment.day.date < local_date:
                terminal_error = EXPIRED_DELIVERY_ERROR
            elif assignment.status != "active":
                terminal_error = RELINQUISHED_DELIVERY_ERROR
            if terminal_error:
                for (
                    status_field,
                    error_field,
                    next_attempt_field,
                    attempt_count_field,
                    result_key,
                ) in delivery_fields:
                    if (
                        result_key == "winner_channel"
                        and defer_winner_terminal
                    ):
                        recovered[result_key][assignment.pk] = False
                        continue
                    if getattr(assignment, status_field) not in (
                        RETRYABLE_DELIVERY_STATUSES
                    ) or getattr(assignment, error_field) in (
                        TERMINAL_ASSIGNMENT_DELIVERY_ERRORS
                    ):
                        continue
                    if _delivery_lease_is_live(
                        status=getattr(assignment, status_field),
                        error_value=getattr(assignment, error_field),
                        legacy_updated_at=assignment.updated_at,
                    ):
                        continue
                    model_filter = {
                        "pk": assignment.pk,
                        f"{status_field}__in": RETRYABLE_DELIVERY_STATUSES,
                    }
                    OfficeManagerAssignment.objects.filter(
                        **model_filter
                    ).update(
                        **{
                            status_field: "failed",
                            error_field: terminal_error,
                            "updated_at": timezone.now(),
                        }
                    )
                    recovered[result_key][assignment.pk] = False
                continue
            if assignment.day.date > local_date:
                continue
            if (
                assignment.winner_channel_announcement_status
                in RETRYABLE_DELIVERY_STATUSES
                and (
                    _delivery_attempt_is_due(
                        status=assignment.winner_channel_announcement_status,
                        error_value=(
                            assignment.winner_channel_announcement_last_error
                        ),
                        next_attempt_at=(
                            assignment.winner_channel_announcement_next_attempt_at
                        ),
                        attempt_count=(
                            assignment.winner_channel_announcement_attempt_count
                        ),
                        legacy_updated_at=assignment.updated_at,
                    )
                    or (
                        not assignment.winner_channel_message_ts
                        and _coordinate_recovery_is_due(
                            status=(
                                assignment.winner_channel_announcement_status
                            ),
                            error_value=(
                                assignment.winner_channel_announcement_last_error
                            ),
                            next_attempt_at=(
                                assignment.winner_channel_announcement_next_attempt_at
                            ),
                            attempt_count=(
                                assignment.winner_channel_announcement_attempt_count
                            ),
                            legacy_updated_at=assignment.updated_at,
                        )
                    )
                )
            ):
                recovered["winner_channel"][assignment.pk] = (
                    OfficeManagerService.deliver_winner_channel_announcement(
                        assignment.pk,
                        now=now,
                    )
                )
            if (
                assignment.winner_dm_status in RETRYABLE_DELIVERY_STATUSES
                and _delivery_attempt_is_due(
                    status=assignment.winner_dm_status,
                    error_value=assignment.winner_dm_last_error,
                    next_attempt_at=assignment.winner_dm_next_attempt_at,
                    attempt_count=assignment.winner_dm_attempt_count,
                    legacy_updated_at=assignment.updated_at,
                )
            ):
                recovered["winner_dm"][assignment.pk] = (
                    OfficeManagerService.deliver_winner_dm(
                        assignment.pk,
                        now=now,
                    )
                )
            if (
                local_now.time().replace(tzinfo=None) >= reminder_time
                and assignment.end_of_day_reminder_status
                in RETRYABLE_DELIVERY_STATUSES
                and _delivery_attempt_is_due(
                    status=assignment.end_of_day_reminder_status,
                    error_value=assignment.end_of_day_reminder_last_error,
                    next_attempt_at=(
                        assignment.end_of_day_reminder_next_attempt_at
                    ),
                    attempt_count=(
                        assignment.end_of_day_reminder_attempt_count
                    ),
                    legacy_updated_at=assignment.updated_at,
                )
            ):
                recovered["end_of_day"][assignment.pk] = (
                    OfficeManagerService.deliver_end_of_day_reminder(
                        assignment.pk,
                        now=now,
                    )
                )

        correction_candidates = (
            OfficeManagerAssignment.objects.filter(
                private_correction_pending=True,
            )
            .filter(
                _retryable_delivery_q(
                    status_field="private_correction_status",
                    error_field="private_correction_last_error",
                )
            )
            .order_by("updated_at", "pk")
        )
        correction_count = 0
        for candidate in correction_candidates.iterator(
            chunk_size=max(100, sweep_limit)
        ):
            if not _delivery_attempt_is_due(
                status=candidate.private_correction_status,
                error_value=candidate.private_correction_last_error,
                next_attempt_at=candidate.private_correction_next_attempt_at,
                attempt_count=candidate.private_correction_attempt_count,
                legacy_updated_at=candidate.updated_at,
            ):
                continue
            source_live = any(
                _delivery_lease_is_live(
                    status=status,
                    error_value=error,
                    legacy_updated_at=candidate.updated_at,
                )
                for status, error in (
                    (
                        candidate.winner_dm_status,
                        candidate.winner_dm_last_error,
                    ),
                    (
                        candidate.end_of_day_reminder_status,
                        candidate.end_of_day_reminder_last_error,
                    ),
                )
            )
            if source_live:
                continue
            recovered["private_correction"][candidate.pk] = (
                OfficeManagerService.deliver_private_correction(candidate.pk)
            )
            correction_count += 1
            if correction_count >= sweep_limit:
                break

        return recovered

    @staticmethod
    def deliver_winner_dm(
        assignment_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                return False
            if assignment.winner_dm_status == "sent":
                return True
            if not _delivery_attempt_is_due(
                status=assignment.winner_dm_status,
                error_value=assignment.winner_dm_last_error,
                next_attempt_at=assignment.winner_dm_next_attempt_at,
                attempt_count=assignment.winner_dm_attempt_count,
                legacy_updated_at=assignment.updated_at,
            ):
                return False
            assignment.winner_dm_status = "sending"
            assignment.winner_dm_last_error = lease_token
            assignment.winner_dm_attempt_count += 1
            assignment.winner_dm_next_attempt_at = None
            assignment.save(
                update_fields=[
                    "winner_dm_status",
                    "winner_dm_last_error",
                    "winner_dm_attempt_count",
                    "winner_dm_next_attempt_at",
                    "updated_at",
                ]
            )

        try:
            slack_client = _office_manager_slack_client()
            dm_channel = _open_dm_channel(
                slack_client,
                assignment.user.slack_id,
            )
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_dm_status",
                error_field="winner_dm_last_error",
                attempt_count_field="winner_dm_attempt_count",
                next_attempt_field="winner_dm_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=False,
            )
            return False

        try:
            assignment = OfficeManagerAssignment.objects.select_related(
                "day", "user"
            ).get(pk=assignment_id)
            if (
                assignment.winner_dm_status != "sending"
                or assignment.winner_dm_last_error != lease_token
            ):
                return assignment.winner_dm_status == "sent"
            if assignment.status != "active":
                OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    winner_dm_status="sending",
                    winner_dm_last_error=lease_token,
                ).update(
                    winner_dm_status="failed",
                    winner_dm_last_error=RELINQUISHED_DELIVERY_ERROR,
                    winner_dm_next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                return False
            if assignment.day.date != _local_now(now).date():
                _terminalize_delivery_lease(
                    OfficeManagerAssignment,
                    assignment_id,
                    status_field="winner_dm_status",
                    error_field="winner_dm_last_error",
                    next_attempt_field="winner_dm_next_attempt_at",
                    lease_token=lease_token,
                    error=EXPIRED_DELIVERY_ERROR,
                )
                return False
            response = slack_client.chat_postMessage(
                channel=dm_channel,
                text=_winner_dm_text(assignment),
                client_msg_id=_slack_client_msg_id(
                    "winner-dm",
                    assignment.id,
                ),
                unfurl_links=False,
                unfurl_media=False,
            )
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
            message_ts = str(response.get("ts") or "")
            if not message_ts:
                raise OfficeManagerDeliveryCoordinateUnknown(
                    "chat.postMessage accepted without ts"
                )
            post_send_local_date = _local_now(now).date()
            with transaction.atomic():
                assignment = (
                    OfficeManagerAssignment.objects.select_for_update(
                        of=("self",)
                    )
                    .select_related("day")
                    .get(pk=assignment_id)
                )
                if (
                    assignment.winner_dm_status != "sending"
                    or assignment.winner_dm_last_error != lease_token
                ):
                    return assignment.winner_dm_status == "sent"
                assignment.winner_dm_status = "sent"
                assignment.winner_dm_sent_at = timezone.now()
                assignment.winner_dm_message_ts = message_ts
                assignment.winner_dm_last_error = ""
                assignment.winner_dm_next_attempt_at = None
                if (
                    assignment.status != "active"
                    or assignment.day.date != post_send_local_date
                ):
                    assignment.private_correction_pending = True
                    assignment.private_correction_status = "pending"
                    assignment.private_correction_last_error = ""
                    assignment.private_correction_next_attempt_at = timezone.now()
                assignment.save(
                    update_fields=[
                        "winner_dm_status",
                        "winner_dm_sent_at",
                        "winner_dm_message_ts",
                        "winner_dm_last_error",
                        "winner_dm_next_attempt_at",
                        "private_correction_pending",
                        "private_correction_status",
                        "private_correction_last_error",
                        "private_correction_next_attempt_at",
                        "updated_at",
                    ]
                )
            if (
                assignment.private_correction_pending
                or OfficeManagerService.queue_private_correction_if_stale(
                    assignment_id,
                    now=now,
                )
            ):
                return OfficeManagerService.deliver_private_correction(
                    assignment_id
                )
        except SlackApiError as exc:
            if _safe_slack_error(exc) == "duplicate_message":
                updated = OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    winner_dm_status="sending",
                    winner_dm_last_error=lease_token,
                ).update(
                    winner_dm_status="sent",
                    winner_dm_sent_at=timezone.now(),
                    winner_dm_last_error="",
                    winner_dm_next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                if updated == 1:
                    if OfficeManagerService.queue_private_correction_if_stale(
                        assignment_id,
                        now=now,
                    ):
                        return OfficeManagerService.deliver_private_correction(
                            assignment_id
                        )
                    return True
                return OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    winner_dm_status="sent",
                ).exists()
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_dm_status",
                error_field="winner_dm_last_error",
                attempt_count_field="winner_dm_attempt_count",
                next_attempt_field="winner_dm_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=_slack_failure_is_transient(exc),
            )
            return False
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="winner_dm_status",
                error_field="winner_dm_last_error",
                attempt_count_field="winner_dm_attempt_count",
                next_attempt_field="winner_dm_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=True,
            )
            return False
        return True

    @staticmethod
    def deliver_end_of_day_reminder(
        assignment_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                return False
            if assignment.end_of_day_reminder_status == "sent":
                return True
            if not _delivery_attempt_is_due(
                status=assignment.end_of_day_reminder_status,
                error_value=assignment.end_of_day_reminder_last_error,
                next_attempt_at=assignment.end_of_day_reminder_next_attempt_at,
                attempt_count=assignment.end_of_day_reminder_attempt_count,
                legacy_updated_at=assignment.updated_at,
            ):
                return False
            assignment.end_of_day_reminder_status = "sending"
            assignment.end_of_day_reminder_last_error = lease_token
            assignment.end_of_day_reminder_attempt_count += 1
            assignment.end_of_day_reminder_next_attempt_at = None
            assignment.save(
                update_fields=[
                    "end_of_day_reminder_status",
                    "end_of_day_reminder_last_error",
                    "end_of_day_reminder_attempt_count",
                    "end_of_day_reminder_next_attempt_at",
                    "updated_at",
                ]
            )

        try:
            slack_client = _office_manager_slack_client()
            dm_channel = _open_dm_channel(
                slack_client,
                assignment.user.slack_id,
            )
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="end_of_day_reminder_status",
                error_field="end_of_day_reminder_last_error",
                attempt_count_field="end_of_day_reminder_attempt_count",
                next_attempt_field="end_of_day_reminder_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=False,
            )
            return False

        try:
            assignment = OfficeManagerAssignment.objects.select_related(
                "day"
            ).get(pk=assignment_id)
            if (
                assignment.end_of_day_reminder_status != "sending"
                or assignment.end_of_day_reminder_last_error != lease_token
            ):
                return assignment.end_of_day_reminder_status == "sent"
            if assignment.status != "active":
                OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    end_of_day_reminder_status="sending",
                    end_of_day_reminder_last_error=lease_token,
                ).update(
                    end_of_day_reminder_status="failed",
                    end_of_day_reminder_last_error=(
                        RELINQUISHED_DELIVERY_ERROR
                    ),
                    end_of_day_reminder_next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                return False
            if assignment.day.date != _local_now(now).date():
                _terminalize_delivery_lease(
                    OfficeManagerAssignment,
                    assignment_id,
                    status_field="end_of_day_reminder_status",
                    error_field="end_of_day_reminder_last_error",
                    next_attempt_field="end_of_day_reminder_next_attempt_at",
                    lease_token=lease_token,
                    error=EXPIRED_DELIVERY_ERROR,
                )
                return False
            response = slack_client.chat_postMessage(
                channel=dm_channel,
                text=_end_of_day_dm_text(assignment),
                client_msg_id=_slack_client_msg_id(
                    "end-of-day",
                    assignment.id,
                ),
                unfurl_links=False,
                unfurl_media=False,
            )
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
            message_ts = str(response.get("ts") or "")
            if not message_ts:
                raise OfficeManagerDeliveryCoordinateUnknown(
                    "chat.postMessage accepted without ts"
                )
            post_send_local_date = _local_now(now).date()
            with transaction.atomic():
                assignment = OfficeManagerAssignment.objects.select_for_update(
                    of=("self",)
                ).select_related("day").get(pk=assignment_id)
                if (
                    assignment.end_of_day_reminder_status != "sending"
                    or assignment.end_of_day_reminder_last_error != lease_token
                ):
                    return assignment.end_of_day_reminder_status == "sent"
                assignment.end_of_day_reminder_status = "sent"
                assignment.end_of_day_reminder_sent_at = timezone.now()
                assignment.end_of_day_reminder_message_ts = message_ts
                assignment.end_of_day_reminder_last_error = ""
                assignment.end_of_day_reminder_next_attempt_at = None
                if (
                    assignment.status != "active"
                    or assignment.day.date != post_send_local_date
                ):
                    assignment.private_correction_pending = True
                    assignment.private_correction_status = "pending"
                    assignment.private_correction_last_error = ""
                    assignment.private_correction_next_attempt_at = timezone.now()
                assignment.save(
                    update_fields=[
                        "end_of_day_reminder_status",
                        "end_of_day_reminder_sent_at",
                        "end_of_day_reminder_message_ts",
                        "end_of_day_reminder_last_error",
                        "end_of_day_reminder_next_attempt_at",
                        "private_correction_pending",
                        "private_correction_status",
                        "private_correction_last_error",
                        "private_correction_next_attempt_at",
                        "updated_at",
                    ]
                )
            if (
                assignment.private_correction_pending
                or OfficeManagerService.queue_private_correction_if_stale(
                    assignment_id,
                    now=now,
                )
            ):
                return OfficeManagerService.deliver_private_correction(
                    assignment_id
                )
        except SlackApiError as exc:
            if _safe_slack_error(exc) == "duplicate_message":
                updated = OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    end_of_day_reminder_status="sending",
                    end_of_day_reminder_last_error=lease_token,
                ).update(
                    end_of_day_reminder_status="sent",
                    end_of_day_reminder_sent_at=timezone.now(),
                    end_of_day_reminder_last_error="",
                    end_of_day_reminder_next_attempt_at=None,
                    updated_at=timezone.now(),
                )
                if updated == 1:
                    if OfficeManagerService.queue_private_correction_if_stale(
                        assignment_id,
                        now=now,
                    ):
                        return OfficeManagerService.deliver_private_correction(
                            assignment_id
                        )
                    return True
                return OfficeManagerAssignment.objects.filter(
                    pk=assignment_id,
                    end_of_day_reminder_status="sent",
                ).exists()
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="end_of_day_reminder_status",
                error_field="end_of_day_reminder_last_error",
                attempt_count_field="end_of_day_reminder_attempt_count",
                next_attempt_field="end_of_day_reminder_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=_slack_failure_is_transient(exc),
            )
            return False
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="end_of_day_reminder_status",
                error_field="end_of_day_reminder_last_error",
                attempt_count_field="end_of_day_reminder_attempt_count",
                next_attempt_field="end_of_day_reminder_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=True,
            )
            return False
        return True

    @staticmethod
    def queue_private_correction_if_stale(
        assignment_id: int,
        *,
        now: datetime | None = None,
    ) -> bool:
        return bool(
            OfficeManagerAssignment.objects.filter(
                pk=assignment_id,
            )
            .filter(
                Q(status="relinquished")
                | ~Q(day__date=_local_now(now).date())
            ).update(
                private_correction_pending=True,
                private_correction_status="pending",
                private_correction_last_error="",
                private_correction_next_attempt_at=timezone.now(),
                updated_at=timezone.now(),
            )
        )

    @staticmethod
    def deliver_private_correction(assignment_id: int) -> bool:
        """Replace any stale private winner/reminder content after cancellation."""
        lease_token = _delivery_lease_token()
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update(of=("self",))
                .select_related("user")
                .get(pk=assignment_id)
            )
            if not assignment.private_correction_pending:
                return assignment.private_correction_status == "sent"
            for status, error in (
                (assignment.winner_dm_status, assignment.winner_dm_last_error),
                (
                    assignment.end_of_day_reminder_status,
                    assignment.end_of_day_reminder_last_error,
                ),
            ):
                if _delivery_lease_is_live(
                    status=status,
                    error_value=error,
                    legacy_updated_at=assignment.updated_at,
                ):
                    return False
            if not _delivery_attempt_is_due(
                status=assignment.private_correction_status,
                error_value=assignment.private_correction_last_error,
                next_attempt_at=assignment.private_correction_next_attempt_at,
                attempt_count=assignment.private_correction_attempt_count,
                legacy_updated_at=assignment.updated_at,
            ):
                return False
            assignment.private_correction_status = "sending"
            assignment.private_correction_last_error = lease_token
            assignment.private_correction_attempt_count += 1
            assignment.private_correction_next_attempt_at = None
            assignment.save(
                update_fields=[
                    "private_correction_status",
                    "private_correction_last_error",
                    "private_correction_attempt_count",
                    "private_correction_next_attempt_at",
                    "updated_at",
                ]
            )

        try:
            slack_client = _office_manager_slack_client()
            dm_channel = _open_dm_channel(slack_client, assignment.user.slack_id)
            assignment.refresh_from_db()
            oldest = assignment.claimed_at - timedelta(minutes=10)
            coordinates = {
                str(assignment.winner_dm_message_ts or ""),
                str(assignment.end_of_day_reminder_message_ts or ""),
            }
            for kind in ("winner-dm", "end-of-day"):
                message_ts = _find_message_ts_by_client_msg_id(
                    slack_client,
                    channel_id=dm_channel,
                    client_msg_id=_slack_client_msg_id(kind, assignment.id),
                    oldest=oldest,
                )
                if message_ts:
                    coordinates.add(message_ts)
            coordinates.discard("")
            for message_ts in sorted(coordinates):
                try:
                    response = slack_client.chat_update(
                        channel=dm_channel,
                        ts=message_ts,
                        text=_private_correction_text(assignment),
                    )
                except SlackApiError as exc:
                    if _safe_slack_error(exc) == "message_not_found":
                        continue
                    raise
                if not response.get("ok", True):
                    if str(response.get("error") or "") == "message_not_found":
                        continue
                    raise SlackApiError("chat.update failed", response)
        except Exception as exc:
            _finish_delivery_failure(
                OfficeManagerAssignment,
                assignment_id,
                status_field="private_correction_status",
                error_field="private_correction_last_error",
                attempt_count_field="private_correction_attempt_count",
                next_attempt_field="private_correction_next_attempt_at",
                lease_token=lease_token,
                exc=exc,
                uncertain=not isinstance(exc, OfficeManagerConfigurationError),
            )
            return False

        updated = OfficeManagerAssignment.objects.filter(
            pk=assignment_id,
            private_correction_status="sending",
            private_correction_last_error=lease_token,
        ).update(
            private_correction_pending=False,
            private_correction_status="sent",
            private_correction_sent_at=timezone.now(),
            private_correction_last_error="",
            private_correction_next_attempt_at=None,
            updated_at=timezone.now(),
        )
        return updated == 1


def run_office_manager_scheduler(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    recovered_deliveries: dict[str, dict[int, bool]] = {
        "announcement": {},
        "message_update": {},
        "winner_channel": {},
        "winner_dm": {},
        "end_of_day": {},
        "private_correction": {},
    }
    winner_channel_retractions = []
    retraction_dead_letters = []
    message_update_dead_letters = []
    announcement_dead_letters = []
    assignment_delivery_dead_letters = []

    def scheduler_result(payload: dict) -> dict:
        if winner_channel_retractions:
            payload["winner_channel_retractions"] = winner_channel_retractions
        delivery_failures = {}
        if retraction_dead_letters:
            delivery_failures.update({
                "winner_channel_retraction_dead_letters": (
                    retraction_dead_letters
                )
            })
        if message_update_dead_letters:
            delivery_failures.update({
                "message_update_dead_letters": message_update_dead_letters,
            })
        if announcement_dead_letters:
            delivery_failures.update({
                "announcement_dead_letters": announcement_dead_letters,
            })
        if assignment_delivery_dead_letters:
            delivery_failures.update({
                "assignment_delivery_dead_letters": (
                    assignment_delivery_dead_letters
                ),
            })
        if delivery_failures:
            payload["delivery_failures"] = delivery_failures
        if any(recovered_deliveries.values()):
            payload["recovered_deliveries"] = recovered_deliveries
        return payload

    # Retractions repair previously committed state and must continue while the
    # creation path is disabled during a rollback. Otherwise a stale winner can
    # remain named publicly until the feature is enabled again.
    enabled = _office_manager_enabled()
    needs_slack_recovery = (
        not dry_run
        and OfficeManagerService.has_pending_committed_delivery_work()
    )
    if not dry_run and (enabled or needs_slack_recovery):
        try:
            _office_manager_slack_token()
        except OfficeManagerConfigurationError:
            return scheduler_result({
                "status": "failed",
                "reason": "slack_bot_token_not_configured",
            })

    if not dry_run and needs_slack_recovery:
        winner_channel_retractions = (
            OfficeManagerService.retry_pending_winner_retractions()
        )
    if not dry_run:
        retraction_dead_letters = (
            OfficeManagerService.unresolved_winner_retraction_dead_letters()
        )
        message_update_dead_letters = (
            OfficeManagerService.unresolved_message_update_dead_letters()
        )
        announcement_dead_letters = (
            OfficeManagerService.unresolved_announcement_dead_letters()
        )
        assignment_delivery_dead_letters = (
            OfficeManagerService.unresolved_assignment_delivery_dead_letters()
        )

    local_now = _local_now(now)
    if not dry_run and (enabled or needs_slack_recovery):
        recovered_deliveries = OfficeManagerService.retry_pending_deliveries(
            now=now,
            allow_new_announcements=enabled,
        )
        # A retry can exhaust its budget in this same tick. Refresh the
        # dead-letter snapshots so operators see the terminal failure now.
        announcement_dead_letters = (
            OfficeManagerService.unresolved_announcement_dead_letters()
        )
        assignment_delivery_dead_letters = (
            OfficeManagerService.unresolved_assignment_delivery_dead_letters()
        )
    if not dry_run and not enabled:
        return scheduler_result({"status": "skipped", "reason": "disabled"})
    local_date = local_now.date()
    try:
        configured_weekdays = _configured_weekdays()
    except ValueError as exc:
        logger.error("Invalid Office Manager weekday configuration: %s", exc)
        return scheduler_result({
            "status": "failed",
            "reason": "invalid_weekday_configuration",
            "local_date": local_date.isoformat(),
        })
    if local_date.weekday() not in configured_weekdays:
        return scheduler_result({
            "status": "skipped",
            "reason": "weekday_not_configured",
            "local_date": local_date.isoformat(),
        })

    announcement_time = _setting_time(
        "OFFICE_MANAGER_ANNOUNCEMENT_HOUR",
        "OFFICE_MANAGER_ANNOUNCEMENT_MINUTE",
        8,
        30,
    )
    if local_now.time().replace(tzinfo=None) < announcement_time:
        return scheduler_result({
            "status": "skipped",
            "reason": "before_announcement",
            "local_date": local_date.isoformat(),
        })

    channel_id = str(getattr(settings, "OFFICE_MANAGER_SLACK_CHANNEL_ID", "") or "").strip()
    if not channel_id:
        return scheduler_result(
            {"status": "failed", "reason": "channel_not_configured"}
        )

    existing_day = OfficeManagerDay.objects.filter(date=local_date).first()
    if existing_day is None and CoworkingService.get_capacity(local_date) <= 0:
        return scheduler_result({
            "status": "skipped",
            "reason": "coworking_closed",
            "local_date": local_date.isoformat(),
        })

    if dry_run:
        return scheduler_result({
            "status": "preview",
            "local_date": local_date.isoformat(),
            "channel_id": channel_id,
            "claim_cutoff_at": _claim_cutoff(local_date).isoformat(),
        })

    cutoff_at = _claim_cutoff(local_date)
    if existing_day is None and local_now >= cutoff_at:
        return scheduler_result({
            "status": "skipped",
            "reason": "volunteer_window_closed",
            "local_date": local_date.isoformat(),
        })

    day, _ = OfficeManagerDay.objects.get_or_create(
        date=local_date,
        defaults={
            "slack_channel_id": channel_id,
            "claim_cutoff_at": cutoff_at,
        },
    )
    if day.slack_channel_id != channel_id:
        logger.warning(
            "Office Manager channel configuration changed after day creation; "
            "preserving the original channel for day_id=%s",
            day.id,
        )

    result = {
        "status": day.status,
        "day_id": day.id,
        "local_date": local_date.isoformat(),
    }
    if day.id in recovered_deliveries["announcement"]:
        result["announcement_sent"] = recovered_deliveries["announcement"][
            day.id
        ]

    if local_now >= day.claim_cutoff_at.astimezone(_timezone()) and day.status == "open":
        OfficeManagerDay.objects.filter(pk=day.pk, status="open").update(
            status="closed",
            closed_at=timezone.now(),
            message_update_pending=True,
        )
        day.refresh_from_db()

    should_deliver_announcement = (
        day.status != "closed"
        and day.announcement_status in RETRYABLE_DELIVERY_STATUSES
        and (
            _delivery_attempt_is_due(
                status=day.announcement_status,
                error_value=day.announcement_last_error,
                next_attempt_at=day.announcement_next_attempt_at,
                attempt_count=day.announcement_attempt_count,
                legacy_updated_at=day.updated_at,
            )
            or (
                not day.slack_message_ts
                and _coordinate_recovery_is_due(
                    status=day.announcement_status,
                    error_value=day.announcement_last_error,
                    next_attempt_at=day.announcement_next_attempt_at,
                    attempt_count=day.announcement_attempt_count,
                    legacy_updated_at=day.updated_at,
                )
            )
        )
    )
    if (
        should_deliver_announcement
        and day.id not in recovered_deliveries["announcement"]
    ):
        result["announcement_sent"] = OfficeManagerService.post_announcement(
            day.id,
            now=now,
        )
        day.refresh_from_db()

    if (
        day.message_update_pending
        and day.announcement_status == "sent"
        and _message_update_attempt_is_due(day)
    ):
        result["message_updated"] = OfficeManagerService.reconcile_message(day.id)
        day.refresh_from_db()

    if winner_channel_retractions:
        result["winner_channel_retractions"] = winner_channel_retractions

    assignment = (
        day.assignments.filter(status="active").select_related("user").first()
    )
    if assignment is not None:
        if assignment.id in recovered_deliveries["winner_channel"]:
            result["winner_channel_announcement_sent"] = (
                recovered_deliveries["winner_channel"][assignment.id]
            )
        if assignment.winner_channel_announcement_status in {
            "pending",
            "failed",
            "sending",
            "unknown",
        } and assignment.id not in recovered_deliveries[
            "winner_channel"
        ] and (
            _delivery_attempt_is_due(
                status=assignment.winner_channel_announcement_status,
                error_value=assignment.winner_channel_announcement_last_error,
                next_attempt_at=(
                    assignment.winner_channel_announcement_next_attempt_at
                ),
                attempt_count=(
                    assignment.winner_channel_announcement_attempt_count
                ),
                legacy_updated_at=assignment.updated_at,
            )
            or (
                not assignment.winner_channel_message_ts
                and _coordinate_recovery_is_due(
                    status=assignment.winner_channel_announcement_status,
                    error_value=(
                        assignment.winner_channel_announcement_last_error
                    ),
                    next_attempt_at=(
                        assignment.winner_channel_announcement_next_attempt_at
                    ),
                    attempt_count=(
                        assignment.winner_channel_announcement_attempt_count
                    ),
                    legacy_updated_at=assignment.updated_at,
                )
            )
        ):
            result["winner_channel_announcement_sent"] = (
                OfficeManagerService.deliver_winner_channel_announcement(
                    assignment.id,
                    now=now,
                )
            )

        if assignment.id in recovered_deliveries["winner_dm"]:
            result["winner_dm_sent"] = recovered_deliveries["winner_dm"][
                assignment.id
            ]
        if (
            assignment.winner_dm_status
            in {"pending", "failed", "sending", "unknown"}
            and assignment.id not in recovered_deliveries["winner_dm"]
            and _delivery_attempt_is_due(
                status=assignment.winner_dm_status,
                error_value=assignment.winner_dm_last_error,
                next_attempt_at=assignment.winner_dm_next_attempt_at,
                attempt_count=assignment.winner_dm_attempt_count,
                legacy_updated_at=assignment.updated_at,
            )
        ):
            result["winner_dm_sent"] = OfficeManagerService.deliver_winner_dm(
                assignment.id,
                now=now,
            )

        reminder_time = _setting_time(
            "OFFICE_MANAGER_END_OF_DAY_REMINDER_HOUR",
            "OFFICE_MANAGER_END_OF_DAY_REMINDER_MINUTE",
            16,
            30,
        )
        if (
            local_now.time().replace(tzinfo=None) >= reminder_time
            and assignment.end_of_day_reminder_status
            in {"pending", "failed", "sending", "unknown"}
            and assignment.id not in recovered_deliveries["end_of_day"]
            and _delivery_attempt_is_due(
                status=assignment.end_of_day_reminder_status,
                error_value=assignment.end_of_day_reminder_last_error,
                next_attempt_at=(
                    assignment.end_of_day_reminder_next_attempt_at
                ),
                attempt_count=assignment.end_of_day_reminder_attempt_count,
                legacy_updated_at=assignment.updated_at,
            )
        ):
            result["end_of_day_reminder_sent"] = (
                OfficeManagerService.deliver_end_of_day_reminder(
                    assignment.id,
                    now=now,
                )
            )
        elif assignment.id in recovered_deliveries["end_of_day"]:
            result["end_of_day_reminder_sent"] = recovered_deliveries[
                "end_of_day"
            ][assignment.id]

    result["status"] = day.status
    return scheduler_result(result)
