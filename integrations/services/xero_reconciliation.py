"""Durable Stripe payout ledger and explicit Xero posting workflow."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
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
    ReconciliationMapping,
    ReconciliationProfile,
    StripePayoutReconciliation,
)
from integrations.services.external_connectors import _xero_required_token
from integrations.services.xero_scopes import normalize_xero_scopes
from organizations.models import Organization


XERO_API_URL = "https://api.xero.com/api.xro/2.0"
XERO_BANK_TRANSACTION_SCOPE = "accounting.banktransactions"
XERO_LEGACY_TRANSACTION_SCOPE = "accounting.transactions"


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
    return {
        "organization_id": profile.organization_id,
        "xero_connection_id": profile.xero_connection_id,
        "stripe_account_id": profile.stripe_account_id,
        "xero_bank_account_id": profile.xero_bank_account_id,
        "xero_bank_account_name": profile.xero_bank_account_name,
        "xero_contact_id": profile.xero_contact_id,
        "xero_contact_name": profile.xero_contact_name,
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
        "standalone_fee_project_option_id": profile.standalone_fee_project_option_id,
        "standalone_fee_project_option_name": profile.standalone_fee_project_option_name,
        "enabled": profile.enabled,
        "xero_write_scope": bool(profile.xero_connection and xero_has_bank_transaction_scope(profile.xero_connection.scopes)),
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
    if include_payload:
        result["report"] = record.report_payload
        result["preview"] = record.preview_payload
    return result


def _tracking(profile: ReconciliationProfile, mapping: ReconciliationMapping) -> list[dict[str, str]]:
    values: list[dict[str, str]] = []
    if mapping.event_tracking_option_id or mapping.event_tracking_option_name:
        item = {
            "TrackingCategoryID": profile.event_tracking_category_id,
            "Name": profile.event_tracking_category_name,
            "TrackingOptionID": mapping.event_tracking_option_id,
            "Option": mapping.event_tracking_option_name,
        }
        values.append({key: value for key, value in item.items() if value})
    if mapping.project_tracking_option_id or mapping.project_tracking_option_name:
        item = {
            "TrackingCategoryID": profile.project_tracking_category_id,
            "Name": profile.project_tracking_category_name,
            "TrackingOptionID": mapping.project_tracking_option_id,
            "Option": mapping.project_tracking_option_name,
        }
        values.append({key: value for key, value in item.items() if value})
    return values


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


def build_xero_preview(record: StripePayoutReconciliation) -> dict[str, Any]:
    try:
        profile = ReconciliationProfile.objects.select_related("xero_connection").get(
            organization=record.organization
        )
    except ReconciliationProfile.DoesNotExist:
        raise ReconciliationValidationError("Reconciliation profile is not configured.")

    errors: list[str] = []
    connection = profile.xero_connection
    if not profile.enabled:
        errors.append("Reconciliation is disabled for this organisation.")
    if connection is None:
        errors.append("A Xero connection must be selected.")
    elif not xero_has_bank_transaction_scope(connection.scopes):
        errors.append("Reconnect Xero with the accounting.banktransactions scope before posting.")
    for label, value in (
        ("Xero bank account", profile.xero_bank_account_id),
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
        tracking = _tracking(profile, mapping)
        gross = int(group.get("gross_cents") or 0)
        group_fee = int(group.get("stripe_fee_cents") or 0)
        treatment_label = "clearing" if clearing else "revenue"
        lines.append(_xero_line(description=f"Stripe {treatment_label} — {group.get('source_label') or group.get('event_name')}", cents=gross, account_code=mapping.account_code or profile.revenue_account_code, tax_type=mapping.tax_type or profile.revenue_tax_type, tracking=tracking))
        line_total_cents += gross
        if group_fee:
            lines.append(_xero_line(description=f"Stripe processing fees — {group.get('source_label') or group.get('event_name')}", cents=-group_fee, account_code=profile.fee_account_code, tax_type=profile.fee_tax_type, tracking=tracking))
            line_total_cents -= group_fee

    standalone_fee = int(report.get("standalone_fee_cents") or 0)
    if standalone_fee:
        if not (profile.standalone_fee_project_option_id or profile.standalone_fee_project_option_name):
            errors.append("Configure a Project Name tracking option for standalone Stripe fees.")
        fee_tracking_item = {
            "TrackingCategoryID": profile.project_tracking_category_id,
            "Name": profile.project_tracking_category_name,
            "TrackingOptionID": profile.standalone_fee_project_option_id,
            "Option": profile.standalone_fee_project_option_name,
        }
        fee_tracking = [{key: value for key, value in fee_tracking_item.items() if value}]
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
        cents = int(adjustment.get("net_cents") or 0)
        lines.append(_xero_line(description=f"Stripe refund/adjustment — {adjustment.get('source_label') or adjustment.get('description') or adjustment.get('id')}", cents=cents, account_code=mapping.account_code if clearing else (mapping.account_code or profile.refund_account_code), tax_type=mapping.tax_type if clearing else (mapping.tax_type or profile.refund_tax_type), tracking=_tracking(profile, mapping)))
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
        "BankAccount": {"AccountID": profile.xero_bank_account_id},
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
        "human_reconciliation_required": True,
        "note": "Posting creates a matching Receive Money transaction; a human must still click Match/OK on the Xero bank statement line.",
    }
    record.preview_payload = preview
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


def post_xero_bank_transaction(record: StripePayoutReconciliation, *, approved_by_slack_id: str) -> StripePayoutReconciliation:
    current = StripePayoutReconciliation.objects.get(pk=record.pk)
    if current.xero_bank_transaction_id:
        return current
    preview = build_xero_preview(record)
    if not preview["ready"]:
        raise ReconciliationValidationError("Payout is not ready to post.", errors=preview["errors"])
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(organization=record.organization)
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")

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
        bank_transaction = existing[0] if existing else None
        if bank_transaction is None:
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
