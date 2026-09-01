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
    OfficeManagerDay,
)
from .permissions import InsufficientBalanceError
from .services import CoworkingService, PointsService

logger = logging.getLogger(__name__)

OFFICE_MANAGER_ACTION_ID = "office_manager_volunteer_today"
NO_FOOD_REMINDER = "Reminder: no food is permitted in the coworking space."
COWORKING_SELF_BOOK_REMINDER = (
    "Using the coworking space today? Please book yourself by asking "
    "`@Roo book me in today`."
)
OFFICE_MANAGER_BOOKING_RESPONSIBILITY = (
    "Please remind anyone using the coworking space to book themselves "
    "through Roo for today."
)


class OfficeManagerConfigurationError(RuntimeError):
    pass


class OfficeManagerClaimError(ValueError):
    def __init__(self, code: str, message: str, *, assignee_slack_user_id: str = ""):
        super().__init__(message)
        self.code = code
        self.assignee_slack_user_id = assignee_slack_user_id


@dataclass(frozen=True)
class OfficeManagerClaimResult:
    assignment: OfficeManagerAssignment
    booking: CoworkingBooking
    status: str
    existing_booking_converted: bool


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
    if day.status == "claimed":
        assignment = day.assignments.filter(status="active").select_related("user").first()
        mention = (
            f"<@{assignment.user.slack_id}>"
            if assignment and assignment.user.slack_id
            else "A member"
        )
        return (
            f"Office Manager of the Day: {mention}. "
            "Roo booked them in without deducting Roo points. "
            f"{COWORKING_SELF_BOOK_REMINDER}"
        )
    if day.status == "closed":
        return (
            "The Office Manager volunteer window is closed for today. "
            f"{COWORKING_SELF_BOOK_REMINDER}"
        )
    return (
        "Volunteer to be today's Office Manager. "
        "Roo will book the selected member in without deducting Roo points. "
        f"{COWORKING_SELF_BOOK_REMINDER}"
    )


def _announcement_blocks(day: OfficeManagerDay) -> list[dict]:
    heading = {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "*Office Manager of the Day*",
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
                        f"{mention} is today's Office Manager.\n"
                        "They have been booked in without deducting Roo points."
                        f"\n\n{COWORKING_SELF_BOOK_REMINDER}"
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
                        "The volunteer window is closed for today."
                        f"\n\n{COWORKING_SELF_BOOK_REMINDER}"
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
                    "Roo will book the selected member in for today without "
                    "deducting Roo points. No channel or thread reply is needed."
                    f"\n\n{COWORKING_SELF_BOOK_REMINDER}"
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
                    "text": {"type": "plain_text", "text": "Volunteer for today"},
                    "style": "primary",
                    "value": json.dumps({"date": day.date.isoformat()}),
                }
            ],
        },
    ]


def _winner_dm_text(assignment: OfficeManagerAssignment) -> str:
    refund_line = ""
    if assignment.points_refunded:
        refund_line = (
            f"\nRoo returned the {assignment.points_refunded} Roo points "
            "previously charged for today's booking."
        )
    return (
        "*You are today's Office Manager.*\n\n"
        "Roo booked you in without deducting Roo points."
        f"{refund_line}\n\n"
        "Today's responsibilities:\n"
        "- Welcome new members and visitors.\n"
        "- Help people get settled and onboarded.\n"
        f"- {OFFICE_MANAGER_BOOKING_RESPONSIBILITY}\n"
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
        f"{mention} is today's *Office Manager of the Day*.\n"
        "Roo booked them in without deducting Roo points. Please say hello "
        "and reach out if you need help getting settled.\n\n"
        f"{COWORKING_SELF_BOOK_REMINDER}\n\n"
        f"{NO_FOOD_REMINDER}"
    )


def _relinquished_winner_channel_text(
    assignment: OfficeManagerAssignment,
) -> str:
    mention = (
        f"<@{assignment.user.slack_id}>"
        if assignment.user.slack_id
        else "The previously selected member"
    )
    availability = (
        "The volunteer position is open again; use the button in Roo's daily "
        "Office Manager announcement."
        if assignment.day.status == "open"
        else "The volunteer window is now closed for today."
    )
    return (
        f"{mention} is no longer today's *Office Manager of the Day*. "
        f"{availability}\n\n"
        f"{COWORKING_SELF_BOOK_REMINDER}\n\n"
        f"{NO_FOOD_REMINDER}"
    )


def _end_of_day_dm_text() -> str:
    return (
        "Office Manager reminder: before you leave, please reset and tidy the "
        f"coworking space.\n\n{NO_FOOD_REMINDER}"
    )


def _safe_slack_error(exc: Exception) -> str:
    if isinstance(exc, SlackApiError):
        return str(exc.response.get("error") or "slack_api_error")
    return exc.__class__.__name__


def _open_dm_channel(slack_client, slack_user_id: str) -> str:
    response = slack_client.conversations_open(users=[slack_user_id])
    channel_id = str((response.get("channel") or {}).get("id") or "")
    if not response.get("ok", True) or not channel_id:
        raise RuntimeError("conversations_open_failed")
    return channel_id


class OfficeManagerService:
    DELIVERY_LEASE_SECONDS = 300
    PENDING_RETRACTION_SWEEP_LIMIT = 100

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
                OfficeManagerAssignment.objects.select_for_update()
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
    def claim(
        *,
        slack_user_id: str,
        booking_date: date,
        now: datetime | None = None,
    ) -> OfficeManagerClaimResult:
        existing_claim = OfficeManagerService._recover_existing_claim(
            slack_user_id=slack_user_id,
            booking_date=booking_date,
        )
        if existing_claim is not None:
            return existing_claim

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
        with transaction.atomic():
            CoworkingService._lock_booking_date(booking_date)
            try:
                day = OfficeManagerDay.objects.select_for_update().get(date=booking_date)
            except OfficeManagerDay.DoesNotExist as exc:
                raise OfficeManagerClaimError(
                    "office_manager_day_not_found",
                    "Today's Office Manager announcement is not available",
                ) from exc

            active_assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .filter(day=day, status="active")
                .select_related("user", "booking")
                .first()
            )
            if active_assignment is not None:
                if active_assignment.user_id == user.id:
                    return OfficeManagerClaimResult(
                        assignment=active_assignment,
                        booking=active_assignment.booking,
                        status="already_claimed_by_you",
                        existing_booking_converted=bool(
                            active_assignment.booking.ledger_entry_id
                        ),
                    )
                raise OfficeManagerClaimError(
                    "already_claimed",
                    "Another member has already been selected",
                    assignee_slack_user_id=active_assignment.user.slack_id or "",
                )

            if day.status == "closed" or local_now >= day.claim_cutoff_at.astimezone(_timezone()):
                raise OfficeManagerClaimError(
                    "claim_closed",
                    "The Office Manager volunteer window is closed",
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
                    refund_ledger, _ = PointsService.refund(
                        user=user,
                        delta=points_refunded,
                        source="COWORKING",
                        description=f"Office Manager booking refund for {booking_date}",
                        created_by_slack_id=str(slack_user_id).strip(),
                        idempotency_key=f"office_manager_refund:{day.id}:{booking.id}",
                        reference_type="OFFICE_MANAGER_ASSIGNMENT",
                        reference_id=str(day.id),
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

            assignment = OfficeManagerAssignment.objects.create(
                day=day,
                user=user,
                booking=booking,
                status="active",
                points_refunded=points_refunded,
                refund_ledger_entry=refund_ledger,
            )
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
            OfficeManagerAssignment.objects.select_for_update()
            .filter(booking=booking, day=day, status="active")
            .first()
        )
        if assignment is None:
            return False, None, None

        if assignment.points_refunded and not assignment.refund_reversal_ledger_entry_id:
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
        assignment.status = "relinquished"
        assignment.relinquished_at = timezone.now()
        assignment.winner_channel_retraction_pending = bool(
            assignment.winner_channel_message_ts
            or assignment.winner_channel_announcement_status
            in {"sending", "sent", "unknown"}
        )
        assignment.save(
            update_fields=[
                "status",
                "relinquished_at",
                "refund_reversal_ledger_entry",
                "winner_channel_retraction_pending",
                "updated_at",
            ]
        )

        reopened = (
            day.date == local_now.date()
            and local_now < day.claim_cutoff_at.astimezone(_timezone())
        )
        day.status = "open" if reopened else "closed"
        day.closed_at = None if reopened else timezone.now()
        day.message_update_pending = True
        day.save(
            update_fields=[
                "status",
                "closed_at",
                "message_update_pending",
                "updated_at",
            ]
        )
        return reopened, day.id, assignment.id

    @staticmethod
    def post_announcement(day_id: int) -> bool:
        with transaction.atomic():
            day = OfficeManagerDay.objects.select_for_update().get(pk=day_id)
            if day.announcement_status in {"sent", "unknown"}:
                return day.announcement_status == "sent"
            lease_cutoff = timezone.now() - timedelta(
                seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
            )
            if (
                day.announcement_status == "sending"
                and day.updated_at >= lease_cutoff
            ):
                return False
            day.announcement_status = "sending"
            day.announcement_attempt_count += 1
            day.announcement_last_error = ""
            day.save(
                update_fields=[
                    "announcement_status",
                    "announcement_attempt_count",
                    "announcement_last_error",
                    "updated_at",
                ]
            )

        try:
            response = _office_manager_slack_client().chat_postMessage(
                channel=day.slack_channel_id,
                text=_announcement_text(day),
                blocks=_announcement_blocks(day),
                client_msg_id=_slack_client_msg_id("daily", day.id),
                unfurl_links=False,
                unfurl_media=False,
            )
            message_ts = str(response.get("ts") or "")
            if not response.get("ok") or not message_ts:
                raise SlackApiError("chat.postMessage failed", response)
        except OfficeManagerConfigurationError as exc:
            OfficeManagerDay.objects.filter(pk=day_id).update(
                announcement_status="failed",
                announcement_last_error=_safe_slack_error(exc),
            )
            return False
        except SlackApiError as exc:
            OfficeManagerDay.objects.filter(pk=day_id).update(
                announcement_status="failed",
                announcement_last_error=_safe_slack_error(exc),
            )
            return False
        except Exception as exc:
            OfficeManagerDay.objects.filter(pk=day_id).update(
                announcement_status="unknown",
                announcement_last_error=_safe_slack_error(exc),
            )
            return False

        OfficeManagerDay.objects.filter(pk=day_id).update(
            announcement_status="sent",
            slack_message_ts=message_ts,
            announced_at=timezone.now(),
            announcement_last_error="",
        )
        return True

    @staticmethod
    def reconcile_message(day_id: int) -> bool:
        day = OfficeManagerDay.objects.get(pk=day_id)
        if not day.slack_message_ts:
            return False
        rendered_text = _announcement_text(day)
        rendered_blocks = _announcement_blocks(day)
        try:
            slack_client = _office_manager_slack_client()
        except OfficeManagerConfigurationError:
            return False
        success = SlackService.update_message(
            day.slack_channel_id,
            day.slack_message_ts,
            rendered_text,
            blocks=rendered_blocks,
            client=slack_client,
        )
        if success:
            with transaction.atomic():
                current_day = OfficeManagerDay.objects.select_for_update().get(
                    pk=day_id
                )
                state_is_current = (
                    _announcement_text(current_day) == rendered_text
                    and _announcement_blocks(current_day) == rendered_blocks
                )
                current_day.message_update_pending = not state_is_current
                current_day.save(
                    update_fields=["message_update_pending", "updated_at"]
                )
        return success

    @staticmethod
    def deliver_winner_channel_announcement(assignment_id: int) -> bool:
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .select_related("day", "user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                return False
            if assignment.winner_channel_announcement_status in {
                "sent",
                "unknown",
            }:
                return (
                    assignment.winner_channel_announcement_status == "sent"
                )
            lease_cutoff = timezone.now() - timedelta(
                seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
            )
            if (
                assignment.winner_channel_announcement_status == "sending"
                and assignment.updated_at >= lease_cutoff
            ):
                return False
            assignment.winner_channel_announcement_status = "sending"
            assignment.winner_channel_announcement_last_error = ""
            assignment.save(
                update_fields=[
                    "winner_channel_announcement_status",
                    "winner_channel_announcement_last_error",
                    "updated_at",
                ]
            )

        try:
            response = _office_manager_slack_client().chat_postMessage(
                channel=assignment.day.slack_channel_id,
                text=_winner_channel_announcement_text(assignment),
                client_msg_id=_slack_client_msg_id("winner", assignment.id),
                unfurl_links=False,
                unfurl_media=False,
            )
            message_ts = str(response.get("ts") or "")
            if not response.get("ok") or not message_ts:
                raise SlackApiError("chat.postMessage failed", response)
        except OfficeManagerConfigurationError as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_channel_announcement_status="failed",
                winner_channel_announcement_last_error=_safe_slack_error(exc),
            )
            return False
        except SlackApiError as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_channel_announcement_status="failed",
                winner_channel_announcement_last_error=_safe_slack_error(exc),
            )
            return False
        except Exception as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_channel_announcement_status="unknown",
                winner_channel_announcement_last_error=_safe_slack_error(exc),
            )
            return False

        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .select_related("day", "user")
                .get(pk=assignment_id)
            )
            assignment.winner_channel_announcement_status = "sent"
            assignment.winner_channel_announcement_sent_at = timezone.now()
            assignment.winner_channel_message_ts = message_ts
            assignment.winner_channel_announcement_last_error = ""
            if assignment.status != "active":
                assignment.winner_channel_retraction_pending = True
            assignment.save(
                update_fields=[
                    "winner_channel_announcement_status",
                    "winner_channel_announcement_sent_at",
                    "winner_channel_message_ts",
                    "winner_channel_announcement_last_error",
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
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .select_related("day", "user")
                .get(pk=assignment_id)
            )
            if not assignment.winner_channel_retraction_pending:
                return True
            channel_id = assignment.day.slack_channel_id
            message_ts = assignment.winner_channel_message_ts
            text = _relinquished_winner_channel_text(assignment)

        if not message_ts:
            try:
                slack_client = _office_manager_slack_client()
                response = slack_client.chat_postMessage(
                    channel=channel_id,
                    text=_winner_channel_announcement_text(assignment),
                    client_msg_id=_slack_client_msg_id(
                        "winner",
                        assignment.id,
                    ),
                    unfurl_links=False,
                    unfurl_media=False,
                )
                message_ts = str(response.get("ts") or "")
                if not response.get("ok") or not message_ts:
                    raise SlackApiError("chat.postMessage failed", response)
            except Exception as exc:
                OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                    winner_channel_retraction_last_error=_safe_slack_error(exc),
                    updated_at=timezone.now(),
                )
                return False
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_channel_announcement_status="sent",
                winner_channel_announcement_sent_at=timezone.now(),
                winner_channel_message_ts=message_ts,
                winner_channel_announcement_last_error="",
                updated_at=timezone.now(),
            )

        try:
            slack_client = _office_manager_slack_client()
        except OfficeManagerConfigurationError as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_channel_retraction_last_error=_safe_slack_error(exc),
                updated_at=timezone.now(),
            )
            return False
        success = SlackService.update_message(
            channel_id,
            message_ts,
            text,
            client=slack_client,
        )
        OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
            winner_channel_retraction_pending=not success,
            winner_channel_retraction_last_error=(
                "" if success else "slack_update_failed"
            ),
            updated_at=timezone.now(),
        )
        return success

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
        assignment_ids = list(
            OfficeManagerAssignment.objects.filter(
                winner_channel_retraction_pending=True,
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
    def deliver_winner_dm(assignment_id: int) -> bool:
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .select_related("user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                return False
            if assignment.winner_dm_status in {"sent", "unknown"}:
                return assignment.winner_dm_status == "sent"
            lease_cutoff = timezone.now() - timedelta(
                seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
            )
            if (
                assignment.winner_dm_status == "sending"
                and assignment.updated_at >= lease_cutoff
            ):
                return False
            assignment.winner_dm_status = "sending"
            assignment.winner_dm_last_error = ""
            assignment.save(
                update_fields=[
                    "winner_dm_status",
                    "winner_dm_last_error",
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
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_dm_status="failed",
                winner_dm_last_error=_safe_slack_error(exc),
            )
            return False

        try:
            with transaction.atomic():
                assignment = (
                    OfficeManagerAssignment.objects.select_for_update()
                    .select_related("user")
                    .get(pk=assignment_id)
                )
                if assignment.status != "active":
                    assignment.winner_dm_status = "failed"
                    assignment.winner_dm_last_error = (
                        "assignment_relinquished_before_delivery"
                    )
                    assignment.save(
                        update_fields=[
                            "winner_dm_status",
                            "winner_dm_last_error",
                            "updated_at",
                        ]
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
                assignment.winner_dm_status = "sent"
                assignment.winner_dm_sent_at = timezone.now()
                assignment.winner_dm_last_error = ""
                assignment.save(
                    update_fields=[
                        "winner_dm_status",
                        "winner_dm_sent_at",
                        "winner_dm_last_error",
                        "updated_at",
                    ]
                )
        except SlackApiError as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_dm_status="failed",
                winner_dm_last_error=_safe_slack_error(exc),
            )
            return False
        except Exception as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                winner_dm_status="unknown",
                winner_dm_last_error=_safe_slack_error(exc),
            )
            return False
        return True

    @staticmethod
    def deliver_end_of_day_reminder(assignment_id: int) -> bool:
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .select_related("user")
                .get(pk=assignment_id)
            )
            if assignment.status != "active":
                return False
            if assignment.end_of_day_reminder_status in {"sent", "unknown"}:
                return assignment.end_of_day_reminder_status == "sent"
            lease_cutoff = timezone.now() - timedelta(
                seconds=OfficeManagerService.DELIVERY_LEASE_SECONDS
            )
            if (
                assignment.end_of_day_reminder_status == "sending"
                and assignment.updated_at >= lease_cutoff
            ):
                return False
            assignment.end_of_day_reminder_status = "sending"
            assignment.end_of_day_reminder_last_error = ""
            assignment.save(
                update_fields=[
                    "end_of_day_reminder_status",
                    "end_of_day_reminder_last_error",
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
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                end_of_day_reminder_status="failed",
                end_of_day_reminder_last_error=_safe_slack_error(exc),
            )
            return False

        try:
            with transaction.atomic():
                assignment = (
                    OfficeManagerAssignment.objects.select_for_update()
                    .select_related("user")
                    .get(pk=assignment_id)
                )
                if assignment.status != "active":
                    assignment.end_of_day_reminder_status = "failed"
                    assignment.end_of_day_reminder_last_error = (
                        "assignment_relinquished_before_delivery"
                    )
                    assignment.save(
                        update_fields=[
                            "end_of_day_reminder_status",
                            "end_of_day_reminder_last_error",
                            "updated_at",
                        ]
                    )
                    return False
                response = slack_client.chat_postMessage(
                    channel=dm_channel,
                    text=_end_of_day_dm_text(),
                    client_msg_id=_slack_client_msg_id(
                        "end-of-day",
                        assignment.id,
                    ),
                    unfurl_links=False,
                    unfurl_media=False,
                )
                if not response.get("ok"):
                    raise SlackApiError("chat.postMessage failed", response)
                assignment.end_of_day_reminder_status = "sent"
                assignment.end_of_day_reminder_sent_at = timezone.now()
                assignment.end_of_day_reminder_last_error = ""
                assignment.save(
                    update_fields=[
                        "end_of_day_reminder_status",
                        "end_of_day_reminder_sent_at",
                        "end_of_day_reminder_last_error",
                        "updated_at",
                    ]
                )
        except SlackApiError as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                end_of_day_reminder_status="failed",
                end_of_day_reminder_last_error=_safe_slack_error(exc),
            )
            return False
        except Exception as exc:
            OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
                end_of_day_reminder_status="unknown",
                end_of_day_reminder_last_error=_safe_slack_error(exc),
            )
            return False
        return True


def run_office_manager_scheduler(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    winner_channel_retractions = (
        []
        if dry_run
        else OfficeManagerService.retry_pending_winner_retractions()
    )

    def scheduler_result(payload: dict) -> dict:
        if winner_channel_retractions:
            payload["winner_channel_retractions"] = winner_channel_retractions
        return payload

    # Retractions repair previously committed state and must continue while the
    # creation path is disabled during a rollback. Otherwise a stale winner can
    # remain named publicly until the feature is enabled again.
    if not dry_run and not _office_manager_enabled():
        return scheduler_result({"status": "skipped", "reason": "disabled"})
    if not dry_run:
        try:
            _office_manager_slack_token()
        except OfficeManagerConfigurationError:
            return scheduler_result({
                "status": "failed",
                "reason": "slack_bot_token_not_configured",
            })

    local_now = _local_now(now)
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

    if local_now >= day.claim_cutoff_at.astimezone(_timezone()) and day.status == "open":
        OfficeManagerDay.objects.filter(pk=day.pk, status="open").update(
            status="closed",
            closed_at=timezone.now(),
            message_update_pending=True,
        )
        day.refresh_from_db()

    should_deliver_announcement = (
        day.announcement_status == "sending"
        or (
            day.status != "closed"
            and day.announcement_status in {"pending", "failed"}
        )
    )
    if should_deliver_announcement:
        result["announcement_sent"] = OfficeManagerService.post_announcement(day.id)
        day.refresh_from_db()

    if day.message_update_pending and day.announcement_status == "sent":
        result["message_updated"] = OfficeManagerService.reconcile_message(day.id)
        day.refresh_from_db()

    if winner_channel_retractions:
        result["winner_channel_retractions"] = winner_channel_retractions

    assignment = (
        day.assignments.filter(status="active").select_related("user").first()
    )
    if assignment is not None:
        if assignment.winner_channel_announcement_status in {
            "pending",
            "failed",
            "sending",
        }:
            result["winner_channel_announcement_sent"] = (
                OfficeManagerService.deliver_winner_channel_announcement(
                    assignment.id
                )
            )

        if assignment.winner_dm_status in {"pending", "failed", "sending"}:
            result["winner_dm_sent"] = OfficeManagerService.deliver_winner_dm(
                assignment.id
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
            in {"pending", "failed", "sending"}
        ):
            result["end_of_day_reminder_sent"] = (
                OfficeManagerService.deliver_end_of_day_reminder(assignment.id)
            )

    result["status"] = day.status
    return result
