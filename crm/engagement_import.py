"""Import reviewed contact-rating snapshots without granting email permission.

Only profile blanks and namespaced evidence/rating attributes are written.
Consent, delivery status, bans, source registrations, and provider audiences
are never mutated. The import cannot create a blast or invoke a sender.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from community_chat.admin_access import require_mlai_admin
from core.models import AccountBan

from .emails import normalize_contact_email
from .engagement import engagement_rating
from .models import Contact, ContactImport, CrmAuditEvent

REQUIRED_COLUMNS = {
    "Email", "MLAI stars provisional", "MLAI score provisional",
    "MLAI score version", "MLAI scored as of", "MLAI score reasons",
    "MLAI source coverage", "MLAI marketing consent",
}
CHUNK_SIZE = 500


@dataclass(frozen=True)
class SnapshotRow:
    """Validated source data for one exact normalized email address."""

    email: str
    display_name: str
    rating: dict
    source: dict
    preference_declined: bool
    new_from_luma: bool


def load_snapshot(path, *, expected_sha256: str) -> tuple[list[SnapshotRow], str]:
    """Validate the complete CSV and its approved checksum before any writes."""

    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256.lower():
        raise ValueError("Snapshot checksum does not match --expected-sha256.")
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if not REQUIRED_COLUMNS.issubset(reader.fieldnames or ()):
        raise ValueError("Missing required rating snapshot columns.")
    rows, seen = [], set()
    for line, source in enumerate(reader, start=2):
        email = normalize_contact_email(source.get("Email", ""))
        if not email or email in seen or None in source:
            raise ValueError(f"Invalid or duplicate contact at row {line}.")
        if source.get("MLAI normalized email") and source["MLAI normalized email"] != email:
            raise ValueError(f"Normalized email mismatch at row {line}.")
        try:
            stars = int(source["MLAI stars provisional"])
            if str(stars) != source["MLAI stars provisional"].strip():
                raise ValueError()
            score = float(source["MLAI score provisional"])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid rating at row {line}.") from exc
        payload = {
            "stars": stars, "score": score,
            "version": source["MLAI score version"],
            "as_of": source["MLAI scored as of"],
            "reason": source["MLAI score reasons"],
            "coverage": {
                "provisional": True, "summary": source["MLAI source coverage"],
                "luma_events_exported": 103, "luma_events_unavailable": 14,
                "slack_public_only": True, "slack_history_start": "2026-08-04",
                "app_activity_available": False, "email_opens_scored": False,
            },
        }
        if score > 100 or engagement_rating({"engagement_rating": payload}) is None:
            raise ValueError(f"Invalid rating at row {line}.")
        rows.append(SnapshotRow(
            email=email,
            display_name=(source.get("MLAI display name") or source.get("Name") or "").strip()[:300],
            rating=payload,
            source=source,
            preference_declined=source["MLAI marketing consent"] == "luma_event_updates_declined_scope_review",
            new_from_luma=source.get("MLAI new from Luma") == "yes",
        ))
        seen.add(email)
    if not rows:
        raise ValueError("The snapshot contains no contacts.")
    return rows, digest


def load_permission_evidence(path) -> dict:
    """Retain source-scoped answers as evidence, without deriving a consent grant."""

    if path is None:
        return {}
    data = json.loads(Path(path).read_text())
    if data.get("extraction_complete") is not True:
        raise ValueError("Permission evidence extraction is incomplete.")
    result = {}
    for item in data.get("evidence", []):
        email = normalize_contact_email(item.get("email", ""))
        if not email:
            raise ValueError("Permission evidence contains an invalid email.")
        result.setdefault(email, []).append({
            key: item.get(key) for key in (
                "event_id", "guest_id", "registered_at", "question_id",
                "question_label", "answer", "scope_hint",
            )
        })
    return result


def merge_snapshot(contact, row: SnapshotRow, *, digest: str, file_name: str, evidence: list) -> bool:
    """Update only rating evidence/profile blanks; preference fields stay intact.

    A hold is monotonic during import. Unknown consent cannot become eligible
    by inferred attendance, and a declined update preference is held for review.
    Suppressed contacts receive their rating while staying suppressed.
    """

    attributes = dict(contact.attributes or {})
    before = json.dumps(attributes, sort_keys=True)
    previous = engagement_rating(attributes)
    if previous is None or previous["as_of"][:10] <= row.rating["as_of"][:10]:
        attributes["engagement_rating"] = row.rating
        attributes["engagement_import"] = {
            "sha256": digest, "file_name": file_name,
            "as_of": row.rating["as_of"],
            "source_columns": row.source,
            "permission_evidence": evidence,
            "permission_review_required": True,
            "registration_time_is_not_consent_time": True,
        }
    if (
        contact.marketing_consent == Contact.MarketingConsent.UNKNOWN
        or row.preference_declined
    ):
        attributes["marketing_review_hold"] = True
    contact.attributes = attributes
    changed = before != json.dumps(attributes, sort_keys=True)
    if not contact.display_name and row.display_name:
        contact.display_name = row.display_name
        changed = True
    return changed


def import_snapshot(
    rows: list[SnapshotRow], *, digest: str, file_name: str,
    evidence_by_email: dict, actor, dry_run: bool = True,
) -> dict:
    """Atomically import a validated snapshot; default to a rollback-only preview."""

    require_mlai_admin(actor)
    counts = Counter(rows=len(rows))
    now = timezone.now()
    with transaction.atomic():
        record = ContactImport.objects.create(
            file_name=file_name[:255], uploaded_by=actor,
            consent=ContactImport.Consent.NONE,
            consent_source=f"engagement_snapshot:{digest}",
            dry_run=dry_run, row_count=len(rows),
            column_plan={"Email": "email", "rating": "attributes.engagement_rating"},
        )
        for start in range(0, len(rows), CHUNK_SIZE):
            chunk = rows[start:start + CHUNK_SIZE]
            emails = [row.email for row in chunk]
            banned = set(AccountBan.objects.annotate(email_key=Lower("email")).filter(
                email_key__in=emails, revoked_at__isnull=True,
            ).values_list("email_key", flat=True))
            existing = set(Contact.objects.filter(email__in=emails).values_list("email", flat=True))
            new_rows = [row for row in chunk if row.email not in existing and row.email not in banned]
            Contact.objects.bulk_create([
                Contact(
                    email=row.email,
                    source=Contact.Source.LUMA if row.new_from_luma else Contact.Source.IMPORT,
                    first_seen_at=now, attributes={"marketing_review_hold": True},
                ) for row in new_rows
            ], ignore_conflicts=True)
            contacts = {
                c.email: c for c in Contact.objects.select_for_update().filter(email__in=emails)
            }
            changed = []
            for row in chunk:
                if row.email in banned:
                    counts["banned_skipped"] += 1
                    continue
                contact = contacts.get(row.email)
                if contact is None:
                    counts["banned_not_created"] += 1
                    continue
                if merge_snapshot(
                    contact, row, digest=digest, file_name=file_name,
                    evidence=evidence_by_email.get(row.email, []),
                ):
                    contact.updated_at = now
                    changed.append(contact)
                    counts["created" if row.email not in existing else "updated"] += 1
                else:
                    counts["unchanged"] += 1
                counts[f"stars_{row.rating['stars']}"] += 1
                if contact.attributes.get("marketing_review_hold") is True:
                    counts["held_for_permission_review"] += 1
                if contact.email_status != Contact.EmailStatus.ACTIVE:
                    counts["existing_suppression_preserved"] += 1
            if changed:
                # Never write permission/status columns, even if they changed concurrently.
                Contact.objects.bulk_update(
                    changed, ["attributes", "display_name", "updated_at"], batch_size=250,
                )
        record.counts = dict(counts)
        record.rows_received = len(rows)
        record.status = ContactImport.Status.COMPLETED
        record.finished_at = now
        record.save(update_fields=["counts", "rows_received", "status", "finished_at", "updated_at"])
        CrmAuditEvent.objects.create(
            actor=actor, action=CrmAuditEvent.Action.IMPORT,
            target_type="contact_import", target_id=str(record.pk),
            detail={"kind": "engagement_snapshot", "sha256": digest, "counts": dict(counts)},
        )
        result = {
            "import_id": None if dry_run else str(record.pk),
            "dry_run": dry_run, "sha256": digest, "counts": dict(counts),
            "consent_granted": 0, "messages_created": 0,
        }
        if dry_run:
            transaction.set_rollback(True)
    return result
