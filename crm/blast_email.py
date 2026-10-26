"""Email blasts through listmonk: one list and one campaign per blast.

The dispatcher loads a blast's email recipients into the list
``crm-blast-<id>``, then creates the campaign and starts it. The list and
campaign ids are stored on the blast, with its row locked, before anything
else happens, and each is looked up by name before one is created, so a
retried dispatch never makes a second list or campaign.

Email deliveries move ``queued`` → ``submitting`` (on the listmonk list) →
``sent`` once listmonk reports the campaign finished.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone as dt_timezone
from email.utils import formataddr
from time import monotonic
from typing import Any, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import listmonk, merge_tags
from .emails import normalize_contact_email
from .listmonk import ListmonkError
from .models import Blast, BlastDelivery, Channel, Contact, SenderMailbox
from .queries import banned

logger = logging.getLogger(__name__)

LIST_PREFIX = "crm-blast-"
SUBSCRIBER_BATCH = 100
# Each dispatcher tick loads at most this many recipients into listmonk and
# stops early after TICK_SECONDS, so the shared scheduler stays responsive.
SUBSCRIBERS_PER_TICK = 500
TICK_SECONDS = 45
MAX_TRACKED_URLS = 10
GO_EXPRESSIONS = {
    "first_name": "{{ .Subscriber.FirstName }}",
    "display_name": "{{ .Subscriber.Name }}",
}
TRACK_SUFFIX = "@TrackLink"
_MARKDOWN_LINK = re.compile(r"\]\(\s*(https?://[^\s()<>]+)\s*\)")

Status = BlastDelivery.Status


def resource_name(blast_id) -> str:
    """The listmonk list and campaign name for a blast."""

    return f"{LIST_PREFIX}{blast_id}"


def campaign_tags(blast_id) -> list[str]:
    return ["crm", f"blast:{blast_id}"]


def iso_z(value: datetime) -> str:
    return value.astimezone(dt_timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


# Content


def listmonk_text(text, event, *, markdown: bool) -> str:
    """A subject or Markdown body as a listmonk Go template.

    Person tags become ``{{ .Subscriber.FirstName }}`` / ``{{ .Subscriber.Name }}``.
    Event tags are the same for everyone, so they are filled in here. Every
    other ``{`` is escaped, so nothing else can open a template action: as
    ``&#123;`` in Markdown, which renders as a brace, or ``{{ "{" }}`` in a
    plain-text subject.
    """

    merge_tags.validate(text)
    parts = []
    for kind, value in merge_tags.split(text):
        if kind == "tag":
            if value in GO_EXPRESSIONS:
                parts.append(GO_EXPRESSIONS[value])
                continue
            value = merge_tags.value_for(value, None, event)
            if markdown:
                value = value.replace("<", "&lt;").replace(">", "&gt;")
        parts.append(value.replace("{", "&#123;" if markdown else '{{ "{" }}'))
    return "".join(parts)


def track_links(markdown: str) -> str:
    """Mark every ``[text](https://…)`` link for listmonk's per-person click tracking."""

    def tracked(match):
        url = match.group(1)
        return match.group(0) if url.endswith(TRACK_SUFFIX) else f"]({url}{TRACK_SUFFIX})"

    return _MARKDOWN_LINK.sub(tracked, markdown)


def tracked_urls(markdown: str) -> list[str]:
    """The distinct URLs a body tracks, in order, up to ``MAX_TRACKED_URLS``."""

    urls: list[str] = []
    for match in _MARKDOWN_LINK.finditer(markdown):
        url = match.group(1).removesuffix(TRACK_SUFFIX)
        if url not in urls:
            urls.append(url)
    return urls[:MAX_TRACKED_URLS]


def from_address(display_name: str, email: str) -> str:
    """``Sam from MLAI <sam@mlai.au>``, quoted or encoded where RFC 5322 needs it."""

    name = " ".join(str(display_name or "").split())
    return formataddr((name, email)) if name else email


def subscriber_name(contact) -> str:
    """The listmonk name; its first word is ``.Subscriber.FirstName``."""

    first = " ".join(str(getattr(contact, "first_name", "") or "").split())
    display = merge_tags.value_for("display_name", contact, None)
    if first and display.split()[0] != first:
        last = " ".join(str(getattr(contact, "last_name", "") or "").split())
        display = f"{first} {last}".strip()
    return display[:250]


def campaign_body(blast) -> str:
    body = listmonk_text(blast.body_markdown, blast.event, markdown=True)
    return body if (getattr(blast, "stats", None) or {}).get("warmup") else track_links(body)


def campaign_payload(blast, list_id: int) -> dict[str, Any]:
    """The listmonk campaign for a blast: Markdown, the mailbox's From and Reply-To."""

    mailbox = blast.mailbox
    reply_to = "".join(str(mailbox.reply_to or "").split())
    payload: dict[str, Any] = {
        "name": resource_name(blast.pk),
        "subject": listmonk_text(" ".join(blast.subject.split()), blast.event, markdown=False),
        "lists": [int(list_id)],
        "from_email": from_address(mailbox.display_name, mailbox.email),
        "type": "regular",
        "content_type": "markdown",
        "body": campaign_body(blast),
        "messenger": "email",
        "tags": campaign_tags(blast.pk),
        "headers": [{"Reply-To": reply_to}] if reply_to else [],
    }
    template_id = listmonk.template_id()
    if template_id:
        payload["template_id"] = template_id
    return payload


# Blast state


def email_stats(blast) -> dict:
    stats = blast.stats if isinstance(blast.stats, dict) else {}
    email = stats.get("email")
    return dict(email) if isinstance(email, dict) else {}


def with_email_stats(stats, **values) -> dict:
    stats = dict(stats) if isinstance(stats, dict) else {}
    email = dict(stats.get("email") or {})
    email.update(values)
    stats["email"] = email
    return stats


def email_started(blast) -> bool:
    return bool(email_stats(blast).get("started_at"))


def merge_email_stats(blast_id, **values) -> None:
    with transaction.atomic():
        blast = Blast.objects.select_for_update().only("stats").get(pk=blast_id)
        Blast.objects.filter(pk=blast_id).update(
            stats=with_email_stats(blast.stats, **values), updated_at=timezone.now()
        )


def email_block(blast) -> Optional[str]:
    """Why this blast's email can't start now, or ``None``."""

    if not getattr(settings, "CRM_SENDING_ENABLED", False):
        return "channel_disabled"
    if not listmonk.configured():
        return "not_configured"
    if blast.mailbox_id is None or not blast.mailbox.enabled:
        return "mailbox_disabled"
    if (blast.stats or {}).get("warmup"):
        from .warmup import admission_block
        return admission_block(blast, timezone.now())
    return None


def _email_rows(blast_id, *statuses):
    return BlastDelivery.objects.filter(blast_id=blast_id, channel=Channel.EMAIL, status__in=statuses)


# Steps


def ensure_list(blast_id) -> int:
    """The blast's listmonk list, found or created once, its id stored first."""

    with transaction.atomic():
        blast = Blast.objects.select_for_update().get(pk=blast_id)
        if blast.listmonk_list_id:
            return blast.listmonk_list_id
        name = resource_name(blast.pk)
        list_id = listmonk.find_list(name) or listmonk.create_list(
            name, tags=campaign_tags(blast.pk), description="MLAI Chat blast recipients"
        )
        blast.listmonk_list_id = list_id
        blast.save(update_fields=["listmonk_list_id", "updated_at"])
    return list_id


def ensure_campaign(blast_id, list_id: int) -> int:
    """The blast's listmonk campaign, found or created once; a draft gets the latest content."""

    with transaction.atomic():
        blast = (
            Blast.objects.select_for_update(of=("self",))
            .select_related("event", "mailbox")
            .get(pk=blast_id)
        )
        payload = campaign_payload(blast, list_id)
        if blast.listmonk_campaign_id:
            campaign = listmonk.get_campaign(blast.listmonk_campaign_id)
            campaign_id = blast.listmonk_campaign_id
        else:
            campaign = listmonk.find_campaign(payload["name"])
            campaign_id = int(campaign["id"]) if campaign else None
        if campaign is None:
            campaign_id = int(listmonk.create_campaign(payload)["id"])
        elif campaign.get("status") == listmonk.DRAFT:
            listmonk.update_campaign(campaign_id, payload)
        blast.listmonk_campaign_id = campaign_id
        blast.stats = with_email_stats(blast.stats, tracked_urls=tracked_urls(payload["body"]))
        blast.save(update_fields=["listmonk_campaign_id", "stats", "updated_at"])
    return campaign_id


def _drop_reason(blast, contact, is_banned: bool) -> Optional[str]:
    """Re-read consent just before handing a person to listmonk; an opt-out since queueing wins."""

    if is_banned:
        return "banned"
    if contact.email_status in (Contact.EmailStatus.UNSUBSCRIBED, Contact.EmailStatus.COMPLAINED):
        return "opted_out"
    if contact.email_status != Contact.EmailStatus.ACTIVE:
        return "bounced"
    if (
        blast.purpose == Blast.Purpose.MARKETING
        and contact.marketing_consent == Contact.MarketingConsent.WITHDRAWN
    ):
        return "consent_withdrawn"
    return None


def _opted_out_in_listmonk(record: dict) -> bool:
    """Blocklisted, or unsubscribed from an earlier blast's list (the sync records why)."""

    if record.get("status") == "blocklisted":
        return True
    return any(
        isinstance(item, dict)
        and item.get("subscription_status") == "unsubscribed"
        and str(item.get("name") or "").startswith(LIST_PREFIX)
        for item in record.get("lists") or ()
    )


def _safe_literal(email: str) -> Optional[str]:
    try:
        return listmonk.sql_string(email)
    except ListmonkError:
        return None


def _lookup(deliveries) -> dict[str, dict]:
    """listmonk subscribers for a batch, by lowercase email: stored ids first, else email."""

    ids = sorted({d.contact.listmonk_subscriber_id for d in deliveries if d.contact.listmonk_subscriber_id})
    emails = [
        literal
        for literal in (_safe_literal(d.address) for d in deliveries if not d.contact.listmonk_subscriber_id)
        if literal
    ]
    parts = []
    if ids:
        parts.append(f"subscribers.id IN {listmonk.sql_ids(ids)}")
    if emails:
        parts.append(f"LOWER(subscribers.email) IN ({', '.join(emails)})")
    if not parts:
        return {}
    records = listmonk.query_subscribers(" OR ".join(parts), per_page=SUBSCRIBER_BATCH * 2)
    return {str(record.get("email") or "").strip().lower(): record for record in records}


def _find_one(email: str) -> Optional[dict]:
    literal = _safe_literal(email)
    if literal is None:
        return None
    records = listmonk.query_subscribers(f"LOWER(subscribers.email) = {literal}", per_page=5)
    return records[0] if records else None


def _record_id(record: dict) -> int:
    value = record.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ListmonkError("unreadable_response")
    return value


def _refresh_name(record: dict, name: str, list_id: int) -> None:
    """Update the name, keeping every list, the status and the attributes.

    listmonk's subscriber update replaces the list memberships, so all the
    current ones are sent back with this blast's list added.
    """

    lists = {int(item["id"]) for item in record.get("lists") or () if isinstance(item, dict) and isinstance(item.get("id"), int)}
    listmonk.update_subscriber(
        _record_id(record),
        {
            "email": record.get("email"),
            "name": name,
            "status": record.get("status") or "enabled",
            "attribs": record.get("attribs") if isinstance(record.get("attribs"), dict) else {},
            "lists": sorted(lists | {int(list_id)}),
            "preconfirm_subscriptions": True,
        },
    )


def _load_batch(blast, list_id: int, deliveries: list, now) -> Counter:
    """Put one batch on the blast's list; every row leaves ``queued``."""

    outcome: dict[int, tuple[str, str]] = {}
    subscriber_ids: dict[int, int] = {}
    live = []
    for delivery in deliveries:
        reason = _drop_reason(blast, delivery.contact, delivery.is_banned)
        if reason:
            outcome[delivery.pk] = (Status.UNSUBSCRIBED, reason)
        elif _safe_literal(delivery.address) is None:
            outcome[delivery.pk] = (Status.FAILED, "unusable_address")
        else:
            live.append(delivery)

    found = _lookup(live)
    to_add = []
    for delivery in live:
        record = found.get(delivery.address)
        name = subscriber_name(delivery.contact)
        if record is None:
            try:
                record = listmonk.create_subscriber(
                    delivery.address,
                    name,
                    lists=[list_id],
                    attribs={"crm_contact_id": str(delivery.contact_id)},
                )
                subscriber_ids[delivery.pk] = _record_id(record)
                outcome[delivery.pk] = (Status.SUBMITTING, "")
                continue
            except ListmonkError as exc:
                if exc.transient or exc.credentials_rejected:
                    raise
                record = _find_one(delivery.address) if exc.conflict else None
                if record is None:
                    outcome[delivery.pk] = (Status.FAILED, f"listmonk_{exc.code}")
                    continue
        if _opted_out_in_listmonk(record):
            outcome[delivery.pk] = (Status.UNSUBSCRIBED, "listmonk_opted_out")
            continue
        subscriber_ids[delivery.pk] = _record_id(record)
        outcome[delivery.pk] = (Status.SUBMITTING, "")
        if name and record.get("name") != name:
            try:
                _refresh_name(record, name, list_id)
                continue
            except ListmonkError as exc:
                if exc.transient or exc.credentials_rejected:
                    raise
        to_add.append(subscriber_ids[delivery.pk])
    listmonk.add_subscribers_to_list(to_add, list_id)

    counts: Counter = Counter()
    by_outcome: dict[tuple[str, str], list[int]] = {}
    for pk, result in outcome.items():
        by_outcome.setdefault(result, []).append(pk)
        counts[str(result[0])] += 1
    for (status, code), pks in by_outcome.items():
        BlastDelivery.objects.filter(pk__in=pks, status=Status.QUEUED).update(
            status=status, error_code=code[:32], updated_at=now
        )
    for delivery in deliveries:
        subscriber_id = subscriber_ids.get(delivery.pk)
        if subscriber_id and subscriber_id != delivery.contact.listmonk_subscriber_id:
            Contact.objects.filter(pk=delivery.contact_id).update(listmonk_subscriber_id=subscriber_id)
    return counts


def load_subscribers(blast, list_id: int, *, now, budget: int = SUBSCRIBERS_PER_TICK) -> Counter:
    """Load queued email recipients onto the list in batches, within this tick's budget."""

    deadline = monotonic() + TICK_SECONDS
    counts: Counter = Counter()
    while budget > 0 and monotonic() < deadline:
        batch = list(
            BlastDelivery.objects.filter(blast=blast, channel=Channel.EMAIL, status=Status.QUEUED)
            .select_related("contact")
            .annotate(is_banned=banned("contact__"))
            .order_by("created_at", "pk")[: min(SUBSCRIBER_BATCH, budget)]
        )
        if not batch:
            break
        counts.update(_load_batch(blast, list_id, batch, now))
        budget -= len(batch)
    return counts


def start_campaign(blast_id, campaign_id: int, *, now) -> str:
    """Set the campaign running unless the blast was cancelled; records ``started_at``."""

    with transaction.atomic():
        from .resend_feedback import monitored_senders
        # Feedback and admission take sender locks before blast/delivery locks.
        list(SenderMailbox.objects.select_for_update().filter(email__in=monitored_senders()).order_by("pk"))
        blast = Blast.objects.select_for_update().get(pk=blast_id)
        if blast.status == Blast.Status.CANCELLED:
            return listmonk.CANCELLED
        if email_started(blast):
            return str(email_stats(blast).get("listmonk_status") or listmonk.RUNNING)
        if email_block(blast):
            return "held"
        try:
            listmonk.set_campaign_status(campaign_id, listmonk.RUNNING)
            status = listmonk.RUNNING
        except ListmonkError as exc:
            if exc.transient or exc.credentials_rejected:
                raise
            # A start that reached listmonk before an interrupted tick saved it.
            status = str(listmonk.get_campaign(campaign_id).get("status") or "")
            if status not in (listmonk.RUNNING, listmonk.FINISHED, "paused"):
                raise
        blast.stats = with_email_stats(blast.stats, started_at=iso_z(now), listmonk_status=status)
        blast.save(update_fields=["stats", "updated_at"])
    return status


def apply_campaign(blast_id, campaign: dict, *, now) -> str:
    """Copy listmonk's campaign status and counts onto the blast and its email rows."""

    status = str(campaign.get("status") or "")
    if status == listmonk.FINISHED:
        _email_rows(blast_id, Status.SUBMITTING).update(status=Status.SENT, updated_at=now)
    elif status == listmonk.CANCELLED:
        _email_rows(blast_id, Status.SUBMITTING).update(
            status=Status.UNKNOWN, error_code="listmonk_cancelled", updated_at=now
        )
    values = {
        name: campaign[name]
        for name in ("sent", "to_send", "views", "clicks", "bounces")
        if isinstance(campaign.get(name), int) and not isinstance(campaign.get(name), bool)
    }
    merge_email_stats(blast_id, listmonk_status=status, synced_at=iso_z(now), **values)
    if status == listmonk.CANCELLED:
        Blast.objects.filter(pk=blast_id).exclude(status=Blast.Status.CANCELLED).update(
            last_error="listmonk_campaign_cancelled"
        )
    return status


def dispatch_email(blast, *, now) -> dict:
    """One tick of a blast's email: load recipients, then create and start the campaign."""

    started = email_started(blast)
    if started:
        if not blast.listmonk_campaign_id:
            return {"campaign": "missing"}
        campaign = listmonk.get_campaign(blast.listmonk_campaign_id)
        return {"campaign": apply_campaign(blast.pk, campaign, now=now)}
    block = email_block(blast)
    if block:
        return {"blocked": block}
    list_id = ensure_list(blast.pk)
    result: dict[str, Any] = dict(load_subscribers(blast, list_id, now=now))
    if _email_rows(blast.pk, Status.QUEUED).exists():
        result["pending"] = True
        return result
    if not _email_rows(blast.pk, Status.SUBMITTING).exists():
        return result
    campaign_id = ensure_campaign(blast.pk, list_id)
    result["campaign"] = start_campaign(blast.pk, campaign_id, now=now)
    return result


def fail_email(blast_id, code: str, *, now) -> None:
    """listmonk refused this blast's email: rows it may have sent become ``unknown``."""

    blast = Blast.objects.only("stats").get(pk=blast_id)
    status = Status.UNKNOWN if email_started(blast) else Status.FAILED
    _email_rows(blast_id, Status.QUEUED, Status.SUBMITTING).update(
        status=status, error_code=code[:32], updated_at=now
    )
    Blast.objects.filter(pk=blast_id).update(last_error=code[:200])


def cancel_campaign(blast, *, now) -> dict:
    """Stop the campaign; call with the blast row locked. Returns stats to merge."""

    if not blast.listmonk_campaign_id or not email_started(blast):
        # Never started: rows already on the list were never sent and stop holding the cap.
        _email_rows(blast.pk, Status.SUBMITTING).update(status=Status.QUEUED, updated_at=now)
        return {}
    try:
        listmonk.set_campaign_status(blast.listmonk_campaign_id, listmonk.CANCELLED)
    except ListmonkError as exc:
        if exc.transient or exc.credentials_rejected:
            logger.warning("crm blast %s listmonk cancel deferred: %s", blast.pk, exc.code)
            return {"cancel_pending": True}
        # No longer running (finished, or cancelled in listmonk); the sync reads which.
        return {"cancel_pending": False}
    _email_rows(blast.pk, Status.SUBMITTING).update(
        status=Status.UNKNOWN, error_code="cancelled_while_sending", updated_at=now
    )
    return {"listmonk_status": listmonk.CANCELLED, "cancel_pending": False}


def retry_cancel(blast_id, *, now) -> None:
    with transaction.atomic():
        blast = Blast.objects.select_for_update().get(pk=blast_id)
        if not email_stats(blast).get("cancel_pending"):
            return
        values = cancel_campaign(blast, now=now)
        blast.stats = with_email_stats(blast.stats, **values)
        if not values.get("cancel_pending") and blast.last_error == "listmonk_cancel_pending":
            blast.last_error = ""
        blast.save(update_fields=["stats", "last_error", "updated_at"])


def send_test_email(blast_id, user) -> str:
    """Send the draft to the caller's own email through listmonk; returns that address."""

    email = normalize_contact_email(getattr(user, "email", ""))
    if not email:
        raise ListmonkError("no_test_address")
    list_id = ensure_list(blast_id)
    campaign_id = ensure_campaign(blast_id, list_id)
    if _find_one(email) is None:
        contact = Contact.objects.filter(email=email).first()
        try:
            listmonk.create_subscriber(email, subscriber_name(contact or user), lists=[])
        except ListmonkError as exc:
            if not exc.conflict:
                raise
    blast = Blast.objects.select_related("event", "mailbox").get(pk=blast_id)
    listmonk.test_campaign(campaign_id, campaign_payload(blast, list_id), [email])
    return email
