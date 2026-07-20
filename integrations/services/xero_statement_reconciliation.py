"""Guarded context and proposals for Xero's browser-only statement queue."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import transaction
from django.utils import timezone

from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceProvider,
    XeroStatementLineSnapshot,
    XeroStatementSuggestion,
)
from startup_updates.models import LinearProjectArtifact, LinearProjectSelection, LumaEventSelection


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


def serialize_statement_suggestion(suggestion: XeroStatementSuggestion) -> dict[str, Any]:
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


def build_statement_reconciliation_context(*, organization) -> dict[str, Any]:
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
        candidates.append(serialized)
    return {
        "statement_candidates": candidates,
        "prior_xero_examples": examples,
        "statement_policy": {
            "prefill": "Account and tax values may only be copied from an exact merchant_key historical Xero pattern.",
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
    context = build_statement_reconciliation_context(organization=organization)
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
                allowed = {
                    (
                        str(pattern.get("contact_name") or "").casefold(),
                        str(pattern.get("account_code") or "").casefold(),
                        str(pattern.get("account_name") or "").casefold(),
                        str(pattern.get("tax_type") or "").casefold(),
                    )
                    for pattern in candidate.get("allowed_historical_patterns") or []
                }
                if not all(proposed) or proposed not in allowed:
                    raise ValueError(f"Prefill fields for {line_id} are not backed by an exact historical Xero pattern")
            elif action == XeroStatementSuggestion.ACTION_NEEDS_REVIEW and any(
                [contact_name, account_code, account_name, tax_type]
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
