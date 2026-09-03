"""Durable Stripe payout ledger and explicit Xero posting workflow."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date

from integrations import http_client
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    HumanitixEvent,
    ReconciliationMapping,
    ReconciliationProfile,
    ReconciliationSuggestion,
    StripePayoutReconciliation,
    XeroStatementLineSnapshot,
)
from integrations.services.external_connectors import _xero_required_token
from integrations.services.xero_scopes import normalize_xero_scopes, xero_has_payment_write_scope
from organizations.models import Organization


XERO_API_URL = "https://api.xero.com/api.xro/2.0"
XERO_BANK_TRANSACTION_SCOPE = "accounting.banktransactions"
XERO_LEGACY_TRANSACTION_SCOPE = "accounting.transactions"
XERO_SETTINGS_WRITE_SCOPE = "accounting.settings"


class ReconciliationValidationError(RuntimeError):
    def __init__(self, message: str, *, errors: list[str] | None = None):
        super().__init__(message)
        self.errors = errors or [message]


class XeroPostingError(RuntimeError):
    pass


def xero_has_bank_transaction_scope(scopes: Any) -> bool:
    normalized = normalize_xero_scopes(scopes)
    return bool(
        XERO_BANK_TRANSACTION_SCOPE in normalized
        or XERO_LEGACY_TRANSACTION_SCOPE in normalized
    )


def xero_has_settings_write_scope(scopes: Any) -> bool:
    return XERO_SETTINGS_WRITE_SCOPE in normalize_xero_scopes(scopes)


def resolve_xero_connection(organization, connection_id: int | None = None):
    query = ExternalServiceConnection.objects.filter(
        organization=organization,
        provider=ExternalServiceProvider.XERO,
    ).exclude(status="disconnected")
    if connection_id:
        query = query.filter(id=connection_id)
    return query.order_by("-updated_at", "-id").first()


def persist_report(*, organization, report: dict[str, Any], stripe_account_id: str = "") -> list[StripePayoutReconciliation]:
    """Upsert every payout without changing an already-posted workflow state."""
    saved: list[StripePayoutReconciliation] = []
    for payout in report.get("payouts", []):
        if not isinstance(payout, dict) or not payout.get("payout_id"):
            continue
        stable_payload = dict(payout)
        source_hash = hashlib.sha256(
            json.dumps(stable_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        defaults = {
            "arrival_date": parse_date(str(payout.get("arrival_date") or "")),
            "currency": str(payout.get("currency") or ""),
            "amount_cents": int(payout.get("deposit_cents") or 0),
            "source_hash": source_hash,
            "report_payload": stable_payload,
            "warnings": list(payout.get("warnings") or []),
        }
        record, created = StripePayoutReconciliation.objects.get_or_create(
            organization=organization,
            stripe_account_id=str(stripe_account_id or ""),
            payout_id=str(payout["payout_id"]),
            defaults=defaults,
        )
        if not created:
            old_hash = record.source_hash
            for field, value in defaults.items():
                setattr(record, field, value)
            update_fields = [*defaults.keys(), "updated_at"]
            if record.status != StripePayoutReconciliation.STATUS_POSTED:
                record.preview_payload = {}
                record.status = StripePayoutReconciliation.STATUS_NEEDS_REVIEW
                update_fields.extend(["preview_payload", "status"])
            elif old_hash and old_hash != source_hash:
                record.warnings = [*record.warnings, "Stripe source data changed after this payout was posted."]
            record.save(update_fields=sorted(set(update_fields)))
        saved.append(record)
    return saved


def serialize_profile(profile: ReconciliationProfile) -> dict[str, Any]:
    scopes = profile.xero_connection.scopes if profile.xero_connection else []
    return {
        "organization_id": profile.organization_id,
        "xero_connection_id": profile.xero_connection_id,
        "stripe_account_id": profile.stripe_account_id,
        "xero_bank_account_id": profile.xero_bank_account_id,
        "xero_bank_account_name": profile.xero_bank_account_name,
        "xero_contact_id": profile.xero_contact_id,
        "xero_contact_name": profile.xero_contact_name,
        "humanitix_contact_id": profile.humanitix_contact_id,
        "humanitix_contact_name": profile.humanitix_contact_name,
        "revenue_account_code": profile.revenue_account_code,
        "fee_account_code": profile.fee_account_code,
        "refund_account_code": profile.refund_account_code,
        "revenue_tax_type": profile.revenue_tax_type,
        "fee_tax_type": profile.fee_tax_type,
        "refund_tax_type": profile.refund_tax_type,
        "line_amount_types": profile.line_amount_types,
        "event_tracking_category_id": profile.event_tracking_category_id,
        "event_tracking_category_name": profile.event_tracking_category_name,
        "project_tracking_category_id": profile.project_tracking_category_id,
        "project_tracking_category_name": profile.project_tracking_category_name,
        "require_statement_tracking": profile.require_statement_tracking,
        "default_project_tracking_option_name": profile.default_project_tracking_option_name,
        "default_project_tracking_option_id": profile.default_project_tracking_option_id,
        "standalone_fee_project_option_id": profile.standalone_fee_project_option_id,
        "standalone_fee_project_option_name": profile.standalone_fee_project_option_name,
        "humanitix_profitability_included": profile.humanitix_profitability_included,
        "profitability_policy_verified_by_slack_id": (
            profile.profitability_policy_verified_by_slack_id
        ),
        "profitability_policy_verified_at": (
            profile.profitability_policy_verified_at.isoformat()
            if profile.profitability_policy_verified_at
            else None
        ),
        "enabled": profile.enabled,
        # Keep the original field for clients already using the payout flow.
        "xero_write_scope": bool(profile.xero_connection and xero_has_bank_transaction_scope(scopes)),
        "can_create_bank_transactions": bool(
            profile.xero_connection and xero_has_bank_transaction_scope(scopes)
        ),
        "can_create_bill_payments": bool(
            profile.xero_connection and xero_has_payment_write_scope(scopes)
        ),
        "can_create_tracking_options": bool(
            profile.xero_connection and xero_has_settings_write_scope(scopes)
        ),
    }


def serialize_mapping(mapping: ReconciliationMapping) -> dict[str, Any]:
    return {
        "id": mapping.id,
        "source_type": mapping.source_type,
        "source_id": mapping.source_id,
        "source_label": mapping.source_label,
        "accounting_treatment": mapping.accounting_treatment,
        "event_tracking_option_id": mapping.event_tracking_option_id,
        "event_tracking_option_name": mapping.event_tracking_option_name,
        "project_tracking_option_id": mapping.project_tracking_option_id,
        "project_tracking_option_name": mapping.project_tracking_option_name,
        "project_source_type": mapping.project_source_type,
        "project_source_id": mapping.project_source_id,
        "reconciliation_note": mapping.reconciliation_note,
        "account_code": mapping.account_code,
        "tax_type": mapping.tax_type,
        "active": mapping.active,
    }


def serialize_payout(record: StripePayoutReconciliation, *, include_payload: bool = False) -> dict[str, Any]:
    result = {
        "payout_id": record.payout_id,
        "stripe_account_id": record.stripe_account_id,
        "arrival_date": record.arrival_date.isoformat() if record.arrival_date else None,
        "currency": record.currency,
        "amount_cents": record.amount_cents,
        "status": record.status,
        "warnings": record.warnings,
        "approved_by_slack_id": record.approved_by_slack_id,
        "approved_at": record.approved_at.isoformat() if record.approved_at else None,
        "xero_bank_transaction_id": record.xero_bank_transaction_id,
        "posted_at": record.posted_at.isoformat() if record.posted_at else None,
        "last_error": record.last_error,
    }
    from integrations.services.reconciliation_context import serialize_suggestion

    result["contextual_suggestions"] = [
        serialize_suggestion(suggestion)
        for suggestion in record.suggestions.exclude(status="superseded").order_by("source_type", "source_id", "-created_at")
    ]
    if include_payload:
        result["report"] = record.report_payload
        result["preview"] = record.preview_payload
    return result


def _mapping_tracking_spec(
    profile: ReconciliationProfile,
    mapping: ReconciliationMapping,
) -> dict[str, str] | None:
    event_assigned = bool(
        str(mapping.event_tracking_option_id or "").strip()
        or str(mapping.event_tracking_option_name or "").strip()
    )
    project_assigned = bool(
        str(mapping.project_tracking_option_id or "").strip()
        or str(mapping.project_tracking_option_name or "").strip()
        or str(mapping.project_source_id or "").strip()
    )
    if event_assigned and project_assigned:
        raise ReconciliationValidationError(
            "Stripe source mapping is not one-dimensional.",
            errors=[
                f"Mapping {mapping.source_type}:{mapping.source_id} must use Event xor Project tracking."
            ],
        )
    if not event_assigned and not project_assigned:
        return None
    if event_assigned:
        spec = {
            "dimension": "event",
            "category_id": str(profile.event_tracking_category_id or "").strip(),
            "category_name": str(profile.event_tracking_category_name or "").strip(),
            "option_id": str(mapping.event_tracking_option_id or "").strip(),
            "option_name": str(mapping.event_tracking_option_name or "").strip(),
            "id_field": "event_tracking_option_id",
        }
    else:
        spec = {
            "dimension": "project",
            "category_id": str(profile.project_tracking_category_id or "").strip(),
            "category_name": str(profile.project_tracking_category_name or "").strip(),
            "option_id": str(mapping.project_tracking_option_id or "").strip(),
            "option_name": str(mapping.project_tracking_option_name or "").strip(),
            "id_field": "project_tracking_option_id",
        }
    missing = [
        label
        for label in ("category_id", "category_name", "option_name")
        if not spec[label]
    ]
    if missing:
        raise ReconciliationValidationError(
            "Stripe source mapping has incomplete Xero tracking metadata.",
            errors=[
                f"Mapping {mapping.source_type}:{mapping.source_id} is missing "
                + ", ".join(missing)
                + "."
            ],
        )
    return spec


def _tracking(
    profile: ReconciliationProfile,
    mapping: ReconciliationMapping,
) -> list[dict[str, str]]:
    spec = _mapping_tracking_spec(profile, mapping)
    if spec is None:
        return []
    item = {
        "TrackingCategoryID": spec["category_id"],
        "Name": spec["category_name"],
        "TrackingOptionID": spec["option_id"],
        "Option": spec["option_name"],
    }
    return [{key: value for key, value in item.items() if value}]


def _xero_line(*, description: str, cents: int, account_code: str, tax_type: str, tracking: list[dict[str, str]]) -> dict[str, Any]:
    line = {
        "Description": description[:4000],
        "Quantity": 1,
        "UnitAmount": round(cents / 100.0, 2),
        "AccountCode": account_code,
        "TaxType": tax_type,
    }
    if tracking:
        line["Tracking"] = tracking
    return line


def _contextual_description(prefix: str, source_label: str, mapping: ReconciliationMapping) -> str:
    parts = [prefix, source_label]
    if mapping.event_tracking_option_name and _normalized_description_part(mapping.event_tracking_option_name) not in {
        _normalized_description_part(value) for value in parts
    }:
        parts.append(f"Event: {mapping.event_tracking_option_name}")
    if not mapping.event_tracking_option_name and mapping.project_tracking_option_name:
        parts.append(f"Project: {mapping.project_tracking_option_name}")
    if mapping.reconciliation_note:
        parts.append(mapping.reconciliation_note.strip())
    return " — ".join(value for value in parts if value)[:4000]


def _normalized_description_part(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _xero_payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _stripe_preview_hash(
    xero_payload: dict[str, Any], statement_binding: dict[str, str] | None
) -> str:
    """Bind approval to both the Xero payload and the exact bank statement row."""

    return _xero_payload_hash(
        {
            "xero_payload": xero_payload,
            "statement_binding": statement_binding,
        }
    )


def _stripe_statement_binding_values(
    *,
    statement_line_id: str,
    bank_account_id: str,
    statement_source_hash: str,
) -> dict[str, str]:
    statement_line_id = str(statement_line_id or "").strip()
    bank_account_id = str(bank_account_id or "").strip()
    statement_source_hash = str(statement_source_hash or "").strip()
    missing = [
        name
        for name, value in (
            ("statement_line_id", statement_line_id),
            ("bank_account_id", bank_account_id),
            ("statement_source_hash", statement_source_hash),
        )
        if not value
    ]
    if missing:
        raise ReconciliationValidationError(
            "Stripe payout statement binding is incomplete.",
            errors=["Provide " + ", ".join(missing) + "."],
        )
    if not re.fullmatch(r"[0-9a-f]{64}", statement_source_hash):
        raise ReconciliationValidationError(
            "Stripe payout statement binding is invalid.",
            errors=["statement_source_hash must be a lowercase SHA-256 value."],
        )
    return {
        "statement_line_id": statement_line_id,
        "bank_account_id": bank_account_id,
        "statement_source_hash": statement_source_hash,
    }


def _stripe_statement_binding(
    record: StripePayoutReconciliation,
    *,
    statement_line_id: str,
    bank_account_id: str,
    statement_source_hash: str,
    statement_capture_selection: Any = None,
) -> XeroStatementLineSnapshot:
    """Resolve the current inbound bank row that authorises one payout write."""

    binding = _stripe_statement_binding_values(
        statement_line_id=statement_line_id,
        bank_account_id=bank_account_id,
        statement_source_hash=statement_source_hash,
    )
    statement_line_id = binding["statement_line_id"]
    bank_account_id = binding["bank_account_id"]
    statement_source_hash = binding["statement_source_hash"]
    statement_line = XeroStatementLineSnapshot.objects.filter(
        organization=record.organization,
        bank_account_id=bank_account_id,
        statement_line_id=statement_line_id,
    ).first()
    if statement_line is None:
        raise ReconciliationValidationError(
            "Stripe payout statement binding is not current.",
            errors=[
                "The bound Xero statement line does not exist for this organisation and bank account."
            ],
        )

    errors: list[str] = []
    if statement_line.source_hash != statement_source_hash:
        errors.append("The bound Xero statement line changed after preview.")
    if statement_line.direction != XeroStatementLineSnapshot.DIRECTION_CREDIT:
        errors.append("A Stripe payout must bind to a credit Xero statement line.")
    arrival_date = record.arrival_date
    if isinstance(arrival_date, str):
        arrival_date = parse_date(arrival_date)
    if arrival_date is None:
        errors.append("The Stripe payout has no arrival date to bind to a statement line.")
    elif statement_line.transaction_date != arrival_date:
        errors.append(
            f"The bound Xero statement line date is {statement_line.transaction_date}, "
            f"not the Stripe payout arrival date {arrival_date}."
        )
    expected_amount = (Decimal(record.amount_cents) / Decimal("100")).quantize(
        Decimal("0.01")
    )
    if statement_line.amount != expected_amount:
        errors.append(
            f"The bound Xero statement line amount is {statement_line.amount}, "
            f"not the Stripe payout amount {expected_amount}."
        )
    if record.currency and statement_line.currency.casefold() != record.currency.casefold():
        errors.append(
            f"The bound Xero statement line currency is {statement_line.currency}, "
            f"not the Stripe payout currency {record.currency}."
        )
    if not statement_line.active or (
        statement_line.queue_state != XeroStatementLineSnapshot.QUEUE_ACTIVE
    ):
        errors.append("The bound Xero statement line is no longer active and unreconciled.")
    if statement_line.is_green_match:
        errors.append("The bound Xero statement line already has a green match.")
    if statement_line.matched_xero_transaction_id:
        errors.append(
            "The bound Xero statement line already identifies a matched Xero transaction."
        )

    from integrations.services.xero_statement_reconciliation import (
        StatementCaptureValidationError,
        validate_current_statement_line_capture,
    )

    try:
        validate_current_statement_line_capture(
            statement_line,
            expected_bank_account_id=bank_account_id,
            expected_source_hash=statement_source_hash,
            selection=statement_capture_selection,
        )
    except StatementCaptureValidationError as exc:
        errors.append(str(exc))
    if errors:
        raise ReconciliationValidationError(
            "Stripe payout statement binding is not current.", errors=errors
        )
    return statement_line


def build_xero_preview(
    record: StripePayoutReconciliation,
    *,
    statement_line_id: str = "",
    bank_account_id: str = "",
    statement_source_hash: str = "",
    statement_capture_selection: Any = None,
) -> dict[str, Any]:
    try:
        profile = ReconciliationProfile.objects.select_related("xero_connection").get(
            organization=record.organization
        )
    except ReconciliationProfile.DoesNotExist:
        raise ReconciliationValidationError("Reconciliation profile is not configured.")

    errors: list[str] = []
    selected_bank_account_id = profile.xero_bank_account_id
    resolved_statement_binding: dict[str, str] | None = None
    if statement_line_id or bank_account_id or statement_source_hash:
        resolved_statement_binding = _stripe_statement_binding_values(
            statement_line_id=statement_line_id,
            bank_account_id=bank_account_id,
            statement_source_hash=statement_source_hash,
        )
        statement_line = _stripe_statement_binding(
            record,
            **resolved_statement_binding,
            statement_capture_selection=statement_capture_selection,
        )
        selected_bank_account_id = statement_line.bank_account_id
    connection = profile.xero_connection
    if not profile.enabled:
        errors.append("Reconciliation is disabled for this organisation.")
    if connection is None:
        errors.append("A Xero connection must be selected.")
    elif not xero_has_bank_transaction_scope(connection.scopes):
        errors.append("Reconnect Xero with the accounting.banktransactions scope before posting.")
    for label, value in (
        ("Xero bank account", selected_bank_account_id),
        ("revenue account code", profile.revenue_account_code),
        ("fee account code", profile.fee_account_code),
        ("refund account code", profile.refund_account_code),
        ("revenue tax type", profile.revenue_tax_type),
        ("fee tax type", profile.fee_tax_type),
        ("refund tax type", profile.refund_tax_type),
    ):
        if not str(value or "").strip():
            errors.append(f"Configure {label}.")
    if profile.line_amount_types not in {"Inclusive", "Exclusive", "NoTax"}:
        errors.append("line_amount_types must be Inclusive, Exclusive, or NoTax.")

    mappings = {
        (item.source_type, item.source_id): item
        for item in ReconciliationMapping.objects.filter(organization=record.organization, active=True)
    }
    report = record.report_payload or {}
    lines: list[dict[str, Any]] = []
    context_notes: list[dict[str, Any]] = []
    line_total_cents = 0

    for group in report.get("revenue_groups") or report.get("events") or []:
        source_key = (str(group.get("source_type") or "luma_event"), str(group.get("source_id") or group.get("event_api_id") or ""))
        mapping = mappings.get(source_key)
        if mapping is None:
            errors.append(f"Map {source_key[0]}:{source_key[1]} ({group.get('source_label') or group.get('event_name')}).")
            continue
        if mapping.accounting_treatment not in {
            ReconciliationMapping.TREATMENT_REVENUE,
            ReconciliationMapping.TREATMENT_CLEARING,
        }:
            errors.append(f"Choose revenue or clearing treatment for {source_key[0]}:{source_key[1]}.")
            continue
        clearing = mapping.accounting_treatment == ReconciliationMapping.TREATMENT_CLEARING
        if clearing and (not mapping.account_code or not mapping.tax_type):
            errors.append(f"Clearing treatment for {source_key[0]}:{source_key[1]} requires an explicit account code and tax type.")
            continue
        try:
            tracking = _tracking(profile, mapping)
        except ReconciliationValidationError as exc:
            errors.extend(exc.errors)
            continue
        if profile.require_statement_tracking and not tracking:
            errors.append(
                f"Map {source_key[0]}:{source_key[1]} to exactly one Event, Project, "
                "or MLAI core option."
            )
            continue
        gross = int(group.get("gross_cents") or 0)
        group_fee = int(group.get("stripe_fee_cents") or 0)
        treatment_label = "clearing" if clearing else "revenue"
        source_label = str(group.get("source_label") or group.get("event_name") or source_key[1])
        lines.append(_xero_line(description=_contextual_description(f"Stripe {treatment_label}", source_label, mapping), cents=gross, account_code=mapping.account_code or profile.revenue_account_code, tax_type=mapping.tax_type or profile.revenue_tax_type, tracking=tracking))
        context_notes.append({
            "source_type": source_key[0],
            "source_id": source_key[1],
            "event_name": mapping.event_tracking_option_name,
            "project_name": (
                "" if mapping.event_tracking_option_name else mapping.project_tracking_option_name
            ),
            "review_note": mapping.reconciliation_note,
        })
        line_total_cents += gross
        if group_fee:
            lines.append(_xero_line(description=f"Stripe processing fees — {group.get('source_label') or group.get('event_name')}", cents=-group_fee, account_code=profile.fee_account_code, tax_type=profile.fee_tax_type, tracking=tracking))
            line_total_cents -= group_fee

    standalone_fee = int(report.get("standalone_fee_cents") or 0)
    if standalone_fee:
        fee_tracking = []
        if profile.standalone_fee_project_option_id or profile.standalone_fee_project_option_name:
            if not all(
                [
                    profile.project_tracking_category_id,
                    profile.project_tracking_category_name,
                    profile.standalone_fee_project_option_name,
                ]
            ):
                errors.append(
                    "Standalone Stripe fee tracking requires the exact Project Name "
                    "category ID/name and option name."
                )
            fee_tracking_item = {
                "TrackingCategoryID": profile.project_tracking_category_id,
                "Name": profile.project_tracking_category_name,
                "TrackingOptionID": profile.standalone_fee_project_option_id,
                "Option": profile.standalone_fee_project_option_name,
            }
            fee_tracking = [
                {key: value for key, value in fee_tracking_item.items() if value}
            ]
        elif profile.require_statement_tracking:
            errors.append(
                "Standalone Stripe fees require an explicit Project or MLAI core option."
            )
        lines.append(_xero_line(description="Stripe standalone fees", cents=-standalone_fee, account_code=profile.fee_account_code, tax_type=profile.fee_tax_type, tracking=fee_tracking))
        line_total_cents -= standalone_fee

    for adjustment in report.get("refunds") or []:
        source_key = (str(adjustment.get("source_type") or "unattributed"), str(adjustment.get("source_id") or adjustment.get("id") or ""))
        mapping = mappings.get(source_key)
        if mapping is None:
            errors.append(f"Map refund/adjustment {source_key[0]}:{source_key[1]}.")
            continue
        if mapping.accounting_treatment not in {
            ReconciliationMapping.TREATMENT_REVENUE,
            ReconciliationMapping.TREATMENT_CLEARING,
        }:
            errors.append(f"Choose revenue or clearing treatment for refund {source_key[0]}:{source_key[1]}.")
            continue
        clearing = mapping.accounting_treatment == ReconciliationMapping.TREATMENT_CLEARING
        if clearing and (not mapping.account_code or not mapping.tax_type):
            errors.append(f"Clearing treatment for refund {source_key[0]}:{source_key[1]} requires an explicit account code and tax type.")
            continue
        try:
            tracking = _tracking(profile, mapping)
        except ReconciliationValidationError as exc:
            errors.extend(exc.errors)
            continue
        if profile.require_statement_tracking and not tracking:
            errors.append(
                f"Map refund {source_key[0]}:{source_key[1]} to exactly one Event, "
                "Project, or MLAI core option."
            )
            continue
        cents = int(adjustment.get("net_cents") or 0)
        adjustment_label = str(adjustment.get("source_label") or adjustment.get("description") or adjustment.get("id") or source_key[1])
        lines.append(_xero_line(description=_contextual_description("Stripe refund/adjustment", adjustment_label, mapping), cents=cents, account_code=mapping.account_code if clearing else (mapping.account_code or profile.refund_account_code), tax_type=mapping.tax_type if clearing else (mapping.tax_type or profile.refund_tax_type), tracking=tracking))
        line_total_cents += cents

    expected_cents = int(report.get("deposit_cents") or record.amount_cents)
    if any("Tie-out mismatch" in str(warning) for warning in report.get("warnings") or []):
        errors.append("Stripe payout does not tie to its balance transactions.")
    if line_total_cents != expected_cents:
        errors.append(f"Xero line total {line_total_cents} cents does not equal payout {expected_cents} cents.")

    contact = {"ContactID": profile.xero_contact_id} if profile.xero_contact_id else {"Name": profile.xero_contact_name or "Stripe Payments"}
    payload = {
        "Type": "RECEIVE",
        "Contact": contact,
        "BankAccount": {"AccountID": selected_bank_account_id},
        "Date": record.arrival_date.isoformat() if record.arrival_date else date.today().isoformat(),
        "Reference": record.payout_id,
        "CurrencyCode": record.currency,
        "LineAmountTypes": profile.line_amount_types,
        "Status": "AUTHORISED",
        "LineItems": lines,
    }
    preview = {
        "ready": not errors,
        "errors": errors,
        "payout_id": record.payout_id,
        "expected_total_cents": expected_cents,
        "line_total_cents": line_total_cents,
        "xero_payload": payload,
        "payload_hash": _stripe_preview_hash(payload, resolved_statement_binding),
        "statement_binding": resolved_statement_binding,
        "context_notes": context_notes,
        "human_reconciliation_required": True,
        "note": "Posting creates a matching Receive Money transaction; a human must still click Match/OK on the Xero bank statement line.",
    }
    stored_preview = dict(preview)
    if record.xero_bank_transaction_id:
        durable_binding = (
            record.preview_payload.get("statement_binding")
            if isinstance(record.preview_payload, dict)
            else None
        )
        # A later read-only unbound/correction preview must not erase—or
        # retroactively invent—the exact row that authorised a posted payout.
        stored_preview["statement_binding"] = (
            dict(durable_binding) if isinstance(durable_binding, dict) else None
        )
        stored_preview["payload_hash"] = _stripe_preview_hash(
            stored_preview["xero_payload"], stored_preview["statement_binding"]
        )
    record.preview_payload = stored_preview
    if record.status != StripePayoutReconciliation.STATUS_POSTED:
        record.status = StripePayoutReconciliation.STATUS_READY if preview["ready"] else StripePayoutReconciliation.STATUS_NEEDS_REVIEW
    record.save(update_fields=["preview_payload", "status", "updated_at"])
    return preview


def _xero_headers(connection: ExternalServiceConnection) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_xero_required_token(connection)}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Xero-Tenant-Id": connection.external_account_id,
    }


def _money_to_cents(value: Any) -> int:
    try:
        amount = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError):
        return 0
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _xero_line_cents(line: dict[str, Any]) -> int:
    if line.get("UnitAmount") not in (None, ""):
        try:
            quantity = Decimal(str(line.get("Quantity") or "1"))
            unit_amount = Decimal(str(line.get("UnitAmount") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return 0
        return int((quantity * unit_amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return _money_to_cents(line.get("LineAmount"))


def _tracking_key(item: dict[str, Any]) -> tuple[str, str]:
    category = str(item.get("Name") or item.get("TrackingCategoryID") or "").strip().casefold()
    option = str(
        item.get("Option")
        or item.get("TrackingOptionName")
        or item.get("TrackingOptionID")
        or ""
    ).strip().casefold()
    return category, option


def _xero_line_key(line: dict[str, Any]) -> tuple[int, str, str, tuple[tuple[str, str], ...]]:
    tracking = tuple(
        sorted(
            _tracking_key(item)
            for item in line.get("Tracking") or []
            if isinstance(item, dict)
        )
    )
    return (
        _xero_line_cents(line),
        str(line.get("AccountCode") or "").strip().casefold(),
        str(line.get("TaxType") or "").strip().casefold(),
        tracking,
    )


def _line_key_payload(
    key: tuple[int, str, str, tuple[tuple[str, str], ...]]
) -> dict[str, Any]:
    return {
        "amount_cents": key[0],
        "account_code": key[1],
        "tax_type": key[2],
        "tracking": [
            {"category": category, "option": option}
            for category, option in key[3]
        ],
    }


def _xero_transaction_date(payload: dict[str, Any]) -> str:
    raw = str(payload.get("DateString") or payload.get("Date") or "").strip()
    if len(raw) >= 10 and raw[:4].isdigit() and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    return ""


def _xero_transaction_total_cents(payload: dict[str, Any]) -> int:
    if payload.get("Total") not in (None, ""):
        return _money_to_cents(payload.get("Total"))
    return sum(
        _xero_line_cents(line)
        for line in payload.get("LineItems") or []
        if isinstance(line, dict)
    )


def _xero_bank_account_id(payload: dict[str, Any]) -> str:
    bank_account = payload.get("BankAccount")
    if not isinstance(bank_account, dict):
        return ""
    return str(bank_account.get("AccountID") or "").strip()


def _xero_transaction_summary(
    payload: dict[str, Any],
    *,
    match_basis: str,
) -> dict[str, Any]:
    line_items = [
        line for line in payload.get("LineItems") or [] if isinstance(line, dict)
    ]
    return {
        "bank_transaction_id": str(payload.get("BankTransactionID") or "").strip(),
        "reference": str(payload.get("Reference") or "").strip(),
        "date": _xero_transaction_date(payload),
        "type": str(payload.get("Type") or "").strip(),
        "status": str(payload.get("Status") or "").strip(),
        "is_reconciled": bool(payload.get("IsReconciled")),
        "total_cents": _xero_transaction_total_cents(payload),
        "line_count": len(line_items),
        "tracking_count": sum(
            len(line.get("Tracking") or [])
            for line in line_items
        ),
        "match_basis": match_basis,
    }


def fetch_xero_bank_transactions(
    profile: ReconciliationProfile,
    *,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Fetch Xero bank transactions once for a correction-preview batch.

    The Accounting API does not expose the bank-feed statement queue, but it
    does expose the accounting transactions already created against it.  This
    read-only fetch lets the preview distinguish a missing transaction from a
    reconciled legacy net-only transaction before any posting is attempted.
    """
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    if not xero_has_bank_transaction_scope(connection.scopes):
        raise ReconciliationValidationError(
            "Reconnect Xero with the accounting.banktransactions scope before auditing payouts."
        )

    headers = _xero_headers(connection)
    transactions: list[dict[str, Any]] = []
    for page in range(1, max(1, max_pages) + 1):
        response = http_client.get(
            f"{XERO_API_URL}/BankTransactions",
            headers=headers,
            params={"page": page, "unitdp": 4},
            timeout=(3, 30),
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("BankTransactions") if isinstance(payload, dict) else []
        rows = [item for item in rows or [] if isinstance(item, dict)]
        transactions.extend(rows)
        if len(rows) < 100:
            return transactions
    raise ReconciliationValidationError(
        "Xero bank-transaction audit exceeded its page limit; no correction "
        "classification was produced from partial data."
    )


def fetch_xero_accounts(
    profile: ReconciliationProfile,
) -> list[dict[str, Any]]:
    """Fetch the current Xero chart of accounts without changing accounting data."""
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    response = http_client.get(
        f"{XERO_API_URL}/Accounts",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("Accounts") if isinstance(payload, dict) else []
    return [item for item in rows or [] if isinstance(item, dict)]


def _matching_xero_transactions(
    record: StripePayoutReconciliation,
    profile: ReconciliationProfile,
    bank_transactions: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], str]]:
    expected_date = record.arrival_date.isoformat() if record.arrival_date else ""
    expected_cents = int((record.report_payload or {}).get("deposit_cents") or record.amount_cents)
    bank_account_id = str(profile.xero_bank_account_id or "").strip()
    eligible = [
        item
        for item in bank_transactions
        if (
            not bank_account_id
            or not _xero_bank_account_id(item)
            or _xero_bank_account_id(item) == bank_account_id
        )
    ]
    exact_reference = [
        (item, "payout_reference")
        for item in eligible
        if str(item.get("Reference") or "").strip().casefold()
        == record.payout_id.strip().casefold()
    ]
    if exact_reference:
        return exact_reference
    return [
        (item, "date_and_amount")
        for item in eligible
        if _xero_transaction_date(item) == expected_date
        and _xero_transaction_total_cents(item) == expected_cents
    ]


def _line_item_differences(
    existing: dict[str, Any],
    proposed: dict[str, Any],
) -> dict[str, Any]:
    current_lines = [
        line for line in existing.get("LineItems") or [] if isinstance(line, dict)
    ]
    proposed_lines = [
        line for line in proposed.get("LineItems") or [] if isinstance(line, dict)
    ]
    current = Counter(_xero_line_key(line) for line in current_lines)
    desired = Counter(_xero_line_key(line) for line in proposed_lines)

    def expanded(counter: Counter) -> list[dict[str, Any]]:
        return [
            _line_key_payload(key)
            for key, count in counter.items()
            for _index in range(count)
        ]

    return {
        "line_items_match": current == desired,
        "current_line_count": len(current_lines),
        "proposed_line_count": len(proposed_lines),
        "missing_proposed_lines": expanded(desired - current),
        "unexpected_existing_lines": expanded(current - desired),
    }


def build_xero_correction_preview(
    record: StripePayoutReconciliation,
    *,
    bank_transactions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare Stripe's desired split with the accounting transaction in Xero.

    This function is deliberately read-only against Xero.  It identifies the
    legacy net-only transactions that must be unreconciled and replaced, while
    allowing genuinely missing payouts to continue through the existing,
    explicit posting workflow.
    """
    proposed = build_xero_preview(record)
    try:
        profile = ReconciliationProfile.objects.select_related("xero_connection").get(
            organization=record.organization
        )
    except ReconciliationProfile.DoesNotExist:
        raise ReconciliationValidationError("Reconciliation profile is not configured.")

    if bank_transactions is None:
        bank_transactions = fetch_xero_bank_transactions(profile)
    inactive_transactions = [
        item
        for item in bank_transactions
        if str(item.get("Status") or "").strip().upper() in {"DELETED", "VOIDED"}
    ]
    active_transactions = [
        item
        for item in bank_transactions
        if str(item.get("Status") or "").strip().upper() not in {"DELETED", "VOIDED"}
    ]
    matches = _matching_xero_transactions(record, profile, active_transactions)
    inactive_matches = _matching_xero_transactions(
        record,
        profile,
        inactive_transactions,
    )
    candidate_summaries = [
        _xero_transaction_summary(item, match_basis=basis)
        for item, basis in matches
    ]
    result: dict[str, Any] = {
        "payout_id": record.payout_id,
        "arrival_date": record.arrival_date.isoformat() if record.arrival_date else None,
        "amount_cents": record.amount_cents,
        "proposed_ready": proposed["ready"],
        "proposed_errors": proposed["errors"],
        "candidate_count": len(matches),
        "existing_transactions": candidate_summaries,
        "ignored_inactive_transactions": [
            _xero_transaction_summary(item, match_basis=basis)
            for item, basis in inactive_matches
        ],
        "classification": "",
        "recommended_action": "",
        "automatic_action_allowed": False,
        "requires_manual_unreconcile": False,
        "human_reconciliation_required": True,
        "differences": {},
        "proposed_xero_payload": proposed["xero_payload"],
        "context_notes": proposed["context_notes"],
    }
    if not matches:
        result.update(
            {
                "classification": "missing_xero_transaction",
                "recommended_action": "create_receive_money",
                "automatic_action_allowed": bool(proposed["ready"]),
            }
        )
        return result
    if len(matches) > 1:
        result.update(
            {
                "classification": "ambiguous_existing_transactions",
                "recommended_action": "manual_review",
            }
        )
        return result

    existing, basis = matches[0]
    differences = _line_item_differences(existing, proposed["xero_payload"])
    summary = _xero_transaction_summary(existing, match_basis=basis)
    result["differences"] = differences
    if differences["line_items_match"]:
        reconciled = summary["is_reconciled"]
        result.update(
            {
                "classification": (
                    "already_correct"
                    if reconciled
                    else "correct_transaction_ready_to_match"
                ),
                "recommended_action": "no_action" if reconciled else "match_existing",
            }
        )
        return result

    looks_net_only = (
        differences["current_line_count"] == 1
        and (
            differences["proposed_line_count"] > 1
            or summary["tracking_count"] == 0
        )
        and summary["total_cents"] == int(proposed["expected_total_cents"])
    )
    reconciled = summary["is_reconciled"]
    result.update(
        {
            "classification": (
                "legacy_net_only"
                if looks_net_only
                else "mismatched_xero_transaction"
            ),
            "recommended_action": (
                "unreconcile_then_replace"
                if reconciled
                else "replace_before_matching"
            ),
            "requires_manual_unreconcile": reconciled,
        }
    )
    return result


def build_event_revenue_rollup(
    records: list[StripePayoutReconciliation],
) -> list[dict[str, Any]]:
    """Aggregate Stripe cash contribution and persisted Luma attendance by source."""
    if not records:
        return []
    organization = records[0].organization
    mappings = {
        (item.source_type, item.source_id): item
        for item in ReconciliationMapping.objects.filter(
            organization=organization,
            active=True,
        )
    }
    luma_ids = {
        str(group.get("source_id") or group.get("event_api_id") or "")
        for record in records
        for group in (
            (record.report_payload or {}).get("revenue_groups")
            or (record.report_payload or {}).get("events")
            or []
        )
        if isinstance(group, dict)
        and str(group.get("source_type") or "luma_event") == "luma_event"
    }
    from startup_updates.models import LumaEventSelection

    luma_events: dict[str, LumaEventSelection] = {}
    for event in LumaEventSelection.objects.filter(
        organization=organization,
        event_id__in=luma_ids,
    ).order_by("-last_synced_at", "-updated_at"):
        luma_events.setdefault(event.event_id, event)

    rows: dict[tuple[str, str], dict[str, Any]] = {}

    def row_for(source_type: str, source_id: str, source_label: str) -> dict[str, Any]:
        key = (source_type, source_id)
        mapping = mappings.get(key)
        luma = luma_events.get(source_id) if source_type == "luma_event" else None
        return rows.setdefault(
            key,
            {
                "source_type": source_type,
                "source_id": source_id,
                "source_label": source_label,
                "mapping_status": "approved" if mapping else "missing",
                "event_name": (
                    mapping.event_tracking_option_name
                    if mapping
                    else (luma.event_name if luma else "")
                ),
                "project_name": mapping.project_tracking_option_name if mapping else "",
                "luma_event_name": luma.event_name if luma else "",
                "luma_event_url": luma.event_url if luma else "",
                "luma_start_at": luma.start_at.isoformat() if luma and luma.start_at else None,
                "luma_last_synced_at": (
                    luma.last_synced_at.isoformat()
                    if luma and luma.last_synced_at
                    else None
                ),
                "luma_registration_count": luma.registration_count if luma else None,
                "luma_checked_in_count": luma.checked_in_count if luma else None,
                "stripe_charge_count": 0,
                "gross_cents": 0,
                "refunds_cents": 0,
                "stripe_fee_cents": 0,
                "payout_ids": set(),
            },
        )

    for record in records:
        report = record.report_payload or {}
        for group in report.get("revenue_groups") or report.get("events") or []:
            if not isinstance(group, dict):
                continue
            source_type = str(group.get("source_type") or "luma_event")
            source_id = str(group.get("source_id") or group.get("event_api_id") or "")
            if not source_id:
                continue
            row = row_for(
                source_type,
                source_id,
                str(group.get("source_label") or group.get("event_name") or source_id),
            )
            row["stripe_charge_count"] += int(group.get("ticket_count") or 0)
            row["gross_cents"] += int(group.get("gross_cents") or 0)
            row["stripe_fee_cents"] += int(group.get("stripe_fee_cents") or 0)
            row["payout_ids"].add(record.payout_id)
        for refund in report.get("refunds") or []:
            if not isinstance(refund, dict):
                continue
            source_type = str(refund.get("source_type") or "unattributed")
            source_id = str(refund.get("source_id") or refund.get("id") or "")
            if not source_id:
                continue
            row = row_for(
                source_type,
                source_id,
                str(refund.get("source_label") or refund.get("description") or source_id),
            )
            row["refunds_cents"] += int(refund.get("net_cents") or 0)
            row["payout_ids"].add(record.payout_id)

    result: list[dict[str, Any]] = []
    for row in rows.values():
        payout_ids = sorted(row.pop("payout_ids"))
        row["payout_ids"] = payout_ids
        row["payout_count"] = len(payout_ids)
        row["net_revenue_before_fees_cents"] = (
            row["gross_cents"] + row["refunds_cents"]
        )
        row["net_cash_contribution_cents"] = (
            row["gross_cents"]
            + row["refunds_cents"]
            - row["stripe_fee_cents"]
        )
        result.append(row)
    return sorted(
        result,
        key=lambda item: (
            item["event_name"] or item["project_name"] or item["source_label"]
        ).casefold(),
    )


def _xero_tracking_option(
    line: dict[str, Any],
    *,
    category_id: str,
    category_name: str,
) -> str:
    target_id = str(category_id or "").strip().casefold()
    target_name = str(category_name or "").strip().casefold()
    for item in line.get("Tracking") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("TrackingCategoryID") or "").strip().casefold()
        item_name = str(item.get("Name") or "").strip().casefold()
        if not (
            (target_id and item_id == target_id)
            or (target_name and item_name == target_name)
        ):
            continue
        return str(
            item.get("Option")
            or item.get("TrackingOptionName")
            or item.get("TrackingOptionID")
            or ""
        ).strip()
    return ""


def build_event_cashflow_validation(
    *,
    event_revenue: list[dict[str, Any]],
    bank_transactions: list[dict[str, Any]],
    payout_previews: list[dict[str, Any]],
    profile: ReconciliationProfile,
    period_start: date | None = None,
    period_end: date | None = None,
    excluded_transfer_transaction_ids: set[str] | None = None,
    account_names_by_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Estimate event/project cashflow without double-counting Stripe payouts.

    Stripe is the source of truth for gross ticket revenue, refunds, and fees.
    Other Xero RECEIVE/SPEND lines supply sponsorship and operating cashflows.
    Any Xero transaction matched to a Stripe payout is excluded because its
    gross/refund/fee split is already represented by ``event_revenue``.
    """
    stripe_transaction_ids = {
        str(transaction.get("bank_transaction_id") or "").strip()
        for preview in payout_previews
        for transaction in preview.get("existing_transactions") or []
        if str(transaction.get("bank_transaction_id") or "").strip()
    }
    excluded_transfer_transaction_ids = {
        str(value or "").strip()
        for value in (excluded_transfer_transaction_ids or set())
        if str(value or "").strip()
    }
    account_names_by_code = {
        str(code or "").strip().casefold(): str(name or "").strip()
        for code, name in (account_names_by_code or {}).items()
        if str(code or "").strip()
    }
    stripe_lines: list[dict[str, Any]] = []
    tracked_lines: list[dict[str, Any]] = []
    excluded_transfer_lines: list[dict[str, Any]] = []
    for transaction in bank_transactions:
        transaction_id = str(transaction.get("BankTransactionID") or "").strip()
        status = str(transaction.get("Status") or "").strip().upper()
        if status in {"DELETED", "VOIDED"}:
            continue
        transaction_type = str(transaction.get("Type") or "").strip().upper()
        if transaction_type not in {"RECEIVE", "SPEND"}:
            continue
        transaction_date = parse_date(_xero_transaction_date(transaction))
        if period_start and (
            transaction_date is None or transaction_date < period_start
        ):
            continue
        if period_end and (
            transaction_date is None or transaction_date > period_end
        ):
            continue
        for index, line in enumerate(transaction.get("LineItems") or []):
            if not isinstance(line, dict):
                continue
            event_name = _xero_tracking_option(
                line,
                category_id=profile.event_tracking_category_id,
                category_name=profile.event_tracking_category_name,
            )
            project_name = _xero_tracking_option(
                line,
                category_id=profile.project_tracking_category_id,
                category_name=profile.project_tracking_category_name,
            )
            raw_cents = _xero_line_cents(line)
            if not raw_cents:
                continue
            account_code = str(line.get("AccountCode") or "").strip()
            source_line = {
                "line_id": f"{transaction_id}:{index}",
                "bank_transaction_id": transaction_id,
                "date": _xero_transaction_date(transaction),
                "transaction_type": transaction_type,
                "event_name": event_name,
                "project_name": project_name,
                "account_code": account_code,
                "account_name": account_names_by_code.get(
                    account_code.casefold(),
                    "",
                ),
                "description": str(line.get("Description") or "").strip(),
                "reference": str(transaction.get("Reference") or "").strip(),
                "contact_name": str(
                    (transaction.get("Contact") or {}).get("Name")
                    if isinstance(transaction.get("Contact"), dict)
                    else ""
                ).strip(),
                "raw_cents": raw_cents,
            }
            if transaction_id in excluded_transfer_transaction_ids:
                excluded_transfer_lines.append(source_line)
                continue
            if not (event_name or project_name):
                continue
            if transaction_id in stripe_transaction_ids:
                stripe_lines.append(
                    source_line
                )
                excluded_transfer_lines.append(source_line)
                continue
            cents = abs(raw_cents)
            tracked_lines.append(
                {
                    **source_line,
                    "signed_cents": cents if transaction_type == "RECEIVE" else -cents,
                }
            )

    matched_line_ids: set[str] = set()
    rows: list[dict[str, Any]] = []
    for revenue in event_revenue:
        event_key = str(revenue.get("event_name") or "").strip().casefold()
        project_key = str(revenue.get("project_name") or "").strip().casefold()
        matching_lines = [
            line
            for line in tracked_lines
            if (
                event_key
                and str(line["event_name"]).strip().casefold() == event_key
            )
            or (
                project_key
                and str(line["project_name"]).strip().casefold() == project_key
            )
        ]
        matching_stripe_lines = [
            line
            for line in stripe_lines
            if (
                event_key
                and str(line["event_name"]).strip().casefold() == event_key
            )
            or (
                project_key
                and str(line["project_name"]).strip().casefold() == project_key
            )
        ]
        matched_line_ids.update(line["line_id"] for line in matching_lines)
        xero_other_income_cents = sum(
            int(line["signed_cents"])
            for line in matching_lines
            if int(line["signed_cents"]) > 0
        )
        xero_cost_cents = sum(
            -int(line["signed_cents"])
            for line in matching_lines
            if int(line["signed_cents"]) < 0
        )
        stripe_net_cents = int(revenue.get("net_cash_contribution_cents") or 0)
        fee_account_code = str(profile.fee_account_code or "").strip().casefold()
        xero_current_stripe_gross_cents = sum(
            int(line["raw_cents"])
            for line in matching_stripe_lines
            if str(line["account_code"]).strip().casefold() != fee_account_code
            and int(line["raw_cents"]) > 0
        )
        xero_current_stripe_refunds_cents = sum(
            int(line["raw_cents"])
            for line in matching_stripe_lines
            if str(line["account_code"]).strip().casefold() != fee_account_code
            and int(line["raw_cents"]) < 0
        )
        xero_current_stripe_fee_cents = sum(
            -int(line["raw_cents"])
            for line in matching_stripe_lines
            if str(line["account_code"]).strip().casefold() == fee_account_code
            and int(line["raw_cents"]) < 0
        )
        current_stripe_net_cents = (
            xero_current_stripe_gross_cents
            + xero_current_stripe_refunds_cents
            - xero_current_stripe_fee_cents
        )
        desired_components = (
            int(revenue.get("gross_cents") or 0),
            int(revenue.get("refunds_cents") or 0),
            int(revenue.get("stripe_fee_cents") or 0),
        )
        current_components = (
            xero_current_stripe_gross_cents,
            xero_current_stripe_refunds_cents,
            xero_current_stripe_fee_cents,
        )
        if current_components == desired_components:
            xero_stripe_coding_status = "correct"
        elif not any(current_components) and any(desired_components):
            xero_stripe_coding_status = "missing_tracking_or_split"
        else:
            xero_stripe_coding_status = "mismatch"
        estimated_cashflow_cents = (
            stripe_net_cents + xero_other_income_cents - xero_cost_cents
        )
        flags: list[str] = []
        if revenue.get("mapping_status") == "missing":
            flags.append("missing_reconciliation_mapping")
        if (
            revenue.get("source_type") == ReconciliationMapping.SOURCE_LUMA_EVENT
            and revenue.get("luma_registration_count") is None
        ):
            flags.append("luma_attendance_not_synced")
        if (
            revenue.get("luma_registration_count") is not None
            and int(revenue.get("stripe_charge_count") or 0)
            > int(revenue.get("luma_registration_count") or 0)
        ):
            flags.append("stripe_charges_exceed_luma_registrations")
        if int(revenue.get("net_revenue_before_fees_cents") or 0) < 0:
            flags.append("refunds_exceed_gross_revenue")
        if not xero_cost_cents:
            flags.append("no_xero_costs_recorded")
        if xero_stripe_coding_status != "correct":
            flags.append("xero_stripe_coding_incomplete")
        if estimated_cashflow_cents < 0:
            flags.append("negative_cashflow")

        if revenue.get("mapping_status") == "missing":
            status = "mapping_required"
        elif estimated_cashflow_cents < 0:
            status = "negative"
        elif estimated_cashflow_cents == 0:
            status = "break_even"
        else:
            status = "positive"
        rows.append(
            {
                **revenue,
                "xero_other_income_cents": xero_other_income_cents,
                "xero_cost_cents": xero_cost_cents,
                "xero_current_stripe_gross_cents": xero_current_stripe_gross_cents,
                "xero_current_stripe_refunds_cents": xero_current_stripe_refunds_cents,
                "xero_current_stripe_fee_cents": xero_current_stripe_fee_cents,
                "xero_current_stripe_net_cents": current_stripe_net_cents,
                "xero_stripe_variance_cents": (
                    current_stripe_net_cents - stripe_net_cents
                ),
                "xero_stripe_coding_status": xero_stripe_coding_status,
                "estimated_cashflow_cents": estimated_cashflow_cents,
                "profitability_status": status,
                "validation_flags": flags,
                "matched_xero_line_count": len(matching_lines),
                "xero_lines": matching_lines,
            }
        )

    unmatched: dict[tuple[str, str], dict[str, Any]] = {}
    for line in tracked_lines:
        if line["line_id"] in matched_line_ids:
            continue
        key = (
            str(line["event_name"]).strip(),
            str(line["project_name"]).strip(),
        )
        row = unmatched.setdefault(
            key,
            {
                "event_name": key[0],
                "project_name": key[1],
                "xero_other_income_cents": 0,
                "xero_cost_cents": 0,
                "matched_xero_line_count": 0,
                "validation_flag": "xero_tracking_without_stripe_revenue",
                "xero_lines": [],
            },
        )
        signed_cents = int(line["signed_cents"])
        if signed_cents > 0:
            row["xero_other_income_cents"] += signed_cents
        else:
            row["xero_cost_cents"] -= signed_cents
        row["matched_xero_line_count"] += 1
        row["xero_lines"].append(line)

    status_counts = Counter(row["profitability_status"] for row in rows)
    return {
        "basis": (
            "Stripe gross revenue, refunds, and fees plus non-Stripe Xero "
            "Receive/Spend Money lines carrying Event Name or Project Name tracking."
        ),
        "period_start": period_start.isoformat() if period_start else None,
        "period_end": period_end.isoformat() if period_end else None,
        "limitations": [
            "This is a cashflow validation, not an accrual P&L.",
            "Bills and journals that do not appear as Xero bank transactions are not included.",
            "Untracked Xero lines cannot be assigned to an event or project.",
        ],
        "event_count": len(rows),
        "status_counts": dict(sorted(status_counts.items())),
        "negative_count": status_counts.get("negative", 0),
        "mapping_required_count": status_counts.get("mapping_required", 0),
        "xero_stripe_coding_incomplete_count": sum(
            1
            for row in rows
            if row["xero_stripe_coding_status"] != "correct"
        ),
        "rows": rows,
        "excluded_payout_transfer_lines": excluded_transfer_lines,
        "unmatched_xero_tracking": sorted(
            unmatched.values(),
            key=lambda item: (
                item["event_name"] or item["project_name"]
            ).casefold(),
        ),
    }


def build_xero_correction_batch(
    records: list[StripePayoutReconciliation],
    *,
    cashflow_period_start: date | None = None,
    cashflow_period_end: date | None = None,
) -> dict[str, Any]:
    if not records:
        return {
            "payout_count": 0,
            "classification_counts": {},
            "payouts": [],
            "event_revenue": [],
            "event_cashflow_validation": {
                "period_start": (
                    cashflow_period_start.isoformat()
                    if cashflow_period_start
                    else None
                ),
                "period_end": (
                    cashflow_period_end.isoformat()
                    if cashflow_period_end
                    else None
                ),
                "event_count": 0,
                "status_counts": {},
                "negative_count": 0,
                "mapping_required_count": 0,
                "xero_stripe_coding_incomplete_count": 0,
                "rows": [],
                "unmatched_xero_tracking": [],
            },
        }
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=records[0].organization
    )
    bank_transactions = fetch_xero_bank_transactions(profile)
    previews = [
        build_xero_correction_preview(
            record,
            bank_transactions=bank_transactions,
        )
        for record in records
    ]
    counts = Counter(item["classification"] for item in previews)
    event_revenue = build_event_revenue_rollup(records)
    return {
        "payout_count": len(records),
        "xero_bank_transaction_count": len(bank_transactions),
        "classification_counts": dict(sorted(counts.items())),
        "automatic_create_count": sum(
            1 for item in previews if item["automatic_action_allowed"]
        ),
        "manual_unreconcile_count": sum(
            1 for item in previews if item["requires_manual_unreconcile"]
        ),
        "payouts": previews,
        "event_revenue": event_revenue,
        "event_cashflow_validation": build_event_cashflow_validation(
            event_revenue=event_revenue,
            bank_transactions=bank_transactions,
            payout_previews=previews,
            profile=profile,
            period_start=cashflow_period_start,
            period_end=cashflow_period_end,
        ),
    }


def _tracking_category_options(categories: list[dict[str, Any]], category_id: str) -> list[dict[str, Any]]:
    for category in categories:
        if str(category.get("TrackingCategoryID") or "") == category_id:
            return [item for item in category.get("Options") or [] if isinstance(item, dict)]
    return []


def _created_tracking_option(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    options = payload.get("Options")
    if isinstance(options, list) and options and isinstance(options[0], dict):
        return options[0]
    for category in payload.get("TrackingCategories") or []:
        if isinstance(category, dict):
            nested = category.get("Options") or []
            if nested and isinstance(nested[0], dict):
                return nested[0]
    return {}


def _configured_tracking_category(
    categories: list[dict[str, Any]],
    *,
    category_id: str,
    category_name: str,
) -> dict[str, Any]:
    """Return one exact active configured category or fail closed."""

    category_id = str(category_id or "").strip()
    category_name = str(category_name or "").strip()
    if not category_id or not category_name:
        raise XeroPostingError(
            "Configure both the Xero tracking category ID and name before posting."
        )
    matches = [
        item
        for item in categories
        if str(item.get("TrackingCategoryID") or "").strip() == category_id
    ]
    if len(matches) != 1:
        raise XeroPostingError(
            f"Xero does not contain one exact tracking category with ID {category_id}."
        )
    category = matches[0]
    observed_name = str(category.get("Name") or "").strip()
    if observed_name.casefold() != category_name.casefold():
        raise XeroPostingError(
            f"Xero tracking category {category_id} is named {observed_name or '(blank)'}, "
            f"not {category_name}."
        )
    if str(category.get("Status") or "").strip().upper() != "ACTIVE":
        raise XeroPostingError(f"Xero tracking category {category_name} is archived.")
    _require_unique_tracking_option_ids(category)
    return category


def _require_unique_tracking_option_ids(category: dict[str, Any]) -> None:
    """Reject a malformed catalogue before resolving an option by name or ID."""

    seen: dict[str, str] = {}
    for option in category.get("Options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(
            option.get("TrackingOptionID") or option.get("OptionID") or ""
        ).strip()
        if not option_id:
            continue
        option_name = str(option.get("Name") or "").strip()
        if option_id in seen:
            raise XeroPostingError(
                f"Xero returned more than one tracking option with ID {option_id} "
                f"({seen[option_id] or '(blank)'} and {option_name or '(blank)'})."
            )
        seen[option_id] = option_name


def _active_tracking_option_by_name(
    category: dict[str, Any],
    *,
    option_name: str,
) -> dict[str, Any] | None:
    option_name = str(option_name or "").strip()
    matches = [
        item
        for item in category.get("Options") or []
        if isinstance(item, dict)
        and str(item.get("Name") or "").strip().casefold() == option_name.casefold()
        and str(item.get("Status") or "").strip().upper() == "ACTIVE"
    ]
    if len(matches) > 1:
        raise XeroPostingError(
            f"Xero contains more than one active tracking option named {option_name}."
        )
    return matches[0] if matches else None


def _tracking_option_by_id(
    category: dict[str, Any],
    *,
    option_id: str,
    option_name: str,
) -> dict[str, Any] | None:
    matches = [
        item
        for item in category.get("Options") or []
        if isinstance(item, dict)
        and str(item.get("TrackingOptionID") or item.get("OptionID") or "").strip()
        == option_id
    ]
    if not matches:
        return None
    if len(matches) != 1:
        raise XeroPostingError(
            f"Xero returned more than one tracking option with ID {option_id}."
        )
    option = matches[0]
    observed_name = str(option.get("Name") or "").strip()
    if observed_name.casefold() != option_name.casefold():
        raise XeroPostingError(
            f"Xero tracking option {option_id} is named {observed_name or '(blank)'}, "
            f"not {option_name}."
        )
    if str(option.get("Status") or "").strip().upper() != "ACTIVE":
        raise XeroPostingError(f"Xero tracking option {option_name} is archived.")
    return option


def _current_event_catalog_names(
    *,
    organization,
    source_type: str,
    source_id: str,
) -> set[str]:
    """Return names from rows that are currently eligible for reconciliation."""

    from startup_updates.models import LumaEventSelection

    if source_type == "luma":
        return set(
            LumaEventSelection.objects.filter(
                organization=organization,
                event_id=source_id,
                selected=True,
            ).values_list("event_name", flat=True)
        )
    if source_type == "humanitix":
        return set(
            HumanitixEvent.objects.filter(
                organization=organization,
                external_event_id=source_id,
                archived=False,
            ).values_list("event_name", flat=True)
        )
    raise XeroPostingError(
        f"Unsupported canonical event source {source_type!r}; refresh and review the suggestion."
    )


def _current_linear_project_catalog_names(
    *,
    organization,
    source_id: str,
) -> set[str]:
    """Return the current selected Linear name(s), preferring synced artifacts."""

    from startup_updates.models import LinearProjectArtifact, LinearProjectSelection

    selections = list(
        LinearProjectSelection.objects.filter(
            organization=organization,
            linear_project_id=source_id,
            selected=True,
        ).only("connection_id", "project_name")
    )
    if not selections:
        return set()
    artifacts = {
        item.connection_id: item.name
        for item in LinearProjectArtifact.objects.filter(
            organization=organization,
            linear_project_id=source_id,
            connection_id__in={item.connection_id for item in selections},
        ).only("connection_id", "name")
    }
    return {
        str(artifacts.get(item.connection_id) or item.project_name or "").strip()
        for item in selections
        if str(artifacts.get(item.connection_id) or item.project_name or "").strip()
    }


def _require_current_stripe_catalog_name(
    *,
    organization,
    dimension: str,
    source_type: str,
    source_id: str,
    option_name: str,
) -> None:
    """Re-read the canonical source immediately before a Stripe option write."""

    if dimension == "event":
        names = _current_event_catalog_names(
            organization=organization,
            source_type=source_type,
            source_id=source_id,
        )
    elif dimension == "project" and source_type == "linear":
        names = _current_linear_project_catalog_names(
            organization=organization,
            source_id=source_id,
        )
    else:
        raise XeroPostingError(
            f"Unsupported canonical {dimension} source {source_type!r}; "
            "refresh and review the Stripe mapping."
        )
    normalized_names = {
        str(name or "").strip().casefold() for name in names if str(name or "").strip()
    }
    expected_name = option_name.strip().casefold()
    if not normalized_names:
        raise XeroPostingError(
            f"The canonical {source_type} {dimension} {source_id} no longer exists."
        )
    if normalized_names != {expected_name}:
        raise XeroPostingError(
            f"The canonical {source_type} {dimension} {source_id} name changed or is "
            "ambiguous; refresh and review the Stripe mapping."
        )


def _validate_approved_stripe_tracking_creation(
    record: StripePayoutReconciliation,
    *,
    profile: ReconciliationProfile,
    mapping: ReconciliationMapping,
    spec: dict[str, str],
) -> None:
    """Bind a missing option to an exact current approved payout suggestion."""

    suggestion = ReconciliationSuggestion.objects.filter(
        organization=record.organization,
        payout=record,
        source_type=mapping.source_type,
        source_id=mapping.source_id,
        status=ReconciliationSuggestion.STATUS_APPROVED,
        source_hash=record.source_hash,
    ).order_by("-reviewed_at", "-id").first()
    option_name = spec["option_name"]
    matches_current = False
    if suggestion is not None:
        if spec["dimension"] == "event":
            matches_current = bool(
                suggestion.allocation_mode == ReconciliationSuggestion.ALLOCATION_EVENT
                and suggestion.event_source_id
                and suggestion.event_tracking_option_name.strip().casefold()
                == option_name.casefold()
            )
        elif (
            suggestion.allocation_mode == ReconciliationSuggestion.ALLOCATION_MLAI_CORE
            and option_name.casefold()
            == str(profile.default_project_tracking_option_name or "").strip().casefold()
        ):
            matches_current = True
        elif (
            suggestion.allocation_mode == ReconciliationSuggestion.ALLOCATION_PROJECT
            and suggestion.project_source_id
            and suggestion.project_source_type == mapping.project_source_type
            and suggestion.project_source_id == mapping.project_source_id
            and suggestion.project_tracking_option_name.strip().casefold()
            == option_name.casefold()
        ):
            matches_current = True
    if not matches_current or suggestion is None:
        raise XeroPostingError(
            f"Missing tracking option {option_name} is not bound to a current approved "
            f"suggestion for {mapping.source_type}:{mapping.source_id}."
        )
    if suggestion.allocation_mode == ReconciliationSuggestion.ALLOCATION_MLAI_CORE:
        return
    source_type = (
        suggestion.event_source_type
        if spec["dimension"] == "event"
        else suggestion.project_source_type
    )
    source_id = (
        suggestion.event_source_id
        if spec["dimension"] == "event"
        else suggestion.project_source_id
    )
    if spec["dimension"] == "project" and source_type == "xero_tracking":
        raise XeroPostingError(
            "A Project sourced from Xero must retain its existing tracking option ID."
        )
    _require_current_stripe_catalog_name(
        organization=record.organization,
        dimension=spec["dimension"],
        source_type=str(source_type or "").strip().casefold(),
        source_id=str(source_id or "").strip(),
        option_name=option_name,
    )


def ensure_xero_tracking_options(
    record: StripePayoutReconciliation,
    *,
    profile: ReconciliationProfile | None = None,
) -> list[ReconciliationMapping]:
    """Resolve/create approved Event and Project tracking options in Xero.

    This is intentionally called only from the explicit posting operation.  A
    monthly-update agent can propose a name, but it cannot mutate Xero merely by
    generating or approving contextual suggestions.
    """
    profile = profile or ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=record.organization
    )
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")

    report = record.report_payload or {}
    source_keys: set[tuple[str, str]] = set()
    for group in [*(report.get("revenue_groups") or report.get("events") or []), *(report.get("refunds") or [])]:
        if not isinstance(group, dict):
            continue
        source_type = str(group.get("source_type") or "luma_event")
        source_id = str(group.get("source_id") or group.get("event_api_id") or group.get("id") or "")
        if source_id:
            source_keys.add((source_type, source_id))
    mappings = list(
        ReconciliationMapping.objects.filter(
            organization=record.organization,
            active=True,
            source_type__in={item[0] for item in source_keys},
            source_id__in={item[1] for item in source_keys},
        )
    )
    mappings = [mapping for mapping in mappings if (mapping.source_type, mapping.source_id) in source_keys]
    tracked: list[tuple[ReconciliationMapping, dict[str, str]]] = []
    for mapping in mappings:
        spec = _mapping_tracking_spec(profile, mapping)
        if spec is None:
            if profile.require_statement_tracking:
                raise ReconciliationValidationError(
                    f"Mapping {mapping.source_type}:{mapping.source_id} requires exactly "
                    "one Event, Project, or MLAI core allocation."
                )
            continue
        tracked.append((mapping, spec))

    standalone_fee = int(report.get("standalone_fee_cents") or 0)
    standalone_spec: dict[str, str] | None = None
    if standalone_fee and (
        profile.standalone_fee_project_option_id
        or profile.standalone_fee_project_option_name
    ) and not profile.standalone_fee_project_option_id:
        standalone_spec = {
            "dimension": "project",
            "category_id": str(profile.project_tracking_category_id or "").strip(),
            "category_name": str(profile.project_tracking_category_name or "").strip(),
            "option_id": str(profile.standalone_fee_project_option_id or "").strip(),
            "option_name": str(profile.standalone_fee_project_option_name or "").strip(),
            "id_field": "standalone_fee_project_option_id",
        }
        if not all(
            standalone_spec[field]
            for field in ("category_id", "category_name", "option_name")
        ):
            raise ReconciliationValidationError(
                "Standalone Stripe fee tracking metadata is incomplete."
            )
    if not tracked and standalone_spec is None:
        return mappings

    headers = _xero_headers(connection)
    response = http_client.get(f"{XERO_API_URL}/TrackingCategories", headers=headers, timeout=(3, 30))
    response.raise_for_status()
    payload = response.json()
    categories = payload.get("TrackingCategories") if isinstance(payload, dict) else []
    categories = [item for item in categories or [] if isinstance(item, dict)]
    entries: list[
        tuple[ReconciliationMapping | None, dict[str, str]]
    ] = [*tracked]
    if standalone_spec is not None:
        entries.append((None, standalone_spec))
    for mapping, spec in entries:
        category = _configured_tracking_category(
            categories,
            category_id=spec["category_id"],
            category_name=spec["category_name"],
        )
        option: dict[str, Any] | None
        if spec["option_id"]:
            option = _tracking_option_by_id(
                category,
                option_id=spec["option_id"],
                option_name=spec["option_name"],
            )
            if option is None:
                raise XeroPostingError(
                    f"Xero tracking option ID {spec['option_id']} no longer exists in "
                    f"{spec['category_name']}; refresh and review the mapping."
                )
        else:
            option = _active_tracking_option_by_name(
                category,
                option_name=spec["option_name"],
            )
        if option is None:
            if mapping is not None:
                _validate_approved_stripe_tracking_creation(
                    record,
                    profile=profile,
                    mapping=mapping,
                    spec=spec,
                )
            elif spec["option_name"].casefold() != str(
                profile.default_project_tracking_option_name or ""
            ).strip().casefold():
                raise XeroPostingError(
                    "A missing standalone Stripe fee option may only use the explicitly "
                    "configured MLAI core default."
                )
            if not xero_has_settings_write_scope(connection.scopes):
                raise ReconciliationValidationError(
                    "Reconnect Xero with accounting.settings before posting missing "
                    "Event Name or Project Name options."
                )
            create_response = http_client.put(
                f"{XERO_API_URL}/TrackingCategories/{spec['category_id']}/Options",
                headers=headers,
                json={"Options": [{"Name": spec["option_name"]}]},
                timeout=(3, 30),
            )
            create_response.raise_for_status()
            option = _created_tracking_option(create_response.json())
            created_name = str(option.get("Name") or "").strip()
            if created_name.casefold() != spec["option_name"].casefold():
                raise XeroPostingError(
                    f"Xero returned a different tracking option after creating "
                    f"{spec['option_name']}."
                )
            if str(option.get("Status") or "").strip().upper() != "ACTIVE":
                raise XeroPostingError(
                    f"Xero returned an archived tracking option for {spec['option_name']}."
                )
            category.setdefault("Options", []).append(option)
            _require_unique_tracking_option_ids(category)
        option_id = str(option.get("TrackingOptionID") or option.get("OptionID") or "").strip()
        if not option_id:
            raise XeroPostingError(
                f"Xero did not return an ID for tracking option {spec['option_name']}."
            )
        if mapping is not None and getattr(mapping, spec["id_field"]) != option_id:
            setattr(mapping, spec["id_field"], option_id)
            mapping.save(update_fields=[spec["id_field"], "updated_at"])
        elif mapping is None and profile.standalone_fee_project_option_id != option_id:
            profile.standalone_fee_project_option_id = option_id
            profile.save(
                update_fields=["standalone_fee_project_option_id", "updated_at"]
            )
    return mappings


def post_xero_bank_transaction(
    record: StripePayoutReconciliation,
    *,
    approved_by_slack_id: str,
    expected_payload_hash: str = "",
    statement_line_id: str,
    bank_account_id: str,
    statement_source_hash: str,
) -> StripePayoutReconciliation:
    binding = _stripe_statement_binding_values(
        statement_line_id=statement_line_id,
        bank_account_id=bank_account_id,
        statement_source_hash=statement_source_hash,
    )
    current = StripePayoutReconciliation.objects.get(pk=record.pk)
    if current.xero_bank_transaction_id:
        stored_binding = (
            current.preview_payload.get("statement_binding")
            if isinstance(current.preview_payload, dict)
            else None
        )
        if stored_binding != binding:
            raise ReconciliationValidationError(
                "The posted Stripe payout cannot be recovered from a different statement binding.",
                errors=[
                    "The stored posted payout binding does not match the requested statement row, account, and source hash."
                ],
            )
        return current
    from integrations.services.xero_statement_reconciliation import (
        select_current_statement_capture,
    )

    statement_capture_selection = select_current_statement_capture(record.organization)
    bound_validation = {
        **binding,
        "statement_capture_selection": statement_capture_selection,
    }
    preview = build_xero_preview(record, **bound_validation)
    if not preview["ready"]:
        raise ReconciliationValidationError("Payout is not ready to post.", errors=preview["errors"])
    if expected_payload_hash and preview["payload_hash"] != expected_payload_hash:
        raise ReconciliationValidationError(
            "Payout preview changed after review; fetch and approve a new preview.",
            errors=["The reviewed payout payload hash is stale."],
        )
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(organization=record.organization)
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    _stripe_statement_binding(record, **bound_validation)
    ensure_xero_tracking_options(record, profile=profile)
    # Rebuild so the reviewed payload contains the resolved Xero option IDs.
    preview = build_xero_preview(record, **bound_validation)
    if expected_payload_hash and preview["payload_hash"] != expected_payload_hash:
        raise ReconciliationValidationError(
            "Payout preview changed after resolving Xero tracking options; "
            "fetch and approve the new preview before posting.",
            errors=["tracking_options_resolved_repreview_required"],
        )

    with transaction.atomic():
        locked = StripePayoutReconciliation.objects.select_for_update().get(pk=record.pk)
        if locked.xero_bank_transaction_id:
            return locked
        if locked.status == StripePayoutReconciliation.STATUS_POSTING:
            raise ReconciliationValidationError("This payout is already being posted.")
        locked.status = StripePayoutReconciliation.STATUS_POSTING
        locked.approved_by_slack_id = approved_by_slack_id
        locked.approved_at = timezone.now()
        locked.last_error = ""
        locked.save(update_fields=["status", "approved_by_slack_id", "approved_at", "last_error", "updated_at"])

    try:
        headers = _xero_headers(connection)
        headers["Idempotency-Key"] = f"stripe-payout-{record.payout_id}"
        where = f'Reference=="{record.payout_id.replace(chr(34), chr(34) * 2)}"'
        existing_response = http_client.get(
            f"{XERO_API_URL}/BankTransactions",
            headers=headers,
            params={"where": where},
            timeout=(3, 30),
        )
        existing_response.raise_for_status()
        existing = existing_response.json().get("BankTransactions", [])
        bank_transaction = next(
            (
                item
                for item in existing
                if str(item.get("Status") or "").strip().upper() != "DELETED"
            ),
            None,
        )
        if bank_transaction is not None:
            differences = _line_item_differences(
                bank_transaction,
                preview["xero_payload"],
            )
            if not differences["line_items_match"]:
                raise ReconciliationValidationError(
                    "A Xero transaction already exists for this payout, but its "
                    "account, tax, tracking, or split lines do not match Stripe. "
                    "Run the payout correction preview; do not create another transaction.",
                    errors=[
                        "Existing Xero transaction requires correction before this "
                        "payout can be marked posted."
                    ],
                )
        if bank_transaction is None:
            # Refresh the live account catalog and authoritative all-account
            # capture at the last possible boundary before the single write.
            fresh_statement_capture_selection = select_current_statement_capture(
                record.organization
            )
            _stripe_statement_binding(
                record,
                **binding,
                statement_capture_selection=fresh_statement_capture_selection,
            )
            response = http_client.put(
                f"{XERO_API_URL}/BankTransactions",
                headers=headers,
                json={"BankTransactions": [preview["xero_payload"]]},
                timeout=(3, 30),
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("BankTransactions") if isinstance(payload, dict) else None
            bank_transaction = rows[0] if isinstance(rows, list) and rows else {}
            validation_errors = bank_transaction.get("ValidationErrors") or []
            if bank_transaction.get("HasErrors") or validation_errors:
                messages = [str(item.get("Message") or item) for item in validation_errors]
                raise XeroPostingError("; ".join(messages) or "Xero rejected the bank transaction.")
        transaction_id = str(bank_transaction.get("BankTransactionID") or "").strip()
        if not transaction_id:
            raise XeroPostingError("Xero did not return a BankTransactionID.")
    except Exception as exc:
        StripePayoutReconciliation.objects.filter(pk=record.pk).update(
            status=StripePayoutReconciliation.STATUS_FAILED,
            last_error=str(exc)[:2000],
            updated_at=timezone.now(),
        )
        if isinstance(exc, (ReconciliationValidationError, XeroPostingError)):
            raise
        raise XeroPostingError("Unable to create the Xero bank transaction.") from exc

    StripePayoutReconciliation.objects.filter(pk=record.pk).update(
        status=StripePayoutReconciliation.STATUS_POSTED,
        xero_bank_transaction_id=transaction_id,
        posted_at=timezone.now(),
        last_error="",
        updated_at=timezone.now(),
    )
    return StripePayoutReconciliation.objects.get(pk=record.pk)


def run_daily_payout_reconciliation(*, now=None) -> dict[str, Any]:
    """Self-throttled scheduler hook; only refreshes ledgers, never posts Xero."""
    if not getattr(settings, "RECONCILIATION_SCHEDULER_ENABLED", True):
        return {"status": "skipped", "reason": "disabled"}
    now = now or timezone.now()
    domain = str(getattr(settings, "RECONCILIATION_DEFAULT_DOMAIN", "mlai.au") or "").strip()
    organization = Organization.objects.filter(domain__iexact=domain).first()
    if organization is None:
        return {"status": "skipped", "reason": "organization_not_found"}
    profile = ReconciliationProfile.objects.filter(organization=organization, enabled=True).first()
    if profile is None:
        return {"status": "skipped", "reason": "profile_not_configured"}
    marker = f"stripe-payout-reconciliation:{domain}:{timezone.localtime(now).date().isoformat()}"
    if not cache.add(marker, "running", timeout=36 * 60 * 60):
        return {"status": "skipped", "reason": "already_run_today"}

    # Imported lazily so reconciliation model/service imports stay acyclic.
    from integrations.services.reconciliation import ReconciliationReportService

    try:
        report = ReconciliationReportService().build_report(
            since=now - timedelta(days=7),
            until=now,
            include_workbook=False,
        )
        records = persist_report(
            organization=organization,
            report=report,
            stripe_account_id=profile.stripe_account_id,
        )
    except Exception:
        # Permit a transient Stripe failure to retry later the same day without
        # hammering the API on every one-minute scheduler tick.
        cache.set(marker, "failed", timeout=30 * 60)
        raise
    return {"status": "completed", "payouts": len(records)}
