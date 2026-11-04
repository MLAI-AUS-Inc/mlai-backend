"""Feedback from listmonk: campaign progress, opens, clicks, bounces and opt-outs.

listmonk has no outbound webhooks, so the ``crm_listmonk_sync`` runner polls
it about every five minutes:

- **Campaigns:** status and counts into ``Blast.stats["email"]``; a finished
  campaign marks its email deliveries ``sent``.
- **Engagement**, for 30 days after a campaign starts: who opened, who
  clicked (and which tracked links) and who bounced, into each
  ``BlastDelivery``. Times are when the sync first saw the event, so they are
  accurate to the sync interval.
- **Opt-outs:** unsubscribes from ``crm-blast-*`` lists and blocklisted
  subscribers become ``Contact.email_status`` changes with a
  ``ContactConsentEvent``, and unsubscribes are then blocklisted in listmonk
  so "unsubscribe" means every list. Contacts opted out in MLAI Chat are
  blocklisted too.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Iterable, Optional

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import blast_email, listmonk
from .blasts import refresh_status
from .listmonk import ListmonkError, sql_ids, sql_string, sql_timestamp
from .models import Blast, BlastDelivery, Channel, Contact, ContactConsentEvent, CrmAuditEvent

logger = logging.getLogger(__name__)

SYNC_THROTTLE_KEY = "crm:listmonk-sync:throttle"
CURSOR_KEY = "crm:listmonk-sync:cursor"
SYNC_TARGET = "listmonk"
ENGAGEMENT_DAYS = 30
# Re-read a little before the cursor: listmonk's clock and ours can differ,
# and every write below is idempotent.
OVERLAP = timedelta(minutes=5)
EPOCH = datetime(2000, 1, 1, tzinfo=dt_timezone.utc)
MAX_BLASTS = 50
UPDATE_CHUNK = 2000
BLOCKLIST_CHUNK = 500
Status = BlastDelivery.Status
_EMAIL_RANK = {
    Contact.EmailStatus.ACTIVE: 0,
    Contact.EmailStatus.UNSUBSCRIBED: 1,
    Contact.EmailStatus.BOUNCED: 2,
    Contact.EmailStatus.COMPLAINED: 3,
}
_OPT_OUTS = {
    # kind: (email status, consent event kind, withdraws marketing consent)
    "unsubscribed": (Contact.EmailStatus.UNSUBSCRIBED, ContactConsentEvent.Kind.UNSUBSCRIBED, True),
    "bounced": (Contact.EmailStatus.BOUNCED, ContactConsentEvent.Kind.BOUNCED, False),
    "complained": (Contact.EmailStatus.COMPLAINED, ContactConsentEvent.Kind.COMPLAINED, True),
}
_SENT_ROWS = (Status.SUBMITTING, Status.SENT, Status.DELIVERED, Status.READ, Status.UNKNOWN)


def record_email_opt_out(email: str, kind: str, *, source: str, now, detail=None) -> bool:
    """Apply one listmonk opt-out to the contact; never weakens a stronger status.

    Unsubscribes and spam complaints also withdraw marketing consent; a
    bounce only marks the address undeliverable. Returns whether anything changed.
    """

    status, consent_kind, withdraws = _OPT_OUTS[kind]
    with transaction.atomic():
        contact = Contact.objects.select_for_update().filter(email=str(email).lower()).first()
        if contact is None:
            return False
        fields = set()
        if _EMAIL_RANK.get(contact.email_status, 0) < _EMAIL_RANK[status]:
            contact.email_status = status
            contact.email_status_changed_at = now
            fields |= {"email_status", "email_status_changed_at"}
        if withdraws and contact.marketing_consent != Contact.MarketingConsent.WITHDRAWN:
            contact.marketing_consent = Contact.MarketingConsent.WITHDRAWN
            contact.marketing_consent_source = source
            contact.marketing_consent_at = now
            fields |= {"marketing_consent", "marketing_consent_source", "marketing_consent_at"}
        if not fields:
            return False
        contact.save(update_fields=sorted(fields | {"updated_at"}))
        ContactConsentEvent.objects.create(
            contact=contact,
            channel=Channel.EMAIL,
            kind=consent_kind,
            source=source,
            detail=detail or {},
            occurred_at=now,
        )
    return True


def _subscribers(query: str) -> dict[str, int]:
    """Matching subscribers as ``{lowercase email: listmonk id}``."""

    found = {}
    for record in listmonk.query_subscribers(query):
        email = str(record.get("email") or "").strip().lower()
        subscriber_id = record.get("id")
        if email and isinstance(subscriber_id, int):
            found[email] = subscriber_id
    return found


def _chunks(values: list, size: int):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _update_rows(rows, emails: Iterable[str], **values) -> int:
    emails = sorted(emails)
    return sum(rows.filter(address__in=chunk).update(**values) for chunk in _chunks(emails, UPDATE_CHUNK))


def _blocklist(subscriber_ids: Iterable[int]) -> int:
    ids = sorted(set(subscriber_ids))
    for chunk in _chunks(ids, BLOCKLIST_CHUNK):
        listmonk.blocklist_subscribers(chunk)
    return len(ids)


# Campaigns and engagement


def blasts_to_sync(now) -> list:
    recent = now - timedelta(days=ENGAGEMENT_DAYS)
    return list(
        Blast.objects.filter(listmonk_campaign_id__isnull=False)
        .filter(
            Q(status__in=(Blast.Status.QUEUED, Blast.Status.SENDING))
            | Q(queued_at__gte=recent)
            | Q(sent_at__gte=recent)
            | Q(stats__email__cancel_pending=True)
        )
        .order_by("-queued_at", "-pk")[:MAX_BLASTS]
    )


def sync_engagement(blast, *, since, now) -> Counter:
    """Opens, clicks, tracked links, bounces and list unsubscribes for one campaign."""

    campaign_id = int(blast.listmonk_campaign_id)
    stamp = sql_timestamp(since)
    rows = BlastDelivery.objects.filter(blast=blast, channel=Channel.EMAIL)
    counts: Counter = Counter()

    opened = _subscribers(
        "subscribers.id IN (SELECT subscriber_id FROM campaign_views"
        f" WHERE campaign_id = {campaign_id} AND created_at >= {stamp})"
    )
    counts["opened"] += _update_rows(
        rows.filter(first_opened_at__isnull=True), opened, first_opened_at=now, updated_at=now
    )

    clicked = _subscribers(
        "subscribers.id IN (SELECT subscriber_id FROM link_clicks"
        f" WHERE campaign_id = {campaign_id} AND created_at >= {stamp})"
    )
    counts["clicked"] += _update_rows(
        rows.filter(first_clicked_at__isnull=True), clicked, first_clicked_at=now, updated_at=now
    )
    if clicked:
        for url in blast_email.email_stats(blast).get("tracked_urls") or []:
            try:
                literal = sql_string(url)
            except ListmonkError:
                continue
            who = _subscribers(
                "subscribers.id IN (SELECT subscriber_id FROM link_clicks"
                f" WHERE campaign_id = {campaign_id} AND created_at >= {stamp}"
                f" AND link_id IN (SELECT id FROM links WHERE url = {literal}))"
            )
            for chunk in _chunks(sorted(who), UPDATE_CHUNK):
                for delivery in rows.filter(address__in=chunk).only("pk", "clicked_urls"):
                    urls = list(delivery.clicked_urls or [])
                    if url not in urls:
                        delivery.clicked_urls = urls + [url]
                        delivery.save(update_fields=["clicked_urls", "updated_at"])

    bounced = _subscribers(
        "subscribers.id IN (SELECT subscriber_id FROM bounces"
        f" WHERE campaign_id = {campaign_id} AND type IN ('hard', 'soft') AND created_at >= {stamp})"
    )
    counts["bounced"] += _update_rows(
        rows.filter(status__in=_SENT_ROWS), bounced, status=Status.BOUNCED, updated_at=now
    )

    if blast.listmonk_list_id:
        # Blocklisting unsubscribes someone from every list, so it says
        # nothing about this blast; those are handled by ``sync_opt_outs``.
        unsubscribed = _subscribers(
            "subscribers.status != 'blocklisted' AND subscribers.id IN (SELECT subscriber_id"
            f" FROM subscriber_lists WHERE list_id = {int(blast.listmonk_list_id)}"
            f" AND status = 'unsubscribed' AND updated_at >= {stamp})"
        )
        counts["unsubscribed"] += _update_rows(
            rows.filter(status__in=_SENT_ROWS),
            unsubscribed,
            status=Status.UNSUBSCRIBED,
            error_code="",
            updated_at=now,
        )
        for email in unsubscribed:
            if record_email_opt_out(
                email,
                "unsubscribed",
                source="listmonk:unsubscribe",
                now=now,
                detail={"blast_id": str(blast.pk)},
            ):
                counts["contacts_opted_out"] += 1
        counts["blocklisted"] += _blocklist(unsubscribed.values())
    return counts


def sync_blast(blast, *, since, now) -> Counter:
    counts: Counter = Counter()
    if blast_email.email_stats(blast).get("cancel_pending"):
        blast_email.retry_cancel(blast.pk, now=now)
    campaign = listmonk.get_campaign(blast.listmonk_campaign_id)
    blast_email.apply_campaign(blast.pk, campaign, now=now)
    counts["campaigns"] += 1
    started_at = blast_email.parse_iso(blast_email.email_stats(blast).get("started_at"))
    if started_at and started_at >= now - timedelta(days=ENGAGEMENT_DAYS):
        counts.update(sync_engagement(blast, since=max(since, started_at - OVERLAP), now=now))
    refresh_status(blast.pk, now=now)
    return counts


# Opt-outs


def sync_opt_outs(*, since, now) -> Counter:
    """Blocklisted subscribers and crm list unsubscribes since ``since``."""

    stamp = sql_timestamp(since)
    blocklisted = f"subscribers.status = 'blocklisted' AND subscribers.updated_at >= {stamp}"
    complained = _subscribers(
        f"{blocklisted} AND subscribers.id IN (SELECT subscriber_id FROM bounces WHERE type = 'complaint')"
    )
    bounced = _subscribers(
        f"{blocklisted} AND subscribers.id IN"
        " (SELECT subscriber_id FROM bounces WHERE type IN ('hard', 'soft'))"
    )
    # Blocklisted without a bounce or complaint: by hand, or listmonk's
    # "unsubscribe from everything".
    other = {
        email: subscriber_id
        for email, subscriber_id in _subscribers(blocklisted).items()
        if email not in complained and email not in bounced
    }
    unsubscribed = _subscribers(
        "subscribers.status != 'blocklisted' AND subscribers.id IN (SELECT sl.subscriber_id"
        " FROM subscriber_lists sl JOIN lists l ON l.id = sl.list_id"
        f" WHERE sl.status = 'unsubscribed' AND l.name LIKE '{blast_email.LIST_PREFIX}%'"
        f" AND sl.updated_at >= {stamp})"
    )
    counts: Counter = Counter()
    for kind, source, emails in (
        ("complained", "listmonk:complaint", complained),
        ("bounced", "listmonk:bounce", bounced),
        ("unsubscribed", "listmonk:blocklist", other),
        ("unsubscribed", "listmonk:unsubscribe", unsubscribed),
    ):
        for email in emails:
            if record_email_opt_out(email, kind, source=source, now=now):
                counts[f"contacts_{kind}"] += 1
    counts["blocklisted"] += _blocklist(unsubscribed.values())
    return counts


def push_local_opt_outs(*, since, now) -> Counter:
    """Blocklist contacts opted out in MLAI Chat (or bounced) since ``since``."""

    ids = list(
        Contact.objects.filter(
            listmonk_subscriber_id__isnull=False, email_status_changed_at__gte=since
        )
        .exclude(email_status=Contact.EmailStatus.ACTIVE)
        .values_list("listmonk_subscriber_id", flat=True)
    )
    if not ids:
        return Counter()
    # Only subscribers listmonk still has, and that aren't blocklisted already.
    live = {
        record["id"]
        for chunk in _chunks(sorted(set(ids)), BLOCKLIST_CHUNK)
        for record in listmonk.query_subscribers(
            f"subscribers.id IN {sql_ids(chunk)} AND subscribers.status != 'blocklisted'"
        )
        if isinstance(record.get("id"), int)
    }
    return Counter({"blocklisted_from_crm": _blocklist(live)})


# Cursor and runner


def load_cursor() -> Optional[datetime]:
    """The last sync's start, from the cache or else the latest sync audit row."""

    candidates = [blast_email.parse_iso(cache.get(CURSOR_KEY))]
    detail = (
        CrmAuditEvent.objects.filter(action=CrmAuditEvent.Action.SYNC, target_type=SYNC_TARGET)
        .order_by("-created_at")
        .values_list("detail", flat=True)
        .first()
    )
    if isinstance(detail, dict):
        candidates.append(blast_email.parse_iso(detail.get("cursor")))
    found = [value for value in candidates if value]
    return max(found) if found else None


def sync_listmonk(*, now) -> dict:
    cursor = load_cursor()
    since = cursor - OVERLAP if cursor else EPOCH
    counts: Counter = Counter()
    problems = []
    for blast in blasts_to_sync(now):
        try:
            counts.update(sync_blast(blast, since=since, now=now))
        except ListmonkError as exc:
            if exc.transient or exc.credentials_rejected:
                raise
            # For example a campaign deleted by hand in listmonk.
            logger.warning("crm_listmonk_sync blast %s skipped: %s", blast.pk, exc.code)
            problems.append(str(blast.pk))
    counts.update(sync_opt_outs(since=since, now=now))
    counts.update(push_local_opt_outs(since=since, now=now))
    stamp = blast_email.iso_z(now)
    cache.set(CURSOR_KEY, stamp, timeout=None)
    changes = {name: total for name, total in counts.items() if name != "campaigns" and total}
    if changes or problems:
        # Kept only when something changed, so the cursor survives a cache flush.
        CrmAuditEvent.objects.create(
            action=CrmAuditEvent.Action.SYNC,
            target_type=SYNC_TARGET,
            detail={"cursor": stamp, **changes, "blasts_with_errors": problems},
        )
    return {**dict(counts), "blasts_with_errors": problems}


def run_listmonk_sync_scheduler(*, now=None) -> dict:
    """Scheduler runner ``crm_listmonk_sync``: self-throttled, quiet about outages.

    Only listmonk refusing the API user's credentials or permissions is
    ``failed`` (that stops the shared scheduler); an outage is ``deferred``.
    """

    if not listmonk.configured():
        return {"status": "skipped", "reason": "not_configured"}
    if not cache.add(SYNC_THROTTLE_KEY, 1, timeout=settings.CRM_LISTMONK_SYNC_INTERVAL_SECONDS):
        return {"status": "skipped", "reason": "throttled"}
    try:
        summary = sync_listmonk(now=now or timezone.now())
    except ListmonkError as exc:
        if exc.credentials_rejected:
            logger.error("crm_listmonk_sync listmonk refused the API credentials: %s", exc.code)
            return {"status": "failed", "reason": "listmonk_credentials_rejected", "status_code": exc.status_code}
        logger.warning("crm_listmonk_sync deferred: %s", exc.code)
        return {"status": "deferred", "reason": "listmonk_unavailable", "code": exc.code}
    return {"status": "partial" if summary["blasts_with_errors"] else "completed", **summary}
