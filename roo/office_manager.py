"""Office Manager of the Day scheduling, claiming, and Slack delivery."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from slack_sdk.errors import SlackApiError

from core.models import User
from integrations.services.slack import SlackService

from .models import (
    CoworkingBooking,
    OfficeManagerAssignment,
    OfficeManagerDay,
)
from .services import CoworkingService, PointsService

logger = logging.getLogger(__name__)

OFFICE_MANAGER_ACTION_ID = "office_manager_volunteer_today"
NO_FOOD_REMINDER = "Reminder: no food is permitted in the coworking space."


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
    for value in values:
        try:
            weekday = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if 0 <= weekday <= 6:
            weekdays.add(weekday)
    return weekdays or {0, 1, 2, 3, 4}


def _announcement_text(day: OfficeManagerDay) -> str:
    if day.status == "claimed":
        assignment = day.assignments.filter(status="active").select_related("user").first()
        if assignment and assignment.user.slack_id:
            return (
                f"Office Manager of the Day: <@{assignment.user.slack_id}>. "
                "Roo booked them in without deducting Roo points."
            )
    if day.status == "closed":
        return "The Office Manager volunteer window is closed for today."
    return (
        "Volunteer to be today's Office Manager. "
        "Roo will book the selected member in without deducting Roo points."
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
                    "text": "The volunteer window is closed for today.",
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
                ),
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"Volunteer before 10:00 AM. {NO_FOOD_REMINDER}",
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


class OfficeManagerService:
    DELIVERY_LEASE_SECONDS = 300

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
            return existing

        profile = SlackService.get_user_profile(cleaned)
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
        try:
            with transaction.atomic():
                user, _ = User.objects.get_or_create(
                    slack_id=cleaned,
                    defaults={
                        "email": generated_email,
                        "first_name": first_name,
                        "last_name": last_name,
                        "avatar_url": profile.get("image_url"),
                        "is_active": True,
                    },
                )
        except IntegrityError:
            user = User.objects.get(slack_id=cleaned)
        if not user.is_active:
            raise OfficeManagerClaimError(
                "member_not_eligible",
                "This member account is inactive",
            )
        return user

    @staticmethod
    def claim(
        *,
        slack_user_id: str,
        booking_date: date,
        now: datetime | None = None,
    ) -> OfficeManagerClaimResult:
        local_now = _local_now(now)
        if booking_date != local_now.date():
            raise OfficeManagerClaimError(
                "claim_closed",
                "Office Manager volunteering is only available for today",
            )

        user = OfficeManagerService.resolve_member(slack_user_id)
        with transaction.atomic():
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

            CoworkingService._lock_booking_date(booking_date)
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
    def relinquish_for_booking(
        booking: CoworkingBooking,
        *,
        now: datetime | None = None,
    ) -> tuple[bool, int | None]:
        assignment = (
            OfficeManagerAssignment.objects.select_for_update()
            .filter(booking=booking, status="active")
            .select_related("day")
            .first()
        )
        if assignment is None:
            return False, None

        day = OfficeManagerDay.objects.select_for_update().get(pk=assignment.day_id)
        local_now = _local_now(now)
        assignment.status = "relinquished"
        assignment.relinquished_at = timezone.now()
        assignment.save(update_fields=["status", "relinquished_at"])

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
        return reopened, day.id

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
            response = SlackService.get_client().chat_postMessage(
                channel=day.slack_channel_id,
                text=_announcement_text(day),
                blocks=_announcement_blocks(day),
                unfurl_links=False,
                unfurl_media=False,
            )
            message_ts = str(response.get("ts") or "")
            if not response.get("ok") or not message_ts:
                raise SlackApiError("chat.postMessage failed", response)
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
        success = SlackService.update_message(
            day.slack_channel_id,
            day.slack_message_ts,
            _announcement_text(day),
            blocks=_announcement_blocks(day),
        )
        if success:
            OfficeManagerDay.objects.filter(pk=day_id).update(
                message_update_pending=False
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
            response = SlackService.get_client().chat_postMessage(
                channel=assignment.day.slack_channel_id,
                text=_winner_channel_announcement_text(assignment),
                unfurl_links=False,
                unfurl_media=False,
            )
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
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

        OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
            winner_channel_announcement_status="sent",
            winner_channel_announcement_sent_at=timezone.now(),
            winner_channel_announcement_last_error="",
        )
        return True

    @staticmethod
    def deliver_winner_dm(assignment_id: int) -> bool:
        with transaction.atomic():
            assignment = (
                OfficeManagerAssignment.objects.select_for_update()
                .select_related("user")
                .get(pk=assignment_id)
            )
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
            channel_response = SlackService.get_client().conversations_open(
                users=[assignment.user.slack_id]
            )
            dm_channel = channel_response["channel"]["id"]
            response = SlackService.get_client().chat_postMessage(
                channel=dm_channel,
                text=_winner_dm_text(assignment),
                unfurl_links=False,
                unfurl_media=False,
            )
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
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

        OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
            winner_dm_status="sent",
            winner_dm_sent_at=timezone.now(),
            winner_dm_last_error="",
        )
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
            channel_response = SlackService.get_client().conversations_open(
                users=[assignment.user.slack_id]
            )
            dm_channel = channel_response["channel"]["id"]
            response = SlackService.get_client().chat_postMessage(
                channel=dm_channel,
                text=_end_of_day_dm_text(),
                unfurl_links=False,
                unfurl_media=False,
            )
            if not response.get("ok"):
                raise SlackApiError("chat.postMessage failed", response)
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

        OfficeManagerAssignment.objects.filter(pk=assignment_id).update(
            end_of_day_reminder_status="sent",
            end_of_day_reminder_sent_at=timezone.now(),
            end_of_day_reminder_last_error="",
        )
        return True


def run_office_manager_scheduler(
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    if not dry_run and not bool(getattr(settings, "OFFICE_MANAGER_ENABLED", False)):
        return {"status": "skipped", "reason": "disabled"}

    local_now = _local_now(now)
    local_date = local_now.date()
    if local_date.weekday() not in _configured_weekdays():
        return {
            "status": "skipped",
            "reason": "weekday_not_configured",
            "local_date": local_date.isoformat(),
        }

    announcement_time = _setting_time(
        "OFFICE_MANAGER_ANNOUNCEMENT_HOUR",
        "OFFICE_MANAGER_ANNOUNCEMENT_MINUTE",
        8,
        30,
    )
    if local_now.time().replace(tzinfo=None) < announcement_time:
        return {
            "status": "skipped",
            "reason": "before_announcement",
            "local_date": local_date.isoformat(),
        }

    channel_id = str(getattr(settings, "OFFICE_MANAGER_SLACK_CHANNEL_ID", "") or "").strip()
    if not channel_id:
        return {"status": "failed", "reason": "channel_not_configured"}

    existing_day = OfficeManagerDay.objects.filter(date=local_date).first()
    if existing_day is None and CoworkingService.get_capacity(local_date) <= 0:
        return {
            "status": "skipped",
            "reason": "coworking_closed",
            "local_date": local_date.isoformat(),
        }

    if dry_run:
        return {
            "status": "preview",
            "local_date": local_date.isoformat(),
            "channel_id": channel_id,
            "claim_cutoff_at": _claim_cutoff(local_date).isoformat(),
        }

    cutoff_at = _claim_cutoff(local_date)
    if existing_day is None and local_now >= cutoff_at:
        return {
            "status": "skipped",
            "reason": "volunteer_window_closed",
            "local_date": local_date.isoformat(),
        }

    day, _ = OfficeManagerDay.objects.get_or_create(
        date=local_date,
        defaults={
            "slack_channel_id": channel_id,
            "claim_cutoff_at": cutoff_at,
        },
    )
    if day.slack_channel_id != channel_id:
        day.slack_channel_id = channel_id
        day.save(update_fields=["slack_channel_id", "updated_at"])

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

    if (
        day.status != "closed"
        and day.announcement_status in {"pending", "failed"}
    ):
        result["announcement_sent"] = OfficeManagerService.post_announcement(day.id)
        day.refresh_from_db()

    if day.message_update_pending and day.announcement_status == "sent":
        result["message_updated"] = OfficeManagerService.reconcile_message(day.id)
        day.refresh_from_db()

    assignment = (
        day.assignments.filter(status="active").select_related("user").first()
    )
    if assignment is not None:
        if assignment.winner_channel_announcement_status in {
            "pending",
            "failed",
        }:
            result["winner_channel_announcement_sent"] = (
                OfficeManagerService.deliver_winner_channel_announcement(
                    assignment.id
                )
            )

        if assignment.winner_dm_status in {"pending", "failed"}:
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
            and assignment.end_of_day_reminder_status in {"pending", "failed"}
        ):
            result["end_of_day_reminder_sent"] = (
                OfficeManagerService.deliver_end_of_day_reminder(assignment.id)
            )

    result["status"] = day.status
    return result
