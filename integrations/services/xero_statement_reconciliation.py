"""Guarded context and proposals for Xero's browser-only statement queue."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceProvider,
    HumanitixEvent,
    ReconciliationDecision,
    ReconciliationPartyIdentity,
    ReconciliationProfile,
    XeroStatementLineSnapshot,
    XeroStatementPosting,
    XeroStatementScan,
    XeroStatementSuggestion,
)
from startup_updates.models import (
    GmailMessageArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LumaEventSelection,
    SlackMessageArtifact,
)
from integrations.services.reconciliation_rules import (
    apply_verified_rule,
    record_reconciliation_decision,
    resolve_reconciliation_rule,
    rule_evidence,
    serialize_reconciliation_rule,
    serialize_suggestion_approval,
)
from integrations.services.reconciliation_tracking import effective_tracking
from integrations.services.external_connectors import ConnectorOAuthError
from integrations.services.xero_tracking_catalog import active_xero_project_options


ALLOWED_STATEMENT_EVIDENCE_PROVIDERS = {
    "admin_rule",
    "document",
    "gmail",
    "humanitix",
    "slack",
    "linear",
    "luma",
    "stripe",
    "xero",
    "xero_ui",
    "startup_memory",
}

STATEMENT_EVIDENCE_WINDOW_DAYS = 31
STATEMENT_EVIDENCE_LIMIT = 5
STATEMENT_ENTITY_EVIDENCE_WINDOW_DAYS = 14
STATEMENT_ENTITY_EVIDENCE_LIMIT = 5
STATEMENT_NEARBY_EVENT_WINDOW_DAYS = 45
STATEMENT_CAPTURE_SCHEMA_VERSION = 2
STATEMENT_CAPTURE_SOURCE_CSV = "xero_uncoded_statement_lines_csv"
STATEMENT_CAPTURE_SOURCE_BROWSER = "xero_browser_all_accounts"
STATEMENT_CAPTURE_SOURCES = {
    STATEMENT_CAPTURE_SOURCE_CSV,
    STATEMENT_CAPTURE_SOURCE_BROWSER,
}
STATEMENT_CAPTURE_REPORT_FORMATS = {
    STATEMENT_CAPTURE_SOURCE_CSV: {
        "xero-statement-lines-compact-v1",
        "xero-uncoded-lines-grouped-v1",
        "uncoded_statement_lines",
    },
    STATEMENT_CAPTURE_SOURCE_BROWSER: {"xero_bank_reconciliation_dom"},
}
_CAPTURE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GROUP_CAPTURE_KEYS = {
    "capture_source",
    "capture_id",
    "source_sha256",
    "account_source_sha256",
    "report_format",
    "tenant_id",
    "organisation_name",
    "bank_account_label",
    "account_position",
    "account_count",
    "active_bank_account_ids",
    "all_accounts_requested",
    "full_organisation_coverage_confirmed",
    "period_start",
    "period_end",
    "date_range_confirmed",
}
_CAPTURE_METADATA_KEYS = {
    "schema_version",
    "scan_id",
    "source_started_at",
    "source_completed_at",
    "pages",
    "derived_complete",
    "blocking_reasons",
    *_GROUP_CAPTURE_KEYS,
}


class StatementCaptureValidationError(ValueError):
    """A statement capture cannot prove current, organisation-wide coverage."""


@dataclass(frozen=True)
class StatementCaptureSelection:
    """The exact coherent scan set that may feed one reconciliation run."""

    capture_id: str = ""
    capture_source: str = ""
    scans: tuple[XeroStatementScan, ...] = ()
    active_bank_accounts: tuple[dict[str, str], ...] = ()
    capture_fingerprint: str = ""
    period_start: str = ""
    period_end: str = ""
    max_age_minutes: int = 30
    blockers: tuple[str, ...] = ()

    @property
    def all_account_capture(self) -> bool:
        return bool(self.scans and not self.blockers)

    @property
    def scan_ids(self) -> tuple[int, ...]:
        return tuple(scan.id for scan in self.scans)

    @property
    def latest_scan(self) -> XeroStatementScan | None:
        if not self.scans:
            return None
        return max(self.scans, key=lambda scan: (scan.started_at, scan.id))

    def readiness_payload(self) -> dict[str, Any]:
        return {
            "schema_version": STATEMENT_CAPTURE_SCHEMA_VERSION,
            "complete": self.all_account_capture,
            "capture_id": self.capture_id,
            "capture_source": self.capture_source,
            "capture_fingerprint": self.capture_fingerprint,
            "statement_scan_ids": list(self.scan_ids),
            "active_bank_account_ids": [
                account["bank_account_id"] for account in self.active_bank_accounts
            ],
            "period_start": self.period_start or None,
            "period_end": self.period_end or None,
            "max_age_minutes": self.max_age_minutes,
            "blockers": list(self.blockers),
        }
_MERCHANT_NOISE = {
    "and",
    "app",
    "air",
    "aus",
    "australia",
    "bank",
    "bpa",
    "business",
    "card",
    "commbank",
    "conte",
    "credit",
    "debit",
    "direct",
    "eftpos",
    "fast",
    "fee",
    "fees",
    "from",
    "help",
    "inc",
    "inv",
    "international",
    "lib",
    "lin",
    "limited",
    "ltd",
    "mastercard",
    "mis",
    "miss",
    "mr",
    "mrs",
    "npp",
    "online",
    "payment",
    "payments",
    "pos",
    "pty",
    "purchase",
    "refund",
    "return",
    "stores",
    "stripe",
    "the",
    "transfer",
    "trip",
    "visa",
    "www",
}


def canonical_bank_account_id(value: Any) -> str:
    """Return the comparison key for a Xero bank-account identifier.

    Xero surfaces the same UUID in both hyphenated and compact forms. Treating
    those strings as different accounts duplicates every active statement row
    and can make a reconciliation run analyse stale queue entries.
    """

    return "".join(
        character
        for character in str(value or "").casefold()
        if character.isalnum()
    )


def _resolved_bank_account_identity(*, organization, bank_account_id: str) -> tuple[str, set[str]]:
    """Choose one existing storage spelling and return every equivalent alias."""

    comparison_key = canonical_bank_account_id(bank_account_id)
    if not comparison_key:
        raise ValueError("bank_account_id is required")

    existing_ids = {
        str(value or "").strip()
        for value in XeroStatementLineSnapshot.objects.filter(
            organization=organization,
        ).values_list("bank_account_id", flat=True).distinct()
        if str(value or "").strip()
    }
    existing_ids.update(
        str(value or "").strip()
        for value in XeroStatementScan.objects.filter(
            organization=organization,
        ).values_list("bank_account_id", flat=True).distinct()
        if str(value or "").strip()
    )
    equivalent_existing = {
        value
        for value in existing_ids
        if canonical_bank_account_id(value) == comparison_key
    }
    compact_existing = {
        value
        for value in equivalent_existing
        if value.casefold() == comparison_key
    }
    if compact_existing:
        preferred = (
            XeroStatementScan.objects.filter(
                organization=organization,
                bank_account_id__in=compact_existing,
            )
            .order_by("-started_at", "-id")
            .values_list("bank_account_id", flat=True)
            .first()
        )
        if not preferred:
            preferred = (
                XeroStatementLineSnapshot.objects.filter(
                    organization=organization,
                    bank_account_id__in=compact_existing,
                )
                .order_by("-last_seen_at", "-id")
                .values_list("bank_account_id", flat=True)
                .first()
            )
        storage_id = str(preferred or sorted(compact_existing)[0])
    elif str(bank_account_id).strip().casefold() == comparison_key:
        storage_id = str(bank_account_id).strip()
    elif equivalent_existing:
        storage_id = str(
            XeroStatementScan.objects.filter(
                organization=organization,
                bank_account_id__in=equivalent_existing,
            )
            .order_by("-started_at", "-id")
            .values_list("bank_account_id", flat=True)
            .first()
            or sorted(equivalent_existing)[0]
        )
    else:
        storage_id = str(bank_account_id).strip()

    return storage_id, equivalent_existing | {storage_id, str(bank_account_id).strip()}


def _active_statement_lines(
    *,
    organization,
    statement_line_ids: list[str] | set[str] | None = None,
) -> list[XeroStatementLineSnapshot]:
    """Return current rows while suppressing legacy account-ID aliases."""

    queryset = XeroStatementLineSnapshot.objects.filter(
        organization=organization,
        active=True,
    )
    if statement_line_ids is not None:
        queryset = queryset.filter(statement_line_id__in=statement_line_ids)
    newest_by_identity: dict[tuple[str, str], XeroStatementLineSnapshot] = {}
    for line in queryset.order_by("-last_seen_at", "-id"):
        identity = (
            canonical_bank_account_id(line.bank_account_id),
            line.statement_line_id,
        )
        newest_by_identity.setdefault(identity, line)
    return sorted(
        newest_by_identity.values(),
        key=lambda line: (line.transaction_date, line.statement_line_id, line.id),
    )


_LOW_SIGNAL_MERCHANT_TERMS = {
    "australia",
    "city",
    "melbourn",
    "melbourne",
    "sydney",
    "university",
}


def merchant_key(value: Any) -> str:
    text = str(value or "").lower()
    text = re.sub(r"\bcard\s+xx\d+\b", " ", text)
    # Xero truncates some Uber narrations after ``HELP.`` while others retain
    # the processor suffix ``HELP.UB``. Treat that suffix like the other bank
    # feed noise tokens so both variants share one exact merchant key.
    text = re.sub(r"\b(?:aud|nzd|usd|pos|mis|npp|bpa|ub|m\s*t)\b", " ", text)
    text = re.sub(r"\b(?:commbank|app|payid|email)\b", " ", text)
    text = re.sub(r"\d{6,}", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _merchant_terms(line: XeroStatementLineSnapshot) -> list[str]:
    terms = {
        token
        for token in merchant_key(line.narration).split()
        if len(token) >= 3 and not token.isdigit() and token not in _MERCHANT_NOISE
    }
    return sorted(terms, key=lambda item: (-len(item), item))[:8]


def _amount_markers(amount: Decimal) -> list[str]:
    fixed = f"{amount:,.2f}"
    plain = fixed.replace(",", "")
    markers = {fixed, plain}
    if amount == amount.to_integral_value():
        markers.update({f"{amount:,.0f}", f"{amount:.0f}"})
    elif fixed.endswith("0"):
        markers.update({fixed[:-1], plain[:-1]})
    return sorted(markers, key=len, reverse=True)


def _contains_amount(text: str, markers: list[str]) -> tuple[bool, str]:
    for marker in markers:
        if re.search(rf"(?<![\d.]){re.escape(marker)}(?![\d.])", text):
            return True, marker
    return False, ""


def _evidence_excerpt(text: str, needles: list[str], *, limit: int = 700) -> str:
    compact = re.sub(r"\s+", " ", str(text or "")).strip()
    if not compact:
        return ""
    folded = compact.casefold()
    positions = [folded.find(item.casefold()) for item in needles if item and folded.find(item.casefold()) >= 0]
    start = max(0, min(positions) - 180) if positions else 0
    excerpt = compact[start : start + limit]
    if start:
        excerpt = f"…{excerpt}"
    if start + limit < len(compact):
        excerpt = f"{excerpt}…"
    return excerpt


def _score_evidence(*, text: str, terms: list[str], markers: list[str], occurred_on, transaction_date):
    folded = text.casefold()
    vendor_hits = [term for term in terms if term in folded]
    strong_vendor_hits = [term for term in vendor_hits if term not in _LOW_SIGNAL_MERCHANT_TERMS]
    amount_hit, amount_marker = _contains_amount(folded, markers)
    days = abs((occurred_on - transaction_date).days)
    if strong_vendor_hits and amount_hit:
        score = 120 + (10 * len(vendor_hits)) - days
    elif len(vendor_hits) >= 2:
        score = 80 + (8 * len(vendor_hits)) - days
    elif strong_vendor_hits and len(strong_vendor_hits[0]) >= 5:
        score = 55 - days
    else:
        return None
    if score < 30:
        return None
    reasons = [f"merchant:{term}" for term in vendor_hits[:3]]
    if amount_hit:
        reasons.append(f"amount:{amount_marker}")
    reasons.append(f"date_delta:{days}d")
    return score, reasons, vendor_hits + ([amount_marker] if amount_marker else [])


def _candidate_context_evidence(
    *,
    organization,
    line: XeroStatementLineSnapshot,
    gmail_rows: list[dict[str, Any]] | None = None,
    slack_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    terms = _merchant_terms(line)
    if not terms:
        return []
    start = line.transaction_date - timedelta(days=STATEMENT_EVIDENCE_WINDOW_DAYS)
    end = line.transaction_date + timedelta(days=STATEMENT_EVIDENCE_WINDOW_DAYS + 1)
    term_query = Q()
    for term in terms:
        term_query |= (
            Q(subject__icontains=term)
            | Q(from_address__icontains=term)
            | Q(snippet__icontains=term)
            | Q(cleaned_text__icontains=term)
            | Q(body_preview__icontains=term)
        )
    markers = _amount_markers(line.amount)
    ranked: list[tuple[float, dict[str, Any]]] = []
    if gmail_rows is None:
        gmail_rows = list(GmailMessageArtifact.objects.filter(
            Q(organization=organization)
            & Q(internal_date__date__gte=start)
            & Q(internal_date__date__lt=end)
            & term_query
        ).values(
            "gmail_message_id", "internal_date", "subject", "from_address",
            "snippet", "cleaned_text", "body_preview", "attachment_manifest",
        )[:250])
    else:
        gmail_rows = [
            row for row in gmail_rows
            if start <= row["internal_date"].date() < end
        ]
    for row in gmail_rows:
        attachment_names = " ".join(
            str(item.get("filename") or "")
            for item in row["attachment_manifest"] or []
            if isinstance(item, dict)
        )
        body = "\n".join(
            str(value or "")
            for value in [
                row["subject"],
                row["from_address"],
                row["snippet"],
                row["cleaned_text"],
                row["body_preview"],
                attachment_names,
            ]
        )
        scored = _score_evidence(
            text=body,
            terms=terms,
            markers=markers,
            occurred_on=row["internal_date"].date(),
            transaction_date=line.transaction_date,
        )
        if not scored:
            continue
        score, reasons, needles = scored
        ranked.append((score, {
            "source_provider": "gmail",
            "source_record_id": row["gmail_message_id"],
            "occurred_at": row["internal_date"].isoformat(),
            "subject": str(row["subject"] or "")[:500],
            "sender": str(row["from_address"] or "")[:500],
            "summary": _evidence_excerpt(body, needles),
            "match_reasons": reasons,
        }))

    slack_term_query = Q()
    for term in terms:
        slack_term_query |= Q(text__icontains=term) | Q(cleaned_text__icontains=term)
    if slack_rows is None:
        slack_rows = list(SlackMessageArtifact.objects.filter(
            Q(organization=organization)
            & Q(posted_at__date__gte=start)
            & Q(posted_at__date__lt=end)
            & slack_term_query
        ).values(
            "channel_id", "channel_name", "slack_message_ts", "author_name",
            "posted_at", "text", "cleaned_text",
        )[:250])
    else:
        slack_rows = [
            row for row in slack_rows
            if start <= row["posted_at"].date() < end
        ]
    for row in slack_rows:
        body = "\n".join(str(value or "") for value in [row["text"], row["cleaned_text"]])
        scored = _score_evidence(
            text=body,
            terms=terms,
            markers=markers,
            occurred_on=row["posted_at"].date(),
            transaction_date=line.transaction_date,
        )
        if not scored:
            continue
        score, reasons, needles = scored
        ranked.append((score, {
            "source_provider": "slack",
            "source_record_id": f'{row["channel_id"]}:{row["slack_message_ts"]}',
            "occurred_at": row["posted_at"].isoformat(),
            "channel_name": str(row["channel_name"] or "")[:255],
            "author_name": str(row["author_name"] or "")[:255],
            "summary": _evidence_excerpt(body, needles),
            "match_reasons": reasons,
        }))

    ranked.sort(key=lambda item: (-item[0], item[1]["occurred_at"], item[1]["source_record_id"]))
    return [item for _score, item in ranked[:STATEMENT_EVIDENCE_LIMIT]]


def _entity_aliases(value: Any) -> list[str]:
    """Return distinctive phrases that can link private context to a catalog entity."""
    raw = re.sub(r"\[[^\]]+\]", " ", str(value or "")).strip()
    candidates = [raw, *re.split(r"\s+(?:-|\||:)\s+", raw)]
    aliases: set[str] = set()
    for candidate in candidates:
        normalized = merchant_key(candidate)
        tokens = normalized.split()
        if len(normalized) >= 6 and len(tokens) >= 2:
            aliases.add(normalized)
    return sorted(aliases, key=lambda item: (-len(item), item))


def _catalog_date(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def _candidate_entity_catalogs(
    *,
    line: XeroStatementLineSnapshot,
    luma_events: list[dict[str, Any]],
    humanitix_events: list[dict[str, Any]],
    linear_projects: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    nearby_events: list[dict[str, Any]] = []
    entity_entries: list[dict[str, Any]] = []
    nearby_event_aliases: set[str] = set()
    for event in [*luma_events, *humanitix_events]:
        event_date = _catalog_date(event.get("start_at"))
        if event_date is None:
            continue
        days = (event_date - line.transaction_date).days
        if abs(days) > STATEMENT_NEARBY_EVENT_WINDOW_DAYS:
            continue
        aliases = _entity_aliases(event.get("name"))
        if not aliases:
            continue
        nearby_event_aliases.update(aliases)
        serialized = {
            "source_type": str(event.get("source_type") or "luma"),
            "source_id": str(event.get("source_id") or ""),
            "name": str(event.get("name") or ""),
            "start_at": event.get("start_at"),
            "date_delta_days": days,
        }
        nearby_events.append(serialized)
        entity_entries.append({**serialized, "aliases": aliases})
    nearby_events.sort(key=lambda item: (abs(item["date_delta_days"]), item["name"], item["source_id"]))
    nearby_events = nearby_events[:12]

    nearby_projects: list[dict[str, Any]] = []
    for project in linear_projects:
        aliases = _entity_aliases(project.get("name"))
        if not aliases:
            continue
        project_dates = [
            value
            for value in (_catalog_date(project.get("start_date")), _catalog_date(project.get("target_date")))
            if value is not None
        ]
        date_delta = min(
            ((value - line.transaction_date).days for value in project_dates),
            key=abs,
            default=None,
        )
        mirrors_nearby_event = any(
            alias in event_alias or event_alias in alias
            for alias in aliases
            for event_alias in nearby_event_aliases
        )
        source_type = str(project.get("source_type") or "linear")
        if source_type != "xero_tracking" and not mirrors_nearby_event and (
            date_delta is None or abs(date_delta) > STATEMENT_NEARBY_EVENT_WINDOW_DAYS
        ):
            continue
        serialized = {
            "source_type": source_type,
            "source_id": str(project.get("source_id") or ""),
            "tracking_option_id": str(project.get("xero_tracking_option_id") or ""),
            "name": str(project.get("name") or ""),
            "status": project.get("status"),
            "start_date": project.get("start_date"),
            "target_date": project.get("target_date"),
            "dimension_hint": project.get("dimension_hint"),
            "members": list(project.get("members") or []),
            "date_delta_days": date_delta,
        }
        nearby_projects.append(serialized)
        entity_entries.append({**serialized, "aliases": aliases})
    nearby_projects.sort(
        key=lambda item: (
            abs(item["date_delta_days"]) if item["date_delta_days"] is not None else 10_000,
            item["name"],
            item["source_id"],
        )
    )
    return nearby_events, nearby_projects[:20], entity_entries


def _matched_catalog_entities(text: str, entity_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = merchant_key(text)
    matched: list[dict[str, Any]] = []
    for entity in entity_entries:
        aliases = [alias for alias in entity["aliases"] if alias in normalized]
        if not aliases:
            continue
        matched.append({
            "source_type": entity["source_type"],
            "source_id": entity["source_id"],
            "name": entity["name"],
            "matched_alias": aliases[0],
        })
    return matched


def _candidate_event_project_evidence(
    *,
    organization,
    line: XeroStatementLineSnapshot,
    entity_entries: list[dict[str, Any]],
    gmail_rows: list[dict[str, Any]] | None = None,
    slack_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Find nearby Gmail/Slack context naming a canonical event or project.

    This deliberately complements merchant matching. Indirect purchases such as
    transport, catering, hardware and printing often never name the event in the
    bank narration, while nearby operating messages do.
    """
    if not entity_entries:
        return []
    start = line.transaction_date - timedelta(days=STATEMENT_ENTITY_EVIDENCE_WINDOW_DAYS)
    end = line.transaction_date + timedelta(days=STATEMENT_ENTITY_EVIDENCE_WINDOW_DAYS + 1)
    ranked: list[tuple[float, dict[str, Any]]] = []

    if gmail_rows is None:
        gmail_rows = list(GmailMessageArtifact.objects.filter(
            organization=organization,
            internal_date__date__gte=start,
            internal_date__date__lt=end,
        ).values(
            "gmail_message_id", "internal_date", "subject", "from_address",
            "snippet", "cleaned_text", "body_preview", "attachment_manifest",
        ).order_by("-internal_date")[:500])
    else:
        gmail_rows = [
            row for row in gmail_rows
            if start <= row["internal_date"].date() < end
        ]
    for row in gmail_rows:
        attachment_names = " ".join(
            str(item.get("filename") or "")
            for item in row["attachment_manifest"] or []
            if isinstance(item, dict)
        )
        body = "\n".join(str(value or "") for value in [
            row["subject"], row["from_address"], row["snippet"], row["cleaned_text"],
            row["body_preview"], attachment_names,
        ])
        matched = _matched_catalog_entities(body, entity_entries)
        if not matched:
            continue
        days = abs((row["internal_date"].date() - line.transaction_date).days)
        aliases = [item["matched_alias"] for item in matched]
        ranked.append((100 + (15 * len(matched)) + max(len(item) for item in aliases) - days, {
            "source_provider": "gmail",
            "source_record_id": row["gmail_message_id"],
            "occurred_at": row["internal_date"].isoformat(),
            "subject": str(row["subject"] or "")[:500],
            "sender": str(row["from_address"] or "")[:500],
            "summary": _evidence_excerpt(body, aliases),
            "match_reasons": [
                *[f'{item["source_type"]}:{item["source_id"]}' for item in matched[:4]],
                f"date_delta:{days}d",
            ],
            "matched_entities": matched[:4],
        }))

    if slack_rows is None:
        slack_rows = list(SlackMessageArtifact.objects.filter(
            organization=organization,
            posted_at__date__gte=start,
            posted_at__date__lt=end,
        ).values(
            "channel_id", "channel_name", "slack_message_ts", "author_name",
            "posted_at", "text", "cleaned_text",
        ).order_by("-posted_at")[:500])
    else:
        slack_rows = [
            row for row in slack_rows
            if start <= row["posted_at"].date() < end
        ]
    for row in slack_rows:
        body = "\n".join(str(value or "") for value in [row["text"], row["cleaned_text"]])
        matched = _matched_catalog_entities(body, entity_entries)
        if not matched:
            continue
        days = abs((row["posted_at"].date() - line.transaction_date).days)
        aliases = [item["matched_alias"] for item in matched]
        ranked.append((100 + (15 * len(matched)) + max(len(item) for item in aliases) - days, {
            "source_provider": "slack",
            "source_record_id": f'{row["channel_id"]}:{row["slack_message_ts"]}',
            "occurred_at": row["posted_at"].isoformat(),
            "channel_name": str(row["channel_name"] or "")[:255],
            "author_name": str(row["author_name"] or "")[:255],
            "summary": _evidence_excerpt(body, aliases),
            "match_reasons": [
                *[f'{item["source_type"]}:{item["source_id"]}' for item in matched[:4]],
                f"date_delta:{days}d",
            ],
            "matched_entities": matched[:4],
        }))

    ranked.sort(key=lambda item: (-item[0], item[1]["occurred_at"], item[1]["source_record_id"]))
    return [item for _score, item in ranked[:STATEMENT_ENTITY_EVIDENCE_LIMIT]]


def _account_parts(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    match = re.match(r"^([A-Za-z0-9.-]+)\s+-\s+(.+)$", raw)
    if not match:
        return "", raw
    return match.group(1).strip(), match.group(2).strip()


def _line_source_hash(payload: dict[str, Any]) -> str:
    immutable = {
        "statement_line_id": payload["statement_line_id"],
        "bank_account_id": payload["bank_account_id"],
        "transaction_date": payload["transaction_date"].isoformat(),
        "narration": payload["narration"],
        "reference": payload["reference"],
        "direction": payload["direction"],
        "amount": str(payload["amount"]),
        "currency": payload["currency"],
    }
    return hashlib.sha256(json.dumps(immutable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _browser_field(raw: dict[str, Any], canonical: str, snapshot_alias: str) -> Any:
    """Read a browser backfill field without silently dropping snapshot-shaped keys.

    The public snapshot serializer names the visible form fields ``current_*``
    while the original management-command contract used shorter names.  Keep
    the original keys canonical, but accept serializer-shaped payloads so a
    Chrome backfill cannot erase the approved Xero examples used by later
    reconciliation runs.
    """

    if canonical in raw:
        return raw.get(canonical)
    return raw.get(snapshot_alias)


def _browser_ui_mode(raw: dict[str, Any], *, visible_fields: dict[str, str]) -> str:
    explicit = str(raw.get("ui_mode") or "").strip().lower()
    allowed = {choice[0] for choice in XeroStatementLineSnapshot.UI_MODE_CHOICES}
    if explicit:
        if explicit not in allowed:
            raise ValueError(f"Invalid Xero statement ui_mode: {explicit}")
        return explicit
    if raw.get("has_green_match") is True or raw.get("match_ready") is True:
        return XeroStatementLineSnapshot.UI_GREEN_MATCH
    # ``ready_in_xero`` is accepted only as a deliberate compatibility input.
    # The historical ``has_ok`` browser flag is intentionally not used: Xero
    # shows OK beside a populated Create form as well as a genuine Match.
    if raw.get("ready_in_xero") is True:
        return XeroStatementLineSnapshot.UI_GREEN_MATCH
    if any(visible_fields.values()):
        return XeroStatementLineSnapshot.UI_CREATE_PREFILLED
    if raw.get("has_discussion") is True:
        return XeroStatementLineSnapshot.UI_DISCUSS
    return XeroStatementLineSnapshot.UI_BLANK_CREATE


def _required_capture_text(
    raw: dict[str, Any],
    field: str,
    *,
    limit: int = 255,
) -> str:
    raw_value = raw.get(field)
    if not isinstance(raw_value, str):
        raise ValueError(f"capture_metadata {field} must be a string")
    value = raw_value.strip()
    if not value:
        raise ValueError(f"capture_metadata {field} is required")
    if len(value) > limit:
        raise ValueError(f"capture_metadata {field} is too long")
    return value


def _capture_boolean(raw: dict[str, Any], field: str) -> bool:
    value = raw.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"capture_metadata {field} must be a boolean")
    return value


def _capture_sha256(
    raw: dict[str, Any],
    field: str,
    *,
    required: bool,
) -> str:
    raw_value = raw.get(field)
    if raw_value in (None, "") and not required:
        return ""
    if not isinstance(raw_value, str):
        raise ValueError(f"capture_metadata {field} must be a SHA-256 hex digest")
    value = raw_value.strip().lower()
    if not value and not required:
        return ""
    if not _CAPTURE_SHA256_RE.fullmatch(value):
        raise ValueError(f"capture_metadata {field} must be a SHA-256 hex digest")
    return value


def _capture_iso_date(raw: dict[str, Any], field: str) -> str:
    raw_value = raw.get(field)
    if raw_value in (None, ""):
        return ""
    if not isinstance(raw_value, str):
        raise ValueError(f"capture_metadata {field} must use YYYY-MM-DD")
    value = raw_value.strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ValueError(f"capture_metadata {field} must use YYYY-MM-DD") from exc


def _metadata_contains_forbidden_key(value: Any) -> bool:
    forbidden = {
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "api_key",
        "secret",
        "lines",
        "narration",
        "reference",
    }
    if isinstance(value, dict):
        return bool(forbidden.intersection(str(key).lower() for key in value)) or any(
            _metadata_contains_forbidden_key(item) for item in value.values()
        )
    if isinstance(value, list):
        return any(_metadata_contains_forbidden_key(item) for item in value)
    return False


def _sanitize_capture_metadata(
    raw: dict[str, Any] | None,
    *,
    bank_account_id: str = "",
    complete_scan: bool | None = None,
) -> dict[str, Any]:
    """Validate and retain only bounded queue-completeness evidence.

    Schema v1 is the historical single-account browser envelope. Schema v2 is
    an attestation covering every active BANK account in one capture and is
    deliberately strict: dropping one field must never turn a partial capture
    into something that looks complete.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("capture_metadata must be an object")
    if _metadata_contains_forbidden_key(raw):
        raise ValueError("capture_metadata contains a forbidden sensitive field")
    group_claimed = bool(_GROUP_CAPTURE_KEYS.intersection(raw)) or raw.get(
        "schema_version"
    ) == STATEMENT_CAPTURE_SCHEMA_VERSION
    if group_claimed:
        unknown = set(raw) - _CAPTURE_METADATA_KEYS
        if unknown:
            raise ValueError(
                "capture_metadata schema v2 contains unsupported fields: "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        if type(raw.get("schema_version")) is not int or raw.get(
            "schema_version"
        ) != STATEMENT_CAPTURE_SCHEMA_VERSION:
            raise ValueError("capture_metadata all-account fields require schema_version=2")
    pages = raw.get("pages") or []
    if not isinstance(pages, list) or len(pages) > 500:
        raise ValueError("capture_metadata pages must be a bounded list")
    safe_pages = []
    page_numbers: set[int] = set()
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("capture_metadata page evidence must be an object")
        if set(page) != {
            "page_number", "page_count", "observed_count", "has_previous", "has_next",
        }:
            raise ValueError("capture_metadata page evidence has unexpected fields")
        if not isinstance(page.get("has_previous"), bool) or not isinstance(page.get("has_next"), bool):
            raise ValueError("capture_metadata pagination flags must be booleans")
        if group_claimed and any(
            type(page.get(field)) is not int
            for field in ("page_number", "page_count", "observed_count")
        ):
            raise ValueError("capture_metadata page counts must be integers")
        try:
            safe_page = {
                "page_number": int(page.get("page_number")),
                "page_count": int(page.get("page_count")),
                "observed_count": int(page.get("observed_count")),
                "has_previous": page["has_previous"],
                "has_next": page["has_next"],
            }
        except (TypeError, ValueError) as exc:
            raise ValueError("capture_metadata page counts must be integers") from exc
        if (
            safe_page["page_number"] < 1
            or safe_page["page_count"] < 1
            or safe_page["observed_count"] < 0
            or safe_page["page_number"] > safe_page["page_count"]
        ):
            raise ValueError("capture_metadata page counts are out of range")
        if safe_page["page_number"] in page_numbers:
            raise ValueError("capture_metadata page numbers must be unique")
        page_numbers.add(safe_page["page_number"])
        safe_pages.append(safe_page)
    blockers = raw.get("blocking_reasons") or []
    if not isinstance(blockers, list) or len(blockers) > 20:
        raise ValueError("capture_metadata blocking_reasons must be a bounded list")
    schema_version = raw.get("schema_version", 1)
    if schema_version not in {1, 2}:
        raise ValueError("capture_metadata schema_version must be 1 or 2")
    safe = {
        "schema_version": schema_version,
        "scan_id": str(raw.get("scan_id") or "")[:128],
        "source_started_at": str(raw.get("source_started_at") or "")[:64],
        "source_completed_at": str(raw.get("source_completed_at") or "")[:64],
        "pages": safe_pages,
        "derived_complete": raw.get("derived_complete") is True,
        "blocking_reasons": [str(reason)[:500] for reason in blockers],
    }
    if not group_claimed:
        return safe

    capture_source = _required_capture_text(raw, "capture_source", limit=64)
    if capture_source not in STATEMENT_CAPTURE_SOURCES:
        raise ValueError("capture_metadata capture_source is not supported")
    capture_id = _required_capture_text(raw, "capture_id", limit=128)
    scan_id = _required_capture_text(raw, "scan_id", limit=128)
    tenant_id = _required_capture_text(raw, "tenant_id")
    organisation_name = _required_capture_text(raw, "organisation_name")
    derived_complete = _capture_boolean(raw, "derived_complete")
    raw_bank_account_label = raw.get("bank_account_label")
    raw_report_format = raw.get("report_format")
    if raw_bank_account_label is not None and not isinstance(raw_bank_account_label, str):
        raise ValueError("capture_metadata bank_account_label must be a string")
    if raw_report_format is not None and not isinstance(raw_report_format, str):
        raise ValueError("capture_metadata report_format must be a string")
    bank_account_label = (raw_bank_account_label or "").strip()
    report_format = (raw_report_format or "").strip()
    if derived_complete and (not bank_account_label or not report_format):
        raise ValueError(
            "A complete all-account capture requires bank_account_label and report_format"
        )
    if capture_source == STATEMENT_CAPTURE_SOURCE_CSV and not report_format:
        raise ValueError("A CSV all-account capture requires report_format")
    if len(bank_account_label) > 255 or len(report_format) > 64:
        raise ValueError("capture_metadata bank account label or report format is too long")
    if report_format and report_format not in STATEMENT_CAPTURE_REPORT_FORMATS[capture_source]:
        raise ValueError(
            "capture_metadata report_format does not match capture_source"
        )
    if type(raw.get("account_position")) is not int or type(
        raw.get("account_count")
    ) is not int:
        raise ValueError(
            "capture_metadata account_position and account_count must be integers"
        )
    account_position = raw["account_position"]
    account_count = raw["account_count"]
    if account_count < 1 or account_count > 100:
        raise ValueError("capture_metadata account_count is out of range")
    if account_position < 1 or account_position > account_count:
        raise ValueError("capture_metadata account_position is out of range")
    active_ids_raw = raw.get("active_bank_account_ids")
    if not isinstance(active_ids_raw, list) or len(active_ids_raw) != account_count:
        raise ValueError(
            "capture_metadata active_bank_account_ids must match account_count"
        )
    if any(not isinstance(value, str) for value in active_ids_raw):
        raise ValueError("capture_metadata active_bank_account_ids contains an invalid ID")
    active_ids = [value.strip() for value in active_ids_raw]
    if any(not value or len(value) > 255 for value in active_ids):
        raise ValueError("capture_metadata active_bank_account_ids contains an invalid ID")
    normalized_ids = [canonical_bank_account_id(value) for value in active_ids]
    if any(not value for value in normalized_ids):
        raise ValueError(
            "capture_metadata active_bank_account_ids contains an invalid ID"
        )
    if len(normalized_ids) != len(set(normalized_ids)):
        raise ValueError("capture_metadata active_bank_account_ids must be unique")
    if bank_account_id and canonical_bank_account_id(bank_account_id) not in normalized_ids:
        raise ValueError(
            "capture_metadata active_bank_account_ids does not contain bank_account_id"
        )

    all_accounts_requested = _capture_boolean(raw, "all_accounts_requested")
    full_coverage = _capture_boolean(raw, "full_organisation_coverage_confirmed")
    date_range_confirmed = _capture_boolean(raw, "date_range_confirmed")
    if complete_scan is not None and derived_complete is not complete_scan:
        raise ValueError(
            "capture_metadata derived_complete must agree with complete"
        )
    if derived_complete and not (
        all_accounts_requested and full_coverage and date_range_confirmed
    ):
        raise ValueError(
            "A complete all-account capture must confirm account and date coverage"
        )
    if group_claimed and any(not isinstance(reason, str) for reason in blockers):
        raise ValueError(
            "capture_metadata blocking_reasons must contain only strings"
        )
    if derived_complete and blockers:
        raise ValueError(
            "A complete all-account capture cannot contain blocking_reasons"
        )

    period_start = _capture_iso_date(raw, "period_start")
    period_end = _capture_iso_date(raw, "period_end")
    if bool(period_start) != bool(period_end):
        raise ValueError(
            "capture_metadata period_start and period_end must be supplied together"
        )
    if capture_source == STATEMENT_CAPTURE_SOURCE_CSV and not (
        period_start and period_end
    ):
        raise ValueError("CSV all-account captures require period_start and period_end")
    if period_start and period_end and period_start > period_end:
        raise ValueError("capture_metadata period_end precedes period_start")

    source_sha256 = _capture_sha256(
        raw,
        "source_sha256",
        required=capture_source == STATEMENT_CAPTURE_SOURCE_CSV,
    )
    account_source_sha256 = _capture_sha256(
        raw,
        "account_source_sha256",
        required=derived_complete,
    )
    return {
        **safe,
        "schema_version": STATEMENT_CAPTURE_SCHEMA_VERSION,
        "capture_source": capture_source,
        "capture_id": capture_id,
        "scan_id": scan_id,
        "source_sha256": source_sha256,
        "account_source_sha256": account_source_sha256,
        "report_format": report_format,
        "tenant_id": tenant_id,
        "organisation_name": organisation_name,
        "bank_account_label": bank_account_label,
        "account_position": account_position,
        "account_count": account_count,
        "active_bank_account_ids": active_ids,
        "all_accounts_requested": all_accounts_requested,
        "full_organisation_coverage_confirmed": full_coverage,
        "period_start": period_start,
        "period_end": period_end,
        "date_range_confirmed": date_range_confirmed,
        "derived_complete": derived_complete,
    }


def active_xero_bank_account_catalog(organization) -> dict[str, Any]:
    """Read and strictly validate the selected tenant's live active BANK accounts."""

    try:
        profile = ReconciliationProfile.objects.select_related("xero_connection").get(
            organization=organization
        )
    except ReconciliationProfile.DoesNotExist as exc:
        raise StatementCaptureValidationError(
            "Reconciliation profile is not configured."
        ) from exc
    connection = profile.xero_connection
    if connection is None:
        raise StatementCaptureValidationError("A Xero connection must be selected.")
    if (
        connection.organization_id != organization.id
        or connection.provider != ExternalServiceProvider.XERO
        or connection.status == "disconnected"
    ):
        raise StatementCaptureValidationError(
            "The selected Xero connection is not active for this organisation."
        )
    tenant_id = str(connection.external_account_id or "").strip()
    organisation_name = str(connection.account_label or "").strip()
    if not tenant_id or not organisation_name:
        raise StatementCaptureValidationError(
            "The selected Xero connection is missing its tenant identity."
        )

    # Local import avoids coupling the statement parser to Xero posting code at
    # module import time while still using the established authenticated client.
    from integrations.services.xero_reconciliation import fetch_xero_accounts

    raw_accounts = fetch_xero_accounts(profile)
    if not isinstance(raw_accounts, list):
        raise StatementCaptureValidationError(
            "Xero returned an invalid account catalogue."
        )
    accounts: list[dict[str, str]] = []
    for raw_account in raw_accounts:
        if not isinstance(raw_account, dict):
            raise StatementCaptureValidationError(
                "Xero returned an invalid account catalogue."
            )
        if str(raw_account.get("Type") or "").strip().upper() != "BANK":
            continue
        if str(raw_account.get("Status") or "").strip().upper() != "ACTIVE":
            continue
        account_id = str(raw_account.get("AccountID") or "").strip()
        name = " ".join(str(raw_account.get("Name") or "").split())
        if not account_id or not name or len(account_id) > 255 or len(name) > 255:
            raise StatementCaptureValidationError(
                "Xero returned an incomplete active bank-account record."
            )
        accounts.append({"bank_account_id": account_id, "name": name})
    if not accounts:
        raise StatementCaptureValidationError(
            "Xero has no active BANK accounts available for reconciliation."
        )
    if len(accounts) > 100:
        raise StatementCaptureValidationError(
            "Xero returned more than 100 active BANK accounts; full coverage cannot be attested."
        )
    normalized_ids = [
        canonical_bank_account_id(account["bank_account_id"])
        for account in accounts
    ]
    if len(normalized_ids) != len(set(normalized_ids)):
        raise StatementCaptureValidationError(
            "Xero returned duplicate active bank-account IDs."
        )
    accounts.sort(key=lambda account: (account["name"].casefold(), account["bank_account_id"].casefold()))
    return {
        "schema_version": 1,
        "tenant_id": tenant_id,
        "organisation_name": organisation_name,
        "accounts": accounts,
    }


def _capture_fingerprint(
    *,
    metadata: dict[str, Any],
    scans: list[XeroStatementScan],
    accounts: list[dict[str, str]],
) -> str:
    payload = {
        "schema_version": STATEMENT_CAPTURE_SCHEMA_VERSION,
        "capture_id": metadata["capture_id"],
        "capture_source": metadata["capture_source"],
        "tenant_id": metadata["tenant_id"],
        "organisation_name": metadata["organisation_name"],
        "active_bank_accounts": accounts,
        "period_start": metadata.get("period_start") or "",
        "period_end": metadata.get("period_end") or "",
        "source_sha256": metadata.get("source_sha256") or "",
        "scans": [
            {
                "id": scan.id,
                "bank_account_id": scan.bank_account_id,
                "payload_hash": scan.payload_hash,
                "expected_count": scan.expected_count,
                "observed_count": scan.observed_count,
                "scan_id": scan.capture_metadata.get("scan_id") or "",
                "account_source_sha256": scan.capture_metadata.get(
                    "account_source_sha256"
                )
                or "",
            }
            for scan in scans
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def select_current_statement_capture(organization) -> StatementCaptureSelection:
    """Select the newest exact, fresh, live-catalog-matched all-account batch."""

    max_age_minutes = int(
        getattr(settings, "XERO_STATEMENT_SCAN_MAX_AGE_MINUTES", 30)
    )
    recent_scans = list(
        XeroStatementScan.objects.filter(organization=organization).order_by(
            "-started_at", "-id"
        )[:101]
    )
    if not recent_scans:
        return StatementCaptureSelection(
            max_age_minutes=max_age_minutes,
            blockers=("Import the current complete all-account Xero bank-feed queue.",),
        )
    latest = recent_scans[0]
    raw_latest = latest.capture_metadata if isinstance(latest.capture_metadata, dict) else {}
    if raw_latest.get("schema_version") != STATEMENT_CAPTURE_SCHEMA_VERSION:
        return StatementCaptureSelection(
            scans=(latest,),
            max_age_minutes=max_age_minutes,
            blockers=(
                "The latest Xero statement scan does not prove complete all-account coverage.",
            ),
        )
    try:
        latest_metadata = _sanitize_capture_metadata(
            raw_latest,
            bank_account_id=latest.bank_account_id,
            complete_scan=latest.status == XeroStatementScan.STATUS_COMPLETE,
        )
    except ValueError as exc:
        return StatementCaptureSelection(
            scans=(latest,),
            max_age_minutes=max_age_minutes,
            blockers=(f"The latest all-account capture metadata is invalid: {exc}",),
        )
    if (
        latest.status != XeroStatementScan.STATUS_COMPLETE
        or latest.expected_count is None
        or latest.expected_count != latest.observed_count
        or latest_metadata["derived_complete"] is not True
    ):
        return StatementCaptureSelection(
            capture_id=latest_metadata["capture_id"],
            capture_source=latest_metadata["capture_source"],
            scans=(latest,),
            max_age_minutes=max_age_minutes,
            blockers=("The latest Xero statement scan is incomplete.",),
        )
    capture_id = latest_metadata["capture_id"]
    capture_source = latest_metadata["capture_source"]
    account_count = latest_metadata["account_count"]
    cohort_descending = recent_scans[:account_count]
    blockers: list[str] = []
    normalized_by_scan: dict[int, dict[str, Any]] = {}
    if len(cohort_descending) != account_count:
        blockers.append(
            "The newest all-account capture is partial: its account positions are incomplete."
        )
    for offset, scan in enumerate(cohort_descending):
        raw_metadata = (
            scan.capture_metadata if isinstance(scan.capture_metadata, dict) else {}
        )
        if (
            raw_metadata.get("schema_version") != STATEMENT_CAPTURE_SCHEMA_VERSION
            or raw_metadata.get("capture_id") != capture_id
        ):
            blockers.append(
                "The newest all-account capture is interrupted by a different scan batch."
            )
            continue
        try:
            normalized_by_scan[scan.id] = _sanitize_capture_metadata(
                raw_metadata,
                bank_account_id=scan.bank_account_id,
                complete_scan=scan.status == XeroStatementScan.STATUS_COMPLETE,
            )
        except ValueError as exc:
            blockers.append(f"All-account scan {scan.id} metadata is invalid: {exc}")
            continue
        expected_position = account_count - offset
        if normalized_by_scan[scan.id]["account_position"] != expected_position:
            blockers.append(
                "The newest all-account capture is partial or its account positions are out of order."
            )
    if blockers:
        return StatementCaptureSelection(
            capture_id=capture_id,
            capture_source=capture_source,
            scans=tuple(reversed(cohort_descending)),
            max_age_minutes=max_age_minutes,
            blockers=tuple(dict.fromkeys(blockers)),
        )
    selected = list(reversed(cohort_descending))

    try:
        catalog = active_xero_bank_account_catalog(organization)
    except StatementCaptureValidationError as exc:
        blockers.append(str(exc))
        catalog = {"tenant_id": "", "organisation_name": "", "accounts": []}
    except (ConnectorOAuthError, requests.RequestException):
        blockers.append(
            "The active Xero BANK account catalogue could not be verified."
        )
        catalog = {"tenant_id": "", "organisation_name": "", "accounts": []}
    accounts = list(catalog.get("accounts") or [])
    catalog_ids = [account["bank_account_id"] for account in accounts]
    catalog_by_id = {
        canonical_bank_account_id(account["bank_account_id"]): account
        for account in accounts
    }
    declared_ids = latest_metadata["active_bank_account_ids"]
    if {canonical_bank_account_id(value) for value in declared_ids} != {
        canonical_bank_account_id(value) for value in catalog_ids
    }:
        blockers.append(
            "The active Xero BANK account catalogue changed after this capture."
        )
    if account_count != len(accounts):
        blockers.append(
            "The all-account capture count does not match the active Xero BANK account catalogue."
        )
    if latest_metadata["tenant_id"] != str(catalog.get("tenant_id") or ""):
        blockers.append("The all-account capture belongs to a different Xero tenant.")
    if latest_metadata["organisation_name"].casefold() != str(
        catalog.get("organisation_name") or ""
    ).casefold():
        blockers.append("The all-account capture names a different Xero organisation.")

    now = timezone.now()
    freshness_cutoff = now - timedelta(minutes=max_age_minutes)
    seen_account_ids: set[str] = set()
    seen_scan_ids: set[str] = set()
    common_fields = (
        "capture_source",
        "capture_id",
        "tenant_id",
        "organisation_name",
        "account_count",
        "active_bank_account_ids",
        "all_accounts_requested",
        "full_organisation_coverage_confirmed",
        "date_range_confirmed",
        "period_start",
        "period_end",
        "source_sha256",
        "report_format",
    )
    for scan in selected:
        metadata = normalized_by_scan[scan.id]
        if any(metadata.get(field) != latest_metadata.get(field) for field in common_fields):
            blockers.append("The newest all-account capture contains mixed metadata.")
            break
        account_id = canonical_bank_account_id(scan.bank_account_id)
        if account_id in seen_account_ids:
            blockers.append("The newest all-account capture repeats a bank account.")
        seen_account_ids.add(account_id)
        external_scan_id = metadata["scan_id"]
        if external_scan_id in seen_scan_ids:
            blockers.append("The newest all-account capture repeats a source scan ID.")
        seen_scan_ids.add(external_scan_id)
        account = catalog_by_id.get(account_id)
        if account and metadata["bank_account_label"].casefold() != account["name"].casefold():
            blockers.append(
                f"All-account scan {scan.id} has a bank-account label mismatch."
            )
        if (
            scan.status != XeroStatementScan.STATUS_COMPLETE
            or scan.expected_count is None
            or scan.expected_count != scan.observed_count
            or metadata["derived_complete"] is not True
            or not metadata["all_accounts_requested"]
            or not metadata["full_organisation_coverage_confirmed"]
            or not metadata["date_range_confirmed"]
        ):
            blockers.append(
                f"All-account scan {scan.id} is incomplete or lacks coverage confirmation."
            )
        if scan.completed_at is None or scan.completed_at < freshness_cutoff:
            blockers.append(
                f"All-account scan {scan.id} is older than {max_age_minutes} minutes."
            )

    if seen_account_ids != {
        canonical_bank_account_id(value) for value in declared_ids
    }:
        blockers.append(
            "The newest all-account capture does not match its declared bank-account IDs."
        )
    if selected and len(seen_account_ids) != account_count:
        blockers.append("The newest all-account capture does not contain every account once.")
    active_lines = list(
        XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            active=True,
            last_scan_id__in=[scan.id for scan in selected],
        ).values_list("bank_account_id", "statement_line_id")
    )
    line_ids = [statement_line_id for _, statement_line_id in active_lines]
    if len(line_ids) != len(set(line_ids)):
        blockers.append(
            "The all-account queue contains duplicate statement-line IDs across bank accounts."
        )

    unique_blockers = tuple(dict.fromkeys(blockers))
    fingerprint = ""
    if selected and not unique_blockers:
        fingerprint = _capture_fingerprint(
            metadata=latest_metadata,
            scans=selected,
            accounts=accounts,
        )
    return StatementCaptureSelection(
        capture_id=capture_id,
        capture_source=capture_source,
        scans=tuple(selected),
        active_bank_accounts=tuple(accounts),
        capture_fingerprint=fingerprint,
        period_start=latest_metadata.get("period_start") or "",
        period_end=latest_metadata.get("period_end") or "",
        max_age_minutes=max_age_minutes,
        blockers=unique_blockers,
    )


def validate_current_statement_line_capture(
    statement_line: XeroStatementLineSnapshot,
    *,
    expected_bank_account_id: str = "",
    expected_source_hash: str = "",
    selection: StatementCaptureSelection | None = None,
) -> StatementCaptureSelection:
    """Assert a row still belongs to the live authoritative capture and account."""

    try:
        current = XeroStatementLineSnapshot.objects.select_related("last_scan").get(
            pk=statement_line.pk,
            organization_id=statement_line.organization_id,
        )
    except XeroStatementLineSnapshot.DoesNotExist as exc:
        raise StatementCaptureValidationError(
            "The captured Xero statement line no longer exists."
        ) from exc
    if current.statement_line_id != statement_line.statement_line_id:
        raise StatementCaptureValidationError(
            "The captured Xero statement line identity changed."
        )
    if (
        expected_bank_account_id
        and canonical_bank_account_id(current.bank_account_id)
        != canonical_bank_account_id(expected_bank_account_id)
    ):
        raise StatementCaptureValidationError(
            "The captured Xero statement line belongs to a different bank account."
        )
    if expected_source_hash and current.source_hash != expected_source_hash:
        raise StatementCaptureValidationError(
            "The captured Xero statement line changed after it was selected."
        )
    if not current.active:
        raise StatementCaptureValidationError(
            "The captured Xero statement line is no longer active."
        )
    selection = selection or select_current_statement_capture(current.organization)
    if not selection.all_account_capture:
        raise StatementCaptureValidationError(
            selection.blockers[0]
            if selection.blockers
            else "A complete all-account Xero statement capture is required."
        )
    if any(scan.organization_id != current.organization_id for scan in selection.scans):
        raise StatementCaptureValidationError(
            "The selected all-account capture belongs to a different organisation."
        )
    latest_scan_id = XeroStatementScan.objects.filter(
        organization_id=current.organization_id
    ).order_by("-started_at", "-id").values_list("id", flat=True).first()
    if latest_scan_id != selection.latest_scan.id:
        raise StatementCaptureValidationError(
            "The Xero statement queue changed after the all-account capture was validated."
        )
    freshness_cutoff = timezone.now() - timedelta(minutes=selection.max_age_minutes)
    if any(
        scan.completed_at is None or scan.completed_at < freshness_cutoff
        for scan in selection.scans
    ):
        raise StatementCaptureValidationError(
            f"The all-account capture is older than {selection.max_age_minutes} minutes."
        )
    if current.last_scan_id not in selection.scan_ids:
        raise StatementCaptureValidationError(
            "The captured Xero statement line is not in the current all-account capture."
        )
    active_account_ids = {
        canonical_bank_account_id(account["bank_account_id"])
        for account in selection.active_bank_accounts
    }
    if canonical_bank_account_id(current.bank_account_id) not in active_account_ids:
        raise StatementCaptureValidationError(
            "The captured Xero statement line's bank account is no longer active."
        )
    return selection


def import_xero_statement_lines(
    *,
    organization,
    bank_account_id: str,
    lines: list[dict[str, Any]],
    currency: str = "AUD",
    expected_count: int | None = None,
    complete_scan: bool = True,
    source: str = "browser",
    requested_by: str = "",
    capture_metadata: dict[str, Any] | None = None,
) -> list[XeroStatementLineSnapshot]:
    if not bank_account_id:
        raise ValueError("bank_account_id is required")
    if not isinstance(lines, list):
        raise ValueError("lines must be a list")
    if complete_scan and not lines and expected_count != 0:
        raise ValueError(
            "An empty complete Xero scan must explicitly declare expected_count=0"
        )
    if expected_count is not None:
        try:
            expected_count = int(expected_count)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected_count must be an integer") from exc
        if expected_count < 0:
            raise ValueError("expected_count cannot be negative")
        if complete_scan and expected_count != len(lines):
            raise ValueError(
                f"Complete Xero scan expected {expected_count} rows but observed {len(lines)}"
            )
    sanitized_capture_metadata = _sanitize_capture_metadata(
        capture_metadata,
        bank_account_id=bank_account_id,
        complete_scan=complete_scan,
    )
    if sanitized_capture_metadata.get("schema_version") == STATEMENT_CAPTURE_SCHEMA_VERSION:
        capture_source = sanitized_capture_metadata["capture_source"]
        if capture_source == STATEMENT_CAPTURE_SOURCE_BROWSER and complete_scan:
            browser_account_hash = hashlib.sha256(
                json.dumps(
                    lines,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                sanitized_capture_metadata["account_source_sha256"]
                != browser_account_hash
            ):
                raise ValueError(
                    "capture_metadata account_source_sha256 does not match the browser payload"
                )
        if capture_source == STATEMENT_CAPTURE_SOURCE_CSV:
            expected_capture_id = "csv-" + hashlib.sha256(
                (
                    sanitized_capture_metadata["tenant_id"]
                    + "\0"
                    + sanitized_capture_metadata["source_sha256"]
                ).encode("utf-8")
            ).hexdigest()[:32]
            if sanitized_capture_metadata["capture_id"] != expected_capture_id:
                raise ValueError(
                    "capture_metadata capture_id does not match the CSV source and tenant"
                )
            expected_scan_id = (
                f"{expected_capture_id}-"
                f"{sanitized_capture_metadata['account_source_sha256'][:16]}"
            )
            if sanitized_capture_metadata["scan_id"] != expected_scan_id:
                raise ValueError(
                    "capture_metadata scan_id does not match the CSV account source"
                )
    saved: list[XeroStatementLineSnapshot] = []
    seen_ids: set[str] = set()
    capture_period_start = (
        date.fromisoformat(sanitized_capture_metadata["period_start"])
        if sanitized_capture_metadata.get("period_start")
        else None
    )
    capture_period_end = (
        date.fromisoformat(sanitized_capture_metadata["period_end"])
        if sanitized_capture_metadata.get("period_end")
        else None
    )
    with transaction.atomic():
        storage_bank_account_id, equivalent_bank_account_ids = _resolved_bank_account_identity(
            organization=organization,
            bank_account_id=bank_account_id,
        )
        scan = XeroStatementScan.objects.create(
            organization=organization,
            bank_account_id=storage_bank_account_id,
            status=XeroStatementScan.STATUS_STARTED,
            source=str(source or "browser")[:32],
            requested_by=str(requested_by or "")[:100],
            expected_count=expected_count,
            observed_count=len(lines),
            payload_hash=hashlib.sha256(
                json.dumps(lines, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            capture_metadata=sanitized_capture_metadata,
        )
        for raw in lines:
            if not isinstance(raw, dict):
                raise ValueError("Each statement line must be an object")
            statement_line_id = str(raw.get("statement_line_id") or "").strip()
            if not statement_line_id or statement_line_id in seen_ids:
                raise ValueError("Every statement line requires a unique statement_line_id")
            seen_ids.add(statement_line_id)
            try:
                transaction_date = datetime.strptime(str(raw.get("date") or "").strip(), "%d %b %Y").date()
            except ValueError as exc:
                raise ValueError(f"Invalid Xero statement date for {statement_line_id}") from exc
            if (
                capture_period_start
                and capture_period_end
                and not capture_period_start <= transaction_date <= capture_period_end
            ):
                raise ValueError(
                    f"Statement date for {statement_line_id} is outside the attested capture period"
                )
            try:
                amount = Decimal(str(raw.get("amount") or "0").replace(",", "")).quantize(Decimal("0.01"))
            except InvalidOperation as exc:
                raise ValueError(f"Invalid Xero statement amount for {statement_line_id}") from exc
            if amount <= 0:
                raise ValueError(f"Statement amount must be positive for {statement_line_id}")
            direction = str(raw.get("direction") or "").strip().lower()
            if direction not in {XeroStatementLineSnapshot.DIRECTION_DEBIT, XeroStatementLineSnapshot.DIRECTION_CREDIT}:
                raise ValueError(f"Invalid statement direction for {statement_line_id}")
            values = {
                "statement_line_id": statement_line_id,
                "bank_account_id": storage_bank_account_id,
                "transaction_date": transaction_date,
                "narration": str(raw.get("narration") or "").strip()[:4000],
                "reference": str(raw.get("reference") or "").strip()[:500],
                "direction": direction,
                "amount": amount,
                "currency": str(raw.get("currency") or currency or "AUD").strip().upper()[:12],
            }
            visible_fields = {
                "current_contact": str(_browser_field(raw, "contact", "current_contact") or "").strip()[:255],
                "current_account": str(_browser_field(raw, "account", "current_account") or "").strip()[:255],
                "current_description": str(
                    _browser_field(raw, "description", "current_description") or ""
                ).strip()[:4000],
                "current_event_name": str(
                    _browser_field(raw, "event_name", "current_event_name") or ""
                ).strip()[:255],
                "current_project_name": str(
                    _browser_field(raw, "project_name", "current_project_name") or ""
                ).strip()[:255],
                "current_tax_type": str(_browser_field(raw, "tax_type", "current_tax_type") or "").strip()[:255],
            }
            ui_mode = _browser_ui_mode(raw, visible_fields=visible_fields)
            create_prefill_complete = all(
                visible_fields[field]
                for field in ("current_contact", "current_account", "current_description", "current_tax_type")
            )
            defaults = {
                **values,
                **visible_fields,
                "queue_state": XeroStatementLineSnapshot.QUEUE_ACTIVE,
                "ui_mode": ui_mode,
                "create_prefill_complete": create_prefill_complete,
                "matched_xero_transaction_id": str(raw.get("matched_xero_transaction_id") or "").strip()[:255],
                "last_scan": scan,
                "ready_in_xero": ui_mode == XeroStatementLineSnapshot.UI_GREEN_MATCH,
                "active": True,
                "source_hash": _line_source_hash(values),
            }
            snapshot, _created = XeroStatementLineSnapshot.objects.update_or_create(
                organization=organization,
                bank_account_id=storage_bank_account_id,
                statement_line_id=statement_line_id,
                defaults=defaults,
            )
            saved.append(snapshot)
        completed_at = timezone.now()

        # A partial scan can still safely retire an alias of a row it actually
        # observed. Do not call that row reconciled: it is the same live Xero
        # item under a different UUID spelling.
        duplicate_alias_ids = list(
            XeroStatementLineSnapshot.objects.filter(
                organization=organization,
                bank_account_id__in=equivalent_bank_account_ids,
                statement_line_id__in=seen_ids,
                active=True,
            )
            .exclude(bank_account_id=storage_bank_account_id)
            .values_list("id", flat=True)
        )
        XeroStatementLineSnapshot.objects.filter(id__in=duplicate_alias_ids).update(
            active=False,
            queue_state=XeroStatementLineSnapshot.QUEUE_INACTIVE,
            last_seen_at=completed_at,
        )
        if complete_scan:
            missing_lines = list(XeroStatementLineSnapshot.objects.filter(
                organization=organization,
                bank_account_id__in=equivalent_bank_account_ids,
                active=True,
            ).exclude(statement_line_id__in=seen_ids))
            missing_line_ids = [line.id for line in missing_lines]
            XeroStatementLineSnapshot.objects.filter(id__in=missing_line_ids).update(
                active=False,
                queue_state=XeroStatementLineSnapshot.QUEUE_RECONCILED,
                last_seen_at=completed_at,
            )
            confirmed_postings = XeroStatementPosting.objects.filter(
                statement_line_id__in=missing_line_ids,
                status=XeroStatementPosting.STATUS_MATCH_READY,
            ).select_related("statement_line", "suggestion")
            for posting in confirmed_postings:
                posting.status = XeroStatementPosting.STATUS_RECONCILED
                posting.reconciled_at = completed_at
                posting.reconciled_scan = scan
                posting.save(update_fields=[
                    "status", "reconciled_at", "reconciled_scan", "updated_at",
                ])
                record_reconciliation_decision(
                    statement_line=posting.statement_line,
                    suggestion=posting.suggestion,
                    decision_type=ReconciliationDecision.TYPE_RECONCILED_CONFIRMED,
                    run_id=posting.suggestion.run_id,
                    actor_type=(
                        ReconciliationDecision.ACTOR_ADMIN
                        if requested_by
                        else ReconciliationDecision.ACTOR_SYSTEM
                    ),
                    actor_id=str(requested_by or "")[:100],
                    outcome={
                        "posting_id": posting.id,
                        "scan_id": scan.id,
                        "confirmation_method": "absent_from_complete_statement_scan",
                        "xero_bank_transaction_id": posting.xero_bank_transaction_id,
                        "xero_payment_id": posting.xero_payment_id,
                    },
                    evidence=[{
                        "source_provider": "xero_ui",
                        "source_record_id": f"statement-scan:{scan.id}",
                        "summary": "The match-ready bank line was absent from the next complete Xero queue scan.",
                    }],
                    discriminator=f"scan:{scan.id}:posting:{posting.id}",
                )
        scan.status = (
            XeroStatementScan.STATUS_COMPLETE
            if complete_scan
            else XeroStatementScan.STATUS_INCOMPLETE
        )
        scan.completed_at = completed_at
        scan.save(update_fields=["status", "completed_at"])
    return saved


def _serialize_evidence(raw_evidence: Any) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for raw in raw_evidence or []:
        if not isinstance(raw, dict):
            continue
        provider = str(raw.get("source_provider") or "").strip().lower()
        source_record_id = str(raw.get("source_record_id") or "").strip()
        if provider not in ALLOWED_STATEMENT_EVIDENCE_PROVIDERS or not source_record_id:
            continue
        result.append({
            "source_provider": provider,
            "source_record_id": source_record_id[:500],
            "summary": str(raw.get("summary") or "").strip()[:1000],
        })
        if len(result) >= 20:
            break
    return result


_COMMENT_PREFIX_RE = re.compile(
    r"^(?:ai\s+reconciliation\s+draft(?:\s*\([^)]*\))?\s*:?\s*|review\s*:\s*)",
    re.IGNORECASE,
)
_COMMENT_META_RE = re.compile(
    r"\b(?:human approval required|human must click ok|no account/tax change proposed)\b[.;:]?",
    re.IGNORECASE,
)
_COMMENT_CONFIDENCE_RE = re.compile(
    r"(?:\bconfidence\s*:\s*\d{1,3}%\.?|\b\d{1,3}%\s+confidence\b[.;:]?)",
    re.IGNORECASE,
)
_COMMENT_GENERIC_RE = re.compile(
    r"^(?:unreconciled bank statement line|needs? review|review required|unknown payment)$",
    re.IGNORECASE,
)


def format_statement_browser_comment(*, description: str, review_note: str, confidence: float) -> str:
    """Return the short, conversational comment shown in Xero Discuss."""

    summary = ""
    for raw in (description, review_note):
        cleaned = _COMMENT_PREFIX_RE.sub("", str(raw or "").strip())
        cleaned = _COMMENT_META_RE.sub("", cleaned)
        cleaned = _COMMENT_CONFIDENCE_RE.sub("", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -;:.")
        for sentence in re.split(r"(?<=[.!?])\s+", cleaned):
            candidate = sentence.strip(" -;:.")
            if candidate and not _COMMENT_GENERIC_RE.fullmatch(candidate):
                summary = candidate
                break
        if summary:
            break
    if not summary:
        summary = "Not enough context to identify this payment"
    if len(summary) > 220:
        summary = f"{summary[:217].rstrip()}…"
    if summary[0].islower():
        summary = f"{summary[0].upper()}{summary[1:]}"
    percentage = int(round(max(0.0, min(float(confidence or 0.0), 1.0)) * 100))
    return f"{summary}. Confidence: {percentage}%."


def serialize_statement_suggestion(suggestion: XeroStatementSuggestion) -> dict[str, Any]:
    applied_rule_decision = suggestion.reconciliation_decisions.filter(
        decision_type=ReconciliationDecision.TYPE_RULE_APPLIED,
        rule__isnull=False,
    ).select_related("rule").order_by("-created_at").first()
    rule_conflict = ReconciliationDecision.objects.filter(
        statement_line=suggestion.statement_line,
        run_id=suggestion.run_id,
        decision_type=ReconciliationDecision.TYPE_RULE_CONFLICT,
    ).order_by("-created_at").first()
    if suggestion.matched_xero_bill_id:
        routing_source = "exact_xero_bill"
    elif applied_rule_decision:
        routing_source = "verified_rule"
    elif rule_conflict:
        routing_source = "rule_conflict"
    elif suggestion.model_name == "deterministic_verified_rule":
        routing_source = "deterministic"
    elif suggestion.model_name:
        routing_source = "monthly_context_agent"
    else:
        routing_source = "manual_or_legacy"
    account_display = " - ".join(
        value for value in (suggestion.account_code, suggestion.account_name) if value
    )
    profile = ReconciliationProfile.objects.filter(
        organization_id=suggestion.organization_id
    ).first()
    tracking = effective_tracking(profile, suggestion) if profile else None
    return {
        "id": suggestion.id,
        "statement_line_id": suggestion.statement_line.statement_line_id,
        "run_id": suggestion.run_id,
        "proposed_action": suggestion.proposed_action,
        "execution_action": normalize_statement_action(suggestion.proposed_action),
        "contact_name": suggestion.contact_name,
        "account_code": suggestion.account_code,
        "account_name": suggestion.account_name,
        "tax_type": suggestion.tax_type,
        "description": suggestion.description,
        "allocation_mode": suggestion.allocation_mode,
        "effective_tracking": tracking,
        "event": {
            "source_type": suggestion.event_source_type or "luma",
            "source_id": suggestion.event_source_id,
            "tracking_option_name": suggestion.event_tracking_option_name,
        } if suggestion.event_source_id else None,
        "project": {
            "source_type": suggestion.project_source_type or "linear",
            "source_id": suggestion.project_source_id,
            "tracking_option_id": suggestion.project_tracking_option_id,
            "tracking_option_name": suggestion.project_tracking_option_name,
        } if suggestion.project_source_id else None,
        "matched_xero_bill_id": suggestion.matched_xero_bill_id,
        "confidence": suggestion.confidence,
        "confidence_breakdown": {
            "identity": suggestion.identity_confidence,
            "accounting": suggestion.accounting_confidence,
            "allocation": suggestion.allocation_confidence,
            "document": suggestion.document_confidence,
        },
        "execution_ready": suggestion.execution_ready,
        "blocking_reasons": suggestion.blocking_reasons or [],
        "rationale": suggestion.rationale,
        "review_note": suggestion.review_note,
        "browser_comment": format_statement_browser_comment(
            description=suggestion.description,
            review_note=suggestion.review_note,
            confidence=suggestion.confidence,
        ),
        "create_fields": {
            "contact_name": suggestion.contact_name,
            "account_code": suggestion.account_code,
            "account_name": suggestion.account_name,
            "account_display": account_display,
            "description": suggestion.description,
            "event_name": suggestion.event_tracking_option_name,
            "project_name": (
                tracking["option_name"]
                if tracking and tracking["kind"] == "project"
                else ""
            ),
            "tax_type": suggestion.tax_type,
        },
        "evidence": suggestion.evidence or [],
        "source_hash": suggestion.source_hash,
        "model_name": suggestion.model_name,
        "status": suggestion.status,
        "applied_rule_id": applied_rule_decision.rule_id if applied_rule_decision else None,
        "routing": {
            "source": routing_source,
            "verified_rule_id": applied_rule_decision.rule_id if applied_rule_decision else None,
            "xero_bill_id": suggestion.matched_xero_bill_id or None,
            "model_name": suggestion.model_name,
        },
        "approval": serialize_suggestion_approval(suggestion),
        "posting": serialize_statement_posting(suggestion.postings.order_by("-created_at").first()),
    }


def normalize_statement_action(action: str) -> str:
    if action in {
        XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION,
        XeroStatementSuggestion.ACTION_PREFILL_CREATE,
    }:
        return XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION
    if action in {
        XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL,
        XeroStatementSuggestion.ACTION_MATCH_BILL,
    }:
        return XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL
    return XeroStatementSuggestion.ACTION_NEEDS_REVIEW


def csv_statement_duplicate_count(statement_line_id: str) -> int:
    match = re.fullmatch(
        r"csv-[a-f0-9]{40}-\d+-of-(\d+)",
        str(statement_line_id or ""),
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else 0


def serialize_statement_posting(posting) -> dict[str, Any] | None:
    if posting is None:
        return None
    return {
        "id": posting.id,
        "operation": posting.operation,
        "status": posting.status,
        "warnings": posting.warnings or [],
        "xero_bank_transaction_id": posting.xero_bank_transaction_id,
        "xero_payment_id": posting.xero_payment_id,
        "xero_bill_id": posting.xero_bill_id,
        "automatic": posting.automatic,
        "posted_at": posting.posted_at.isoformat() if posting.posted_at else None,
        "reconciled_at": posting.reconciled_at.isoformat() if posting.reconciled_at else None,
        "reconciled_scan_id": posting.reconciled_scan_id,
        "last_error": posting.last_error,
    }


def serialize_statement_line(line: XeroStatementLineSnapshot) -> dict[str, Any]:
    account_code, account_name = _account_parts(line.current_account)
    latest = line.suggestions.exclude(status=XeroStatementSuggestion.STATUS_SUPERSEDED).order_by("-created_at").first()
    return {
        "statement_line_id": line.statement_line_id,
        "bank_account_id": line.bank_account_id,
        "transaction_date": line.transaction_date.isoformat(),
        "narration": line.narration,
        "merchant_key": merchant_key(line.narration),
        "reference": line.reference,
        "direction": line.direction,
        "amount": str(line.amount),
        "currency": line.currency,
        "source_hash": line.source_hash,
        "current_xero_fields": {
            "contact_name": line.current_contact,
            "account_code": account_code,
            "account_name": account_name,
            "description": line.current_description,
            "event_name": line.current_event_name,
            "project_name": line.current_project_name,
            "tax_type": line.current_tax_type,
        },
        "ready_in_xero": line.ready_in_xero,
        "queue_state": line.queue_state,
        "ui_mode": line.ui_mode,
        "create_prefill_complete": line.create_prefill_complete,
        "matched_xero_transaction_id": line.matched_xero_transaction_id,
        "last_scan_id": line.last_scan_id,
        "is_green_match": line.is_green_match,
        "latest_suggestion": serialize_statement_suggestion(latest) if latest else None,
    }


def _bill_candidates(organization, line: XeroStatementLineSnapshot) -> list[dict[str, Any]]:
    if line.direction != XeroStatementLineSnapshot.DIRECTION_DEBIT:
        return []
    queryset = ExternalFinancialRecord.objects.filter(
        organization=organization,
        provider=ExternalServiceProvider.XERO,
        record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
        amount=line.amount,
        currency=line.currency,
        transaction_date__gte=line.transaction_date - timedelta(days=92),
        transaction_date__lte=line.transaction_date + timedelta(days=31),
    ).exclude(status__in=["DELETED", "VOIDED", "PAID"])
    return [
        {
            "xero_bill_id": record.external_record_id,
            "contact_name": record.merchant_name,
            "amount": str(record.amount),
            "currency": record.currency,
            "date": record.transaction_date.isoformat() if record.transaction_date else None,
            "status": record.status,
            "description": record.description,
        }
        for record in queryset.order_by("transaction_date", "id")[:10]
    ]


def _confirmed_reconciliation_examples(
    *,
    organization,
    active_lines: list[XeroStatementLineSnapshot],
) -> list[dict[str, Any]]:
    """Return durable human-accepted accounting patterns for later runs.

    Active green matches retain the previous behaviour. Completed API postings
    and manually prefilled lines that disappear from a complete queue scan remain
    available after they leave Xero's unreconciled list.
    """

    examples: list[dict[str, Any]] = []
    seen_line_ids: set[int] = set()

    def append_example(*, line, fields: dict[str, Any], outcome_source: str, confirmed_at=None):
        required = ("contact_name", "account_code", "account_name", "description", "tax_type")
        normalized = {key: str(value or "").strip() for key, value in fields.items()}
        if not all(normalized.get(field) for field in required):
            return
        seen_line_ids.add(line.id)
        examples.append({
            "statement_line_id": line.statement_line_id,
            "transaction_date": line.transaction_date.isoformat(),
            "merchant_key": merchant_key(line.narration),
            "narration": line.narration,
            "direction": line.direction,
            **normalized,
            "outcome_source": outcome_source,
            "confirmed_at": confirmed_at.isoformat() if confirmed_at else None,
        })

    for line in active_lines:
        if not line.is_green_match:
            continue
        account_code, account_name = _account_parts(line.current_account)
        append_example(
            line=line,
            fields={
                "contact_name": line.current_contact,
                "account_code": account_code,
                "account_name": account_name,
                "description": line.current_description,
                "event_name": line.current_event_name,
                "project_name": line.current_project_name,
                "tax_type": line.current_tax_type,
            },
            outcome_source="active_green_match",
            confirmed_at=line.last_seen_at,
        )

    confirmed_postings = XeroStatementPosting.objects.filter(
        organization=organization,
        status=XeroStatementPosting.STATUS_RECONCILED,
        operation=XeroStatementPosting.OPERATION_BANK_TRANSACTION,
    ).select_related("statement_line", "suggestion").order_by("reconciled_at", "id")
    for posting in confirmed_postings:
        suggestion = posting.suggestion
        append_example(
            line=posting.statement_line,
            fields={
                "contact_name": suggestion.contact_name,
                "account_code": suggestion.account_code,
                "account_name": suggestion.account_name,
                "description": suggestion.description,
                "event_name": suggestion.event_tracking_option_name,
                "project_name": suggestion.project_tracking_option_name,
                "tax_type": suggestion.tax_type,
            },
            outcome_source="confirmed_api_posting",
            confirmed_at=posting.reconciled_at,
        )

    manual_lines = XeroStatementLineSnapshot.objects.filter(
        organization=organization,
        queue_state=XeroStatementLineSnapshot.QUEUE_RECONCILED,
        create_prefill_complete=True,
    ).order_by("last_seen_at", "id")
    for line in manual_lines:
        if line.id in seen_line_ids:
            continue
        account_code, account_name = _account_parts(line.current_account)
        append_example(
            line=line,
            fields={
                "contact_name": line.current_contact,
                "account_code": account_code,
                "account_name": account_name,
                "description": line.current_description,
                "event_name": line.current_event_name,
                "project_name": line.current_project_name,
                "tax_type": line.current_tax_type,
            },
            outcome_source="confirmed_manual_prefill",
            confirmed_at=line.last_seen_at,
        )
    return examples


def build_statement_reconciliation_context(
    *,
    organization,
    include_external_evidence: bool = True,
    luma_events: list[dict[str, Any]] | None = None,
    humanitix_events: list[dict[str, Any]] | None = None,
    linear_projects: list[dict[str, Any]] | None = None,
    statement_line_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    luma_events = luma_events or []
    humanitix_events = humanitix_events or []
    linear_projects = linear_projects or []
    lines = _active_statement_lines(
        organization=organization,
        statement_line_ids=statement_line_ids,
    )
    prefetched_gmail: list[dict[str, Any]] | None = None
    prefetched_slack: list[dict[str, Any]] | None = None
    if include_external_evidence and lines:
        window_days = max(
            STATEMENT_EVIDENCE_WINDOW_DAYS,
            STATEMENT_ENTITY_EVIDENCE_WINDOW_DAYS,
        )
        evidence_start = lines[0].transaction_date - timedelta(days=window_days)
        evidence_end = lines[-1].transaction_date + timedelta(days=window_days + 1)
        prefetched_gmail = list(
            GmailMessageArtifact.objects.filter(
                organization=organization,
                internal_date__date__gte=evidence_start,
                internal_date__date__lt=evidence_end,
            ).values(
                "gmail_message_id", "internal_date", "subject", "from_address",
                "snippet", "cleaned_text", "body_preview", "attachment_manifest",
            ).order_by("-internal_date")[:10000]
        )
        prefetched_slack = list(
            SlackMessageArtifact.objects.filter(
                organization=organization,
                posted_at__date__gte=evidence_start,
                posted_at__date__lt=evidence_end,
            ).values(
                "channel_id", "channel_name", "slack_message_ts", "author_name",
                "posted_at", "text", "cleaned_text",
            ).order_by("-posted_at")[:10000]
        )
    verified_identities = {
        (identity.bank_narration_key, identity.direction): identity
        for identity in ReconciliationPartyIdentity.objects.filter(
            organization=organization,
            status=ReconciliationPartyIdentity.STATUS_VERIFIED,
            active=True,
        )
    }
    examples = _confirmed_reconciliation_examples(
        organization=organization,
        active_lines=lines,
    )
    allowed_patterns: dict[str, list[dict[str, str]]] = {}
    candidates: list[dict[str, Any]] = []
    for example in examples:
        allowed_patterns.setdefault(example["merchant_key"], []).append({
            "example_statement_line_id": example["statement_line_id"],
            "contact_name": example["contact_name"],
            "account_code": example["account_code"],
            "account_name": example["account_name"],
            "description": example["description"],
            "event_name": example.get("event_name", ""),
            "project_name": example.get("project_name", ""),
            "tax_type": example["tax_type"],
            "outcome_source": example["outcome_source"],
        })
    for line in lines:
        if line.is_green_match:
            continue
        serialized = serialize_statement_line(line)
        verified_rule, rule_conflicts = resolve_reconciliation_rule(line)
        matching_bills = _bill_candidates(organization, line)
        serialized["verified_rule"] = (
            serialize_reconciliation_rule(verified_rule) if verified_rule else None
        )
        serialized["rule_conflicts"] = [
            serialize_reconciliation_rule(rule) for rule in rule_conflicts
        ]
        identity = verified_identities.get((serialized["merchant_key"], line.direction))
        serialized["verified_identity"] = {
            "id": identity.id,
            "canonical_name": identity.canonical_name,
            "xero_contact_id": identity.xero_contact_id,
            "xero_contact_name": identity.xero_contact_name,
            "linear_user_id": identity.linear_user_id,
            "linear_name": identity.linear_name,
            "linear_email": identity.linear_email,
            "confidence": identity.confidence,
            "verified_by_slack_id": identity.verified_by_slack_id,
        } if identity else None
        line_merchant_key = serialized["merchant_key"]
        verified_contact_names = {
            str(value or "").strip().casefold()
            for value in (
                getattr(identity, "canonical_name", ""),
                getattr(identity, "xero_contact_name", ""),
            )
            if str(value or "").strip()
        }
        for bill in matching_bills:
            bill_contact = str(bill.get("contact_name") or "").strip()
            bill_merchant_key = merchant_key(bill_contact)
            narration_match = bool(
                bill_merchant_key
                and (
                    bill_merchant_key == line_merchant_key
                    or f" {bill_merchant_key} " in f" {line_merchant_key} "
                )
            )
            identity_match = bill_contact.casefold() in verified_contact_names
            bill["merchant_key_match"] = narration_match
            bill["verified_identity_match"] = identity_match
            bill["exact_outstanding_match"] = bool(narration_match or identity_match)
        serialized["matching_xero_bills"] = matching_bills
        if matching_bills:
            serialized["deferred_verified_rule"] = serialized["verified_rule"]
            serialized["deferred_rule_conflicts"] = serialized["rule_conflicts"]
            serialized["verified_rule"] = None
            serialized["rule_conflicts"] = []
        else:
            serialized["deferred_verified_rule"] = None
            serialized["deferred_rule_conflicts"] = []
        serialized["allowed_historical_patterns"] = allowed_patterns.get(serialized["merchant_key"], [])
        nearby_events, nearby_projects, entity_entries = _candidate_entity_catalogs(
            line=line,
            luma_events=luma_events,
            humanitix_events=humanitix_events,
            linear_projects=linear_projects,
        )
        serialized["nearby_events"] = nearby_events
        serialized["nearby_projects"] = nearby_projects
        serialized["context_evidence"] = (
            _candidate_context_evidence(
                organization=organization,
                line=line,
                gmail_rows=prefetched_gmail,
                slack_rows=prefetched_slack,
            )
            if include_external_evidence
            else []
        )
        serialized["event_project_context_evidence"] = (
            _candidate_event_project_evidence(
                organization=organization,
                line=line,
                entity_entries=entity_entries,
                gmail_rows=prefetched_gmail,
                slack_rows=prefetched_slack,
            )
            if include_external_evidence
            else []
        )
        candidates.append(serialized)
    approved_options: dict[tuple[str, str, str], dict[str, Any]] = {}
    for example in examples:
        key = tuple(
            str(example.get(field) or "").strip()
            for field in ("account_code", "account_name", "tax_type")
        )
        if not all(key):
            continue
        option = approved_options.setdefault(
            key,
            {
                "account_code": key[0],
                "account_name": key[1],
                "tax_type": key[2],
                "examples": [],
            },
        )
        if len(option["examples"]) < 5:
            option["examples"].append(
                {
                    "statement_line_id": example["statement_line_id"],
                    "merchant_key": example["merchant_key"],
                    "contact_name": example["contact_name"],
                    "description": example["description"],
                }
            )
    return {
        "statement_candidates": candidates,
        "prior_xero_examples": examples,
        "approved_accounting_options": list(approved_options.values()),
        "statement_policy": {
            "verified_rules": "A statement_candidate.verified_rule is admin-authoritative. Copy every supplied accounting and tracking field exactly. If deferred_verified_rule or deferred_rule_conflicts is present, prefer the exact matching Xero bill instead. If rule_conflicts is non-empty, return needs_review and do not choose between the rules.",
            "create_bank_transaction": "Prefer an exact merchant_key pattern. Otherwise copy one complete account/tax tuple from approved_accounting_options when the evidence strongly supports it.",
            "pay_existing_bill": "Prefer paying a supplied matching_xero_bill over creating a Spend Money transaction. Never invent a bill ID.",
            "execution": "The backend rechecks confidence, current Xero state and idempotency before any API write.",
            "approval": "The human remains the only actor who clicks Match/OK on the Xero bank statement line.",
        },
    }


def _catalogs(organization):
    events = {
        ("luma", item.event_id): item
        for item in LumaEventSelection.objects.filter(organization=organization)
    }
    events.update({
        ("humanitix", item.external_event_id): item
        for item in HumanitixEvent.objects.filter(organization=organization)
    })
    projects = {
        ("linear", item.linear_project_id): {
            "name": item.name,
            "tracking_option_id": "",
        }
        for item in LinearProjectArtifact.objects.filter(organization=organization)
    }
    for item in LinearProjectSelection.objects.filter(organization=organization):
        projects.setdefault(("linear", item.linear_project_id), {
            "name": item.project_name or item.linear_project_id,
            "tracking_option_id": "",
        })
    try:
        xero_projects = active_xero_project_options(organization=organization)
    except Exception:
        xero_projects = []
    xero_by_name = {item["name"].strip().casefold(): item for item in xero_projects}
    for key, project in projects.items():
        matching = xero_by_name.get(project["name"].strip().casefold())
        if matching:
            project["tracking_option_id"] = matching["tracking_option_id"]
    for item in xero_projects:
        projects[("xero_tracking", item["source_id"])] = {
            "name": item["name"],
            "tracking_option_id": item["tracking_option_id"],
        }
    return events, projects


def _bounded_confidence(value: Any, *, fallback: float = 0.0) -> float:
    try:
        score = float(value if value is not None else fallback)
    except (TypeError, ValueError):
        score = fallback
    return max(0.0, min(score, 1.0))


def _statement_execution_assessment(
    *,
    item: dict[str, Any],
    normalized_action: str,
    overall_confidence: float,
    has_tracking_assignment: bool,
    require_tracking: bool = False,
) -> tuple[dict[str, float], bool, list[str]]:
    """Apply deterministic readiness gates to independent confidence scores."""

    score_keys = (
        "identity_confidence",
        "accounting_confidence",
        "allocation_confidence",
        "document_confidence",
    )
    explicit_scores = any(key in item for key in score_keys)
    scores = {
        "identity": _bounded_confidence(item.get("identity_confidence"), fallback=overall_confidence),
        "accounting": _bounded_confidence(item.get("accounting_confidence"), fallback=overall_confidence),
        "allocation": _bounded_confidence(item.get("allocation_confidence"), fallback=overall_confidence),
        "document": _bounded_confidence(item.get("document_confidence"), fallback=overall_confidence),
    }
    reasons: list[str] = []
    if normalized_action == XeroStatementSuggestion.ACTION_NEEDS_REVIEW:
        reasons.append("Suggestion action still requires review.")
    elif not explicit_scores:
        if require_tracking and not has_tracking_assignment:
            reasons.append("Every executable suggestion requires an Event, Project, or MLAI core allocation.")
        threshold_name = (
            "XERO_STATEMENT_BILL_PAYMENT_MIN_CONFIDENCE"
            if normalized_action == XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL
            else "XERO_STATEMENT_BANK_TRANSACTION_MIN_CONFIDENCE"
        )
        threshold_default = 0.98 if normalized_action == XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL else 0.92
        threshold = float(getattr(settings, threshold_name, threshold_default))
        if overall_confidence < threshold:
            reasons.append(f"Legacy overall confidence must be at least {threshold:.0%}.")
    else:
        identity_threshold = float(getattr(settings, "XERO_STATEMENT_IDENTITY_MIN_CONFIDENCE", 0.80))
        accounting_threshold = float(getattr(settings, "XERO_STATEMENT_ACCOUNTING_MIN_CONFIDENCE", 0.90))
        allocation_threshold = float(getattr(settings, "XERO_STATEMENT_ALLOCATION_MIN_CONFIDENCE", 0.75))
        if scores["identity"] < identity_threshold:
            reasons.append(f"Identity confidence must be at least {identity_threshold:.0%}.")
        if require_tracking and not has_tracking_assignment:
            reasons.append("Every executable suggestion requires an Event, Project, or MLAI core allocation.")
        if has_tracking_assignment and scores["allocation"] < allocation_threshold:
            reasons.append(f"Allocation confidence must be at least {allocation_threshold:.0%}.")
        if normalized_action == XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION:
            if scores["accounting"] < accounting_threshold:
                reasons.append(f"Accounting confidence must be at least {accounting_threshold:.0%}.")
        elif normalized_action == XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL:
            document_threshold = float(getattr(settings, "XERO_STATEMENT_BILL_DOCUMENT_MIN_CONFIDENCE", 0.95))
            if scores["document"] < document_threshold:
                reasons.append(f"Bill-document confidence must be at least {document_threshold:.0%}.")
    return scores, not reasons, reasons


def save_statement_suggestions(
    *,
    organization,
    run_id: str,
    suggestions: list[dict[str, Any]],
    model_name: str = "",
    decision_actor_type: str = ReconciliationDecision.ACTOR_AGENT,
) -> list[XeroStatementSuggestion]:
    if not isinstance(suggestions, list):
        raise ValueError("statement_suggestions must be a list")
    line_by_id = {
        line.statement_line_id: line
        for line in XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            statement_line_id__in=[str(item.get("statement_line_id") or "") for item in suggestions if isinstance(item, dict)],
            active=True,
        )
    }
    events, projects = _catalogs(organization)
    profile = ReconciliationProfile.objects.filter(organization=organization).first()
    context = build_statement_reconciliation_context(organization=organization, include_external_evidence=False)
    context_by_id = {item["statement_line_id"]: item for item in context["statement_candidates"]}
    saved: list[XeroStatementSuggestion] = []
    with transaction.atomic():
        for item in suggestions:
            if not isinstance(item, dict):
                raise ValueError("Each statement suggestion must be an object")
            line_id = str(item.get("statement_line_id") or "").strip()
            line = line_by_id.get(line_id)
            candidate = context_by_id.get(line_id)
            if line is None or candidate is None:
                raise ValueError(f"Unknown or already-ready statement line: {line_id}")
            verified_rule, rule_conflicts = resolve_reconciliation_rule(line)
            if candidate.get("matching_xero_bills"):
                verified_rule = None
                rule_conflicts = []
            if rule_conflicts:
                conflict_evidence = [
                    evidence
                    for rule in rule_conflicts
                    for evidence in rule_evidence(rule)
                ][:20]
                record_reconciliation_decision(
                    statement_line=line,
                    decision_type=ReconciliationDecision.TYPE_RULE_CONFLICT,
                    run_id=run_id,
                    actor_type=ReconciliationDecision.ACTOR_SYSTEM,
                    outcome={
                        "rule_ids": [rule.id for rule in rule_conflicts],
                        "reason": "Multiple equally specific verified rules disagree.",
                    },
                )
                item = {
                    **item,
                    "proposed_action": XeroStatementSuggestion.ACTION_NEEDS_REVIEW,
                    "account_code": "",
                    "account_name": "",
                    "tax_type": "",
                    "matched_xero_bill_id": "",
                    "event": None,
                    "project": None,
                    "allocation_mode": XeroStatementSuggestion.ALLOCATION_UNASSIGNED,
                    "description": "",
                    "review_note": "Conflicting verified reconciliation rules need an admin decision.",
                    "evidence": conflict_evidence,
                }
            if verified_rule:
                item = apply_verified_rule(rule=verified_rule, line=line, item=item)
            action = str(item.get("proposed_action") or XeroStatementSuggestion.ACTION_NEEDS_REVIEW)
            if action not in {choice[0] for choice in XeroStatementSuggestion.ACTION_CHOICES}:
                raise ValueError(f"Invalid proposed_action for {line_id}")
            event_payload = item.get("event") if isinstance(item.get("event"), dict) else {}
            event_id = str(event_payload.get("source_id") or "").strip()
            event_source_type = str(event_payload.get("source_type") or "luma").strip()
            if event_source_type not in {"luma", "humanitix"}:
                raise ValueError(f"Unknown event source for {line_id}: {event_source_type}")
            event_key = (event_source_type, event_id)
            if event_id and event_key not in events:
                raise ValueError(
                    f"Unknown {event_source_type} event for {line_id}: {event_id}"
                )
            project_payload = item.get("project") if isinstance(item.get("project"), dict) else {}
            project_id = str(project_payload.get("source_id") or "").strip()
            project_source_type = str(project_payload.get("source_type") or "linear").strip()
            if project_source_type not in {"linear", "xero_tracking"}:
                raise ValueError(f"Unknown project source for {line_id}: {project_source_type}")
            project_key = (project_source_type, project_id)
            if project_id and project_key not in projects:
                raise ValueError(f"Unknown {project_source_type} project for {line_id}: {project_id}")
            if event_id and project_id:
                raise ValueError(f"Suggestion {line_id} must choose either an event or a project, not both")
            requested_mode = str(item.get("allocation_mode") or "").strip()
            allocation_mode = (
                XeroStatementSuggestion.ALLOCATION_EVENT if event_id
                else XeroStatementSuggestion.ALLOCATION_PROJECT if project_id
                else requested_mode
            )
            valid_modes = {choice[0] for choice in XeroStatementSuggestion.ALLOCATION_CHOICES}
            if not allocation_mode:
                allocation_mode = XeroStatementSuggestion.ALLOCATION_UNASSIGNED
            if allocation_mode not in valid_modes:
                raise ValueError(f"Invalid allocation_mode for {line_id}: {allocation_mode}")
            if requested_mode and requested_mode != allocation_mode:
                raise ValueError(f"allocation_mode does not match the selected allocation for {line_id}")
            if allocation_mode == XeroStatementSuggestion.ALLOCATION_MLAI_CORE and (event_id or project_id):
                raise ValueError(f"MLAI core cannot be combined with a specific allocation for {line_id}")
            if allocation_mode == XeroStatementSuggestion.ALLOCATION_EVENT and not event_id:
                raise ValueError(f"Event allocation requires a known event for {line_id}")
            if allocation_mode == XeroStatementSuggestion.ALLOCATION_PROJECT and not project_id:
                raise ValueError(f"Project allocation requires a known project for {line_id}")
            if allocation_mode == XeroStatementSuggestion.ALLOCATION_UNASSIGNED and (event_id or project_id):
                raise ValueError(f"Unassigned allocation cannot include tracking for {line_id}")
            if (
                profile
                and profile.require_statement_tracking
                and allocation_mode == XeroStatementSuggestion.ALLOCATION_MLAI_CORE
                and not profile.default_project_tracking_option_name
            ):
                raise ValueError("Configure the default Project Name before using mandatory tracking")
            evidence = _serialize_evidence(item.get("evidence"))
            review_note = str(item.get("review_note") or "").strip()[:4000]
            description = str(item.get("description") or "").strip()[:4000]
            if (review_note or description) and not evidence:
                raise ValueError(f"Descriptions and notes for {line_id} require source evidence")

            contact_name = str(item.get("contact_name") or "").strip()[:255]
            account_code = str(item.get("account_code") or "").strip()[:64]
            account_name = str(item.get("account_name") or "").strip()[:255]
            tax_type = str(item.get("tax_type") or "").strip()[:255]
            normalized_action = normalize_statement_action(action)
            if normalized_action == XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION:
                proposed = (contact_name.casefold(), account_code.casefold(), account_name.casefold(), tax_type.casefold())
                exact_allowed = {
                    (
                        str(pattern.get("contact_name") or "").casefold(),
                        str(pattern.get("account_code") or "").casefold(),
                        str(pattern.get("account_name") or "").casefold(),
                        str(pattern.get("tax_type") or "").casefold(),
                    )
                    for pattern in candidate.get("allowed_historical_patterns") or []
                }
                approved_accounting = {
                    (
                        str(option.get("account_code") or "").casefold(),
                        str(option.get("account_name") or "").casefold(),
                        str(option.get("tax_type") or "").casefold(),
                    )
                    for option in context.get("approved_accounting_options") or []
                }
                proposed_accounting = proposed[1:]
                if not all(proposed) or (
                    verified_rule is None
                    and proposed not in exact_allowed
                    and proposed_accounting not in approved_accounting
                ):
                    raise ValueError(
                        f"Prefill fields for {line_id} are not backed by exact merchant history or an approved accounting option"
                    )
            elif normalized_action == XeroStatementSuggestion.ACTION_NEEDS_REVIEW and any(
                [account_code, account_name, tax_type]
            ):
                raise ValueError(f"Needs-review suggestion for {line_id} cannot contain unverified accounting fields")
            matched_bill_id = str(item.get("matched_xero_bill_id") or "").strip()[:255]
            if normalized_action == XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL:
                allowed_bills = {str(bill["xero_bill_id"]) for bill in candidate.get("matching_xero_bills") or []}
                if not matched_bill_id or matched_bill_id not in allowed_bills:
                    raise ValueError(f"Bill match for {line_id} is not in the supplied Xero candidates")

            overall_confidence = _bounded_confidence(item.get("confidence"))
            if allocation_mode == XeroStatementSuggestion.ALLOCATION_MLAI_CORE:
                item = {**item, "allocation_confidence": 1.0}
            scores, execution_ready, blocking_reasons = _statement_execution_assessment(
                item=item,
                normalized_action=normalized_action,
                overall_confidence=overall_confidence,
                has_tracking_assignment=allocation_mode != XeroStatementSuggestion.ALLOCATION_UNASSIGNED,
                require_tracking=bool(profile and profile.require_statement_tracking),
            )
            if csv_statement_duplicate_count(line.statement_line_id) > 1:
                execution_ready = False
                duplicate_reason = (
                    "Identical CSV statement lines cannot be prepared automatically because "
                    "the report has no stable per-line identifier."
                )
                if duplicate_reason not in blocking_reasons:
                    blocking_reasons.append(duplicate_reason)

            XeroStatementSuggestion.objects.filter(
                organization=organization,
                statement_line=line,
                status=XeroStatementSuggestion.STATUS_PROPOSED,
            ).exclude(run_id=run_id).update(status=XeroStatementSuggestion.STATUS_SUPERSEDED, updated_at=timezone.now())
            suggestion, _created = XeroStatementSuggestion.objects.update_or_create(
                organization=organization,
                statement_line=line,
                run_id=run_id,
                defaults={
                    "proposed_action": action,
                    "contact_name": contact_name,
                    "account_code": account_code,
                    "account_name": account_name,
                    "tax_type": tax_type,
                    "description": description,
                    "event_source_type": event_source_type if event_id else "",
                    "event_source_id": event_id,
                    "event_tracking_option_name": str(
                        getattr(events.get(event_key), "event_name", "") or ""
                    )[:255],
                    "allocation_mode": allocation_mode,
                    "project_source_type": project_source_type if project_id else "",
                    "project_source_id": project_id,
                    "project_tracking_option_id": str(
                        (projects.get(project_key) or {}).get("tracking_option_id") or ""
                    )[:255],
                    "project_tracking_option_name": str(
                        (projects.get(project_key) or {}).get("name") or ""
                    )[:255],
                    "matched_xero_bill_id": matched_bill_id,
                    "confidence": overall_confidence,
                    "identity_confidence": scores["identity"],
                    "accounting_confidence": scores["accounting"],
                    "allocation_confidence": scores["allocation"],
                    "document_confidence": scores["document"],
                    "execution_ready": execution_ready,
                    "blocking_reasons": blocking_reasons,
                    "rationale": str(item.get("rationale") or "")[:4000],
                    "review_note": review_note,
                    "evidence": evidence,
                    "source_hash": line.source_hash,
                    "model_name": str(item.get("model_name") or model_name or "")[:255],
                    "status": XeroStatementSuggestion.STATUS_PROPOSED,
                },
            )
            if verified_rule:
                record_reconciliation_decision(
                    statement_line=line,
                    suggestion=suggestion,
                    rule=verified_rule,
                    decision_type=ReconciliationDecision.TYPE_RULE_APPLIED,
                    run_id=run_id,
                    actor_type=ReconciliationDecision.ACTOR_SYSTEM,
                    outcome={
                        "rule": serialize_reconciliation_rule(verified_rule),
                        "execution_ready": execution_ready,
                        "blocking_reasons": blocking_reasons,
                    },
                    evidence=rule_evidence(verified_rule),
                )
            record_reconciliation_decision(
                statement_line=line,
                suggestion=suggestion,
                rule=verified_rule,
                decision_type=ReconciliationDecision.TYPE_SUGGESTION_SAVED,
                run_id=run_id,
                actor_type=decision_actor_type,
                outcome={
                    "proposed_action": action,
                    "execution_ready": execution_ready,
                    "blocking_reasons": blocking_reasons,
                },
                evidence=evidence,
            )
            saved.append(suggestion)
    return saved


def prepare_verified_rule_suggestions(
    *,
    organization,
    run_id: str,
    statement_line_ids: list[str],
) -> dict[str, Any]:
    """Prepare deterministic suggestions and leave unresolved lines for Valley."""

    requested_ids = list(dict.fromkeys(
        str(item or "").strip()
        for item in statement_line_ids
        if str(item or "").strip()
    ))
    lines = _active_statement_lines(
        organization=organization,
        statement_line_ids=requested_ids,
    )
    prepared_payloads: list[dict[str, Any]] = []
    deterministic_line_ids: list[str] = []
    conflict_line_ids: list[str] = []
    deferred_bill_line_ids: list[str] = []
    unresolved_line_ids: list[str] = []
    for line in lines:
        rule, conflicts = resolve_reconciliation_rule(line)
        if (rule or conflicts) and _bill_candidates(organization, line):
            deferred_bill_line_ids.append(line.statement_line_id)
            unresolved_line_ids.append(line.statement_line_id)
            continue
        if rule:
            deterministic_line_ids.append(line.statement_line_id)
            prepared_payloads.append({
                "statement_line_id": line.statement_line_id,
                "proposed_action": XeroStatementSuggestion.ACTION_NEEDS_REVIEW,
                "confidence": 0.99,
                "identity_confidence": 1.0,
                "accounting_confidence": 1.0,
                "allocation_confidence": 1.0 if (
                    rule.event_source_id or rule.project_source_id
                ) else 0.0,
                "document_confidence": 0.0,
                "rationale": f"Applied verified reconciliation rule #{rule.id}.",
            })
            continue
        if conflicts:
            conflict_line_ids.append(line.statement_line_id)
            prepared_payloads.append({
                "statement_line_id": line.statement_line_id,
                "proposed_action": XeroStatementSuggestion.ACTION_NEEDS_REVIEW,
                "confidence": 0.0,
                "identity_confidence": 0.0,
                "accounting_confidence": 0.0,
                "allocation_confidence": 0.0,
                "document_confidence": 0.0,
                "rationale": "Verified reconciliation rules conflict.",
            })
            continue
        unresolved_line_ids.append(line.statement_line_id)

    saved = (
        save_statement_suggestions(
            organization=organization,
            run_id=run_id,
            suggestions=prepared_payloads,
            model_name="deterministic_verified_rule",
            decision_actor_type=ReconciliationDecision.ACTOR_SYSTEM,
        )
        if prepared_payloads
        else []
    )
    return {
        "suggestions": saved,
        "deterministic_line_ids": deterministic_line_ids,
        "conflict_line_ids": conflict_line_ids,
        "deferred_bill_line_ids": deferred_bill_line_ids,
        "unresolved_line_ids": unresolved_line_ids,
    }
