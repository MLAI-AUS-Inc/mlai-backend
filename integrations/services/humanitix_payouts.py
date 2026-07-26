"""Humanitix payout-report import and safe Xero Receive Money preview.

Humanitix's public API does not expose payout records.  The global Payouts CSV
is therefore the accounting source of truth.  Import is idempotent by payout
reference, stores no attendee data, and never writes to Xero.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, TextIO

from django.db import transaction
from django.utils import timezone

from integrations import http_client
from integrations.models import (
    ExternalServiceConnection,
    ExternalServiceProvider,
    HumanitixEvent,
    HumanitixPayout,
    HumanitixPayoutLine,
    ReconciliationMapping,
    ReconciliationProfile,
)
from integrations.services.xero_reconciliation import (
    XERO_API_URL,
    ReconciliationValidationError,
    XeroPostingError,
    _created_tracking_option,
    _tracking_category_options,
    _xero_headers,
    xero_has_bank_transaction_scope,
    xero_has_settings_write_scope,
)


MONEY_QUANTUM = Decimal("0.01")


class HumanitixPayoutImportError(RuntimeError):
    pass


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _row_map(row: dict[str, Any]) -> dict[str, Any]:
    return {_header_key(key): value for key, value in row.items()}


def _value(row: dict[str, Any], *aliases: str) -> Any:
    mapped = _row_map(row)
    for alias in aliases:
        key = _header_key(alias)
        if key in mapped and str(mapped[key] or "").strip():
            return mapped[key]
    return ""


def _money(value: Any) -> Decimal:
    raw = str(value or "").strip()
    if not raw:
        return Decimal("0.00")
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("()").replace(",", "").replace("$", "").replace("AUD", "").strip()
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise HumanitixPayoutImportError(f"Invalid money value: {value}")
    if negative:
        parsed = -parsed
    return parsed.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _positive_deduction(value: Any) -> Decimal:
    return abs(_money(value))


def _parse_date(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = re.sub(r"(?<=\d)(?:st|nd|rd|th)\b", "", raw, flags=re.IGNORECASE)
    normalized = re.sub(r"^[A-Za-z]{3,9}\s+", "", normalized).strip()
    candidates = list(
        dict.fromkeys(
            [
                raw,
                normalized,
                normalized.split(",", 1)[0].strip(),
            ]
        )
    )
    for fmt in (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        for candidate in candidates:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    raise HumanitixPayoutImportError(f"Invalid date value: {value}")


def _stable_hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _first_nonempty(rows: list[dict[str, Any]], *aliases: str) -> Any:
    for row in rows:
        value = _value(row, *aliases)
        if str(value or "").strip():
            return value
    return ""


def _sum_money(rows: list[dict[str, Any]], *aliases: str, deduction: bool = False) -> Decimal:
    parser = _positive_deduction if deduction else _money
    return sum((parser(_value(row, *aliases)) for row in rows), Decimal("0.00")).quantize(
        MONEY_QUANTUM
    )


def _payout_amount(rows: list[dict[str, Any]]) -> Decimal:
    values = [
        _money(_value(row, "payout amount", "amount paid", "net payout", "payment amount", "amount"))
        for row in rows
        if str(
            _value(row, "payout amount", "amount paid", "net payout", "payment amount", "amount")
            or ""
        ).strip()
    ]
    if not values:
        return Decimal("0.00")
    unique = set(values)
    # Global payout exports commonly repeat the total on each event/component
    # row.  Distinct values indicate per-row payout amounts and are summed.
    return values[0] if len(unique) == 1 else sum(values, Decimal("0.00")).quantize(MONEY_QUANTUM)


def _event_for_row(
    *,
    organization,
    row: dict[str, Any],
) -> tuple[HumanitixEvent | None, str, str, list[str]]:
    external_event_id = str(
        _value(row, "event id", "eventid", "humanitix event id", "event identifier") or ""
    ).strip()
    event_name = str(_value(row, "event", "event name", "event title") or "").strip()
    warnings: list[str] = []
    if external_event_id:
        event = HumanitixEvent.objects.filter(
            organization=organization,
            external_event_id=external_event_id,
        ).first()
        if event is not None:
            return event, event.external_event_id, event_name or event.event_name, warnings

        normalized = _normalized_name(event_name)
        matches = [
            candidate
            for candidate in HumanitixEvent.objects.filter(organization=organization)
            if _normalized_name(candidate.event_name) == normalized
        ]
        if len(matches) == 1:
            event = matches[0]
            warnings.append(
                f"Humanitix report event ID {external_event_id} was linked by exact event name."
            )
            return event, event.external_event_id, event_name or event.event_name, warnings
        if not event_name:
            warnings.append(
                f"Humanitix event ID {external_event_id} is not in the synced catalogue."
            )
        elif not matches:
            warnings.append(
                f'Humanitix report event ID {external_event_id} and event "{event_name}" '
                "are not in the synced catalogue."
            )
        else:
            warnings.append(
                f'Humanitix report event ID {external_event_id} has an ambiguous event name '
                f'"{event_name}".'
            )
        return None, external_event_id, event_name, warnings

    normalized = _normalized_name(event_name)
    matches = [
        event
        for event in HumanitixEvent.objects.filter(organization=organization)
        if _normalized_name(event.event_name) == normalized
    ]
    if len(matches) == 1:
        return matches[0], matches[0].external_event_id, event_name, warnings
    if not event_name:
        warnings.append("Payout row is missing an event name.")
    elif not matches:
        warnings.append(f'Humanitix event "{event_name}" is not in the synced catalogue.')
    else:
        warnings.append(f'Humanitix event name "{event_name}" is ambiguous.')
    return None, "", event_name, warnings


def _component_values(row: dict[str, Any]) -> dict[str, Decimal]:
    total_sales = _money(
        _value(
            row,
            "sales via humanitix payments",
            "humanitix sales",
            "online sales",
            "sales",
        )
    )
    box_office = _money(
        _value(row, "sales via box office card payments", "box office card sales", "box office sales")
    )
    donations = _money(_value(row, "additional donations", "donations", "donation"))
    add_ons = _money(_value(row, "add-on sales", "add ons", "addons", "add-on earnings"))
    explicit_ticket_sales = _value(row, "ticket sales", "ticket earnings")
    if str(explicit_ticket_sales or "").strip():
        ticket_sales = _money(explicit_ticket_sales)
    else:
        ticket_sales = max(total_sales + box_office - donations - add_ons, Decimal("0.00"))
    return {
        HumanitixPayoutLine.COMPONENT_TICKET_SALES: ticket_sales,
        HumanitixPayoutLine.COMPONENT_DONATIONS: donations,
        HumanitixPayoutLine.COMPONENT_ADD_ONS: add_ons,
        HumanitixPayoutLine.COMPONENT_REFUNDS: -_positive_deduction(
            _value(row, "refunds", "refund amount")
        ),
        HumanitixPayoutLine.COMPONENT_ABSORBED_FEES: -_positive_deduction(
            _value(row, "absorbed humanitix fees", "absorbed fees", "fees absorbed")
        ),
        HumanitixPayoutLine.COMPONENT_ADJUSTMENTS: _money(
            _value(row, "adjustments", "adjustment amount")
        ),
    }


def _ensure_event_mapping(event: HumanitixEvent) -> ReconciliationMapping:
    mapping, created = ReconciliationMapping.objects.get_or_create(
        organization=event.organization,
        source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
        source_id=event.external_event_id,
        defaults={
            "source_label": event.event_name,
            "accounting_treatment": ReconciliationMapping.TREATMENT_REVENUE,
            "event_tracking_option_name": event.event_name[:255],
            "active": True,
        },
    )
    changed = []
    if not mapping.source_label:
        mapping.source_label = event.event_name
        changed.append("source_label")
    if not mapping.event_tracking_option_name:
        mapping.event_tracking_option_name = event.event_name[:255]
        changed.append("event_tracking_option_name")
    if created is False and changed:
        mapping.save(update_fields=[*changed, "updated_at"])
    return mapping


def import_payout_rows(
    *,
    organization,
    connection: ExternalServiceConnection,
    rows: Iterable[dict[str, Any]],
) -> list[HumanitixPayout]:
    if connection.provider != ExternalServiceProvider.HUMANITIX:
        raise HumanitixPayoutImportError("Connection is not a Humanitix connection.")
    if connection.organization_id != organization.id:
        raise HumanitixPayoutImportError("Humanitix connection belongs to another organisation.")

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_row in rows:
        row = dict(raw_row or {})
        reference = str(
            _value(
                row,
                "payout reference",
                "payment reference",
                "bank reference",
                "reference",
                "payout id",
            )
            or ""
        ).strip()
        if not reference:
            if not any(str(value or "").strip() for value in row.values()):
                continue
            raise HumanitixPayoutImportError("Every payout row must include a payout reference.")
        grouped[reference].append(row)

    imported: list[HumanitixPayout] = []
    for reference, payout_rows in grouped.items():
        payout_amount = _payout_amount(payout_rows)
        payout_date = _parse_date(
            _first_nonempty(
                payout_rows,
                "payout date",
                "paid date",
                "date paid",
                "payment date",
                "date",
            )
        )
        cleared_date = _parse_date(
            _first_nonempty(payout_rows, "cleared date", "bank cleared date")
        )
        currency = str(_first_nonempty(payout_rows, "currency") or "AUD").upper().strip()
        humanitix_sales = _sum_money(
            payout_rows,
            "sales via humanitix payments",
            "humanitix sales",
            "online sales",
            "sales",
        )
        box_office_sales = _sum_money(
            payout_rows,
            "sales via box office card payments",
            "box office card sales",
            "box office sales",
        )
        refunds = _sum_money(payout_rows, "refunds", "refund amount", deduction=True)
        absorbed_fees = _sum_money(
            payout_rows,
            "absorbed humanitix fees",
            "absorbed fees",
            "fees absorbed",
            deduction=True,
        )
        adjustments = _sum_money(payout_rows, "adjustments", "adjustment amount")
        safe_source = {
            "payout_reference": reference,
            "payout_date": payout_date.isoformat() if payout_date else None,
            "cleared_date": cleared_date.isoformat() if cleared_date else None,
            "currency": currency,
            "payout_amount": str(payout_amount),
            "humanitix_sales": str(humanitix_sales),
            "box_office_card_sales": str(box_office_sales),
            "refunds": str(refunds),
            "absorbed_fees": str(absorbed_fees),
            "adjustments": str(adjustments),
            "row_count": len(payout_rows),
        }
        warnings: list[str] = []
        component_columns_present = any(
            str(
                _value(
                    row,
                    "sales via humanitix payments",
                    "humanitix sales",
                    "online sales",
                    "sales",
                    "ticket sales",
                    "additional donations",
                    "donations",
                    "add-on sales",
                    "absorbed humanitix fees",
                    "refunds",
                    "adjustments",
                )
                or ""
            ).strip()
            for row in payout_rows
        )
        component_total = sum(
            (
                sum(_component_values(row).values(), Decimal("0.00"))
                for row in payout_rows
            ),
            Decimal("0.00"),
        ).quantize(MONEY_QUANTUM)
        expected = (
            component_total
            if component_columns_present
            else (
                humanitix_sales
                + box_office_sales
                - refunds
                - absorbed_fees
                + adjustments
            ).quantize(MONEY_QUANTUM)
        )
        if payout_amount == 0:
            warnings.append("Payout amount is missing or zero.")
        if component_columns_present and expected != payout_amount:
            warnings.append(
                f"Payout components total {expected} but the bank payout is {payout_amount}."
            )
        if not component_columns_present:
            warnings.append(
                "Payout export contains only a net amount; import a payout breakdown before posting."
            )

        with transaction.atomic():
            payout, created = HumanitixPayout.objects.get_or_create(
                organization=organization,
                payout_reference=reference,
                defaults={"connection": connection},
            )
            previous_hash = payout.source_hash
            payout.connection = connection
            payout.payout_date = payout_date
            payout.cleared_date = cleared_date
            payout.currency = currency[:12]
            payout.payout_amount = payout_amount
            payout.humanitix_sales = humanitix_sales
            payout.box_office_card_sales = box_office_sales
            payout.refunds = refunds
            payout.absorbed_fees = absorbed_fees
            payout.adjustments = adjustments
            payout.source_payload = safe_source
            payout.source_hash = _stable_hash(safe_source)
            payout.preview_payload = {}
            payout.warnings = list(warnings)
            if payout.status != HumanitixPayout.STATUS_POSTED:
                payout.status = HumanitixPayout.STATUS_NEEDS_REVIEW
            elif previous_hash and previous_hash != payout.source_hash:
                payout.warnings.append(
                    "Humanitix source data changed after this payout was posted."
                )
            payout.save()
            if not created and payout.status != HumanitixPayout.STATUS_POSTED:
                payout.lines.all().delete()

            for row_number, row in enumerate(payout_rows, start=1):
                event, external_event_id, event_name, event_warnings = _event_for_row(
                    organization=organization,
                    row=row,
                )
                payout.warnings.extend(event_warnings)
                if event:
                    _ensure_event_mapping(event)
                components = _component_values(row) if component_columns_present else {}
                nonzero_components = [
                    (component, amount)
                    for component, amount in components.items()
                    if amount != 0
                ]
                if not nonzero_components:
                    if len(payout_rows) == 1:
                        net_amount = payout_amount
                    else:
                        net_amount = _money(
                            _value(row, "event payout amount", "row amount", "net event payout", "amount")
                        )
                    nonzero_components = [
                        (HumanitixPayoutLine.COMPONENT_NET_PAYOUT, net_amount)
                    ]
                for component, amount in nonzero_components:
                    HumanitixPayoutLine.objects.update_or_create(
                        payout=payout,
                        source_line_key=f"{row_number}:{external_event_id or _normalized_name(event_name)}:{component}",
                        defaults={
                            "event": event,
                            "external_event_id": external_event_id,
                            "event_name": event_name[:500],
                            "component": component,
                            "amount": amount,
                            "tax_amount": (
                                _money(_value(row, "tax", "tax amount", "gst"))
                                if component
                                == HumanitixPayoutLine.COMPONENT_TICKET_SALES
                                else Decimal("0.00")
                            ),
                            "metadata": {
                                "row_number": row_number,
                                "gateway_breakdown": (
                                    event.financial_summary.gateway_breakdown
                                    if event and hasattr(event, "financial_summary")
                                    else {}
                                ),
                            },
                        },
                    )
            payout.warnings = list(dict.fromkeys(payout.warnings))
            payout.save(update_fields=["warnings", "updated_at"])
        imported.append(payout)
    return imported


def import_payout_csv(
    *,
    organization,
    connection: ExternalServiceConnection,
    source: TextIO | str | bytes,
) -> list[HumanitixPayout]:
    if isinstance(source, bytes):
        stream = io.StringIO(source.decode("utf-8-sig"))
    elif isinstance(source, str):
        stream = io.StringIO(source)
    else:
        stream = source
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise HumanitixPayoutImportError("Humanitix payout CSV has no header row.")
    return import_payout_rows(
        organization=organization,
        connection=connection,
        rows=reader,
    )


def _tracking(profile: ReconciliationProfile, mapping: ReconciliationMapping) -> list[dict[str, str]]:
    if not (mapping.event_tracking_option_id or mapping.event_tracking_option_name):
        return []
    item = {
        "TrackingCategoryID": profile.event_tracking_category_id,
        "Name": profile.event_tracking_category_name,
        "TrackingOptionID": mapping.event_tracking_option_id,
        "Option": mapping.event_tracking_option_name,
    }
    return [{key: value for key, value in item.items() if value}]


def _xero_line(
    *,
    description: str,
    amount: Decimal,
    account_code: str,
    tax_type: str,
    tracking: list[dict[str, str]],
) -> dict[str, Any]:
    line = {
        "Description": description[:4000],
        "Quantity": 1,
        "UnitAmount": float(amount.quantize(MONEY_QUANTUM)),
        "AccountCode": account_code,
        "TaxType": tax_type,
    }
    if tracking:
        line["Tracking"] = tracking
    return line


def build_humanitix_xero_preview(payout: HumanitixPayout) -> dict[str, Any]:
    errors: list[str] = []
    try:
        profile = ReconciliationProfile.objects.select_related("xero_connection").get(
            organization=payout.organization
        )
    except ReconciliationProfile.DoesNotExist:
        profile = None
        errors.append("Reconciliation profile is not configured.")

    if profile:
        connection = profile.xero_connection
        if not profile.enabled:
            errors.append("Reconciliation is disabled for this organisation.")
        if connection is None:
            errors.append("A Xero connection must be selected.")
        elif not xero_has_bank_transaction_scope(connection.scopes):
            errors.append(
                "Reconnect Xero with accounting.banktransactions before posting."
            )
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
    lines: list[dict[str, Any]] = []
    line_total = Decimal("0.00")
    component_order = {
        HumanitixPayoutLine.COMPONENT_TICKET_SALES: 0,
        HumanitixPayoutLine.COMPONENT_DONATIONS: 1,
        HumanitixPayoutLine.COMPONENT_ADD_ONS: 2,
        HumanitixPayoutLine.COMPONENT_REFUNDS: 3,
        HumanitixPayoutLine.COMPONENT_ABSORBED_FEES: 4,
        HumanitixPayoutLine.COMPONENT_ADJUSTMENTS: 5,
        HumanitixPayoutLine.COMPONENT_NET_PAYOUT: 6,
    }
    payout_lines = sorted(
        payout.lines.select_related("event").all(),
        key=lambda line: (
            line.event_name.casefold(),
            component_order.get(line.component, 99),
            line.source_line_key,
        ),
    )
    for source_line in payout_lines:
        if source_line.event is None:
            errors.append(f'Map Humanitix payout row for "{source_line.event_name or "unknown event"}".')
            continue
        mapping = ReconciliationMapping.objects.filter(
            organization=payout.organization,
            source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            source_id=source_line.event.external_event_id,
            active=True,
        ).first()
        if mapping is None:
            errors.append(f"Map Humanitix event {source_line.event.event_name}.")
            continue
        if source_line.component == HumanitixPayoutLine.COMPONENT_NET_PAYOUT:
            errors.append(
                f"{source_line.event.event_name} has only a net payout amount; import its payout breakdown."
            )
            continue
        if profile is None:
            continue
        if source_line.component == HumanitixPayoutLine.COMPONENT_ABSORBED_FEES:
            account_code = profile.fee_account_code
            tax_type = profile.fee_tax_type
            label = "Humanitix absorbed fees"
        elif source_line.component == HumanitixPayoutLine.COMPONENT_REFUNDS:
            account_code = mapping.account_code or profile.refund_account_code
            tax_type = mapping.tax_type or profile.refund_tax_type
            label = "Humanitix refunds"
        else:
            account_code = mapping.account_code or profile.revenue_account_code
            tax_type = mapping.tax_type or profile.revenue_tax_type
            label = {
                HumanitixPayoutLine.COMPONENT_TICKET_SALES: "Humanitix ticket sales",
                HumanitixPayoutLine.COMPONENT_DONATIONS: "Humanitix donations",
                HumanitixPayoutLine.COMPONENT_ADD_ONS: "Humanitix add-ons",
                HumanitixPayoutLine.COMPONENT_ADJUSTMENTS: "Humanitix adjustment",
            }.get(source_line.component, "Humanitix revenue")
        lines.append(
            _xero_line(
                description=f"{label} — {source_line.event.event_name}",
                amount=source_line.amount,
                account_code=account_code,
                tax_type=tax_type,
                tracking=_tracking(profile, mapping),
            )
        )
        line_total += source_line.amount

    line_total = line_total.quantize(MONEY_QUANTUM)
    if line_total != payout.payout_amount:
        errors.append(
            f"Xero lines total {line_total} but the Humanitix payout is {payout.payout_amount}."
        )
    if payout.warnings:
        errors.extend(
            warning
            for warning in payout.warnings
            if "changed after" not in warning.lower()
        )

    if profile:
        contact = (
            {"ContactID": profile.humanitix_contact_id}
            if profile.humanitix_contact_id
            else {"Name": profile.humanitix_contact_name or "Humanitix"}
        )
        payload = {
            "Type": "RECEIVE",
            "Contact": contact,
            "BankAccount": {"AccountID": profile.xero_bank_account_id},
            "Date": (
                payout.cleared_date
                or payout.payout_date
                or date.today()
            ).isoformat(),
            "Reference": payout.payout_reference,
            "CurrencyCode": payout.currency,
            "LineAmountTypes": profile.line_amount_types,
            "Status": "AUTHORISED",
            "LineItems": lines,
        }
    else:
        payload = {}
    preview = {
        "ready": not errors,
        "errors": list(dict.fromkeys(errors)),
        "payout_reference": payout.payout_reference,
        "expected_total": str(payout.payout_amount),
        "line_total": str(line_total),
        "xero_payload": payload,
        "human_reconciliation_required": True,
        "note": (
            "Execution will create a matching Receive Money transaction; "
            "a human must still click Match/OK in Xero."
        ),
    }
    payout.preview_payload = preview
    if payout.status != HumanitixPayout.STATUS_POSTED:
        payout.status = (
            HumanitixPayout.STATUS_READY
            if preview["ready"]
            else HumanitixPayout.STATUS_NEEDS_REVIEW
        )
    payout.save(update_fields=["preview_payload", "status", "updated_at"])
    return preview


def ensure_humanitix_tracking_options(
    payout: HumanitixPayout,
    *,
    profile: ReconciliationProfile | None = None,
) -> list[ReconciliationMapping]:
    """Resolve approved Humanitix event names to Xero tracking option IDs."""
    profile = profile or ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=payout.organization
    )
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    event_ids = {
        event_id
        for event_id in payout.lines.values_list("external_event_id", flat=True)
        if event_id
    }
    mappings = list(
        ReconciliationMapping.objects.filter(
            organization=payout.organization,
            source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            source_id__in=event_ids,
            active=True,
        )
    )
    missing = [
        mapping
        for mapping in mappings
        if mapping.event_tracking_option_name and not mapping.event_tracking_option_id
    ]
    if not missing:
        return mappings
    if not xero_has_settings_write_scope(connection.scopes):
        raise ReconciliationValidationError(
            "Reconnect Xero with accounting.settings before posting missing Event Name options."
        )
    if not profile.event_tracking_category_id:
        raise ReconciliationValidationError(
            "Configure the Xero Event Name tracking category ID."
        )

    headers = _xero_headers(connection)
    response = http_client.get(
        f"{XERO_API_URL}/TrackingCategories",
        headers=headers,
        timeout=(3, 30),
    )
    response.raise_for_status()
    payload = response.json()
    categories = payload.get("TrackingCategories") if isinstance(payload, dict) else []
    categories = [item for item in categories or [] if isinstance(item, dict)]
    for mapping in missing:
        option = next(
            (
                item
                for item in _tracking_category_options(
                    categories,
                    profile.event_tracking_category_id,
                )
                if str(item.get("Name") or "").strip().casefold()
                == mapping.event_tracking_option_name.strip().casefold()
            ),
            None,
        )
        if option is None:
            create_response = http_client.put(
                (
                    f"{XERO_API_URL}/TrackingCategories/"
                    f"{profile.event_tracking_category_id}/Options"
                ),
                headers=headers,
                json={"Options": [{"Name": mapping.event_tracking_option_name}]},
                timeout=(3, 30),
            )
            create_response.raise_for_status()
            option = _created_tracking_option(create_response.json())
        option_id = str(
            option.get("TrackingOptionID") or option.get("OptionID") or ""
        ).strip()
        if not option_id:
            raise XeroPostingError(
                f"Xero did not return an ID for tracking option {mapping.event_tracking_option_name}."
            )
        mapping.event_tracking_option_id = option_id
        mapping.save(update_fields=["event_tracking_option_id", "updated_at"])
    return mappings


def post_humanitix_xero_bank_transaction(
    payout: HumanitixPayout,
    *,
    approved_by_slack_id: str,
) -> HumanitixPayout:
    """Create one idempotent Xero Receive Money transaction for a reviewed payout."""
    current = HumanitixPayout.objects.get(pk=payout.pk)
    if current.xero_bank_transaction_id:
        return current
    preview = build_humanitix_xero_preview(current)
    if not preview["ready"]:
        raise ReconciliationValidationError(
            "Humanitix payout is not ready to post.",
            errors=preview["errors"],
        )
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=current.organization
    )
    connection = profile.xero_connection
    if connection is None:
        raise ReconciliationValidationError("A Xero connection must be selected.")
    ensure_humanitix_tracking_options(current, profile=profile)
    preview = build_humanitix_xero_preview(current)
    if not preview["ready"]:
        raise ReconciliationValidationError(
            "Humanitix payout is not ready to post.",
            errors=preview["errors"],
        )

    with transaction.atomic():
        locked = HumanitixPayout.objects.select_for_update().get(pk=current.pk)
        if locked.xero_bank_transaction_id:
            return locked
        if locked.status == HumanitixPayout.STATUS_POSTING:
            raise ReconciliationValidationError("This Humanitix payout is already being posted.")
        locked.status = HumanitixPayout.STATUS_POSTING
        locked.approved_by_slack_id = approved_by_slack_id
        locked.approved_at = timezone.now()
        locked.last_error = ""
        locked.save(
            update_fields=[
                "status",
                "approved_by_slack_id",
                "approved_at",
                "last_error",
                "updated_at",
            ]
        )

    try:
        headers = _xero_headers(connection)
        reference_hash = hashlib.sha256(
            current.payout_reference.encode("utf-8")
        ).hexdigest()[:32]
        headers["Idempotency-Key"] = f"humanitix-payout-{reference_hash}"
        escaped_reference = current.payout_reference.replace('"', '""')
        existing_response = http_client.get(
            f"{XERO_API_URL}/BankTransactions",
            headers=headers,
            params={"where": f'Reference=="{escaped_reference}"'},
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
                raise XeroPostingError(
                    "; ".join(messages) or "Xero rejected the Humanitix bank transaction."
                )
        transaction_id = str(
            bank_transaction.get("BankTransactionID") or ""
        ).strip()
        if not transaction_id:
            raise XeroPostingError("Xero did not return a BankTransactionID.")
    except Exception as exc:
        HumanitixPayout.objects.filter(pk=current.pk).update(
            status=HumanitixPayout.STATUS_FAILED,
            last_error=str(exc)[:2000],
            updated_at=timezone.now(),
        )
        if isinstance(exc, (ReconciliationValidationError, XeroPostingError)):
            raise
        raise XeroPostingError(
            "Unable to create the Humanitix Xero bank transaction."
        ) from exc

    HumanitixPayout.objects.filter(pk=current.pk).update(
        status=HumanitixPayout.STATUS_POSTED,
        xero_bank_transaction_id=transaction_id,
        posted_at=timezone.now(),
        last_error="",
        updated_at=timezone.now(),
    )
    return HumanitixPayout.objects.get(pk=current.pk)


def serialize_humanitix_payout(
    payout: HumanitixPayout,
    *,
    include_payload: bool = False,
) -> dict[str, Any]:
    result = {
        "payout_reference": payout.payout_reference,
        "payout_date": payout.payout_date.isoformat() if payout.payout_date else None,
        "cleared_date": payout.cleared_date.isoformat() if payout.cleared_date else None,
        "currency": payout.currency,
        "payout_amount": str(payout.payout_amount),
        "status": payout.status,
        "warnings": payout.warnings,
        "approved_by_slack_id": payout.approved_by_slack_id,
        "approved_at": payout.approved_at.isoformat() if payout.approved_at else None,
        "xero_bank_transaction_id": payout.xero_bank_transaction_id,
        "posted_at": payout.posted_at.isoformat() if payout.posted_at else None,
        "last_error": payout.last_error,
        "lines": [
            {
                "event_id": line.external_event_id,
                "event_name": line.event_name,
                "component": line.component,
                "amount": str(line.amount),
                "tax_amount": str(line.tax_amount),
            }
            for line in payout.lines.all()
        ],
    }
    if include_payload:
        result["source"] = payout.source_payload
        result["preview"] = payout.preview_payload
    return result
