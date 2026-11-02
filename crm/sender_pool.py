"""Durable, paced campaign splitting over a configured pool of senders.

The user-facing blast owns an immutable set of one-recipient email blasts.
They reuse listmonk's existing idempotent delivery and suppression pipeline.
Only admission moves a child out of draft; all campaigns share sender locks
and a rolling 24-hour ledger of attempted starts, including uncertain ones.
"""

from datetime import timedelta
import uuid

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Blast, BlastDelivery, Channel, Contact, CrmAuditEvent, SenderMailbox

PARENT = "sender_pool"
CHILD = "sender_pool_child"
ATTEMPT = "sender_pool_attempt"
DAILY_LIMIT = 30
SPACING = timedelta(minutes=48)
LIVE = (Blast.Status.QUEUED, Blast.Status.SENDING)


def addresses():
    """The explicit configured roster, independent of the older warm-up roster."""
    return tuple(dict.fromkeys(getattr(settings, "CRM_CAMPAIGN_SENDERS", ())))


def enabled():
    return bool(getattr(settings, "CRM_SENDER_POOL_ENABLED", False) and addresses())


def is_parent(blast):
    return bool((blast.stats or {}).get(PARENT))


def child_parent(blast):
    return (blast.stats or {}).get(CHILD, {}).get("parent_id")


def children(parent_id):
    return Blast.objects.filter(stats__sender_pool_child__parent_id=str(parent_id))


def delivery_filter(parent_id):
    return Q(blast_id=parent_id) | Q(blast__stats__sender_pool_child__parent_id=str(parent_id))


def lock_senders(*, warmup=False):
    """Lock one sending group before blast/contact rows, in primary-key order.

    Warm-up queueing already holds its own sender locks. Never expand that
    transaction to pool rows, which could invert the webhook's lock order.
    """
    from .resend_feedback import monitored_senders
    roster = monitored_senders() if warmup else addresses()
    return list(SenderMailbox.objects.select_for_update().filter(
        email__in=roster).order_by("pk"))


def prepare_draft(blast):
    """New campaigns use the pool; saved warm-up plans and queued campaigns do not."""
    if Channel.EMAIL not in (blast.channels or []):
        blast.stats = {key: value for key, value in (blast.stats or {}).items() if key != PARENT}
        return
    if not enabled() or (blast.stats or {}).get("warmup") or child_parent(blast):
        return
    if Channel.EMAIL in (blast.channels or []):
        blast.stats = {**(blast.stats or {}), PARENT: {"version": 1}}
        # The existing draft/test UI still has a representative From selector.
        # Actual senders are frozen and exposed separately when queued.
        if not blast.mailbox_id or blast.mailbox.email not in addresses():
            blast.mailbox = SenderMailbox.objects.filter(email__in=addresses()).order_by("email").first()


def assignments(recipient_ids, sender_ids):
    """Stable, balanced assignment; each recipient appears once in this campaign."""
    if not sender_ids:
        raise ValueError("sender_pool_empty")
    return [(contact_id, sender_ids[index % len(sender_ids)])
            for index, contact_id in enumerate(sorted(set(recipient_ids), key=str))]


def snapshot(blast, recipients):
    """Freeze the roster and content while the parent and sender locks are held."""
    from .blasts import BlastError
    if not enabled():
        raise BlastError("sender_pool_disabled", 409)
    mailboxes = list(SenderMailbox.objects.filter(email__in=addresses()).order_by("email"))
    if len(mailboxes) != len(addresses()) or not all(m.enabled for m in mailboxes):
        raise BlastError("sender_pool_unavailable", 409)
    if any(m.reply_to.lower() != m.email.lower() or m.daily_limit > DAILY_LIMIT for m in mailboxes):
        raise BlastError("sender_pool_invalid_configuration", 409)
    by_contact = {row[0]: row for row in recipients}
    batches, deliveries = [], []
    for ordinal, (contact_id, mailbox_id) in enumerate(assignments(by_contact, [m.pk for m in mailboxes])):
        _, address, consent = by_contact[contact_id]
        if consent != Contact.MarketingConsent.GRANTED:
            raise BlastError("explicit_consent_required", 409)
        batch = Blast(
            id=uuid.uuid5(blast.pk, f"email:{contact_id}"),
            purpose=blast.purpose, event=blast.event, created_by=blast.created_by,
            channels=[Channel.EMAIL], mailbox_id=mailbox_id,
            subject=blast.subject, body_markdown=blast.body_markdown, recipient_count=1,
            stats={CHILD: {"parent_id": str(blast.pk), "ordinal": ordinal}},
        )
        batches.append(batch)
        deliveries.append(BlastDelivery(blast=batch, contact_id=contact_id, channel=Channel.EMAIL,
            address=address.lower(), consent_basis=BlastDelivery.ConsentBasis.EXPLICIT))
    Blast.objects.bulk_create(batches, batch_size=500)
    BlastDelivery.objects.bulk_create(deliveries, batch_size=500)
    blast.stats = {**blast.stats, PARENT: {"version": 1,
        "senders": [m.email for m in mailboxes], "recipients": len(recipients),
        "daily_limit": DAILY_LIMIT, "spacing_seconds": int(SPACING.total_seconds())}}


def attempted(mailbox, now):
    """Count start attempts, never delivery outcomes: bounces cannot release quota."""
    return CrmAuditEvent.objects.filter(target_type=ATTEMPT, target_id=str(mailbox.pk),
                                       created_at__gte=now - timedelta(hours=24))


def pool_daily_limit():
    """Leave room for other sending streams inside the provider account allowance."""
    return max(0, min(DAILY_LIMIT * len(addresses()),
                      int(getattr(settings, "CRM_SENDER_POOL_DAILY_LIMIT", 60))))


def pool_quota_full(now):
    return CrmAuditEvent.objects.filter(target_type=ATTEMPT,
        created_at__gte=now-timedelta(hours=24)).count() >= pool_daily_limit()


def start_block(blast, now, *, reserved=False):
    """Final admission guard, called with sender rows locked immediately before start."""
    from .blast_email import email_stats
    if blast.mailbox.email not in addresses():
        return "sender_removed"
    if blast.mailbox.reply_to.lower() != blast.mailbox.email.lower():
        return "reply_to_mismatch"
    if not enabled():
        return "sender_pool_disabled"
    parent_id = child_parent(blast)
    if not parent_id:
        return "sender_pool_required"
    parent = Blast.objects.get(pk=parent_id)
    if parent.status not in LIVE:
        return "parent_not_live"
    rows = BlastDelivery.objects.filter(blast=blast, channel=Channel.EMAIL)
    if rows.count() != 1:
        return "sender_pool_invalid_batch"
    delivery = rows.first()
    if delivery.status not in (BlastDelivery.Status.QUEUED, BlastDelivery.Status.SUBMITTING):
        return "delivery_not_pending"
    contact = Contact.objects.select_for_update().get(pk=delivery.contact_id)
    from .queries import banned
    if (contact.email_status != Contact.EmailStatus.ACTIVE
            or contact.marketing_consent != Contact.MarketingConsent.GRANTED
            or contact.email.lower() != delivery.address
            or Contact.objects.filter(pk=delivery.contact_id).annotate(blocked=banned()).filter(blocked=True).exists()):
        rows.filter(status__in=("queued", "submitting")).update(
            status="unsubscribed", error_code="consent_changed", updated_at=now)
        return "consent_changed"
    if reserved:
        return None
    if email_stats(blast).get("start_attempted_at"):
        return "start_unconfirmed"
    recent = attempted(blast.mailbox, now)
    if pool_quota_full(now) or recent.count() >= min(DAILY_LIMIT, blast.mailbox.daily_limit):
        return "daily_limit"
    if recent.filter(created_at__gt=now - SPACING).exists():
        return "sender_spacing"
    return None


def admit(now):
    """At most one outstanding child per sender; no catch-up bursts after downtime."""
    if not enabled():
        return
    with transaction.atomic():
        mailboxes = [m for m in lock_senders() if m.email in addresses() and m.enabled]
        if health_problem(now):
            pause("sender_pool_feedback_unhealthy", now=now)
            return
        if pool_quota_full(now):
            return
        parents = Blast.objects.filter(status__in=LIVE, stats__has_key=PARENT)
        parent_ids = [str(pk) for pk in parents.values_list("pk", flat=True)]
        for mailbox in mailboxes:
            if Blast.objects.filter(mailbox=mailbox, status__in=LIVE).exclude(stats__has_key=PARENT).exists():
                continue
            recent = attempted(mailbox, now)
            if recent.count() >= min(DAILY_LIMIT, mailbox.daily_limit) or recent.filter(created_at__gt=now-SPACING).exists():
                continue
            child = (Blast.objects.select_for_update().filter(mailbox=mailbox, status=Blast.Status.DRAFT,
                stats__sender_pool_child__parent_id__in=parent_ids).order_by("created_at", "stats__sender_pool_child__ordinal").first())
            if child:
                child.status, child.queued_at = Blast.Status.QUEUED, now
                child.save(update_fields=["status", "queued_at", "updated_at"])


def pause(reason, *, now):
    """A pool safety stop never touches or resumes the separate warm-up senders."""
    count = SenderMailbox.objects.filter(email__in=addresses(), enabled=True).update(enabled=False, updated_at=now)
    if count:
        CrmAuditEvent.objects.create(action="edit", target_type="sender_pool_monitor",
            detail={"operation": "paused", "reason": reason, "mailboxes": count})


def health_problem(now):
    """Stop when an attempted send remains uncertain or lacks timely signed feedback."""
    if not getattr(settings, "CRM_RESEND_WEBHOOK_SECRET", ""):
        return "feedback_not_configured"
    rows = BlastDelivery.objects.filter(blast__mailbox__email__in=addresses(),
        blast__stats__has_key=CHILD).exclude(blast__status=Blast.Status.DRAFT)
    if rows.filter(status=BlastDelivery.Status.UNKNOWN).exists():
        return "uncertain_send"
    overdue = CrmAuditEvent.objects.filter(target_type=ATTEMPT,
        detail__kind="campaign", created_at__lt=now-timedelta(hours=1))
    overdue_ids = list(overdue.values_list("detail__blast_id", flat=True))
    if rows.filter(blast_id__in=overdue_ids, status__in=("sent", "submitting"), provider_id="").exists():
        return "feedback_missing"
    return None


def start_campaign(blast_id, campaign_id, *, now):
    """Commit a reservation before external I/O; a crash cannot retry this start."""
    from . import blast_email, listmonk
    from roo.permissions import is_mlai_admin_user
    with transaction.atomic():
        lock_senders()
        blast = Blast.objects.select_for_update(of=("self",)).select_related("mailbox").get(pk=blast_id)
        if blast.status not in LIVE or not blast.mailbox.enabled or start_block(blast, now):
            return "held"
        parent = Blast.objects.get(pk=child_parent(blast))
        if not is_mlai_admin_user(parent.created_by):
            return "held"
        CrmAuditEvent.objects.create(action="send", target_type=ATTEMPT,
            target_id=str(blast.mailbox_id), detail={"blast_id": str(blast.pk), "kind": "campaign"})
        blast.stats = blast_email.with_email_stats(blast.stats, start_attempted_at=now.isoformat())
        blast.save(update_fields=["stats", "updated_at"])
    # Reacquire the same locks so feedback/cancellation can stop the start in
    # between transactions. The durable reservation is deliberately not refunded.
    with transaction.atomic():
        lock_senders()
        blast = Blast.objects.select_for_update(of=("self",)).select_related("mailbox").get(pk=blast_id)
        parent = Blast.objects.get(pk=child_parent(blast))
        if (blast.status not in LIVE or parent.status not in LIVE or not blast.mailbox.enabled
                or not enabled() or not getattr(settings, "CRM_SENDING_ENABLED", False)
                or not is_mlai_admin_user(parent.created_by) or start_block(blast, now, reserved=True)):
            BlastDelivery.objects.filter(blast=blast, status__in=("queued", "submitting")).update(
                status="unsubscribed", error_code="cancelled_before_submission", updated_at=now)
            return "held"
        try:
            listmonk.set_campaign_status(campaign_id, listmonk.RUNNING)
        except listmonk.ListmonkError:
            BlastDelivery.objects.filter(blast=blast, status="submitting").update(
                status="unknown", error_code="start_unconfirmed", updated_at=now)
            pause("start_unconfirmed", now=now)
            return "held"
        blast.stats = blast_email.with_email_stats(blast.stats,
            started_at=blast_email.iso_z(now), listmonk_status=listmonk.RUNNING)
        blast.save(update_fields=["stats", "updated_at"])
    return listmonk.RUNNING


def reserve_test(mailbox_id, *, actor, now):
    """Tests use the same quota; the reservation remains even on transport failure."""
    from .blasts import BlastError
    with transaction.atomic():
        lock_senders()
        mailbox = SenderMailbox.objects.get(pk=mailbox_id)
        if not mailbox.enabled or not enabled():
            raise BlastError("mailbox_disabled", 409)
        if pool_quota_full(now) or attempted(mailbox, now).count() >= min(DAILY_LIMIT, mailbox.daily_limit):
            raise BlastError("daily_limit", 409)
        if attempted(mailbox, now).filter(created_at__gt=now-SPACING).exists():
            raise BlastError("sender_spacing", 409)
        CrmAuditEvent.objects.create(actor=actor, action="send", target_type=ATTEMPT,
            target_id=str(mailbox.pk), detail={"kind": "test"})


def send_test(blast, *, actor, now):
    """Commit test quota first, then recheck pauses before the external send."""
    from . import blast_email, listmonk
    from .blasts import BlastError, require_mlai_admin
    reserve_test(blast.mailbox_id, actor=actor, now=now)
    with transaction.atomic():
        lock_senders()
        current = Blast.objects.select_for_update(of=("self",)).select_related("mailbox").get(pk=blast.pk)
        require_mlai_admin(actor)
        if (not enabled() or not getattr(settings, "CRM_SENDING_ENABLED", False)
                or current.status != Blast.Status.DRAFT or not current.mailbox.enabled
                or current.mailbox_id != blast.mailbox_id
                or current.mailbox.reply_to.lower() != current.mailbox.email.lower()):
            raise BlastError("mailbox_disabled", 409)
        try:
            return blast_email.send_test_email(current.pk, actor, pool_mailbox_id=current.mailbox_id)
        except listmonk.ListmonkError:
            # Commit the stop even when the test API's acknowledgement is lost.
            pause("test_send_unconfirmed", now=now)
    raise BlastError("listmonk_unavailable", 503)


def describe(blast):
    """Public campaign routing information, without provider credentials."""
    return {"mode": "pool", "senders": (blast.stats or {}).get(PARENT, {}).get("senders", list(addresses())),
            "per_sender_daily_limit": DAILY_LIMIT, "pool_daily_limit": pool_daily_limit(), "window_hours": 24,
            "reply_to": "each_sender"} if is_parent(blast) else {"mode": "single"}
