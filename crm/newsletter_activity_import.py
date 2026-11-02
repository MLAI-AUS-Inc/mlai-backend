"""Import dated Substack click evidence, without changing any email preferences."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone as datetime_timezone
from pathlib import Path

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models.functions import Lower
from django.utils import timezone

from community_chat.admin_access import require_mlai_admin
from core.models import AccountBan

from .emails import normalize_contact_email
from .models import Contact, CrmAuditEvent

ATTRIBUTE_KEY = "engagement_newsletter"
TARGET_TYPE = "crm_substack_activity"
CURSOR_KEY = "crm:substack-activity:last-exported-at"
LOCK_ID = 684249104419867
CHUNK_SIZE = 500


@dataclass(frozen=True)
class NewsletterActivityRow:
    """The only subscriber fields retained from the source CSV."""

    email: str
    last_clicked_at: str | None


def export_time(value) -> datetime:
    """Require an explicit timezone and normalize export timestamps to UTC."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError as exc:
        raise ValueError("Export timestamp must be an aware ISO datetime.") from exc
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Export timestamp must be an aware ISO datetime.")
    return parsed.astimezone(datetime_timezone.utc)


def _click(value) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Click timestamp must be an ISO date or aware datetime.")
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat() if len(value) == 10 else export_time(value).isoformat()
    except ValueError as exc:
        raise ValueError("Click timestamp must be an ISO date or aware datetime.") from exc


def _click_key(value: str):
    # Date-only evidence never invents a time. Exact timestamps win ties on the
    # same UTC calendar date, and otherwise the later source date wins.
    if len(value) == 10:
        return (date.fromisoformat(value), "")
    return (export_time(value).date(), export_time(value).isoformat())


def load_newsletter_activity(path, *, expected_sha256: str, exported_at) -> tuple[list[NewsletterActivityRow], str]:
    """Validate the complete source before writes; ignore every unrelated column."""

    exported = export_time(exported_at)
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("A valid expected SHA-256 is required.")
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("Newsletter export checksum does not match --expected-sha256.")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    fields = reader.fieldnames or []
    if not {"Email", "Last clicked at"}.issubset(fields) or len(fields) != len(set(fields)):
        raise ValueError("Newsletter export needs unique Email and Last clicked at columns.")
    rows, seen = [], set()
    for line, source in enumerate(reader, start=2):
        email = normalize_contact_email(source.get("Email", ""))
        if not email or email in seen or None in source or source.get("Last clicked at") is None:
            raise ValueError(f"Invalid or duplicate newsletter contact at row {line}.")
        try:
            clicked = _click(source["Last clicked at"])
            if clicked and (date.fromisoformat(clicked) > exported.date() if len(clicked) == 10 else export_time(clicked) > exported):
                raise ValueError("Click occurs after export.")
        except ValueError as exc:
            raise ValueError(f"Invalid newsletter click timestamp at row {line}.") from exc
        rows.append(NewsletterActivityRow(email=email, last_clicked_at=clicked))
        seen.add(email)
    if not rows:
        raise ValueError("Newsletter export contains no subscribers.")
    return rows, digest


def merge_newsletter_activity(attributes, row: NewsletterActivityRow, *, digest: str, exported_at) -> tuple[object, str]:
    """Replace only source evidence, preserving older real clicks and every preference."""

    if not isinstance(attributes, dict):
        return attributes, "malformed_attributes_skipped"
    exported = export_time(exported_at)
    previous = attributes.get(ATTRIBUTE_KEY)
    old_click = None
    if ATTRIBUTE_KEY in attributes:
        try:
            if not isinstance(previous, dict) or previous.get("source") != "substack_export":
                raise ValueError()
            old_export = export_time(previous.get("exported_at"))
            old_click = _click(previous.get("last_clicked_at"))
        except ValueError:
            return attributes, "malformed_source_skipped"
        if old_export >= exported:
            return attributes, "newer_or_equal_skipped"
    candidates = [value for value in (old_click, row.last_clicked_at) if value]
    clicked = max(candidates, key=_click_key) if candidates else None
    result = deepcopy(attributes)
    result[ATTRIBUTE_KEY] = {
        "source": "substack_export", "exported_at": exported.isoformat(),
        "sha256": digest, "last_clicked_at": clicked,
    }
    return result, "updated"


def _lock_import():
    # PostgreSQL serializes global freshness and contact updates together. No
    # tables or migrations are needed; SQLite's transaction handles local runs.
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_ID])


def _latest_export():
    detail = CrmAuditEvent.objects.filter(
        action=CrmAuditEvent.Action.IMPORT, target_type=TARGET_TYPE,
    ).order_by("-created_at", "-pk").values_list("detail", flat=True).first()
    return export_time(detail["exported_at"]) if isinstance(detail, dict) and detail.get("exported_at") else None


def _publish_cursor():
    # Re-read committed freshness under the same lock so overlapping callbacks
    # cannot move the cache cursor backward. Audit remains the durable fallback.
    try:
        with transaction.atomic():
            _lock_import()
            latest = _latest_export()
            if latest:
                cache.set(CURSOR_KEY, latest.isoformat(), timeout=None)
    except Exception:
        # The import is already committed. An unavailable cache must not turn a
        # successful source import into a retry that appears to have rolled back.
        return


def import_newsletter_activity(path, *, expected_sha256: str, exported_at, actor, dry_run=True) -> dict:
    """Atomically import existing contacts only; preview rolls back all writes."""

    require_mlai_admin(actor)
    exported = export_time(exported_at)
    now = timezone.now()
    if exported > now:
        raise ValueError("Export timestamp cannot be in the future.")
    rows, digest = load_newsletter_activity(path, expected_sha256=expected_sha256, exported_at=exported)
    counts = Counter(rows=len(rows))
    with transaction.atomic():
        _lock_import()
        latest = _latest_export()
        if latest is not None and exported <= latest:
            return {"status": "newer_or_equal_skipped", "exported_at": exported.isoformat(), "sha256": digest, "counts": dict(counts)}
        for start in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[start:start + CHUNK_SIZE]
            emails = [row.email for row in chunk]
            contacts = {contact.email: contact for contact in Contact.objects.select_for_update().filter(email__in=emails)}
            banned = set(AccountBan.objects.annotate(email_key=Lower("email")).filter(
                email_key__in=emails, revoked_at__isnull=True,
            ).values_list("email_key", flat=True))
            changed = []
            for row in chunk:
                if row.email in banned:
                    counts["banned_skipped"] += 1
                    continue
                contact = contacts.get(row.email)
                if contact is None:
                    counts["missing_skipped"] += 1
                    continue
                attributes, status = merge_newsletter_activity(contact.attributes, row, digest=digest, exported_at=exported)
                counts[status] += 1
                if status == "updated":
                    contact.attributes = attributes
                    changed.append(contact)
            if changed:
                # No profile, permission, status, timestamps or source rows change.
                Contact.objects.bulk_update(changed, ["attributes"], batch_size=250)
        result = {"status": "dry_run" if dry_run else "completed", "exported_at": exported.isoformat(), "sha256": digest, "counts": dict(counts)}
        if dry_run:
            transaction.set_rollback(True)
        else:
            CrmAuditEvent.objects.create(
                actor=actor, action=CrmAuditEvent.Action.IMPORT, target_type=TARGET_TYPE,
                target_id=digest, detail=result,
            )
            transaction.on_commit(_publish_cursor)
        return result
