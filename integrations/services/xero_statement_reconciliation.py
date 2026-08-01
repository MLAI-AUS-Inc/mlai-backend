"""Guarded context and proposals for Xero's browser-only statement queue."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

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


def _candidate_context_evidence(*, organization, line: XeroStatementLineSnapshot) -> list[dict[str, Any]]:
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
    gmail_rows = GmailMessageArtifact.objects.filter(
        Q(organization=organization)
        & Q(internal_date__date__gte=start)
        & Q(internal_date__date__lt=end)
        & term_query
    ).values(
        "gmail_message_id",
        "internal_date",
        "subject",
        "from_address",
        "snippet",
        "cleaned_text",
        "body_preview",
        "attachment_manifest",
    )[:250]
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
    slack_rows = SlackMessageArtifact.objects.filter(
        Q(organization=organization)
        & Q(posted_at__date__gte=start)
        & Q(posted_at__date__lt=end)
        & slack_term_query
    ).values(
        "channel_id",
        "channel_name",
        "slack_message_ts",
        "author_name",
        "posted_at",
        "text",
        "cleaned_text",
    )[:250]
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
        if not mirrors_nearby_event and (
            date_delta is None or abs(date_delta) > STATEMENT_NEARBY_EVENT_WINDOW_DAYS
        ):
            continue
        serialized = {
            "source_type": "linear",
            "source_id": str(project.get("source_id") or ""),
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

    gmail_rows = GmailMessageArtifact.objects.filter(
        organization=organization,
        internal_date__date__gte=start,
        internal_date__date__lt=end,
    ).values(
        "gmail_message_id",
        "internal_date",
        "subject",
        "from_address",
        "snippet",
        "cleaned_text",
        "body_preview",
        "attachment_manifest",
    ).order_by("-internal_date")[:500]
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

    slack_rows = SlackMessageArtifact.objects.filter(
        organization=organization,
        posted_at__date__gte=start,
        posted_at__date__lt=end,
    ).values(
        "channel_id", "channel_name", "slack_message_ts", "author_name", "posted_at", "text", "cleaned_text",
    ).order_by("-posted_at")[:500]
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


def _sanitize_capture_metadata(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Keep bounded completeness evidence and reject credential-shaped data."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("capture_metadata must be an object")
    forbidden = {
        "token", "access_token", "refresh_token", "authorization", "cookie",
        "api_key", "secret", "lines", "narration", "reference",
    }
    if forbidden.intersection(str(key).lower() for key in raw):
        raise ValueError("capture_metadata contains a forbidden sensitive field")
    pages = raw.get("pages") or []
    if not isinstance(pages, list) or len(pages) > 500:
        raise ValueError("capture_metadata pages must be a bounded list")
    safe_pages = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("capture_metadata page evidence must be an object")
        if not isinstance(page.get("has_previous"), bool) or not isinstance(page.get("has_next"), bool):
            raise ValueError("capture_metadata pagination flags must be booleans")
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
        ):
            raise ValueError("capture_metadata page counts are out of range")
        safe_pages.append(safe_page)
    blockers = raw.get("blocking_reasons") or []
    if not isinstance(blockers, list) or len(blockers) > 20:
        raise ValueError("capture_metadata blocking_reasons must be a bounded list")
    return {
        "schema_version": 1,
        "scan_id": str(raw.get("scan_id") or "")[:128],
        "source_started_at": str(raw.get("source_started_at") or "")[:64],
        "source_completed_at": str(raw.get("source_completed_at") or "")[:64],
        "pages": safe_pages,
        "derived_complete": raw.get("derived_complete") is True,
        "blocking_reasons": [str(reason)[:500] for reason in blockers],
    }


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
    saved: list[XeroStatementLineSnapshot] = []
    seen_ids: set[str] = set()
    with transaction.atomic():
        scan = XeroStatementScan.objects.create(
            organization=organization,
            bank_account_id=bank_account_id,
            status=XeroStatementScan.STATUS_STARTED,
            source=str(source or "browser")[:32],
            requested_by=str(requested_by or "")[:100],
            expected_count=expected_count,
            observed_count=len(lines),
            payload_hash=hashlib.sha256(
                json.dumps(lines, sort_keys=True, separators=(",", ":"), default=str).encode()
            ).hexdigest(),
            capture_metadata=_sanitize_capture_metadata(capture_metadata),
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
                "bank_account_id": bank_account_id,
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
                bank_account_id=bank_account_id,
                statement_line_id=statement_line_id,
                defaults=defaults,
            )
            saved.append(snapshot)
        completed_at = timezone.now()
        if complete_scan:
            missing_lines = list(XeroStatementLineSnapshot.objects.filter(
                organization=organization,
                bank_account_id=bank_account_id,
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
        "event": {
            "source_type": suggestion.event_source_type or "luma",
            "source_id": suggestion.event_source_id,
            "tracking_option_name": suggestion.event_tracking_option_name,
        } if suggestion.event_source_id else None,
        "project": {"source_type": "linear", "source_id": suggestion.project_source_id, "tracking_option_name": suggestion.project_tracking_option_name} if suggestion.project_source_id else None,
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
            "project_name": suggestion.project_tracking_option_name,
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
) -> dict[str, Any]:
    luma_events = luma_events or []
    humanitix_events = humanitix_events or []
    linear_projects = linear_projects or []
    lines = list(
        XeroStatementLineSnapshot.objects.filter(organization=organization, active=True).order_by(
            "transaction_date", "statement_line_id"
        )
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
            _candidate_context_evidence(organization=organization, line=line)
            if include_external_evidence
            else []
        )
        serialized["event_project_context_evidence"] = (
            _candidate_event_project_evidence(
                organization=organization,
                line=line,
                entity_entries=entity_entries,
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
        item.linear_project_id: item
        for item in LinearProjectArtifact.objects.filter(organization=organization)
    }
    for item in LinearProjectSelection.objects.filter(organization=organization):
        projects.setdefault(item.linear_project_id, item)
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
        if normalized_action == XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION:
            if scores["accounting"] < accounting_threshold:
                reasons.append(f"Accounting confidence must be at least {accounting_threshold:.0%}.")
            if has_tracking_assignment and scores["allocation"] < allocation_threshold:
                reasons.append(f"Allocation confidence must be at least {allocation_threshold:.0%}.")
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
            if project_id and project_id not in projects:
                raise ValueError(f"Unknown Linear project for {line_id}: {project_id}")
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
            scores, execution_ready, blocking_reasons = _statement_execution_assessment(
                item=item,
                normalized_action=normalized_action,
                overall_confidence=overall_confidence,
                has_tracking_assignment=bool(event_id or project_id),
            )

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
                    "project_source_id": project_id,
                    "project_tracking_option_name": str(getattr(projects.get(project_id), "name", "") or getattr(projects.get(project_id), "project_name", "") or "")[:255],
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
    lines = list(
        XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            statement_line_id__in=requested_ids,
            active=True,
        ).order_by("transaction_date", "statement_line_id")
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
