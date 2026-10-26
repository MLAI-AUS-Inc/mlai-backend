"""Finite, consent-based email rollout using ordinary CRM blasts and audit records.

Each planned blast has one recipient, a fixed date and a deterministic UUID.
No schema change or separate sending path is needed. The existing dispatcher
owns delivery; this runner only admits a due draft after checking feedback.
"""

import logging
import re
import uuid
from collections import Counter
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from community_chat.admin_access import require_mlai_admin
from .community_optins import contact_is_opted_in, sync_community_optins
from .models import Blast, BlastDelivery, Channel, Contact, CrmAuditEvent, SenderMailbox
from .resend_feedback import monitored_senders, pause_senders

logger = logging.getLogger(__name__)
MELBOURNE = ZoneInfo("Australia/Melbourne")
MAX_CONTACTS = 56  # <= four messages/day/domain with two domains.
MAX_LATE = timedelta(hours=6)
FEEDBACK_GRACE = timedelta(hours=1)
MESSAGES = (
    ("Your MLAI community links", """Hi {{first_name}},

You opted in to MLAI community emails when setting up your account. Here are a couple of useful places to get started:

- [MLAI](https://mlai.au/) brings together Australia's AI and startup community. The homepage has our current events, founder tools and ways to get involved.
- [MLAI Chat](https://chat.mlai.au/) is where you can sign in to your community account.

If you have a question about getting involved, you can reply to this email and reach the MLAI team at hi@mlai.au.

Thanks for being part of the community,
The MLAI team

You're receiving this because you opted in to MLAI emails. You can unsubscribe using the link below at any time.
"""),
    ("Find your next MLAI event", """Hi {{first_name}},

Looking for a way to meet people working on AI and startups in Australia?

The [MLAI homepage](https://mlai.au/) lists upcoming community events and their registration links. Check the listing for the latest date, location and availability before making plans.

You can also explore the founder tools on the same page, including coding, marketing and fundraising resources. Pick the part that's useful for what you're working on.

Questions about the community are welcome at hi@mlai.au; replying here reaches that inbox.

The MLAI team

You're receiving this because you opted in to MLAI emails. You can unsubscribe using the link below at any time.
"""),
    ("Ways to take part in MLAI", """Hi {{first_name}},

MLAI is a volunteer community for people building with AI in Australia. If you'd like to take part, the [MLAI website](https://mlai.au/) has links to volunteering, community events and articles about startups and AI.

Have a project or a community idea you'd like to share? Tell us what you're building and what kind of help would be useful. You can reply to this email to reach hi@mlai.au.

You can return to your [MLAI Chat account](https://chat.mlai.au/) whenever you need it.

Thanks for being part of MLAI,
The MLAI team

You're receiving this because you opted in to MLAI emails. You can unsubscribe using the link below at any time.
"""),
)


def schedule_slots(roster, start_date, sender_count=2):
    """Stable sender/weekday/time, recent members first, at most once per seven days."""
    if sender_count != 2 or len(roster) > MAX_CONTACTS:
        raise ValueError("warmup_requires_two_senders_and_at_most_56_contacts")
    for index, (contact_id, recent) in enumerate(roster):
        sender = index % sender_count
        ordinal = index // sender_count
        for week in range(3):
            if week == 0 and not recent:
                continue
            day = start_date + timedelta(days=week * 7 + ordinal % 7)
            due = datetime.combine(day, time(10 + (ordinal // 7) * 2), tzinfo=MELBOURNE)
            yield contact_id, sender, week, due


def run_blasts(run_id):
    return Blast.objects.filter(stats__warmup__run_id=run_id)


def run_state(run_id):
    event = CrmAuditEvent.objects.filter(target_type="warmup_run", target_id=run_id).order_by("-pk").first()
    return event.detail if event else {}


def audit_state(run_id, operation, **detail):
    CrmAuditEvent.objects.create(action=CrmAuditEvent.Action.SYNC, target_type="warmup_run",
                                 target_id=run_id, detail={"operation": operation, **detail})


def plan_run(run_id, start_date, *, actor, dry_run=False):
    """Import proven opt-ins and save a reviewable schedule without queueing any email."""
    require_mlai_admin(actor)
    if not re.fullmatch(r"[a-z0-9-]{1,48}", run_id):
        raise ValueError("invalid_run_id")
    with transaction.atomic():
        mailboxes = list(SenderMailbox.objects.select_for_update().filter(
            email__in=monitored_senders()).order_by("email"))
        if len(mailboxes) != 2 or any(not mailbox.enabled for mailbox in mailboxes):
            raise ValueError("two_enabled_monitored_mailboxes_required")
        if run_state(run_id):
            return status_report(run_id)
        imported = sync_community_optins(actor=actor, dry_run=False)
        now = timezone.now()
        eligible = [c for c in Contact.objects.filter(
            marketing_consent=Contact.MarketingConsent.GRANTED, email_status=Contact.EmailStatus.ACTIVE,
            user__isnull=False,
        ).select_related("user") if contact_is_opted_in(c)]
        eligible.sort(key=lambda c: (not bool(c.user.last_login and c.user.last_login >= now - timedelta(days=90)), str(c.pk)))
        if not eligible or len(eligible) > MAX_CONTACTS:
            raise ValueError("warmup_audience_outside_reviewed_limit")
        roster = [(str(c.pk), bool(c.user.last_login and c.user.last_login >= now - timedelta(days=90))) for c in eligible]
        by_id = {str(c.pk): c for c in eligible}
        counts = Counter()
        ends = datetime.combine(start_date + timedelta(days=21), time(10), tzinfo=MELBOURNE)
        for contact_id, sender_index, week, due in schedule_slots(roster, start_date):
            contact, mailbox = by_id[contact_id], mailboxes[sender_index]
            blast_id = uuid.uuid5(uuid.NAMESPACE_URL, f"mlai:warmup:{run_id}:{contact_id}:{week}")
            subject, body = MESSAGES[week]
            Blast.objects.create(
                id=blast_id, purpose=Blast.Purpose.MARKETING, channels=[Channel.EMAIL],
                mailbox=mailbox, subject=subject, body_markdown=body, created_by=actor,
                segment_definition={"match": "all", "rules": [
                    {"type": "field", "field": "email", "op": "equals", "value": contact.email}
                ], "exclude": []},
                stats={"warmup": {"run_id": run_id, "contact_id": contact_id, "week": week + 1,
                                  "scheduled_at": due.isoformat(), "ends_at": ends.isoformat()}},
            )
            counts[f"week_{week + 1}"] += 1
        audit_state(run_id, "planned", starts_on=start_date.isoformat(), ends_at=ends.isoformat(),
                    contacts=len(eligible), planned=dict(counts), import_counts=imported)
        result = status_report(run_id)
        if dry_run:
            transaction.set_rollback(True)
        return result


def activate_run(run_id):
    """Require real signed provider tests on both routes before enabling the saved schedule."""
    if not getattr(settings, "CRM_RESEND_WEBHOOK_SECRET", ""):
        raise ValueError("webhook_not_configured")
    if not getattr(settings, "CRM_WARMUP_ENABLED", False) or run_id != getattr(settings, "CRM_WARMUP_RUN_ID", ""):
        raise ValueError("warmup_not_configured")
    with transaction.atomic():
        mailboxes = list(SenderMailbox.objects.select_for_update().filter(email__in=monitored_senders()).order_by("pk"))
        if len(mailboxes) != 2 or any(not m.enabled for m in mailboxes):
            raise ValueError("mailboxes_not_enabled")
        state = run_state(run_id)
        if state.get("operation") == "activated":
            return status_report(run_id)
        if state.get("operation") != "planned":
            raise ValueError("run_not_planned")
        receipts = CrmAuditEvent.objects.filter(target_type="resend_event", detail__test=True,
                                                 created_at__gte=timezone.now() - timedelta(hours=24))
        proven = {(r.detail.get("sender"), r.detail.get("event")) for r in receipts}
        for sender in monitored_senders():
            for event in ("email.delivered", "email.bounced", "email.complained"):
                if (sender, event) not in proven:
                    raise ValueError("provider_test_missing")
        audit_state(run_id, "activated", ends_at=state["ends_at"], contacts=state["contacts"], planned=state["planned"])
    return status_report(run_id)


def status_report(run_id):
    blasts = run_blasts(run_id)
    counts = Counter(blasts.values_list("status", flat=True))
    deliveries = BlastDelivery.objects.filter(blast__in=blasts, channel=Channel.EMAIL)
    by_sender = {}
    for sender in monitored_senders():
        by_sender[sender] = dict(Counter(deliveries.filter(blast__mailbox__email=sender).values_list("status", flat=True)))
    return {"run_id": run_id, "state": run_state(run_id), "blasts": dict(counts),
            "deliveries": dict(Counter(deliveries.values_list("status", flat=True))),
            "by_sender": by_sender,
            "mailboxes": list(SenderMailbox.objects.filter(email__in=monitored_senders()).values("email", "enabled", "daily_limit"))}


def health_problem(run_id, now):
    """Fail closed on missing feedback, errors, or stopped routes; no blind catch-up."""
    rows = BlastDelivery.objects.filter(blast__in=run_blasts(run_id), channel=Channel.EMAIL)
    if rows.filter(status__in=["failed", "undelivered", "bounced", "unknown"]).exists():
        return "delivery_failed"
    if rows.filter(error_code="resend_complained").exists():
        return "complaint"
    if rows.filter(blast__queued_at__lt=now - FEEDBACK_GRACE,
                   status__in=["queued", "submitting", "sent"]).exists():
        return "delivery_feedback_missing"
    if SenderMailbox.objects.filter(email__in=monitored_senders(), enabled=False).exists():
        return "mailbox_paused"
    return None


def admission_block(blast, now):
    """Rechecked both when queueing and immediately before listmonk starts a campaign."""
    meta = (blast.stats or {}).get("warmup")
    if not meta:
        return None
    run_id = meta.get("run_id")
    if (not getattr(settings, "CRM_WARMUP_ENABLED", False)
            or run_id != getattr(settings, "CRM_WARMUP_RUN_ID", "")
            or run_state(run_id).get("operation") != "activated"):
        return "warmup_inactive"
    if now >= datetime.fromisoformat(meta["ends_at"]):
        return "warmup_finished"
    due = datetime.fromisoformat(meta["scheduled_at"])
    if now < due or now > due + MAX_LATE:
        return "outside_send_window"
    problem = health_problem(run_id, now)
    if problem:
        return problem
    contact = Contact.objects.filter(pk=meta["contact_id"]).first()
    if not contact or not contact_is_opted_in(contact):
        return "consent_not_current"
    # A queued/ambiguous send reserves the frequency cap too. Never compensate
    # for a missed send with two messages next week.
    recent = BlastDelivery.objects.filter(contact=contact, channel=Channel.EMAIL,
        blast__queued_at__gt=now - timedelta(days=7)).exclude(blast=blast)
    if recent.exclude(status__in=["failed", "unsubscribed"]).exists():
        return "weekly_frequency_cap"
    return None


def run_warmup_scheduler(*, now=None):
    """A minute tick, bounded to one admission per sender and no network calls."""
    if not getattr(settings, "CRM_WARMUP_ENABLED", False):
        return {"status": "disabled"}
    run_id = getattr(settings, "CRM_WARMUP_RUN_ID", "")
    if not run_id or not cache.add(f"crm:warmup:{run_id}", True, timeout=55):
        return {"status": "skipped"}
    now = now or timezone.now()
    try:
        return _tick(run_id, now)
    except Exception as exc:
        # A broken warm-up must not crash the shared scheduler or admit sends.
        logger.error("CRM warmup deferred: %s", type(exc).__name__)
        return {"status": "deferred", "reason": type(exc).__name__}


def _tick(run_id, now):
    from .blasts import queue_blast
    with transaction.atomic():
        mailboxes = list(SenderMailbox.objects.select_for_update().filter(
            email__in=monitored_senders()).order_by("pk"))
        state = run_state(run_id)
        if state.get("operation") != "activated":
            return {"status": "skipped", "state": state.get("operation", "unplanned")}
        sync_community_optins()
        problem = health_problem(run_id, now)
        if problem:
            pause_senders(problem, now=now)
            audit_state(run_id, "paused", reason=problem, ends_at=state["ends_at"])
            return {"status": "paused", "reason": problem}
        if now >= datetime.fromisoformat(state["ends_at"]):
            run_blasts(run_id).filter(status="draft").update(status="cancelled", last_error="warmup_finished")
            audit_state(run_id, "completed", ends_at=state["ends_at"], report=status_report(run_id)["deliveries"])
            return {"status": "completed"}
        admitted = 0
        for mailbox in mailboxes:
            # Keep only one outstanding message per domain and leave an hour
            # between admissions so feedback has time to stop the next one.
            if BlastDelivery.objects.filter(blast__in=run_blasts(run_id), blast__mailbox=mailbox,
                blast__queued_at__gt=now - timedelta(hours=1)).exclude(status="unsubscribed").exists():
                continue
            candidates = run_blasts(run_id).filter(mailbox=mailbox, status="draft").order_by("stats__warmup__scheduled_at")
            for blast in candidates:
                due = datetime.fromisoformat(blast.stats["warmup"]["scheduled_at"])
                if due > now:
                    break
                reason = admission_block(blast, now)
                if reason == "weekly_frequency_cap":
                    continue
                if reason:
                    blast.status = Blast.Status.CANCELLED
                    blast.last_error = reason
                    blast.save(update_fields=["status", "last_error", "updated_at"])
                    continue
                queue_blast(blast.pk, confirm={"email": 1}, idempotency_key=f"warmup:{blast.pk}",
                            actor=blast.created_by, now=now)
                admitted += 1
                break
        return {"status": "completed", "admitted": admitted}
