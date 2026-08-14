"""Create supplier bills and attach source documents for the reconciliation agent.

The agent's invoices mailbox (treasurer@mlai.au) produces extracted supplier
invoices; this module turns them into real Xero ACCPAY bills so the bank
statement line green-matches the bill on the reconcile screen, and attaches the
source PDF as the audit trail. mlai-backend stays the only Xero writer: scope
checks, idempotency and the confirm contract mirror xero_statement_posting.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from django.conf import settings
from django.utils import timezone

from integrations import http_client
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceProvider,
    ReconciliationProfile,
)
from integrations.services.xero_reconciliation import (
    XERO_API_URL,
    ReconciliationValidationError,
    XeroPostingError,
    _xero_headers,
)
from integrations.services.xero_scopes import (
    xero_has_attachments_scope,
    xero_has_invoice_write_scope,
)
from integrations.services.xero_statement_posting import (
    _ensure_bill_tracking,
    _first_xero_row,
    resolve_xero_tracking_assignment,
)
from integrations.services.reconciliation_tracking import xero_tracking_entry

BILL_STATUS_DRAFT = "DRAFT"
BILL_STATUS_AUTHORISED = "AUTHORISED"
ATTACHMENT_ENTITIES = {
    "invoice": "Invoices",
    "bank_transaction": "BankTransactions",
}
ALLOWED_ATTACHMENT_CONTENT_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
}
_XERO_GUID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{30,35}$")


def _clean_text(value: Any, *, limit: int = 255) -> str:
    """Strip control characters (PDF text layers can smuggle NULs) and bound length."""

    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return text[:limit]


def _money(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount


def _iso_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _profile_and_connection(organization):
    try:
        profile = ReconciliationProfile.objects.select_related("xero_connection").get(
            organization=organization
        )
    except ReconciliationProfile.DoesNotExist:
        raise ReconciliationValidationError("Reconciliation profile is not configured.")
    if not profile.enabled:
        raise ReconciliationValidationError("Reconciliation is disabled for this organisation.")
    if profile.xero_connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    return profile, profile.xero_connection


def _bill_effective_tracking(
    profile: ReconciliationProfile | None,
    payload: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    raw = payload.get("effective_tracking")
    if not isinstance(raw, dict):
        if profile and profile.require_statement_tracking:
            errors.append("Every bill requires an Event, Project, or MLAI core allocation.")
        return None
    mode = _clean_text(raw.get("allocation_mode"), limit=16)
    kind = _clean_text(raw.get("kind"), limit=16)
    if mode not in {"event", "project", "mlai_core"}:
        errors.append("effective_tracking.allocation_mode must be event, project or mlai_core.")
        return None
    if mode == "event" and kind != "event":
        errors.append("Event allocation must use the Event Name tracking category.")
        return None
    if mode in {"project", "mlai_core"} and kind != "project":
        errors.append("Project allocation must use the Project Name tracking category.")
        return None
    if profile is None:
        return None
    default = mode == "mlai_core"
    option_name = (
        profile.default_project_tracking_option_name
        if default
        else _clean_text(raw.get("option_name"))
    )
    option_id = (
        profile.default_project_tracking_option_id
        if default
        else _clean_text(raw.get("option_id"))
    )
    if not option_name:
        errors.append("effective_tracking requires a tracking option name.")
        return None
    category_id = (
        profile.event_tracking_category_id if kind == "event"
        else profile.project_tracking_category_id
    )
    category_name = (
        profile.event_tracking_category_name if kind == "event"
        else profile.project_tracking_category_name
    )
    if not category_id:
        errors.append(f"Configure the Xero {category_name} tracking category ID.")
    return {
        "allocation_mode": mode,
        "kind": kind,
        "category_id": category_id,
        "category_name": category_name,
        "option_id": option_id,
        "option_name": option_name,
        "default": default,
    }


def _parse_line_items(payload: dict[str, Any], *, contact_name: str, invoice_number: str) -> tuple[list[dict[str, Any]], Decimal, list[str]]:
    errors: list[str] = []
    raw_lines = payload.get("line_amounts")
    lines: list[dict[str, Any]] = []
    if isinstance(raw_lines, list) and raw_lines:
        for index, raw in enumerate(raw_lines):
            if not isinstance(raw, dict):
                errors.append(f"line_amounts[{index}] must be an object.")
                continue
            amount = _money(raw.get("amount"))
            quantity = _money(raw.get("quantity")) if str(raw.get("quantity") or "").strip() else None
            unit_amount = _money(raw.get("unit_amount")) if str(raw.get("unit_amount") or "").strip() else None
            if amount is None and quantity is not None and unit_amount is not None:
                amount = (quantity * unit_amount).quantize(Decimal("0.01"))
            if amount is None or amount <= 0:
                errors.append(f"line_amounts[{index}].amount must be a positive amount.")
                continue
            if quantity is not None and quantity <= 0:
                errors.append(f"line_amounts[{index}].quantity must be positive when provided.")
                continue
            if unit_amount is not None and unit_amount <= 0:
                errors.append(f"line_amounts[{index}].unit_amount must be positive when provided.")
                continue
            if (
                quantity is not None
                and unit_amount is not None
                and (quantity * unit_amount).quantize(Decimal("0.01")) != amount
            ):
                errors.append(
                    f"line_amounts[{index}] quantity × unit_amount does not equal amount."
                )
                continue
            lines.append({
                "description": _clean_text(raw.get("description"), limit=4000)
                or f"{contact_name} invoice {invoice_number}",
                "amount": amount,
                "quantity": quantity,
                "unit_amount": unit_amount,
                "account_code": _clean_text(raw.get("account_code"), limit=64),
                "tax_type": _clean_text(raw.get("tax_type"), limit=255),
            })
    else:
        total = _money(payload.get("total"))
        if total is None or total <= 0:
            errors.append("Provide a positive total or a non-empty line_amounts list.")
            total = Decimal("0.00")
        lines.append({
            "description": _clean_text(payload.get("description"), limit=4000)
            or f"{contact_name} invoice {invoice_number}",
            "amount": total,
            "quantity": None,
            "unit_amount": None,
            "account_code": _clean_text(payload.get("account_code"), limit=64),
            "tax_type": _clean_text(payload.get("tax_type"), limit=255),
        })

    line_total = sum((line["amount"] for line in lines), Decimal("0.00"))
    declared_total = _money(payload.get("total"))
    if isinstance(raw_lines, list) and raw_lines and declared_total is not None and declared_total != line_total:
        errors.append(
            f"line_amounts sum to {line_total} but total says {declared_total}; they must agree."
        )
    return lines, line_total, errors


def build_reconciliation_bill_preview(organization, *, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a draft-bill request and return the exact Xero payload it would send."""

    errors: list[str] = []
    warnings: list[str] = []
    profile = None
    connection = None
    try:
        profile, connection = _profile_and_connection(organization)
    except ReconciliationValidationError as exc:
        errors.extend(exc.errors)
    tracking_assignment = _bill_effective_tracking(profile, payload, errors)

    contact_name = _clean_text(payload.get("contact_name"))
    invoice_number = _clean_text(payload.get("invoice_number"))
    if not contact_name:
        errors.append("contact_name is required.")
    if not invoice_number:
        errors.append("invoice_number is required.")

    issue_date = _iso_date(payload.get("issue_date"))
    if issue_date is None:
        errors.append("issue_date must be an ISO date (YYYY-MM-DD).")
    due_date = _iso_date(payload.get("due_date")) or issue_date

    currency = _clean_text(payload.get("currency"), limit=12).upper()
    if currency and not re.fullmatch(r"[A-Z]{3}", currency):
        errors.append("currency must be a 3-letter code when provided.")

    lines, line_total, line_errors = _parse_line_items(
        payload, contact_name=contact_name or "Supplier", invoice_number=invoice_number or ""
    )
    errors.extend(line_errors)

    requested_status = _clean_text(payload.get("status"), limit=32).upper() or BILL_STATUS_DRAFT
    if requested_status not in {BILL_STATUS_DRAFT, BILL_STATUS_AUTHORISED}:
        errors.append("status must be DRAFT or AUTHORISED.")
        requested_status = BILL_STATUS_DRAFT
    resolved_status = requested_status
    if requested_status == BILL_STATUS_AUTHORISED and not all(
        line["account_code"] and line["tax_type"] for line in lines
    ):
        resolved_status = BILL_STATUS_DRAFT
        warnings.append(
            "Downgraded to DRAFT: every line needs an account_code and tax_type before authorising."
        )

    if connection is not None and not xero_has_invoice_write_scope(connection.scopes):
        errors.append("Reconnect Xero with accounting.transactions before creating bills.")

    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    gmail_message_id = _clean_text(source.get("gmail_message_id"))
    reference = _clean_text(payload.get("reference")) or (
        f"treasurer-inbox:{gmail_message_id}" if gmail_message_id else ""
    )

    line_items = []
    for line in lines:
        item: dict[str, Any] = {
            "Description": line["description"],
            "Quantity": float(
                line["quantity"]
                if line["quantity"] is not None and line["unit_amount"] is not None
                else Decimal("1")
            ),
            "UnitAmount": float(
                line["unit_amount"]
                if line["quantity"] is not None and line["unit_amount"] is not None
                else line["amount"]
            ),
        }
        if line["account_code"]:
            item["AccountCode"] = line["account_code"]
        if line["tax_type"]:
            item["TaxType"] = line["tax_type"]
        if tracking_assignment:
            item["Tracking"] = [xero_tracking_entry(tracking_assignment)]
        line_items.append(item)

    document_metadata = {
        "subtotal": str(payload.get("subtotal") or ""),
        "tax_amount": str(payload.get("tax_amount") or ""),
        "total": str(payload.get("total") or line_total),
        "amount_due": str(payload.get("amount_due") or ""),
        "source": payload.get("source") if isinstance(payload.get("source"), dict) else {},
    }

    xero_payload: dict[str, Any] = {
        "Type": "ACCPAY",
        "Contact": {"Name": contact_name},
        "InvoiceNumber": invoice_number,
        "Date": issue_date.isoformat() if issue_date else "",
        "DueDate": due_date.isoformat() if due_date else "",
        "LineAmountTypes": _clean_text(payload.get("line_amount_types"), limit=16)
        or (profile.line_amount_types if profile else "Inclusive"),
        "LineItems": line_items,
        "Status": resolved_status,
    }
    if currency:
        xero_payload["CurrencyCode"] = currency
    if reference:
        xero_payload["Reference"] = reference

    return {
        "ready": not errors,
        "errors": errors,
        "warnings": warnings,
        "status": resolved_status,
        "contact_name": contact_name,
        "invoice_number": invoice_number,
        "total": str(line_total),
        "document_metadata": document_metadata,
        "effective_tracking": tracking_assignment,
        "tracking_policy_ready": not (
            profile and profile.require_statement_tracking and not tracking_assignment
        ),
        "xero_payload": xero_payload,
    }


def _serialize_bill_row(row: dict[str, Any]) -> dict[str, Any]:
    contact = row.get("Contact") if isinstance(row.get("Contact"), dict) else {}
    return {
        "xero_invoice_id": str(row.get("InvoiceID") or ""),
        "invoice_number": str(row.get("InvoiceNumber") or ""),
        "status": str(row.get("Status") or "").upper(),
        "contact_name": str(contact.get("Name") or ""),
        "total": str(row.get("Total") if row.get("Total") is not None else ""),
        "amount_due": str(row.get("AmountDue") if row.get("AmountDue") is not None else ""),
        "currency": str(row.get("CurrencyCode") or ""),
        "date": str(row.get("DateString") or "")[:10],
        "due_date": str(row.get("DueDateString") or "")[:10],
    }


def _existing_bill_for_contact(connection, *, invoice_number: str, contact_name: str) -> dict[str, Any] | None:
    """Same-contact ACCPAY invoice with this number, if Xero already has one.

    Invoice numbers are only vendor-unique (every supplier has an INV-001), so a
    row for a different contact is not an idempotency hit.
    """

    escaped = invoice_number.replace('"', '""')
    response = http_client.get(
        f"{XERO_API_URL}/Invoices",
        headers=_xero_headers(connection),
        params={"where": f'Type=="ACCPAY"&&InvoiceNumber=="{escaped}"'},
        timeout=(3, 30),
    )
    response.raise_for_status()
    rows = response.json().get("Invoices", [])
    wanted = contact_name.strip().casefold()
    for row in rows if isinstance(rows, list) else []:
        if str(row.get("Status") or "").upper() in {"DELETED", "VOIDED"}:
            continue
        contact = row.get("Contact") if isinstance(row.get("Contact"), dict) else {}
        if str(contact.get("Name") or "").strip().casefold() == wanted:
            return row
    return None


def _contact_payload(connection, contact_name: str) -> dict[str, str]:
    """Reuse an existing Xero contact when exactly one matches; otherwise let the
    invoice create it by name. Several same-name contacts is ambiguous."""

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
        and row.get("ContactID")
    ]
    if len(exact) > 1:
        raise XeroPostingError(f"Xero contains multiple contacts named {contact_name}.")
    if exact:
        return {"ContactID": str(exact[0]["ContactID"])}
    return {"Name": contact_name}


def _mirror_authorised_bill(
    connection,
    row: dict[str, Any],
    *,
    document_metadata: dict[str, Any] | None = None,
) -> None:
    """Upsert the local bill mirror so _exact_outstanding_bills sees a bill we just
    created without waiting for the next connector sync (mirrors the field mapping
    in external_connectors._upsert_xero_invoices)."""

    external_record_id = str(row.get("InvoiceID") or "")
    if not external_record_id or str(row.get("Status") or "").upper() != BILL_STATUS_AUTHORISED:
        return
    contact = row.get("Contact") if isinstance(row.get("Contact"), dict) else {}
    line_items = row.get("LineItems") if isinstance(row.get("LineItems"), list) else []
    first_line = line_items[0] if line_items and isinstance(line_items[0], dict) else {}
    ExternalFinancialRecord.objects.update_or_create(
        provider=ExternalServiceProvider.XERO,
        external_account_id=connection.external_account_id,
        external_record_id=external_record_id,
        defaults={
            "record_type": ExternalFinancialRecord.RECORD_XERO_BILL,
            "connection": connection,
            "financial_account": None,
            "user": connection.user,
            "organization": connection.organization,
            "currency": str(row.get("CurrencyCode") or ""),
            "amount": _money(row.get("AmountDue")) or _money(row.get("Total")),
            "direction": "debit",
            "status": BILL_STATUS_AUTHORISED,
            "posted_at": timezone.now(),
            "transaction_date": _iso_date(row.get("DateString")),
            "description": str(first_line.get("Description") or "") or "Xero bill",
            "merchant_name": str(contact.get("Name") or ""),
            "category": "bill",
            "class_name": "ACCPAY",
            "raw_payload": {
                **row,
                "_mlai_document_metadata": document_metadata or {},
            },
        },
    )


def _validate_authorised_lines(connection, xero_payload: dict[str, Any]) -> None:
    """Resolve every account/tax tuple against current Xero metadata before PUT."""

    lines = xero_payload.get("LineItems") if isinstance(xero_payload.get("LineItems"), list) else []
    account_response = http_client.get(
        f"{XERO_API_URL}/Accounts",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    account_response.raise_for_status()
    accounts = account_response.json().get("Accounts", []) or []
    tax_response = http_client.get(
        f"{XERO_API_URL}/TaxRates",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    tax_response.raise_for_status()
    tax_rates = tax_response.json().get("TaxRates", []) or []
    for index, line in enumerate(lines):
        code = str(line.get("AccountCode") or "").strip()
        account_matches = [
            row
            for row in accounts
            if str(row.get("Code") or "").strip().casefold() == code.casefold()
            and str(row.get("Status") or "ACTIVE").upper() == "ACTIVE"
        ]
        if len(account_matches) != 1:
            raise XeroPostingError(
                f"Xero does not contain one active account with code {code} for bill line {index + 1}."
            )
        requested_tax = str(line.get("TaxType") or "").strip()
        tax_matches = [
            row
            for row in tax_rates
            if (
                str(row.get("TaxType") or "").strip().casefold()
                == requested_tax.casefold()
                or str(row.get("Name") or "").strip().casefold()
                == requested_tax.casefold()
            )
            and str(row.get("Status") or "ACTIVE").upper() == "ACTIVE"
            and row.get("CanApplyToExpenses") is not False
            and row.get("TaxType")
        ]
        if len(tax_matches) != 1:
            raise XeroPostingError(
                f"Xero does not contain one active expense tax rate {requested_tax} for bill line {index + 1}."
            )
        line["TaxType"] = str(tax_matches[0]["TaxType"])


def create_reconciliation_bill(
    organization,
    *,
    payload: dict[str, Any],
    requested_by_slack_id: str,
) -> dict[str, Any]:
    """Create (or idempotently recover) one ACCPAY bill in Xero."""

    preview = build_reconciliation_bill_preview(organization, payload=payload)
    if not preview["ready"]:
        raise ReconciliationValidationError(
            "Bill request is not ready to post.", errors=preview["errors"]
        )
    profile, connection = _profile_and_connection(organization)
    invoice_number = preview["invoice_number"]
    contact_name = preview["contact_name"]

    existing = _existing_bill_for_contact(
        connection, invoice_number=invoice_number, contact_name=contact_name
    )
    if existing is not None:
        existing_total = _money(existing.get("Total"))
        requested_total = _money(preview["total"])
        if existing_total is not None and requested_total is not None and existing_total != requested_total:
            raise XeroPostingError(
                f"Xero already has bill {invoice_number} for {contact_name} with total "
                f"{existing_total}, not {requested_total}. Review it before creating another."
            )
        tracking = resolve_xero_tracking_assignment(
            connection, profile, preview.get("effective_tracking")
        )
        if tracking:
            full_response = http_client.get(
                f"{XERO_API_URL}/Invoices/{existing['InvoiceID']}",
                headers=_xero_headers(connection),
                timeout=(3, 30),
            )
            full_response.raise_for_status()
            full_bill = _first_xero_row(full_response.json(), "Invoices")
            _ensure_bill_tracking(connection, bill=full_bill, tracking=tracking[0])
        _mirror_authorised_bill(
            connection,
            existing,
            document_metadata=preview["document_metadata"],
        )
        return {
            "created": False,
            "bill": _serialize_bill_row(existing),
            "warnings": preview["warnings"],
            "document_metadata": preview["document_metadata"],
            "requested_by": requested_by_slack_id,
        }

    xero_payload = dict(preview["xero_payload"])
    tracking = resolve_xero_tracking_assignment(
        connection, profile, preview.get("effective_tracking")
    )
    if tracking:
        for line_item in xero_payload.get("LineItems") or []:
            line_item["Tracking"] = tracking
    xero_payload["Contact"] = _contact_payload(connection, contact_name)
    if xero_payload["Status"] == BILL_STATUS_AUTHORISED:
        _validate_authorised_lines(connection, xero_payload)
    headers = _xero_headers(connection)
    idempotency_seed = f"{organization.id}|{contact_name.casefold()}|{invoice_number.casefold()}"
    headers["Idempotency-Key"] = (
        f"mlai-bill-{hashlib.sha256(idempotency_seed.encode('utf-8')).hexdigest()[:32]}"
    )
    response = http_client.put(
        f"{XERO_API_URL}/Invoices",
        headers=headers,
        json={"Invoices": [xero_payload]},
        timeout=(3, 30),
    )
    response.raise_for_status()
    row = _first_xero_row(response.json(), "Invoices")
    if not str(row.get("InvoiceID") or "").strip():
        raise XeroPostingError("Xero did not return an InvoiceID.")
    _mirror_authorised_bill(
        connection,
        row,
        document_metadata=preview["document_metadata"],
    )
    return {
        "created": True,
        "bill": _serialize_bill_row(row),
        "warnings": preview["warnings"],
        "document_metadata": preview["document_metadata"],
        "requested_by": requested_by_slack_id,
    }


def attach_reconciliation_document(
    organization,
    *,
    payload: dict[str, Any],
    requested_by_slack_id: str,
) -> dict[str, Any]:
    """Attach a source document (PDF/image) to a Xero invoice or bank transaction."""

    _, connection = _profile_and_connection(organization)
    errors: list[str] = []

    entity_type = _clean_text(payload.get("xero_entity_type"), limit=32).lower()
    resource = ATTACHMENT_ENTITIES.get(entity_type)
    if resource is None:
        errors.append("xero_entity_type must be one of: " + ", ".join(sorted(ATTACHMENT_ENTITIES)))
    xero_id = _clean_text(payload.get("xero_id"), limit=64)
    if not xero_id or not _XERO_GUID_RE.fullmatch(xero_id):
        errors.append("xero_id must be a Xero identifier.")
    filename = _clean_text(payload.get("filename"), limit=100)
    filename = filename.replace("/", "_").replace("\\", "_")
    if not filename or "." not in filename:
        errors.append("filename must include an extension, e.g. invoice.pdf.")
    content_type = _clean_text(payload.get("content_type"), limit=100).lower() or "application/pdf"
    if content_type not in ALLOWED_ATTACHMENT_CONTENT_TYPES:
        errors.append(
            "content_type must be one of: " + ", ".join(sorted(ALLOWED_ATTACHMENT_CONTENT_TYPES))
        )
    try:
        raw = base64.b64decode(str(payload.get("content_base64") or ""), validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if not raw:
        errors.append("content_base64 must be non-empty base64 content.")
    try:
        declared_size = int(payload.get("size_bytes"))
    except (TypeError, ValueError):
        declared_size = 0
    if declared_size <= 0:
        errors.append("size_bytes must be a positive integer.")
    elif raw and declared_size != len(raw):
        errors.append("size_bytes does not match the decoded attachment content.")
    declared_hash = str(payload.get("content_sha256") or "").strip().lower()
    actual_hash = hashlib.sha256(raw).hexdigest() if raw else ""
    if not re.fullmatch(r"[0-9a-f]{64}", declared_hash):
        errors.append("content_sha256 must be a lowercase SHA-256 value.")
    elif actual_hash and declared_hash != actual_hash:
        errors.append("content_sha256 does not match the decoded attachment content.")
    max_bytes = int(getattr(settings, "XERO_ATTACHMENT_MAX_BYTES", 10 * 1024 * 1024))
    if raw and len(raw) > max_bytes:
        errors.append(f"Attachment exceeds the {max_bytes // (1024 * 1024)} MB limit.")
    if not xero_has_attachments_scope(connection.scopes):
        errors.append("Reconnect Xero with accounting.attachments before attaching documents.")
    if errors:
        raise ReconciliationValidationError("Attachment request is not valid.", errors=errors)

    listing = http_client.get(
        f"{XERO_API_URL}/{resource}/{xero_id}/Attachments",
        headers=_xero_headers(connection),
        timeout=(3, 30),
    )
    listing.raise_for_status()
    for row in listing.json().get("Attachments", []) or []:
        if str(row.get("FileName") or "").casefold() == filename.casefold():
            existing_size = int(row.get("ContentLength") or 0)
            if existing_size and existing_size != len(raw):
                raise ReconciliationValidationError(
                    "Xero already has an attachment with this filename but different content.",
                    errors=[
                        f"Existing size is {existing_size} bytes; requested size is {len(raw)} bytes."
                    ],
                )
            return {
                "created": False,
                "attachment": {
                    "attachment_id": str(row.get("AttachmentID") or ""),
                    "filename": str(row.get("FileName") or filename),
                    "size": existing_size or len(raw),
                    "content_type": str(row.get("MimeType") or content_type),
                    "content_sha256": declared_hash,
                    "entity_type": entity_type,
                    "xero_id": xero_id,
                },
                "requested_by": requested_by_slack_id,
            }

    headers = _xero_headers(connection)
    headers["Content-Type"] = content_type
    idempotency_seed = f"{organization.id}|{entity_type}|{xero_id}|{filename.casefold()}|{declared_hash}"
    headers["Idempotency-Key"] = (
        f"mlai-attachment-{hashlib.sha256(idempotency_seed.encode('utf-8')).hexdigest()[:32]}"
    )
    response = http_client.put(
        f"{XERO_API_URL}/{resource}/{xero_id}/Attachments/{quote(filename, safe='')}",
        headers=headers,
        data=raw,
        timeout=(3, 60),
    )
    response.raise_for_status()
    rows = response.json().get("Attachments", []) or []
    row = rows[0] if rows and isinstance(rows[0], dict) else {}
    return {
        "created": True,
        "attachment": {
            "attachment_id": str(row.get("AttachmentID") or ""),
            "filename": str(row.get("FileName") or filename),
            "size": int(row.get("ContentLength") or len(raw)),
            "content_type": str(row.get("MimeType") or content_type),
            "content_sha256": declared_hash,
            "entity_type": entity_type,
            "xero_id": xero_id,
        },
        "requested_by": requested_by_slack_id,
    }
