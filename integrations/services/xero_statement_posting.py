"""API-first Xero writes for evidence-backed statement suggestions."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from integrations import http_client
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceProvider,
    ReconciliationDecision,
    ReconciliationProfile,
    XeroStatementLineSnapshot,
    XeroStatementPosting,
    XeroStatementSuggestion,
)
from integrations.services.xero_reconciliation import (
    XERO_API_URL,
    ReconciliationValidationError,
    XeroPostingError,
    _active_tracking_option_by_name,
    _configured_tracking_category,
    _created_tracking_option,
    _current_event_catalog_names,
    _current_linear_project_catalog_names,
    _tracking_option_by_id,
    _xero_headers,
    xero_has_bank_transaction_scope,
    xero_has_settings_write_scope,
)
from integrations.services.xero_scopes import xero_has_payment_write_scope
from integrations.services.xero_statement_reconciliation import (
    StatementCaptureSelection,
    StatementCaptureValidationError,
    csv_statement_duplicate_count,
    normalize_statement_action,
    validate_current_statement_line_capture,
)
from integrations.services.reconciliation_rules import record_reconciliation_decision
from integrations.services.reconciliation_tracking import (
    effective_tracking,
    effective_tracking_errors,
    xero_tracking_entry,
)
from integrations.services.reconciliation_bank_accounts import active_bank_account


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_payload(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _reference(line: XeroStatementLineSnapshot) -> str:
    return f"MLAI-STMT-{line.source_hash[:20]}"


def _as_money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00")


def _exact_outstanding_bills(line: XeroStatementLineSnapshot):
    query = ExternalFinancialRecord.objects.filter(
        organization=line.organization,
        provider=ExternalServiceProvider.XERO,
        record_type=ExternalFinancialRecord.RECORD_XERO_BILL,
        amount=line.amount,
    ).exclude(status__in=["DELETED", "VOIDED", "PAID"])
    if line.currency:
        query = query.filter(currency__iexact=line.currency)
    return query.order_by("transaction_date", "id")


def _operation_for(suggestion: XeroStatementSuggestion) -> str:
    action = normalize_statement_action(suggestion.proposed_action)
    if action == XeroStatementSuggestion.ACTION_CREATE_BANK_TRANSACTION:
        return XeroStatementPosting.OPERATION_BANK_TRANSACTION
    if action == XeroStatementSuggestion.ACTION_PAY_EXISTING_BILL:
        return XeroStatementPosting.OPERATION_BILL_PAYMENT
    return ""


def _has_dimension_scores(suggestion: XeroStatementSuggestion) -> bool:
    return any(
        float(getattr(suggestion, field, 0.0) or 0.0) > 0.0
        for field in (
            "identity_confidence",
            "accounting_confidence",
            "allocation_confidence",
            "document_confidence",
        )
    )


def _dimension_errors(
    suggestion: XeroStatementSuggestion,
    *,
    operation: str,
    profile: ReconciliationProfile,
) -> list[str]:
    if not _has_dimension_scores(suggestion):
        threshold = float(
            getattr(
                settings,
                "XERO_STATEMENT_BILL_PAYMENT_MIN_CONFIDENCE"
                if operation == XeroStatementPosting.OPERATION_BILL_PAYMENT
                else "XERO_STATEMENT_BANK_TRANSACTION_MIN_CONFIDENCE",
                0.98 if operation == XeroStatementPosting.OPERATION_BILL_PAYMENT else 0.92,
            )
        )
        if suggestion.confidence < threshold:
            return [f"Legacy overall confidence must be at least {threshold:.0%}."]
        return effective_tracking_errors(profile, suggestion)

    errors: list[str] = []
    identity_threshold = float(getattr(settings, "XERO_STATEMENT_IDENTITY_MIN_CONFIDENCE", 0.80))
    if suggestion.identity_confidence < identity_threshold:
        errors.append(f"Identity confidence must be at least {identity_threshold:.0%}.")
    if operation == XeroStatementPosting.OPERATION_BANK_TRANSACTION:
        accounting_threshold = float(getattr(settings, "XERO_STATEMENT_ACCOUNTING_MIN_CONFIDENCE", 0.90))
        if suggestion.accounting_confidence < accounting_threshold:
            errors.append(f"Accounting confidence must be at least {accounting_threshold:.0%}.")
    elif operation == XeroStatementPosting.OPERATION_BILL_PAYMENT:
        document_threshold = float(getattr(settings, "XERO_STATEMENT_BILL_DOCUMENT_MIN_CONFIDENCE", 0.95))
        if suggestion.document_confidence < document_threshold:
            errors.append(f"Bill-document confidence must be at least {document_threshold:.0%}.")
    if effective_tracking(profile, suggestion):
        allocation_threshold = float(getattr(settings, "XERO_STATEMENT_ALLOCATION_MIN_CONFIDENCE", 0.75))
        if suggestion.allocation_confidence < allocation_threshold:
            errors.append(f"Allocation confidence must be at least {allocation_threshold:.0%}.")
    errors.extend(effective_tracking_errors(profile, suggestion))
    return errors


def _tracking_preview(profile: ReconciliationProfile, suggestion: XeroStatementSuggestion) -> list[dict[str, str]]:
    assignment = effective_tracking(profile, suggestion)
    return [xero_tracking_entry(assignment)] if assignment else []


def _posting_payload(
    *,
    suggestion: XeroStatementSuggestion,
    profile: ReconciliationProfile,
    operation: str,
) -> dict[str, Any]:
    line = suggestion.statement_line
    if operation == XeroStatementPosting.OPERATION_BILL_PAYMENT:
        return {
            "Invoice": {"InvoiceID": suggestion.matched_xero_bill_id},
            "Account": {"AccountID": line.bank_account_id},
            "Date": line.transaction_date.isoformat(),
            "Amount": float(line.amount),
            "Reference": _reference(line),
        }

    line_item: dict[str, Any] = {
        "Description": suggestion.description[:4000],
        "Quantity": 1,
        "UnitAmount": float(line.amount),
        "AccountCode": suggestion.account_code,
        "TaxType": suggestion.tax_type,
    }
    tracking = _tracking_preview(profile, suggestion)
    if tracking:
        line_item["Tracking"] = tracking
    return {
        "Type": "SPEND" if line.direction == XeroStatementLineSnapshot.DIRECTION_DEBIT else "RECEIVE",
        "Contact": {"Name": suggestion.contact_name},
        "BankAccount": {"AccountID": line.bank_account_id},
        "Date": line.transaction_date.isoformat(),
        "Reference": _reference(line),
        "CurrencyCode": line.currency,
        "LineAmountTypes": profile.line_amount_types,
        "LineItems": [line_item],
        "Status": "AUTHORISED",
    }


def build_statement_posting_preview(
    suggestion: XeroStatementSuggestion,
    *,
    profile: ReconciliationProfile | None = None,
    active_bank_accounts: list[dict[str, str]] | None = None,
    capture_selection: StatementCaptureSelection | None = None,
) -> dict[str, Any]:
    """Validate and durably preview the Xero object for one suggestion."""

    suggestion = XeroStatementSuggestion.objects.select_related(
        "statement_line", "statement_line__last_scan", "organization"
    ).get(pk=suggestion.pk)
    line = suggestion.statement_line
    operation = _operation_for(suggestion)
    errors: list[str] = []
    warnings: list[str] = []
    if profile is None:
        try:
            profile = ReconciliationProfile.objects.select_related("xero_connection").get(
                organization=suggestion.organization
            )
        except ReconciliationProfile.DoesNotExist:
            raise ReconciliationValidationError("Reconciliation profile is not configured.")

    connection = profile.xero_connection
    if not profile.enabled:
        errors.append("Reconciliation is disabled for this organisation.")
    if connection is None:
        errors.append("A Xero connection must be selected.")
    terminal_posting_statuses = (
        XeroStatementPosting.STATUS_MATCH_READY,
        XeroStatementPosting.STATUS_RECONCILED,
    )
    if suggestion.status != XeroStatementSuggestion.STATUS_PROPOSED:
        already_posted = suggestion.postings.filter(
            status__in=terminal_posting_statuses
        ).exists()
        if not already_posted:
            errors.append("Only a proposed statement suggestion can be posted.")
    if not line.active:
        errors.append("The statement line is no longer in the active Xero queue.")
    if line.is_green_match:
        errors.append("The statement line is already ready or matched in Xero.")
    if suggestion.source_hash != line.source_hash:
        errors.append("The statement line changed after this suggestion was generated.")
    if not suggestion.execution_ready:
        errors.extend(
            str(reason) for reason in (suggestion.blocking_reasons or [])
            if str(reason) not in errors
        )
    if not operation:
        errors.append("This suggestion needs review and cannot be posted automatically.")
    scan_metadata = (
        line.last_scan.capture_metadata
        if line.last_scan_id and isinstance(line.last_scan.capture_metadata, dict)
        else {}
    )
    validated_capture_selection = capture_selection
    if capture_selection is not None or scan_metadata.get("schema_version") == 2:
        try:
            validated_capture_selection = validate_current_statement_line_capture(
                line,
                expected_bank_account_id=line.bank_account_id,
                expected_source_hash=line.source_hash,
                selection=capture_selection,
            )
        except StatementCaptureValidationError as exc:
            errors.append(str(exc))
    if active_bank_accounts is None and validated_capture_selection is not None:
        active_bank_accounts = list(validated_capture_selection.active_bank_accounts)
    if operation:
        if csv_statement_duplicate_count(line.statement_line_id) > 1:
            errors.append(
                "Identical CSV statement lines cannot be prepared automatically because "
                "the report has no stable per-line identifier."
            )
        try:
            selected_bank_account = active_bank_account(
                profile,
                line.bank_account_id,
                accounts=active_bank_accounts,
                allow_legacy_profile_account=scan_metadata.get("schema_version") != 2,
            )
        except ReconciliationValidationError as exc:
            selected_bank_account = None
            errors.extend(exc.errors)
        if selected_bank_account is None and not any(
            "bank-account catalogue" in error for error in errors
        ):
            errors.append("The statement line does not belong to an active Xero BANK account.")
    if operation == XeroStatementPosting.OPERATION_BANK_TRANSACTION:
        semantic_local_duplicate = XeroStatementPosting.objects.filter(
            organization=suggestion.organization,
            operation=XeroStatementPosting.OPERATION_BANK_TRANSACTION,
            status__in=terminal_posting_statuses,
            statement_line__bank_account_id=line.bank_account_id,
            statement_line__direction=line.direction,
            statement_line__transaction_date=line.transaction_date,
            statement_line__amount=line.amount,
            suggestion__contact_name__iexact=suggestion.contact_name,
        ).exclude(statement_line=line).first()
        if semantic_local_duplicate:
            errors.append(
                "A different statement line already created the same Xero transaction "
                f"(posting {semantic_local_duplicate.id}); review for a duplicate import."
            )

    if operation == XeroStatementPosting.OPERATION_BANK_TRANSACTION:
        errors.extend(_dimension_errors(suggestion, operation=operation, profile=profile))
        if connection and not xero_has_bank_transaction_scope(connection.scopes):
            errors.append("Reconnect Xero with accounting.banktransactions before posting.")
        if not all([
            suggestion.contact_name,
            suggestion.account_code,
            suggestion.account_name,
            suggestion.tax_type,
            suggestion.description,
        ]):
            errors.append("Contact, account, tax type, and description must all be verified.")
        if line.direction == XeroStatementLineSnapshot.DIRECTION_DEBIT and _exact_outstanding_bills(line).exists():
            errors.append("An outstanding Xero bill has the same amount; pay the bill instead of creating Spend Money.")
    elif operation == XeroStatementPosting.OPERATION_BILL_PAYMENT:
        errors.extend(_dimension_errors(suggestion, operation=operation, profile=profile))
        if connection and not xero_has_payment_write_scope(connection.scopes):
            errors.append("Reconnect Xero with accounting.payments before paying bills.")
        if line.direction != XeroStatementLineSnapshot.DIRECTION_DEBIT:
            errors.append("Only a debit statement line can pay a Xero bill.")
        matches = list(_exact_outstanding_bills(line)[:2])
        if not suggestion.matched_xero_bill_id:
            errors.append("The suggestion does not identify a Xero bill.")
        elif not any(row.external_record_id == suggestion.matched_xero_bill_id for row in matches):
            errors.append("The selected Xero bill is not an exact outstanding amount/currency match.")
        if len(matches) != 1:
            errors.append("Bill payment requires one unambiguous exact bill match.")

    payload = _posting_payload(suggestion=suggestion, profile=profile, operation=operation) if operation else {}
    tracking_assignment = effective_tracking(profile, suggestion)
    payload_hash = _hash_payload({
        "xero_payload": payload,
        "effective_tracking": tracking_assignment,
    })
    idempotency_key = f"mlai-statement-v2-{line.id}-{line.source_hash[:16]}-{operation or 'review'}"
    posting = None
    if operation:
        posting, _created = XeroStatementPosting.objects.get_or_create(
            organization=suggestion.organization,
            statement_line=line,
            source_hash=line.source_hash,
            defaults={
                "suggestion": suggestion,
                "operation": operation,
                "status": XeroStatementPosting.STATUS_READY if not errors else XeroStatementPosting.STATUS_PREVIEWED,
                "payload_hash": payload_hash,
                "idempotency_key": idempotency_key,
                "preview_payload": payload,
                "warnings": warnings,
            },
        )
        posting_is_active = (
            posting.status == XeroStatementPosting.STATUS_POSTING
            and posting.updated_at >= timezone.now() - timedelta(minutes=10)
        )
        if posting_is_active:
            errors.append("This statement suggestion is already being posted.")
        elif posting.status not in terminal_posting_statuses:
            posting.suggestion = suggestion
            posting.operation = operation
            posting.status = XeroStatementPosting.STATUS_READY if not errors else XeroStatementPosting.STATUS_PREVIEWED
            posting.payload_hash = payload_hash
            posting.idempotency_key = idempotency_key
            posting.preview_payload = payload
            posting.warnings = warnings
            posting.last_error = ""
            posting.save(update_fields=[
                "suggestion", "operation", "status", "payload_hash", "idempotency_key", "preview_payload",
                "warnings", "last_error", "updated_at",
            ])
    legacy_threshold = None
    if operation and not _has_dimension_scores(suggestion):
        legacy_threshold = float(
            getattr(
                settings,
                "XERO_STATEMENT_BILL_PAYMENT_MIN_CONFIDENCE"
                if operation == XeroStatementPosting.OPERATION_BILL_PAYMENT
                else "XERO_STATEMENT_BANK_TRANSACTION_MIN_CONFIDENCE",
                0.98 if operation == XeroStatementPosting.OPERATION_BILL_PAYMENT else 0.92,
            )
        )
    result = {
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
        "operation": operation or None,
        "confidence": suggestion.confidence,
        "minimum_confidence": legacy_threshold,
        "confidence_breakdown": {
            "identity": suggestion.identity_confidence,
            "accounting": suggestion.accounting_confidence,
            "allocation": suggestion.allocation_confidence,
            "document": suggestion.document_confidence,
        },
        "statement_line_id": line.statement_line_id,
        "xero_payload": payload,
        "effective_tracking": tracking_assignment,
        "tracking_policy_ready": not effective_tracking_errors(profile, suggestion),
        "untracked_executable_count": XeroStatementSuggestion.objects.filter(
            organization=suggestion.organization,
            statement_line__active=True,
            status=XeroStatementSuggestion.STATUS_PROPOSED,
            execution_ready=True,
            allocation_mode=XeroStatementSuggestion.ALLOCATION_UNASSIGNED,
            event_source_id="",
            project_source_id="",
        ).count(),
        "payload_hash": payload_hash,
        "posting_id": posting.id if posting else None,
        "human_reconciliation_required": True,
        "note": "The API creates the matching Xero transaction; a human still clicks Match/OK on the bank statement line.",
    }
    record_reconciliation_decision(
        statement_line=line,
        suggestion=suggestion,
        decision_type=(
            ReconciliationDecision.TYPE_PREVIEW_READY
            if result["ready"]
            else ReconciliationDecision.TYPE_PREVIEW_BLOCKED
        ),
        run_id=suggestion.run_id,
        actor_type=ReconciliationDecision.ACTOR_SYSTEM,
        outcome={
            "ready": result["ready"],
            "operation": result["operation"],
            "errors": result["errors"],
            "payload_hash": payload_hash,
        },
        evidence=suggestion.evidence or [],
    )
    return result


def _resolve_contact_id(connection, contact_name: str) -> str:
    escaped = contact_name.replace('"', '""')
    response = http_client.get(
        f"{XERO_API_URL}/Contacts",
        headers=_xero_headers(connection),
        params={"where": f'Name=="{escaped}"'},
        timeout=(3, 30),
    )
    response.raise_for_status()
    contacts = response.json().get("Contacts", [])
    exact = [
        row for row in contacts
        if str(row.get("Name") or "").strip().casefold() == contact_name.strip().casefold()
    ]
    if len(exact) != 1 or not exact[0].get("ContactID"):
        raise XeroPostingError(f"Xero does not contain one exact contact named {contact_name}.")
    return str(exact[0]["ContactID"])


def _resolve_tax_type(connection, tax_type: str, direction: str) -> str:
    """Translate a Xero UI tax-rate name into its BankTransactions API code."""

    requested = str(tax_type or "").strip()
    if requested and requested == requested.upper() and not any(char.isspace() for char in requested):
        return requested

    response = http_client.get(
        f"{XERO_API_URL}/TaxRates",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    response.raise_for_status()
    matches = []
    for row in response.json().get("TaxRates", []):
        if str(row.get("Name") or "").strip().casefold() != requested.casefold():
            continue
        if str(row.get("Status") or "").upper() != "ACTIVE":
            continue
        applies = (
            row.get("CanApplyToExpenses")
            if direction == XeroStatementLineSnapshot.DIRECTION_DEBIT
            else row.get("CanApplyToRevenue")
        )
        if applies is False:
            continue
        if row.get("TaxType"):
            matches.append(row)
    if len(matches) != 1:
        raise XeroPostingError(
            f"Xero does not contain one active tax rate named {requested} for this transaction."
        )
    return str(matches[0]["TaxType"])


def _normalized_tracking_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _require_current_catalog_name(
    *,
    names: set[str],
    expected_name: str,
    source_label: str,
) -> None:
    normalized_names = {_normalized_tracking_name(name) for name in names}
    normalized_expected = _normalized_tracking_name(expected_name)
    if not normalized_names or normalized_names == {""}:
        raise XeroPostingError(
            f"The canonical {source_label} no longer exists. Refresh the reconciliation context "
            "and review the suggestion again."
        )
    if normalized_names != {normalized_expected}:
        raise XeroPostingError(
            f"The canonical {source_label} name changed or is ambiguous. Refresh the reconciliation "
            "context and review the suggestion again."
        )


def _validate_canonical_tracking_creation(
    suggestion: XeroStatementSuggestion,
    assignment: dict[str, Any],
) -> None:
    """Fail closed before creating a non-default option from a stale suggestion."""

    option_name = str(assignment.get("option_name") or "").strip()
    kind = str(assignment.get("kind") or "").strip().casefold()
    organization = suggestion.organization
    if kind == "event":
        source_type = str(suggestion.event_source_type or "luma").strip().casefold()
        source_id = str(suggestion.event_source_id or "").strip()
        if not source_id:
            raise XeroPostingError(
                "A missing Event Name option can only be created from a canonical event ID."
            )
        names = _current_event_catalog_names(
            organization=organization,
            source_type=source_type,
            source_id=source_id,
        )
        _require_current_catalog_name(
            names=names,
            expected_name=option_name,
            source_label=f"{source_type} event {source_id}",
        )
        return

    if kind == "project":
        source_type = str(suggestion.project_source_type or "linear").strip().casefold()
        source_id = str(suggestion.project_source_id or "").strip()
        if source_type == "xero_tracking":
            raise XeroPostingError(
                "A Project sourced from Xero must retain its existing tracking option ID. "
                "Refresh the Xero tracking catalogue and review the suggestion again."
            )
        if source_type != "linear":
            raise XeroPostingError(
                f"Unsupported canonical project source {source_type!r}; review the suggestion again."
            )
        if not source_id:
            raise XeroPostingError(
                "A missing Project Name option can only be created from a canonical Linear project ID."
            )
        names = _current_linear_project_catalog_names(
            organization=organization,
            source_id=source_id,
        )
        _require_current_catalog_name(
            names=names,
            expected_name=option_name,
            source_label=f"Linear project {source_id}",
        )
        return

    raise XeroPostingError("Only canonical Event Name or Project Name options may be created.")


def _resolve_xero_tracking_assignment(
    connection,
    profile,
    assignment: dict[str, Any] | None,
    *,
    execution_suggestion: XeroStatementSuggestion | None = None,
) -> list[dict[str, str]]:
    """Resolve tracking, creating only defaults or an execution-validated statement option.

    Preview and bill-intake callers omit ``execution_suggestion`` and therefore
    cannot create a non-default option. Statement execution supplies it only
    after the existing readiness and automatic-post gates have passed.
    """

    if not assignment:
        return []
    response = http_client.get(
        f"{XERO_API_URL}/TrackingCategories",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    response.raise_for_status()
    categories = response.json().get("TrackingCategories", [])
    category_id = str(assignment["category_id"])
    category_name = str(assignment["category_name"])
    option_name = str(assignment["option_name"])
    option_id = str(assignment.get("option_id") or "")
    category = _configured_tracking_category(
        categories,
        category_id=category_id,
        category_name=category_name,
    )
    if option_id:
        option = _tracking_option_by_id(
            category,
            option_id=option_id,
            option_name=option_name,
        )
        if option is None:
            if (
                execution_suggestion is not None
                and str(execution_suggestion.project_source_type or "")
                .strip()
                .casefold()
                == "xero_tracking"
            ):
                _validate_canonical_tracking_creation(execution_suggestion, assignment)
            raise XeroPostingError(
                f"Xero tracking option ID {option_id} no longer exists in "
                f"{category_name}; refresh and review the suggestion."
            )
    else:
        option = _active_tracking_option_by_name(
            category,
            option_name=option_name,
        )
    if option is None:
        if not assignment.get("default") and execution_suggestion is not None:
            _validate_canonical_tracking_creation(execution_suggestion, assignment)
        if assignment.get("default") or execution_suggestion is not None:
            if not xero_has_settings_write_scope(connection.scopes):
                raise XeroPostingError(
                    f"Reconnect Xero with accounting.settings to create tracking option {option_name}."
                )
            create = http_client.put(
                f"{XERO_API_URL}/TrackingCategories/{category_id}/Options",
                headers=_xero_headers(connection),
                json={"Options": [{"Name": option_name}]},
                timeout=(3, 30),
            )
            create.raise_for_status()
            option = _created_tracking_option(create.json())
            created_name = str((option or {}).get("Name") or "").strip()
            if created_name.casefold() != option_name.casefold():
                raise XeroPostingError(
                    f"Xero returned a different tracking option after creating {option_name}."
                )
            if str((option or {}).get("Status") or "").strip().upper() != "ACTIVE":
                raise XeroPostingError(
                    f"Xero returned an archived tracking option for {option_name}."
                )
        else:
            raise XeroPostingError(f"Xero does not contain one active tracking option named {option_name}.")
    resolved_option_id = str((option or {}).get("TrackingOptionID") or (option or {}).get("OptionID") or "")
    if not resolved_option_id:
        raise XeroPostingError(f"Xero did not return a tracking option ID for {option_name}.")
    if assignment.get("default") and profile.default_project_tracking_option_id != resolved_option_id:
        profile.default_project_tracking_option_id = resolved_option_id
        profile.save(update_fields=["default_project_tracking_option_id", "updated_at"])
    return [{
        "TrackingCategoryID": category_id,
        "Name": category_name,
        "TrackingOptionID": resolved_option_id,
        "Option": option_name,
    }]


def resolve_xero_tracking_assignment(
    connection,
    profile,
    assignment: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Resolve tracking without permission to create a non-default option."""

    return _resolve_xero_tracking_assignment(connection, profile, assignment)


def _resolved_tracking(connection, profile, suggestion) -> list[dict[str, str]]:
    return _resolve_xero_tracking_assignment(
        connection,
        profile,
        effective_tracking(profile, suggestion),
        execution_suggestion=suggestion,
    )


def _first_xero_row(payload: Any, key: str) -> dict[str, Any]:
    rows = payload.get(key) if isinstance(payload, dict) else None
    row = rows[0] if isinstance(rows, list) and rows else {}
    errors = row.get("ValidationErrors") or []
    if row.get("HasErrors") or errors:
        messages = [str(item.get("Message") or item) for item in errors]
        raise XeroPostingError("; ".join(messages) or f"Xero rejected the {key} request.")
    return row


def _preflight_bill(connection, *, bill_id: str, line: XeroStatementLineSnapshot) -> dict[str, Any]:
    response = http_client.get(
        f"{XERO_API_URL}/Invoices/{bill_id}",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    response.raise_for_status()
    bill = _first_xero_row(response.json(), "Invoices")
    if str(bill.get("Type") or "").upper() != "ACCPAY":
        raise XeroPostingError("The selected Xero invoice is not an accounts-payable bill.")
    if str(bill.get("Status") or "").upper() != "AUTHORISED":
        raise XeroPostingError("The selected Xero bill is no longer authorised and outstanding.")
    if _as_money(bill.get("AmountDue")) != line.amount:
        raise XeroPostingError("The Xero bill amount due no longer matches the statement line.")
    currency = str(bill.get("CurrencyCode") or "").upper()
    if currency and line.currency and currency != line.currency.upper():
        raise XeroPostingError("The Xero bill currency no longer matches the statement line.")
    return bill


def _ensure_bill_tracking(
    connection,
    *,
    bill: dict[str, Any],
    tracking: dict[str, str],
) -> None:
    """Add one assignment to untracked bill lines without changing financial data."""

    category_id = str(tracking.get("TrackingCategoryID") or "")
    option_id = str(tracking.get("TrackingOptionID") or "")
    option_name = str(tracking.get("Option") or "")
    changed = False
    line_items: list[dict[str, Any]] = []
    preserve_fields = (
        "LineItemID",
        "Description",
        "Quantity",
        "UnitAmount",
        "AccountCode",
        "TaxType",
        "ItemCode",
        "DiscountRate",
        "DiscountAmount",
    )
    for source_line in bill.get("LineItems") or []:
        line_item_id = str(source_line.get("LineItemID") or "")
        if not line_item_id:
            raise XeroPostingError("The existing bill has a line without a stable LineItemID.")
        existing = [item for item in source_line.get("Tracking") or [] if isinstance(item, dict)]
        if len(existing) > 1:
            raise XeroPostingError("The existing bill line already has multiple tracking assignments.")
        if existing:
            current = existing[0]
            current_category = str(current.get("TrackingCategoryID") or "")
            current_option_id = str(current.get("TrackingOptionID") or current.get("OptionID") or "")
            current_option_name = str(current.get("Option") or current.get("Name") or "")
            if current_category != category_id or (
                current_option_id and current_option_id != option_id
            ) or (
                not current_option_id and current_option_name.casefold() != option_name.casefold()
            ):
                raise XeroPostingError(
                    "The existing bill has tracking that conflicts with the approved allocation."
                )
            line_tracking = existing
        else:
            line_tracking = [tracking]
            changed = True
        line_item = {
            field: source_line[field]
            for field in preserve_fields
            if field in source_line and source_line[field] is not None
        }
        line_item["Tracking"] = line_tracking
        line_items.append(line_item)

    if not changed:
        return
    contact = bill.get("Contact") if isinstance(bill.get("Contact"), dict) else {}
    invoice = {
        key: bill[key]
        for key in (
            "InvoiceID",
            "Type",
            "Date",
            "DueDate",
            "LineAmountTypes",
            "Reference",
            "CurrencyCode",
            "Status",
        )
        if key in bill and bill[key] is not None
    }
    if contact.get("ContactID"):
        invoice["Contact"] = {"ContactID": contact["ContactID"]}
    invoice["LineItems"] = line_items
    response = http_client.post(
        f"{XERO_API_URL}/Invoices/{bill['InvoiceID']}",
        headers=_xero_headers(connection),
        json={"Invoices": [invoice]},
        timeout=(3, 30),
    )
    response.raise_for_status()
    _first_xero_row(response.json(), "Invoices")


def _existing_by_reference(connection, *, resource: str, reference: str) -> dict[str, Any] | None:
    escaped = reference.replace('"', '""')
    response = http_client.get(
        f"{XERO_API_URL}/{resource}",
        headers=_xero_headers(connection),
        params={"where": f'Reference=="{escaped}"'},
        timeout=(3, 30),
    )
    response.raise_for_status()
    rows = response.json().get(resource, [])
    return rows[0] if isinstance(rows, list) and rows else None


def _semantic_bank_transaction_candidates(
    connection,
    *,
    posting: XeroStatementPosting,
) -> list[dict[str, Any]]:
    """Find same-bank/date/direction/amount/contact rows without trusting a reference.

    These are treated as a duplicate guard, not automatically adopted: two real
    same-day transactions can share these fields, so only the stable MLAI
    reference is sufficient for idempotent recovery.
    """

    line = posting.statement_line
    expected_type = "SPEND" if line.direction == XeroStatementLineSnapshot.DIRECTION_DEBIT else "RECEIVE"
    transaction_date = line.transaction_date
    where = (
        f'Type=="{expected_type}"&&'
        f'Date==DateTime({transaction_date.year},{transaction_date.month},{transaction_date.day})&&'
        f"Total=={line.amount:.2f}"
    )
    response = http_client.get(
        f"{XERO_API_URL}/BankTransactions",
        headers=_xero_headers(connection),
        params={"where": where},
        timeout=(3, 30),
    )
    response.raise_for_status()
    rows = response.json().get("BankTransactions", [])
    expected_bank_id = str(posting.preview_payload["BankAccount"]["AccountID"])
    expected_contact = posting.suggestion.contact_name.strip().casefold()
    matches = []
    for row in rows if isinstance(rows, list) else []:
        bank = row.get("BankAccount") if isinstance(row.get("BankAccount"), dict) else {}
        contact = row.get("Contact") if isinstance(row.get("Contact"), dict) else {}
        if str(row.get("Type") or "").upper() != expected_type:
            continue
        if str(bank.get("AccountID") or "") != expected_bank_id:
            continue
        if _as_money(row.get("Total")) != line.amount:
            continue
        if str(contact.get("Name") or "").strip().casefold() != expected_contact:
            continue
        currency = str(row.get("CurrencyCode") or "").strip().upper()
        if currency and line.currency and currency != line.currency.upper():
            continue
        matches.append(row)
    return matches


def _validate_existing_bank_transaction(row: dict[str, Any], posting: XeroStatementPosting) -> None:
    expected_type = "SPEND" if posting.statement_line.direction == XeroStatementLineSnapshot.DIRECTION_DEBIT else "RECEIVE"
    bank = row.get("BankAccount") if isinstance(row.get("BankAccount"), dict) else {}
    if str(row.get("Type") or "").upper() != expected_type:
        raise XeroPostingError("The existing Xero reference belongs to a different transaction type.")
    if str(bank.get("AccountID") or "") != str(posting.preview_payload["BankAccount"]["AccountID"]):
        raise XeroPostingError("The existing Xero reference belongs to a different bank account.")
    if _as_money(row.get("Total")) != posting.statement_line.amount:
        raise XeroPostingError("The existing Xero reference has a different amount.")


def _validate_existing_payment(row: dict[str, Any], posting: XeroStatementPosting) -> None:
    invoice = row.get("Invoice") if isinstance(row.get("Invoice"), dict) else {}
    if str(invoice.get("InvoiceID") or "") != posting.suggestion.matched_xero_bill_id:
        raise XeroPostingError("The existing Xero payment reference belongs to a different bill.")
    if _as_money(row.get("Amount")) != posting.statement_line.amount:
        raise XeroPostingError("The existing Xero payment reference has a different amount.")


def execute_statement_posting(
    suggestion: XeroStatementSuggestion,
    *,
    requested_by_slack_id: str,
    automatic: bool = False,
    capture_selection: StatementCaptureSelection | None = None,
) -> XeroStatementPosting:
    """Create the matching BankTransaction or Payment, exactly once."""

    if automatic and not getattr(settings, "XERO_STATEMENT_AUTO_POST_ENABLED", False):
        raise ReconciliationValidationError("Automatic statement posting is disabled.")
    preview = build_statement_posting_preview(
        suggestion,
        capture_selection=capture_selection,
    )
    if not preview["ready"]:
        raise ReconciliationValidationError("Statement suggestion is not ready to post.", errors=preview["errors"])
    posting_id = preview["posting_id"]
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=suggestion.organization
    )
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")

    with transaction.atomic():
        posting = XeroStatementPosting.objects.select_for_update().select_related(
            "suggestion", "statement_line"
        ).get(pk=posting_id)
        if posting.status in {
            XeroStatementPosting.STATUS_MATCH_READY,
            XeroStatementPosting.STATUS_RECONCILED,
        }:
            return posting
        if posting.status == XeroStatementPosting.STATUS_POSTING:
            raise ReconciliationValidationError("This statement suggestion is already being posted.")
        posting.status = XeroStatementPosting.STATUS_POSTING
        posting.requested_by_slack_id = requested_by_slack_id
        posting.automatic = automatic
        posting.last_error = ""
        posting.save(update_fields=[
            "status", "requested_by_slack_id", "automatic", "last_error", "updated_at"
        ])

    try:
        payload = dict(posting.preview_payload)
        reference = str(payload["Reference"])
        headers = _xero_headers(connection)
        headers["Idempotency-Key"] = posting.idempotency_key
        if posting.operation == XeroStatementPosting.OPERATION_BANK_TRANSACTION:
            existing = _existing_by_reference(
                connection, resource="BankTransactions", reference=reference
            )
            if existing:
                _validate_existing_bank_transaction(existing, posting)
            else:
                semantic_matches = _semantic_bank_transaction_candidates(
                    connection,
                    posting=posting,
                )
                if semantic_matches:
                    record_reconciliation_decision(
                        statement_line=posting.statement_line,
                        suggestion=suggestion,
                        decision_type=ReconciliationDecision.TYPE_PREVIEW_BLOCKED,
                        run_id=suggestion.run_id,
                        actor_type=ReconciliationDecision.ACTOR_SYSTEM,
                        outcome={
                            "reason": "semantic_duplicate",
                            "xero_bank_transaction_ids": [
                                str(row.get("BankTransactionID") or "")
                                for row in semantic_matches
                            ],
                        },
                        evidence=suggestion.evidence or [],
                        discriminator="semantic_duplicate",
                    )
                    raise XeroPostingError(
                        "Xero already contains a bank transaction with the same bank account, "
                        "direction, date, amount and contact. Review it before creating another."
                    )
            row = existing or {}
            if not row:
                payload["Contact"] = {
                    "ContactID": _resolve_contact_id(connection, suggestion.contact_name)
                }
                payload["LineItems"][0]["TaxType"] = _resolve_tax_type(
                    connection,
                    suggestion.tax_type,
                    posting.statement_line.direction,
                )
                tracking = _resolved_tracking(connection, profile, suggestion)
                if tracking:
                    payload["LineItems"][0]["Tracking"] = tracking
                response = http_client.put(
                    f"{XERO_API_URL}/BankTransactions",
                    headers=headers,
                    json={"BankTransactions": [payload]},
                    timeout=(3, 30),
                )
                response.raise_for_status()
                row = _first_xero_row(response.json(), "BankTransactions")
            xero_bank_transaction_id = str(row.get("BankTransactionID") or "").strip()
            if not xero_bank_transaction_id:
                raise XeroPostingError("Xero did not return a BankTransactionID.")
            xero_payment_id = ""
            xero_bill_id = ""
        else:
            existing = _existing_by_reference(connection, resource="Payments", reference=reference)
            if existing:
                _validate_existing_payment(existing, posting)
            row = existing or {}
            if not row:
                bill = _preflight_bill(
                    connection,
                    bill_id=suggestion.matched_xero_bill_id,
                    line=posting.statement_line,
                )
                tracking = _resolved_tracking(connection, profile, suggestion)
                if tracking:
                    _ensure_bill_tracking(connection, bill=bill, tracking=tracking[0])
                response = http_client.put(
                    f"{XERO_API_URL}/Payments",
                    headers=headers,
                    json={"Payments": [payload]},
                    timeout=(3, 30),
                )
                response.raise_for_status()
                row = _first_xero_row(response.json(), "Payments")
            xero_payment_id = str(row.get("PaymentID") or "").strip()
            if not xero_payment_id:
                raise XeroPostingError("Xero did not return a PaymentID.")
            xero_bank_transaction_id = ""
            xero_bill_id = suggestion.matched_xero_bill_id
    except Exception as exc:
        XeroStatementPosting.objects.filter(pk=posting_id).update(
            status=XeroStatementPosting.STATUS_FAILED,
            last_error=str(exc)[:2000],
            updated_at=timezone.now(),
        )
        if isinstance(exc, (ReconciliationValidationError, XeroPostingError)):
            raise
        raise XeroPostingError(str(exc)) from exc

    now = timezone.now()
    with transaction.atomic():
        posting = XeroStatementPosting.objects.select_for_update().get(pk=posting_id)
        posting.status = XeroStatementPosting.STATUS_MATCH_READY
        posting.xero_bank_transaction_id = xero_bank_transaction_id
        posting.xero_payment_id = xero_payment_id
        posting.xero_bill_id = xero_bill_id
        posting.posted_at = now
        posting.last_error = ""
        posting.save(update_fields=[
            "status", "xero_bank_transaction_id", "xero_payment_id", "xero_bill_id",
            "posted_at", "last_error", "updated_at",
        ])
        XeroStatementSuggestion.objects.filter(pk=posting.suggestion_id).update(
            status=XeroStatementSuggestion.STATUS_APPLIED,
            applied_at=now,
            updated_at=now,
        )
        record_reconciliation_decision(
            statement_line=posting.statement_line,
            suggestion=posting.suggestion,
            decision_type=(
                ReconciliationDecision.TYPE_DUPLICATE_RECOVERED
                if existing
                else ReconciliationDecision.TYPE_EXECUTED
            ),
            run_id=posting.suggestion.run_id,
            actor_type=ReconciliationDecision.ACTOR_ADMIN,
            actor_id=requested_by_slack_id,
            outcome={
                "operation": posting.operation,
                "xero_bank_transaction_id": xero_bank_transaction_id,
                "xero_payment_id": xero_payment_id,
                "xero_bill_id": xero_bill_id,
            },
            evidence=posting.suggestion.evidence or [],
        )
    return posting
