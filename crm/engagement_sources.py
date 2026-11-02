"""Read dated activity already ingested by MLAI, without contacting providers.

This adapter never changes contacts, consent, delivery state or source ingestion.
Public Slack message bodies and private/DM tables are deliberately not read.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.db.models.functions import Lower

from integrations.models import (
    CommunityBridgeChannel, CommunityBridgeIdentityLink,
    CommunityBridgeMessageLink, CommunityBridgeReceipt,
)

from .engagement_scoring import ActivityEvidence, SOURCE_KINDS
from .models import BlastDelivery, CrmAuditEvent, CrmEvent, EventRegistration

MELBOURNE = ZoneInfo("Australia/Melbourne")
MAX_ROWS = 50_000
SEED_KEY = "engagement_activity"
SEED_VERSION = "source-events-v1"
TEST_CHANNEL = re.compile(r"(^|[-_ ])(test|testing|sandbox|staging|fixture|synthetic|e2e)([-_ ]|$)", re.I)
SLACK_STAMP = re.compile(r"^\d{9,12}\.\d{1,9}$")
SLACK_USER = re.compile(r"^[UW][A-Z0-9]{8,}$")


def source_time(value):
    """Parse explicit dates/aware timestamps, never replace missing dates with now."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None and value.utcoffset() is not None else None
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return source_time(date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _instant(value, *, end=False):
    value = source_time(value)
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.max if end else time.min, tzinfo=MELBOURNE)
    return None


def _iso(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else None


def _inside(value, since, now):
    instant = _instant(value)
    return instant is not None and since <= instant <= now


def _real_email(value):
    email = str(value or "").strip().lower()
    domain = email.rpartition("@")[2]
    if "@" not in email or domain in {"example.com", "example.net", "example.org", "resend.dev", "slack.placeholder.com"}:
        return None
    if domain.endswith((".test", ".invalid", ".localhost")):
        return None
    return email


def _bounded(query, counts, name):
    rows = list(query[:MAX_ROWS + 1])
    if len(rows) > MAX_ROWS:
        counts[f"{name}_row_limit"] += 1
    return rows[:MAX_ROWS]


def _freshness(latest, now, *, expected, detail, **extra):
    latest = _instant(latest)
    status = "partial" if latest else "unavailable"
    if latest and (latest > now or now - latest > timedelta(days=2)):
        status = "stale"
    return {"status": status, "expected": expected, "last_success": _iso(latest), "detail": detail, **extra}


def _unavailable(detail, *, expected=True):
    return {"status": "unavailable", "expected": expected, "last_success": None, "detail": detail}


def _seed(contact, since, now, counts):
    attributes = contact.attributes if isinstance(contact.attributes, dict) else {}
    seed = attributes.get(SEED_KEY)
    if not isinstance(seed, dict) or seed.get("version") != SEED_VERSION:
        return [], None
    cutoff = _instant(seed.get("as_of"), end=True)
    events = seed.get("events")
    if not isinstance(events, list) or cutoff is None:
        return [], None
    found = []
    for raw in events[:1000]:
        if not isinstance(raw, dict):
            continue
        source, kind, event_id = (raw.get(key) for key in ("source", "kind", "event_id"))
        occurred = source_time(raw.get("occurred_at"))
        if (source not in SOURCE_KINDS or kind not in SOURCE_KINDS[source]
                or not isinstance(event_id, str) or not event_id.strip()
                or not _inside(occurred, since, now)):
            continue
        found.append(ActivityEvidence(source, kind, event_id, occurred, raw.get("human_verified") is True))
    counts["seed_events"] += len(found)
    return found, cutoff


def _luma(contacts, seeded, seed_cutoffs, since, now, counts):
    stats = CrmEvent.objects.aggregate(total=Count("pk"), synced=Count("guests_synced_at"), last_success=Max("guests_synced_at"))
    coverage = _freshness(stats["last_success"], now,
        expected=bool(getattr(settings, "CRM_LUMA_SYNC_ENABLED", False) or stats["synced"]),
        detail="Managed calendar guest sync only; unsynced/external events are incomplete. Registration is not attendance.",
        events=stats["total"], events_unsynced=stats["total"] - stats["synced"],
        query_window={"from": _iso(since), "through": _iso(now)})
    audit = CrmAuditEvent.objects.filter(action="sync", target_type="luma_calendar").order_by("-created_at").values("created_at", "detail").first()
    if audit:
        detail = audit.get("detail") or {}
        coverage["last_attempt"] = _iso(audit.get("created_at"))
        coverage["status"] = _freshness(audit.get("created_at"), now, expected=True, detail="")["status"]
        if any(detail.get(key) for key in ("failed_events", "events_left", "rate_limited")):
            coverage.update(status="stale", latest_pass_incomplete=True)
        else:
            coverage["last_success"] = _iso(audit.get("created_at"))
    ids = {contact.pk: email for email, contact in contacts.items()}
    seed_ids = {event.event_id.removeprefix("luma:") for values in seeded.values() for event in values if event.source == "luma"}
    authoritative = {f"luma:{row['luma_event_id']}": row["guests_synced_at"] for row in CrmEvent.objects.filter(
        luma_event_id__in=seed_ids, guests_synced_at__isnull=False, start_at__isnull=False,
    ).values("luma_event_id", "guests_synced_at")}
    result = {email: [] for email in contacts}
    # A complete newer event sync can correct/cancel seed evidence. A same-day
    # date-only export has no exact cutoff, so its entire day is protected.
    for email, values in seeded.items():
        for event in values:
            newer = authoritative.get(event.event_id)
            if event.source == "luma" and newer and seed_cutoffs[email] and newer > seed_cutoffs[email]:
                continue
            result[email].append(event)
    rows = _bounded(EventRegistration.objects.filter(
        contact_id__in=ids, removed_at__isnull=True, event__start_at__gte=since, event__start_at__lte=now,
    ).order_by("pk").values("contact_id", "approval_status", "checked_in_at", "event__luma_event_id", "event__start_at"), counts, "luma")
    for row in rows:
        checked = row["checked_in_at"]
        if checked is not None and _instant(checked) and checked <= now:
            kind = "checkin"
        elif row["approval_status"] == "approved":
            kind = "registration"
        else:
            continue
        result[ids[row["contact_id"]]].append(ActivityEvidence(
            "luma", kind, f"luma:{row['event__luma_event_id']}", row["event__start_at"], True,
        ))
        counts["luma_events"] += 1
    return result, coverage


def _public_channel(row):
    if row.get("slack_visibility") != "public" or row.get("slack_deleted"):
        return False
    if TEST_CHANNEL.search(str(row.get("slack_channel_name") or "")):
        return False
    flags = row.get("pilot_settings") or {}
    return not (isinstance(flags, dict) and (
        any(flags.get(key) is True for key in ("test", "is_test", "synthetic", "e2e"))
        or flags.get("environment") in {"test", "testing", "staging", "sandbox"}
    ))


def human_slack_authors(rows, candidates, bot_id=""):
    """Positive raw-public-message evidence, excluding bots even with user IDs."""
    humans, bots = set(), {bot_id} if bot_id else set()
    for row in rows:
        prefix = "payload__event__"
        subtype = row.get(prefix + "subtype") or ""
        if subtype == "message_changed":
            prefix += "message__"
            subtype = row.get(prefix + "subtype") or ""
        author = row.get(prefix + "user")
        if author not in candidates or not SLACK_USER.fullmatch(str(author)):
            continue
        if row.get(prefix + "bot_id") or row.get(prefix + "bot_profile") or subtype == "bot_message":
            bots.add(author)
        elif row.get("payload__event__type") == "message" and subtype in {"", "thread_broadcast", "file_share"}:
            humans.add(author)
    return humans - bots


def slack_message_time(value):
    """Slack's source timestamp is occurrence time; ingestion time is never used."""
    if not isinstance(value, str) or not SLACK_STAMP.fullmatch(value):
        return None
    try:
        return datetime.fromtimestamp(float(Decimal(value)), tz=timezone.utc)
    except (InvalidOperation, ValueError, OverflowError, OSError):
        return None


def _monitored_slack_channel(row):
    """Expect freshness only for an active reader or an established source feed."""
    if (not row.get("enabled", True) or row.get("slack_archived", False)
            or row.get("mirror_decision") != "mirror"
            or row.get("sync_paused_reason") or row.get("hold_reason")):
        return False
    current_reader = (row.get("reader_grant_id") is not None
        and row.get("reader_grant__status") == "active"
        and row.get("reader_grant__revoked_at") is None)
    source_history = (row.get("has_source_history") is True
        or bool(row.get("sync_state__latest_source_activity"))
        or row.get("sync_state__last_successful_delivery_at") is not None)
    return current_reader or source_history


def _slack(users, activities, since, now, counts):
    workspace = str(getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "") or "")
    if not workspace:
        return _unavailable("Public Slack ingestion is not configured; private/DM activity is excluded.", expected=False)
    channels = list(CommunityBridgeChannel.objects.filter(slack_workspace_id=workspace).annotate(
        # Any retained source history establishes prior ingestion, including a
        # subsequently deleted message; no message content is loaded here.
        has_source_history=Exists(CommunityBridgeMessageLink._base_manager.filter(
            channel_id=OuterRef("pk"), source_platform="slack")),
    ).values(
        "id", "enabled", "slack_archived", "slack_visibility", "slack_deleted", "slack_channel_name", "pilot_settings",
        "mirror_decision", "sync_paused_reason", "hold_reason", "reader_grant_id", "reader_grant__status", "reader_grant__revoked_at",
        "has_source_history", "sync_state__latest_source_activity", "sync_state__last_successful_delivery_at", "sync_state__last_successful_scan_at",
    ))
    channels = [row for row in channels if _public_channel(row)]
    monitored = [row for row in channels if _monitored_slack_channel(row)]
    observed = [_instant(row["sync_state__last_successful_scan_at"]) for row in monitored]
    observed = [value for value in observed if value]
    coverage = _freshness(max(observed) if observed else None, now, expected=bool(monitored),
        detail="Public human messages with exact account email mapping only; no private/DM, bot, reaction or test activity. Raw receipt retention limits human verification. Empty mappings without reader authority or source history are coverage gaps.",
        channels=len(channels), channels_without_scan=sum(row["sync_state__last_successful_scan_at"] is None for row in channels),
        monitored_channels=len(monitored), unmonitored_channels=len(channels) - len(monitored),
        query_window={"from": _iso(since), "through": _iso(now)})
    if channels and not monitored:
        coverage["status"] = "partial"
    stale_channels = sum(
        _instant(row["sync_state__last_successful_scan_at"]) is not None
        and (now - _instant(row["sync_state__last_successful_scan_at"]) > timedelta(days=2)
             or _instant(row["sync_state__last_successful_scan_at"]) > now)
        for row in monitored
    )
    coverage["stale_active_channels"] = stale_channels
    if stale_channels:
        coverage["status"] = "stale"
    authors = {}
    for email, user in users.items():
        if user.get("slack_id"):
            authors[str(user["slack_id"])] = email
    user_emails = {user["id"]: email for email, user in users.items()}
    for row in CommunityBridgeIdentityLink.objects.filter(
        slack_workspace_id=workspace, revoked_at__isnull=True, user_id__in=user_emails,
    ).values("slack_user_id", "user_id"):
        author, email = row["slack_user_id"], user_emails[row["user_id"]]
        authors[author] = email if authors.get(author, email) == email else None
    authors = {author: email for author, email in authors.items() if email and SLACK_USER.fullmatch(author)}
    if not authors or not channels:
        return coverage
    channel_ids = [row["id"] for row in channels]
    raw_fields = ["payload__event__type"]
    for prefix in ("payload__event__", "payload__event__message__"):
        raw_fields.extend(prefix + key for key in ("user", "subtype", "bot_id", "bot_profile"))
    receipts = _bounded(CommunityBridgeReceipt.objects.filter(platform="slack", channel_id__in=channel_ids).filter(
        Q(payload__event__user__in=authors) | Q(payload__event__message__user__in=authors),
    ).order_by("-pk").values(*raw_fields), counts, "slack_receipts")
    human_authors = human_slack_authors(receipts, authors, str(getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "") or ""))
    counts["slack_human_authors"] += len(human_authors)
    if not human_authors:
        return coverage
    rows = _bounded(CommunityBridgeMessageLink.objects.filter(
        channel_id__in=channel_ids, source_platform="slack", source_author_id__in=human_authors,
        source_deleted_at__isnull=True, source_message_id__gte=str(int(since.timestamp())),
    ).order_by("-source_message_id").values(
        "source_author_id", "source_channel_id", "source_message_id", "source_payload__delivery_type",
    ), counts, "slack_messages")
    for row in rows:
        stamp = slack_message_time(row["source_message_id"])
        if row["source_payload__delivery_type"] not in {"create", "edit"} or not _inside(stamp, since, now):
            continue
        email = authors[row["source_author_id"]]
        activities[email].append(ActivityEvidence("slack", "slack_message",
            f"slack:{workspace}:{row['source_channel_id']}:{row['source_message_id']}", stamp, True))
        counts["slack_messages"] += 1
    return coverage


def _newsletter(contacts, activities, since, now, counts):
    configured = all(str(getattr(settings, key, "") or "").strip() for key in ("LISTMONK_BASE_URL", "LISTMONK_API_USER", "LISTMONK_API_TOKEN"))
    cursor = source_time(cache.get("crm:listmonk-sync:cursor"))
    if cursor is None:
        detail = CrmAuditEvent.objects.filter(action="sync", target_type="listmonk").order_by("-created_at").values_list("detail", flat=True).first()
        cursor = source_time(detail.get("cursor")) if isinstance(detail, dict) else None
    coverage = _freshness(cursor, now, expected=configured,
        detail="CRM/listmonk clicks plus dated historical newsletter export. Clicks may be scanners; opens earn zero. Substack has no connected per-subscriber ingestion; undated aggregate counts are excluded.",
        listmonk_click_time="first_observed_by_sync", substack_automatic=False, email_opens_scored=False,
        query_window={"from": _iso(since), "through": _iso(now)})
    ids = {contact.pk: email for email, contact in contacts.items()}
    rows = _bounded(BlastDelivery.objects.filter(
        contact_id__in=ids, channel="email", first_clicked_at__gte=since, first_clicked_at__lte=now,
    ).order_by("pk").values("pk", "contact_id", "first_clicked_at"), counts, "newsletter")
    for row in rows:
        activities[ids[row["contact_id"]]].append(ActivityEvidence(
            "newsletter", "newsletter_click", f"crm-click:{row['pk']}", row["first_clicked_at"], False))
        counts["newsletter_clicks"] += 1
    for email, contact in contacts.items():
        attributes = contact.attributes if isinstance(contact.attributes, dict) else {}
        imported = attributes.get("engagement_import")
        columns = imported.get("source_columns") if isinstance(imported, dict) else None
        click = source_time(columns.get("Last clicked at")) if isinstance(columns, dict) else None
        if _inside(click, since, now):
            activities[email].append(ActivityEvidence("newsletter", "newsletter_click", f"newsletter-export-click:{_iso(click)}", click, False))
            counts["newsletter_export_clicks"] += 1
        if isinstance(columns, dict):
            counts["newsletter_export_contacts"] += 1
    if coverage["status"] == "unavailable" and not configured and counts["newsletter_export_contacts"]:
        coverage.update(status="partial", expected=False)
    return coverage


def collect_activity(contacts, *, as_of, lookback_days=180):
    """Read one bounded contact batch and return activities, coverage and counts.

    Account joins use exact primary email only. A failed source is explicit and
    never silently converted to zero activity. The caller owns rating writes.
    """
    now = source_time(as_of)
    if not isinstance(now, datetime):
        raise ValueError("Activity collection requires an aware as_of datetime.")
    if type(lookback_days) is not int or not 1 <= lookback_days <= 180:
        raise ValueError("Activity lookback must be between 1 and 180 days.")
    since = datetime.combine(now.astimezone(MELBOURNE).date() - timedelta(days=lookback_days - 1), time.min, tzinfo=MELBOURNE)
    contacts = {_real_email(contact.email): contact for contact in contacts if _real_email(contact.email)}
    counts = Counter(contacts=len(contacts))
    seeded, seed_cutoffs = {}, {}
    for email, contact in contacts.items():
        seeded[email], seed_cutoffs[email] = _seed(contact, since, now, counts)
    activities = {email: list(values) for email, values in seeded.items()}
    coverage = {}
    if not contacts:
        return {"activities": activities, "coverage": {}, "counts": dict(counts)}
    try:
        users = {row["email_key"]: row for row in get_user_model().objects.filter(is_active=True).annotate(
            email_key=Lower("email"),
        ).filter(email_key__in=contacts).values("id", "email_key", "slack_id", "last_login")}
        for email, user in users.items():
            if _inside(user["last_login"], since, now):
                activities[email].append(ActivityEvidence("app", "app_login", f"app-login:{user['id']}:{_iso(user['last_login'])}", user["last_login"], True))
                counts["app_logins"] += 1
        coverage["app"] = {"status": "partial", "expected": True, "last_success": _iso(now),
            "detail": "Latest explicit account sign-in only; login frequency unavailable. Session refresh, device polling and background activity are excluded.",
            "query_window": {"from": _iso(since), "through": _iso(now)}}
    except Exception as exc:
        users = {}
        coverage["app"] = _unavailable(f"Account activity read failed ({type(exc).__name__}).")
    try:
        activities, coverage["luma"] = _luma(contacts, activities, seed_cutoffs, since, now, counts)
    except Exception as exc:
        coverage["luma"] = _unavailable(f"Luma activity read failed ({type(exc).__name__}).")
    try:
        coverage["slack"] = _slack(users, activities, since, now, counts)
        if coverage["app"]["status"] == "unavailable":
            coverage["slack"] = _unavailable("Account identity lookup unavailable; Slack attribution is incomplete.")
    except Exception as exc:
        coverage["slack"] = _unavailable(f"Public Slack activity read failed ({type(exc).__name__}).")
    try:
        coverage["newsletter"] = _newsletter(contacts, activities, since, now, counts)
    except Exception as exc:
        coverage["newsletter"] = _unavailable(f"Newsletter activity read failed ({type(exc).__name__}).")
    for source in ("luma", "slack", "newsletter"):
        seed_count = sum(item.source == source for values in seeded.values() for item in values)
        coverage[source]["seed_events"] = seed_count
        if seed_count and coverage[source]["status"] == "unavailable" and not coverage[source]["expected"]:
            coverage[source]["status"] = "partial"
        seed_coverage = [contact.attributes.get(SEED_KEY, {}).get("coverage") for contact in contacts.values()
            if isinstance(contact.attributes, dict) and isinstance(contact.attributes.get(SEED_KEY), dict)]
        seed_coverage = [value for value in seed_coverage if isinstance(value, dict)]
        if seed_coverage:
            coverage[source]["seed_coverage"] = seed_coverage[0].get(source, seed_coverage[0])
    for source, entry in coverage.items():
        dated = [item.occurred_at for values in activities.values() for item in values if item.source == source]
        dated.sort(key=_instant)
        entry["observed_range"] = {"from": _iso(dated[0]) if dated else None, "through": _iso(dated[-1]) if dated else None}
        entry["source_limitations"] = [entry["detail"]]
    if any(key.endswith("_row_limit") for key in counts):
        for source in ("luma", "slack", "newsletter"):
            if any(key.startswith(source) and key.endswith("_row_limit") for key in counts):
                coverage[source].update(status="stale", row_limit_reached=True)
    return {"activities": activities, "coverage": coverage, "counts": dict(counts)}
