"""Import contacts from a CSV file or from chunks uploaded in MLAI Chat.

Parsing is pure so it can be checked without a database. Applying an import
never overwrites a filled field, never re-subscribes anyone who unsubscribed,
bounced or is banned, and stores no row data in the import record.

The management command parses the whole file and calls ``apply_import``. An
upload stores its column plan with ``start_upload_import``; each chunk of
``{header: value}`` rows then goes through the same ``_parse_row`` and
``_apply_chunk`` path in ``apply_upload_chunk``.

Uploads act for someone in MLAI Chat, so each upload step refuses an actor who
isn't an MLAI admin. ``apply_import`` is the operator's command (shell access,
no acting account) and isn't gated.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Iterable, Iterator, Optional

from django.db import transaction
from django.db.models.functions import Lower
from django.utils import timezone

from community_chat.admin_access import require_mlai_admin
from core.models import AccountBan

from .emails import normalize_contact_email
from .luma_sync import users_by_email
from .contact_fields import fill_phone, grant_whatsapp_opt_in
from .models import Channel, Contact, ContactConsentEvent, ContactImport, CrmAuditEvent

PROFILE_FIELDS = (
    "first_name",
    "last_name",
    "display_name",
    "company",
    "job_title",
    "city",
    "phone",
    "linkedin_url",
)
_FIELD_LIMITS = {
    "first_name": 150,
    "last_name": 150,
    "display_name": 300,
    "company": 300,
    "job_title": 300,
    "city": 120,
    "phone": 64,
    "linkedin_url": 500,
}
TARGETS = (
    "email", *PROFILE_FIELDS, "tags", "consent_at", "consent_source", "whatsapp_opt_in", "ignore"
)
COLUMN_ALIASES = {
    "email": ("email", "email address", "e mail", "e mail address", "user email"),
    "first_name": ("first name", "firstname", "given name", "fname"),
    "last_name": ("last name", "lastname", "surname", "family name", "lname"),
    "display_name": ("name", "full name", "display name"),
    "company": ("company", "company name", "organisation", "organization"),
    "job_title": ("job title", "title", "role", "position"),
    "city": ("city", "location"),
    "phone": ("phone", "phone number", "mobile", "mobile number"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin profile"),
    "tags": ("tags", "tag"),
    "consent_at": (
        "opt in date",
        "opt in time",
        "optin time",
        "opted in at",
        "consent date",
        "consent at",
        "subscribed at",
        "confirm time",
    ),
    "consent_source": ("opt in source", "consent source", "signup source", "source"),
    "whatsapp_opt_in": ("whatsapp opt in", "whatsapp optin", "whatsapp consent", "whatsapp"),
}
MAX_TAGS = 50
MAX_ATTRIBUTE_KEYS = 50
STALE_UPLOAD_AFTER = timedelta(hours=1)
CHUNK_SIZE = 1000
BULK_BATCH_SIZE = 250
ERROR_SAMPLE_LIMIT = 20
MAX_COLUMNS = 200
MAX_UPLOAD_ROWS = 1_000_000
FIRST_DATA_ROW = 2  # row 1 of the file is the header
_ATTRIBUTE_TARGET = re.compile(r"^attributes\.[a-z0-9][a-z0-9_]{0,254}$")
_YES = {"yes", "y", "true", "1", "on", "checked", "agree", "i agree", "opted in"}
_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


class ImportFileError(ValueError):
    """The file cannot be imported as a whole (for example, no email column)."""

    def __init__(self, message: str, code: str = "invalid_file"):
        self.code = code
        super().__init__(message)


class ImportChunkError(ValueError):
    """An uploaded chunk was refused; nothing in it was applied."""

    def __init__(self, code: str, **detail):
        self.code, self.detail = code, detail
        super().__init__(code)


@dataclass
class ParsedRow:
    row_number: int
    email: str
    profile: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)
    consent_at: Optional[datetime] = None
    consent_source: str = ""
    whatsapp_opt_in: bool = False

    def merge(self, other: "ParsedRow") -> None:
        """Fill this row's blanks from a later duplicate of the same email."""

        for name, value in other.profile.items():
            self.profile.setdefault(name, value)
        self.tags = _unique([*self.tags, *other.tags])[:MAX_TAGS]
        for key, value in other.attributes.items():
            if len(self.attributes) < MAX_ATTRIBUTE_KEYS:
                self.attributes.setdefault(key, value)
        self.consent_at = self.consent_at or other.consent_at
        self.consent_source = self.consent_source or other.consent_source
        self.whatsapp_opt_in = self.whatsapp_opt_in or other.whatsapp_opt_in


@dataclass
class ParsedFile:
    rows: list
    counts: Counter
    errors: list


def normalize_header(value: str) -> str:
    return " ".join(re.sub(r"[_\-]+", " ", str(value or "")).casefold().split())


def plan_columns(
    headers: Iterable[str], overrides: Optional[dict] = None, *, require_email: bool = True
) -> dict:
    """Map each CSV header to a contact field, ``attributes.<key>`` or ``ignore``.

    With ``require_email=False`` a plan without an email column is still
    returned, so the upload screen can show it and let a person pick one.
    """

    overrides = {normalize_header(key): value for key, value in (overrides or {}).items()}
    aliases = {
        normalize_header(alias): target
        for target, names in COLUMN_ALIASES.items()
        for alias in names
    }
    plan, used = {}, set()
    for header in headers:
        key = normalize_header(header)
        target = overrides.get(key)
        if target is not None:
            if target not in TARGETS:
                raise ImportFileError(
                    f"Unknown field {target!r} in --map for column {header!r}.", "unknown_target"
                )
        else:
            target = aliases.get(key)
            if target in used:
                target = None
        if target is None:
            slug = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
            target = f"attributes.{slug}" if slug else "ignore"
        if target not in ("ignore",) and not target.startswith("attributes."):
            used.add(target)
        plan[header] = target
    if require_email and "email" not in plan.values():
        raise ImportFileError(
            "The file has no email column; name one with --map HEADER=email.", "no_email_column"
        )
    return plan


def validate_column_plan(mapping) -> dict:
    """Check an uploaded ``{header: target}`` plan, as edited after ``plan_columns``."""

    if not isinstance(mapping, dict) or not mapping:
        raise ImportFileError("Send a mapping of column headers to fields.", "invalid_mapping")
    if len(mapping) > MAX_COLUMNS:
        raise ImportFileError(f"A file can have at most {MAX_COLUMNS} columns.", "too_many_columns")
    plan, used = {}, set()
    for header, target in mapping.items():
        if not isinstance(header, str) or len(header) > 255 or not isinstance(target, str):
            raise ImportFileError("Column headers and fields must be text.", "invalid_mapping")
        if target not in TARGETS and not _ATTRIBUTE_TARGET.match(target):
            raise ImportFileError(f"Unknown field for column {header!r}.", "unknown_target")
        if target != "ignore" and not target.startswith("attributes."):
            if target in used:
                raise ImportFileError(f"Two columns map to {target}.", "duplicate_target")
            used.add(target)
        plan[header] = target
    if "email" not in used:
        raise ImportFileError("The mapping has no email column.", "no_email_column")
    return plan


def parse_rows(rows: Iterable[dict], plan: dict, *, first_row_number: int = FIRST_DATA_ROW) -> ParsedFile:
    """Validate rows and keep one per email, merging duplicates into the first."""

    by_email: dict[str, ParsedRow] = {}
    counts: Counter = Counter()
    errors: list = []
    for row_number, raw in enumerate(rows, start=first_row_number):
        counts["rows"] += 1
        parsed = _parse_row(row_number, raw, plan, counts)
        if parsed is None:
            counts["invalid"] += 1
            if len(errors) < ERROR_SAMPLE_LIMIT:
                errors.append({"row": row_number, "reason": "invalid_email"})
            continue
        existing = by_email.get(parsed.email)
        if existing is None:
            by_email[parsed.email] = parsed
        else:
            counts["duplicate"] += 1
            existing.merge(parsed)
    return ParsedFile(rows=list(by_email.values()), counts=counts, errors=errors)


def read_csv(handle, *, delimiter: str = ",", overrides: Optional[dict] = None) -> tuple[dict, ParsedFile]:
    reader = csv.DictReader(handle, delimiter=delimiter)
    if not reader.fieldnames:
        raise ImportFileError("The file is empty or has no header row.", "empty_file")
    plan = plan_columns(reader.fieldnames, overrides)
    return plan, parse_rows(reader, plan)


def parse_consent_datetime(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        parsed = None
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def apply_import(
    parsed: ParsedFile,
    *,
    file_name: str,
    consent: str,
    consent_source: str = "",
    tags: Iterable[str] = (),
    actor=None,
    dry_run: bool = False,
    column_plan: Optional[dict] = None,
    origin: str = ContactImport.Origin.COMMAND,
    now: Optional[datetime] = None,
) -> ContactImport:
    """Write the parsed rows in chunks and return the finished import record."""

    now = now or timezone.now()
    default_source, extra_tags = _import_options(file_name, consent, consent_source, tags)
    counts = Counter(parsed.counts)
    record_fields = {
        "file_name": file_name[:255],
        "uploaded_by": actor,
        "consent": consent,
        "consent_source": default_source,
        "tags": extra_tags,
        "origin": origin,
        "column_plan": column_plan or {},
        "row_count": counts["rows"],
    }
    if dry_run:
        with transaction.atomic():
            for chunk in _chunks(parsed.rows, CHUNK_SIZE):
                counts.update(_apply_chunk(chunk, consent, default_source, extra_tags, now))
            transaction.set_rollback(True)
        return ContactImport.objects.create(
            **record_fields,
            dry_run=True,
            status=ContactImport.Status.COMPLETED,
            counts=dict(counts),
            error_sample=parsed.errors,
            rows_received=counts["rows"],
            finished_at=timezone.now(),
        )

    record = ContactImport.objects.create(**record_fields)
    try:
        for chunk in _chunks(parsed.rows, CHUNK_SIZE):
            with transaction.atomic():
                counts.update(_apply_chunk(chunk, consent, default_source, extra_tags, now))
    except Exception:
        record.status = ContactImport.Status.FAILED
        record.counts = dict(counts)
        record.error_sample = parsed.errors
        record.finished_at = timezone.now()
        record.save(update_fields=["status", "counts", "error_sample", "finished_at", "updated_at"])
        raise
    record.status = ContactImport.Status.COMPLETED
    record.counts = dict(counts)
    record.error_sample = parsed.errors
    record.rows_received = counts["rows"]
    record.finished_at = timezone.now()
    record.save(
        update_fields=["status", "counts", "error_sample", "rows_received", "finished_at", "updated_at"]
    )
    CrmAuditEvent.objects.create(
        actor=actor,
        action=CrmAuditEvent.Action.IMPORT,
        target_type="contact_import",
        target_id=str(record.pk),
        detail=dict(counts),
    )
    return record


def start_upload_import(
    *,
    file_name: str,
    consent: str,
    column_plan: dict,
    consent_source: str = "",
    tags: Iterable[str] = (),
    row_count: int = 0,
    dry_run: bool = False,
    actor,
) -> ContactImport:
    """Open an upload; its rows arrive later in chunks of up to ``CHUNK_SIZE``."""

    require_mlai_admin(actor)
    plan = validate_column_plan(column_plan)
    default_source, extra_tags = _import_options(file_name, consent, consent_source, tags)
    expire_stale_uploads()
    with transaction.atomic():
        record = ContactImport.objects.create(
            file_name=file_name[:255],
            uploaded_by=actor,
            consent=consent,
            consent_source=default_source,
            tags=extra_tags,
            dry_run=dry_run,
            origin=ContactImport.Origin.UPLOAD,
            column_plan=plan,
            row_count=max(0, min(int(row_count or 0), MAX_UPLOAD_ROWS)),
        )
        _audit_upload(record, actor, "start")
    return record


def apply_upload_chunk(
    import_id, *, start_row: int, rows: list, actor, now: Optional[datetime] = None
) -> ContactImport:
    """Apply one chunk with the stored column plan and merge its counts.

    ``start_row`` is the zero-based index of the chunk's first data row, so it
    must equal ``rows_received``. Re-sending a chunk that was already applied
    returns the record unchanged, which makes a retried request safe. The import
    row stays locked while the chunk is applied, so chunks never interleave.
    """

    if not isinstance(rows, list) or not rows:
        raise ImportChunkError("no_rows")
    if len(rows) > CHUNK_SIZE:
        raise ImportChunkError("too_many_rows", limit=CHUNK_SIZE)
    if any(not isinstance(row, dict) for row in rows):
        raise ImportChunkError("invalid_rows")
    if isinstance(start_row, bool) or not isinstance(start_row, int) or start_row < 0:
        raise ImportChunkError("invalid_start_row")
    # After the shape checks (no database work), before anything is read or written.
    require_mlai_admin(actor)
    now = now or timezone.now()
    with transaction.atomic():
        record = ContactImport.objects.select_for_update().get(pk=import_id)
        if record.status != ContactImport.Status.RUNNING or record.origin != ContactImport.Origin.UPLOAD:
            raise ImportChunkError("import_not_running")
        received = record.rows_received
        if start_row < received and start_row + len(rows) <= received:
            return record
        if start_row != received:
            raise ImportChunkError("start_row_mismatch", expected_start_row=received)
        if record.row_count and received + len(rows) > record.row_count:
            raise ImportChunkError("row_count_exceeded", row_count=record.row_count)

        parsed = parse_rows(rows, record.column_plan, first_row_number=start_row + FIRST_DATA_ROW)
        if record.dry_run:
            with transaction.atomic():
                applied = _apply_chunk(parsed.rows, record.consent, record.consent_source, record.tags, now)
                transaction.set_rollback(True)
        else:
            applied = _apply_chunk(parsed.rows, record.consent, record.consent_source, record.tags, now)
        counts = Counter(record.counts)
        counts.update(parsed.counts)
        counts.update(applied)
        record.counts = dict(counts)
        record.error_sample = [*record.error_sample, *parsed.errors][:ERROR_SAMPLE_LIMIT]
        record.rows_received = received + len(rows)
        record.save(update_fields=["counts", "error_sample", "rows_received", "updated_at"])
    return record


def finish_upload_import(import_id, *, actor) -> ContactImport:
    """Close an upload and record its counts in the audit log."""

    require_mlai_admin(actor)
    with transaction.atomic():
        record = ContactImport.objects.select_for_update().get(pk=import_id)
        if record.status != ContactImport.Status.RUNNING or record.origin != ContactImport.Origin.UPLOAD:
            raise ImportChunkError("import_not_running")
        record.status = ContactImport.Status.COMPLETED
        record.finished_at = timezone.now()
        record.save(update_fields=["status", "finished_at", "updated_at"])
        _audit_upload(record, actor, "finish")
    return record


def cancel_upload_import(import_id, *, actor) -> ContactImport:
    """Stop an upload that is still running. A finished upload is returned unchanged.

    Rows already applied stay applied; a dry run never wrote any.
    """

    require_mlai_admin(actor)
    with transaction.atomic():
        record = ContactImport.objects.select_for_update().get(pk=import_id)
        if record.status != ContactImport.Status.RUNNING or record.origin != ContactImport.Origin.UPLOAD:
            return record
        _close_upload(record, reason="stopped")
        _audit_upload(record, actor, "cancel")
    return record


def expire_stale_uploads(now: Optional[datetime] = None) -> int:
    """Close uploads abandoned mid-stream: no chunk for ``STALE_UPLOAD_AFTER``."""

    now = now or timezone.now()
    stale = ContactImport.objects.filter(
        status=ContactImport.Status.RUNNING,
        origin=ContactImport.Origin.UPLOAD,
        updated_at__lt=now - STALE_UPLOAD_AFTER,
    )
    expired = 0
    with transaction.atomic():
        for record in stale.select_for_update(skip_locked=True):
            _close_upload(record, reason="expired")
            expired += 1
    return expired


def _close_upload(record: ContactImport, *, reason: str) -> None:
    record.status = ContactImport.Status.FAILED
    record.finished_at = timezone.now()
    record.error_sample = [*record.error_sample, {"reason": reason}][-ERROR_SAMPLE_LIMIT:]
    record.save(update_fields=["status", "finished_at", "error_sample", "updated_at"])


def _audit_upload(record: ContactImport, actor, operation: str) -> None:
    CrmAuditEvent.objects.create(
        actor=actor,
        action=CrmAuditEvent.Action.IMPORT,
        target_type="contact_import",
        target_id=str(record.pk),
        detail={
            "operation": operation,
            "origin": record.origin,
            "dry_run": record.dry_run,
            "row_count": record.row_count,
            "rows_received": record.rows_received,
            "counts": dict(record.counts),
        },
    )


def _import_options(file_name: str, consent: str, consent_source: str, tags: Iterable[str]) -> tuple[str, list]:
    if consent not in ContactImport.Consent.values:
        raise ValueError(f"Unknown consent value {consent!r}.")
    extra_tags = _unique(_clean_tag(tag) for tag in tags if _clean_tag(tag))
    default_source = str(consent_source or "").strip()[:255] or f"csv:{file_name}"[:255]
    return default_source, extra_tags


def _apply_chunk(rows: list, consent: str, default_source: str, extra_tags: list, now: datetime) -> Counter:
    counts: Counter = Counter()
    emails = [row.email for row in rows]
    existed = set(Contact.objects.filter(email__in=emails).values_list("email", flat=True))
    banned = set(
        AccountBan.objects.annotate(email_key=Lower("email"))
        .filter(email_key__in=emails, revoked_at__isnull=True)
        .values_list("email_key", flat=True)
    )
    users = users_by_email(emails)
    Contact.objects.bulk_create(
        [
            Contact(
                email=row.email,
                source=Contact.Source.IMPORT,
                first_seen_at=row.consent_at or now,
                user=users.get(row.email),
            )
            for row in rows
            if row.email not in existed and row.email not in banned
        ],
        ignore_conflicts=True,
    )
    contacts = {
        contact.email: contact
        for contact in Contact.objects.select_for_update().filter(email__in=emails)
    }

    changed, consent_events = [], []
    for row in rows:
        contact = contacts.get(row.email)
        is_new = row.email not in existed
        if contact is None or row.email in banned or (
            not is_new
            and (
                contact.email_status != Contact.EmailStatus.ACTIVE
                or contact.marketing_consent == Contact.MarketingConsent.WITHDRAWN
            )
        ):
            counts["suppressed"] += 1
            continue
        dirty = False
        for name, value in row.profile.items():
            if name != "phone" and value and not getattr(contact, name):
                setattr(contact, name, value)
                dirty = True
        if fill_phone(contact, row.profile.get("phone", ""), now=now):
            dirty = True
        merged_tags = _unique([*contact.tags, *row.tags, *extra_tags])[:MAX_TAGS]
        if merged_tags != list(contact.tags):
            contact.tags = merged_tags
            dirty = True
        for key, value in row.attributes.items():
            if key not in contact.attributes and len(contact.attributes) < MAX_ATTRIBUTE_KEYS:
                contact.attributes[key] = value
                dirty = True
        if contact.user_id is None and users.get(row.email):
            contact.user = users[row.email]
            dirty = True
        if (
            consent == ContactImport.Consent.GRANTED
            and contact.marketing_consent == Contact.MarketingConsent.UNKNOWN
        ):
            occurred_at = row.consent_at or now
            contact.marketing_consent = Contact.MarketingConsent.GRANTED
            contact.marketing_consent_source = (row.consent_source or default_source)[:255]
            contact.marketing_consent_at = occurred_at
            consent_events.append(
                ContactConsentEvent(
                    contact=contact,
                    channel=Channel.EMAIL,
                    kind=ContactConsentEvent.Kind.GRANTED,
                    source=contact.marketing_consent_source,
                    detail={"import_source": default_source, "row": row.row_number},
                    occurred_at=occurred_at,
                )
            )
            counts["consent_granted"] += 1
            dirty = True
        if row.whatsapp_opt_in:
            whatsapp_event = grant_whatsapp_opt_in(
                contact,
                source=row.consent_source or default_source,
                at=row.consent_at or now,
            )
            if whatsapp_event is not None:
                whatsapp_event.detail = {"import_source": default_source, "row": row.row_number}
                consent_events.append(whatsapp_event)
                counts["whatsapp_opt_in"] += 1
                dirty = True
        if is_new:
            counts["created"] += 1
        elif dirty:
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
        if dirty:
            contact.updated_at = timezone.now()
            changed.append(contact)

    if changed:
        Contact.objects.bulk_update(
            changed,
            [
                *PROFILE_FIELDS,
                "tags",
                "attributes",
                "user",
                "marketing_consent",
                "marketing_consent_source",
                "marketing_consent_at",
                "phone_e164",
                "sms_status",
                "sms_status_changed_at",
                "whatsapp_opt_in",
                "whatsapp_opt_in_source",
                "whatsapp_opt_in_at",
                "updated_at",
            ],
            batch_size=BULK_BATCH_SIZE,
        )
    ContactConsentEvent.objects.bulk_create(consent_events)
    return counts


def _parse_row(row_number: int, raw: dict, plan: dict, counts: Counter) -> Optional[ParsedRow]:
    email = ""
    parsed = ParsedRow(row_number=row_number, email="")
    for header, target in plan.items():
        value = " ".join(str(raw.get(header) or "").split())
        if not value or target == "ignore":
            continue
        if target == "email":
            email = normalize_contact_email(value)
        elif target in _FIELD_LIMITS:
            if target == "linkedin_url" and not value.startswith("https://"):
                parsed.attributes.setdefault("linkedin", value[:500])
                continue
            parsed.profile.setdefault(target, value[: _FIELD_LIMITS[target]])
        elif target == "tags":
            parsed.tags = _unique(
                [*parsed.tags, *(tag for tag in map(_clean_tag, re.split(r"[;,]", value)) if tag)]
            )[:MAX_TAGS]
        elif target == "consent_at":
            parsed.consent_at = parse_consent_datetime(value)
            if parsed.consent_at is None:
                counts["consent_date_unparsed"] += 1
        elif target == "consent_source":
            parsed.consent_source = value[:255]
        elif target == "whatsapp_opt_in":
            parsed.whatsapp_opt_in = value.casefold() in _YES
        elif target.startswith("attributes.") and len(parsed.attributes) < MAX_ATTRIBUTE_KEYS:
            parsed.attributes.setdefault(target.removeprefix("attributes."), value[:2000])
    if not email:
        return None
    parsed.email = email
    return parsed


def clean_tags(values: Iterable[str]) -> list:
    """Collapse whitespace, cut each tag to 64 characters, and drop blanks and repeats."""

    return _unique(tag for tag in map(_clean_tag, values) if tag)


def _clean_tag(value: str) -> str:
    return " ".join(str(value or "").split())[:64]


def _unique(values: Iterable[str]) -> list:
    seen, ordered = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _chunks(rows: list, size: int) -> Iterator[list]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]
