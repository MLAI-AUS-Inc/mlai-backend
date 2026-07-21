"""Guarded context and proposals for Xero's browser-only statement queue."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceProvider,
    XeroStatementLineSnapshot,
    XeroStatementSuggestion,
)
from startup_updates.models import (
    GmailMessageArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LumaEventSelection,
    SlackMessageArtifact,
)


ALLOWED_STATEMENT_EVIDENCE_PROVIDERS = {
    "gmail",
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


def import_xero_statement_lines(
    *, organization, bank_account_id: str, lines: list[dict[str, Any]], currency: str = "AUD"
) -> list[XeroStatementLineSnapshot]:
    if not bank_account_id:
        raise ValueError("bank_account_id is required")
    if not isinstance(lines, list) or not lines:
        raise ValueError("lines must be a non-empty list")
    saved: list[XeroStatementLineSnapshot] = []
    seen_ids: set[str] = set()
    with transaction.atomic():
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
            defaults = {
                **values,
                "current_contact": str(raw.get("contact") or "").strip()[:255],
                "current_account": str(raw.get("account") or "").strip()[:255],
                "current_description": str(raw.get("description") or "").strip()[:4000],
                "current_event_name": str(raw.get("event_name") or "").strip()[:255],
                "current_project_name": str(raw.get("project_name") or "").strip()[:255],
                "current_tax_type": str(raw.get("tax_type") or "").strip()[:255],
                "ready_in_xero": bool(raw.get("has_ok")),
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
        XeroStatementLineSnapshot.objects.filter(
            organization=organization,
            bank_account_id=bank_account_id,
            active=True,
        ).exclude(statement_line_id__in=seen_ids).update(active=False, last_seen_at=timezone.now())
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


def format_statement_browser_comment(*, description: str, review_note: str, confidence: float) -> str:
    """Return the short, conversational comment shown in Xero Discuss."""

    summary = str(description or "").strip() or str(review_note or "").strip()
    summary = _COMMENT_PREFIX_RE.sub("", summary)
    summary = _COMMENT_META_RE.sub("", summary)
    summary = re.sub(r"\s+", " ", summary).strip(" -;:.")
    summary = re.split(r"(?<=[.!?])\s+", summary, maxsplit=1)[0].strip()
    if not summary:
        summary = "Not enough context to identify this payment"
    if len(summary) > 220:
        summary = f"{summary[:217].rstrip()}…"
    if summary and summary[0].islower():
        summary = f"{summary[0].upper()}{summary[1:]}"
    percentage = int(round(max(0.0, min(float(confidence or 0.0), 1.0)) * 100))
    return f"{summary}. Confidence: {percentage}%."


def serialize_statement_suggestion(suggestion: XeroStatementSuggestion) -> dict[str, Any]:
    account_display = " - ".join(
        value for value in (suggestion.account_code, suggestion.account_name) if value
    )
    return {
        "id": suggestion.id,
        "statement_line_id": suggestion.statement_line.statement_line_id,
        "run_id": suggestion.run_id,
        "proposed_action": suggestion.proposed_action,
        "contact_name": suggestion.contact_name,
        "account_code": suggestion.account_code,
        "account_name": suggestion.account_name,
        "tax_type": suggestion.tax_type,
        "description": suggestion.description,
        "event": {"source_type": "luma", "source_id": suggestion.event_source_id, "tracking_option_name": suggestion.event_tracking_option_name} if suggestion.event_source_id else None,
        "project": {"source_type": "linear", "source_id": suggestion.project_source_id, "tracking_option_name": suggestion.project_tracking_option_name} if suggestion.project_source_id else None,
        "matched_xero_bill_id": suggestion.matched_xero_bill_id,
        "confidence": suggestion.confidence,
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


def build_statement_reconciliation_context(*, organization, include_external_evidence: bool = True) -> dict[str, Any]:
    lines = list(
        XeroStatementLineSnapshot.objects.filter(organization=organization, active=True).order_by(
            "transaction_date", "statement_line_id"
        )
    )
    examples: list[dict[str, Any]] = []
    allowed_patterns: dict[str, list[dict[str, str]]] = {}
    candidates: list[dict[str, Any]] = []
    for line in lines:
        serialized = serialize_statement_line(line)
        if line.ready_in_xero:
            fields = serialized["current_xero_fields"]
            example = {
                "statement_line_id": line.statement_line_id,
                "merchant_key": serialized["merchant_key"],
                "narration": line.narration,
                **fields,
            }
            examples.append(example)
            allowed_patterns.setdefault(serialized["merchant_key"], []).append({
                "example_statement_line_id": line.statement_line_id,
                **fields,
            })
    for line in lines:
        if line.ready_in_xero:
            continue
        serialized = serialize_statement_line(line)
        serialized["matching_xero_bills"] = _bill_candidates(organization, line)
        serialized["allowed_historical_patterns"] = allowed_patterns.get(serialized["merchant_key"], [])
        serialized["context_evidence"] = (
            _candidate_context_evidence(organization=organization, line=line)
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
            "prefill": "Prefer an exact merchant_key pattern. Otherwise copy one complete account/tax tuple from approved_accounting_options when the evidence strongly supports it.",
            "bill_match": "An existing bill may only be proposed from matching_xero_bills on the same candidate.",
            "approval": "Suggestions prefill the browser form only; the human remains the only actor who clicks OK.",
        },
    }


def _catalogs(organization):
    events = {
        item.event_id: item
        for item in LumaEventSelection.objects.filter(organization=organization)
    }
    projects = {
        item.linear_project_id: item
        for item in LinearProjectArtifact.objects.filter(organization=organization)
    }
    for item in LinearProjectSelection.objects.filter(organization=organization):
        projects.setdefault(item.linear_project_id, item)
    return events, projects


def save_statement_suggestions(
    *, organization, run_id: str, suggestions: list[dict[str, Any]], model_name: str = ""
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
            action = str(item.get("proposed_action") or XeroStatementSuggestion.ACTION_NEEDS_REVIEW)
            if action not in {choice[0] for choice in XeroStatementSuggestion.ACTION_CHOICES}:
                raise ValueError(f"Invalid proposed_action for {line_id}")
            event_payload = item.get("event") if isinstance(item.get("event"), dict) else {}
            event_id = str(event_payload.get("source_id") or "").strip()
            if event_id and event_id not in events:
                raise ValueError(f"Unknown Luma event for {line_id}: {event_id}")
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
            if action == XeroStatementSuggestion.ACTION_PREFILL_CREATE:
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
                    proposed not in exact_allowed and proposed_accounting not in approved_accounting
                ):
                    raise ValueError(
                        f"Prefill fields for {line_id} are not backed by exact merchant history or an approved accounting option"
                    )
            elif action == XeroStatementSuggestion.ACTION_NEEDS_REVIEW and any(
                [account_code, account_name, tax_type]
            ):
                raise ValueError(f"Needs-review suggestion for {line_id} cannot contain unverified accounting fields")
            matched_bill_id = str(item.get("matched_xero_bill_id") or "").strip()[:255]
            if action == XeroStatementSuggestion.ACTION_MATCH_BILL:
                allowed_bills = {str(bill["xero_bill_id"]) for bill in candidate.get("matching_xero_bills") or []}
                if not matched_bill_id or matched_bill_id not in allowed_bills:
                    raise ValueError(f"Bill match for {line_id} is not in the supplied Xero candidates")

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
                    "event_source_id": event_id,
                    "event_tracking_option_name": str(getattr(events.get(event_id), "event_name", "") or "")[:255],
                    "project_source_id": project_id,
                    "project_tracking_option_name": str(getattr(projects.get(project_id), "name", "") or getattr(projects.get(project_id), "project_name", "") or "")[:255],
                    "matched_xero_bill_id": matched_bill_id,
                    "confidence": max(0.0, min(float(item.get("confidence") or 0.0), 1.0)),
                    "rationale": str(item.get("rationale") or "")[:4000],
                    "review_note": review_note,
                    "evidence": evidence,
                    "source_hash": line.source_hash,
                    "model_name": str(item.get("model_name") or model_name or "")[:255],
                    "status": XeroStatementSuggestion.STATUS_PROPOSED,
                },
            )
            saved.append(suggestion)
    return saved
