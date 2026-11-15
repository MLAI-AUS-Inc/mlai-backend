"""Automatic event reminders, like Luma's: a day before and an hour before.

Each managed Luma event gets two reminder blasts (event update, guests who are
going) with fixed text and a fixed send time. The scheduler runner then:

1. keeps each reminder in step with its event (moved, cancelled, switched off);
2. two hours before a send, posts a heads-up to the staff Slack channel;
3. 45 minutes before, refreshes the guest list from Luma and runs the
   pre-send checks;
4. at the send time, queues it through the normal path, with every check,
   opt-out and sending limit a staff blast has.

Everything is behind ``CRM_AUTO_REMINDERS_ENABLED`` (off by default). A
reminder never sends after its event has started.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as day_time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from .message_types import REMINDER, refresh_event_guests, reminder_body, reminder_subject
from .models import Blast, CrmAuditEvent, CrmEvent

logger = logging.getLogger(__name__)

THROTTLE_KEY = "crm:reminders:throttle"
OFFSETS = {
    Blast.ReminderOffset.DAY_BEFORE: timedelta(hours=24),
    Blast.ReminderOffset.HOUR_BEFORE: timedelta(hours=1),
}
WINDOW_OPEN, WINDOW_CLOSE = day_time(8, 0), day_time(20, 0)
HEADS_UP_BEFORE = timedelta(hours=2)
PREPARE_BEFORE = timedelta(minutes=45)
MIN_LEAD = timedelta(minutes=15)
PLAN_AHEAD = timedelta(days=8)
DEFAULT_ZONE = "Australia/Melbourne"


def enabled() -> bool:
    return bool(getattr(settings, "CRM_AUTO_REMINDERS_ENABLED", False))


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(str(name or "").strip() or DEFAULT_ZONE)
    except (ValueError, ZoneInfoNotFoundError):
        return ZoneInfo(DEFAULT_ZONE)


def settings_for(event) -> dict:
    raw = event.reminder_settings if isinstance(event.reminder_settings, dict) else {}
    return {
        Blast.ReminderOffset.DAY_BEFORE: raw.get("day_before", True) is not False,
        Blast.ReminderOffset.HOUR_BEFORE: raw.get("hour_before", True) is not False,
        "note": " ".join(str(raw.get("note") or "").split())[:280],
        "force": raw.get("force") is True,
    }


def send_time(event, offset: str) -> Optional[datetime]:
    """The reminder's send time, moved into 8am–8pm (event local time) and before the event.

    A target before 8am moves to 8am that day if the event is still at least 15
    minutes away; a day-before reminder otherwise moves to 8pm the evening
    before. A target after 8pm moves back to 8pm that day.
    """

    start = event.start_at
    if start is None:
        return None
    zone = _zone(event.timezone)
    target = (start - OFFSETS[offset]).astimezone(zone)
    open_at = target.replace(hour=WINDOW_OPEN.hour, minute=0, second=0, microsecond=0)
    close_at = target.replace(hour=WINDOW_CLOSE.hour, minute=0, second=0, microsecond=0)
    if open_at <= target <= close_at:
        chosen = target
    elif target > close_at:
        chosen = close_at
    elif open_at <= start - MIN_LEAD:
        chosen = open_at
    elif offset == Blast.ReminderOffset.DAY_BEFORE:
        chosen = close_at - timedelta(days=1)
    else:
        return None
    if chosen >= start - MIN_LEAD:
        return None
    return chosen.astimezone(ZoneInfo("UTC"))


def eligible(event, config: dict) -> bool:
    if config["force"]:
        return True
    if str(event.visibility or "").lower() == "private":
        return False
    guests = event.approved_count or event.guest_count or 0
    return guests >= int(getattr(settings, "CRM_REMINDER_MIN_GUESTS", 3))


def system_actor():
    """The first configured deliverability owner who is an MLAI admin acts for automatic reminders."""

    from community_chat.admin_access import is_mlai_admin_user
    from .deliverability import policy
    User = get_user_model()
    for email in policy.value("CRM_DELIVERABILITY_OWNERS") or ():
        user = User.objects.filter(email__iexact=email, is_active=True).first()
        # Queueing requires an MLAI admin; a shared inbox account may not be one.
        if user is not None and is_mlai_admin_user(user):
            return user
    return None


def ensure_reminders(event, *, now, actor=None) -> dict:
    """Create, update or cancel this event's two reminder drafts. Idempotent."""

    config = settings_for(event)
    result = {}
    for offset in OFFSETS:
        existing = Blast.objects.filter(event=event, message_type=REMINDER, reminder_offset=offset).exclude(
            status=Blast.Status.CANCELLED).order_by("created_at").first()
        when = send_time(event, offset)
        # A reminder already planned stays due once its time passes (the runner
        # sends it); only a new one needs a send time still ahead.
        wanted = (config[offset] and eligible(event, config) and when is not None
                  and (when > now or existing is not None))
        if existing is not None and existing.status != Blast.Status.DRAFT:
            result[offset] = existing.status
            continue
        if not wanted:
            if existing is not None:
                existing.status = Blast.Status.CANCELLED
                existing.last_error = "reminder_not_wanted"
                existing.save(update_fields=["status", "last_error", "updated_at"])
                result[offset] = "cancelled"
            else:
                result[offset] = "none"
            continue
        subject, body = reminder_subject(event, offset), reminder_body(event, offset, config["note"])
        if existing is None:
            existing = Blast(
                purpose=Blast.Purpose.EVENT_UPDATE, message_type=REMINDER, reminder_offset=offset,
                event=event, quick_segment="going", channels=["email"], created_by=actor,
                sms_only_without_whatsapp=True,
            )
            created = True
        else:
            created = False
        changed = (created or existing.scheduled_for != when or existing.subject != subject
                   or existing.body_markdown != body)
        if changed:
            existing.scheduled_for, existing.subject, existing.body_markdown = when, subject, body
            stats = dict(existing.stats or {})
            stats.pop("reminder", None)  # A moved or reworded reminder starts its steps again.
            existing.stats = stats
            existing.preflight = {}
            _attach_stream(existing, actor)
            existing.save()
            CrmAuditEvent.objects.create(actor=None, action=CrmAuditEvent.Action.EDIT, target_type="blast",
                target_id=str(existing.pk), detail={"operation": "reminder_planned", "offset": offset,
                                                    "scheduled_for": when.isoformat()})
        result[offset] = "planned"
    return result


def _attach_stream(blast, actor) -> None:
    from .blasts import BlastError
    from .deliverability.streams import assign_stream
    try:
        assign_stream(blast, actor=actor, mailbox=None)
    except BlastError as exc:
        # Recorded; the queue step refuses it again and staff hear about it then.
        blast.last_error = exc.code


def _step(blast, name: str) -> bool:
    return bool(((blast.stats or {}).get("reminder") or {}).get(name))


def _mark(blast, name: str, value=True) -> None:
    stats = dict(blast.stats or {})
    reminder = dict(stats.get("reminder") or {})
    reminder[name] = value
    stats["reminder"] = reminder
    Blast.objects.filter(pk=blast.pk).update(stats=stats)
    blast.stats = stats


def _slack(text: str) -> None:
    from .deliverability import policy
    channel = getattr(settings, "CRM_REMINDER_SLACK_CHANNEL", "") or policy.value("CRM_DELIVERABILITY_SLACK_CHANNEL")
    if not channel:
        return
    try:
        from jobs.services.slack import post_slack_message
        post_slack_message({"channel": channel, "text": text})
    except Exception as exc:
        logger.warning("crm reminder slack post failed: %s", type(exc).__name__)


def _local(blast) -> str:
    from .merge_tags import format_event_start
    return format_event_start(blast.scheduled_for, blast.event.timezone if blast.event else "")


def _event_link(blast) -> str:
    return settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip("/") + f"/admin/events/{blast.event_id}"


def heads_up(blast) -> None:
    from .blasts import blast_audience
    try:
        people = blast_audience(blast)["people"]
    except Exception:
        people = None
    count = f"{people} guests" if people is not None else "guests who are going"
    _slack(f"MLAI email: the reminder for {blast.event.name} goes to {count} at {_local(blast)}. "
           f"To skip it, switch it off on the event page: {_event_link(blast)}")


def prepare(blast, *, now) -> None:
    refresh_event_guests(blast.event)
    from .deliverability import policy
    if policy.value("CRM_PREFLIGHT_ENABLED") and blast.stream_id:
        from .deliverability.preflight.service import run_preflight
        try:
            run_preflight(Blast.objects.select_related("event", "mailbox", "stream").get(pk=blast.pk), now=now)
        except Exception as exc:
            logger.warning("crm reminder preflight deferred blast=%s error=%s", blast.pk, type(exc).__name__)


def send(blast, *, now, actor) -> str:
    """Queue the reminder through ``queue_blast`` with freshly confirmed counts."""

    from .blasts import BlastError, blast_audience, queue_blast
    blast = Blast.objects.select_related("event", "mailbox", "stream", "segment").get(pk=blast.pk)
    for attempt in range(2):
        try:
            summary = blast_audience(blast, now=now)
            if not summary["people"]:
                blast.status = Blast.Status.CANCELLED
                blast.last_error = "no_recipients"
                blast.save(update_fields=["status", "last_error", "updated_at"])
                return "no_recipients"
            queue_blast(
                blast.pk,
                confirm={channel: summary["channels"][channel]["reachable"] for channel in blast.channels},
                idempotency_key=f"reminder:{blast.pk}",
                actor=actor,
                now=now,
                definition_hash_confirm=summary.get("definition_hash"),
                recipient_fingerprint_confirm=summary.get("recipient_fingerprint"),
                preflight_hash=(Blast.objects.get(pk=blast.pk).preflight or {}).get("content_hash"),
            )
            return "queued"
        except BlastError as exc:
            if exc.code in ("count_changed", "audience_changed") and attempt == 0:
                continue  # A guest registered or cancelled between the count and the queue.
            Blast.objects.filter(pk=blast.pk).update(last_error=exc.code)
            return exc.code
    return "count_changed"


def plan_upcoming(*, now, actor) -> int:
    events = CrmEvent.objects.filter(start_at__gt=now, start_at__lte=now + PLAN_AHEAD,
                                     raw_payload__platform="luma", raw_payload__access="manage")
    for event in events:
        with transaction.atomic():
            ensure_reminders(event, now=now, actor=actor)
    return events.count()


def run_reminder_scheduler(*, now=None) -> dict:
    """Scheduler runner: self-throttled to once a minute and never raises."""

    if not enabled():
        return {"status": "skipped", "reason": "disabled"}
    if not cache.add(THROTTLE_KEY, 1, timeout=55):
        return {"status": "skipped", "reason": "throttled"}
    now = now or timezone.now()
    actor = system_actor()
    if actor is None:
        return {"status": "deferred", "reason": "no_owner"}
    summary = {"planned_events": 0, "heads_up": 0, "prepared": 0, "queued": 0, "skipped": 0}
    try:
        summary["planned_events"] = plan_upcoming(now=now, actor=actor)
        due = (Blast.objects.filter(message_type=REMINDER, status=Blast.Status.DRAFT,
                                    scheduled_for__lte=now + HEADS_UP_BEFORE)
               .select_related("event", "mailbox", "stream").order_by("scheduled_for"))
        for blast in due:
            if blast.event is None or blast.event.start_at is None or blast.event.start_at <= now:
                blast.status, blast.last_error = Blast.Status.CANCELLED, "event_started"
                blast.save(update_fields=["status", "last_error", "updated_at"])
                summary["skipped"] += 1
                continue
            if not _step(blast, "heads_up"):
                heads_up(blast)
                _mark(blast, "heads_up")
                summary["heads_up"] += 1
            if blast.scheduled_for - PREPARE_BEFORE <= now and not _step(blast, "prepared"):
                prepare(blast, now=now)
                _mark(blast, "prepared")
                summary["prepared"] += 1
            if blast.scheduled_for <= now and not _step(blast, "queue_result"):
                outcome = send(blast, now=now, actor=actor)
                _mark(blast, "queue_result", outcome)
                if outcome == "queued":
                    summary["queued"] += 1
                else:
                    summary["skipped"] += 1
                    _slack(f"MLAI email: the reminder for {blast.event.name} didn't send ({outcome}). "
                           f"{_event_link(blast)}")
    except Exception as exc:
        logger.error("crm reminders deferred: %s", type(exc).__name__)
        return {"status": "deferred", "reason": type(exc).__name__, **summary}
    return {"status": "completed", **summary}
