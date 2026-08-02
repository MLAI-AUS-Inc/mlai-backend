"""Period-bounded, source-traceable reconciliation profitability reporting."""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.db.models import Q

from integrations.models import (
    HumanitixEvent,
    HumanitixEventFinancialSummary,
    HumanitixPayout,
    HumanitixPayoutLine,
    ReconciliationMapping,
    ReconciliationProfile,
    StripePayoutReconciliation,
)
from integrations.services.xero_reconciliation import (
    _matching_xero_transactions,
    build_event_cashflow_validation,
    build_event_revenue_rollup,
    fetch_xero_accounts,
    fetch_xero_bank_transactions,
)
from startup_updates.models import LumaEventSelection


REPORT_VERSION = "reconciliation-profitability-v1"
EVENT_FINANCE_AUDIT_VERSION = "reconciliation-event-finance-audit-v1"

EVENT_FINANCE_CATEGORY_SPECS = (
    {
        "key": "ticket_sales",
        "label": "Ticket sales",
        "kind": "revenue",
        "transaction_type": "RECEIVE",
        "xero_account_type": "REVENUE",
        "account_names": ("Ticket Sales",),
    },
    {
        "key": "sponsorship_revenue",
        "label": "Sponsorship revenue",
        "kind": "revenue",
        "transaction_type": "RECEIVE",
        "xero_account_type": "REVENUE",
        "account_names": (
            "Sponsorships & Grants",
            "Sponsorships and Grants",
        ),
    },
    {
        "key": "catering_cost",
        "label": "Catering cost",
        "kind": "cost",
        "transaction_type": "SPEND",
        "xero_account_type": "EXPENSE",
        "account_names": (
            "Catering / Food & Beverages",
            "Catering / Food and Beverages",
        ),
    },
    {
        "key": "contractor_cost",
        "label": "Contractor cost",
        "kind": "cost",
        "transaction_type": "SPEND",
        "xero_account_type": "EXPENSE",
        "account_names": ("Contractor Expenses",),
    },
)


def _cents(value: Any) -> int:
    return int(
        (Decimal(str(value or "0")) * 100).quantize(
            Decimal("1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return (
        value.replace(year=value.year + 1, month=1, day=1)
        if value.month == 12
        else value.replace(month=value.month + 1, day=1)
    )


def _month_periods(period_start: date, period_end: date) -> list[tuple[date, date]]:
    periods = []
    cursor = _month_start(period_start)
    while cursor <= period_end:
        next_month = _next_month(cursor)
        periods.append(
            (
                max(cursor, period_start),
                min(period_end, date.fromordinal(next_month.toordinal() - 1)),
            )
        )
        cursor = next_month
    return periods


def _humanitix_revenue_rollup(
    *,
    organization,
    period_start: date,
    period_end: date,
) -> list[dict[str, Any]]:
    events = list(
        HumanitixEvent.objects.filter(
            organization=organization,
        )
        .filter(
            Q(start_at__date__gte=period_start, start_at__date__lte=period_end)
            | Q(start_at__isnull=True, last_synced_at__date__gte=period_start, last_synced_at__date__lte=period_end)
        )
        .select_related("financial_summary")
        .order_by("start_at", "external_event_id")
    )
    if not events:
        return []
    mappings = {
        item.source_id: item
        for item in ReconciliationMapping.objects.filter(
            organization=organization,
            source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            active=True,
        )
    }
    event_ids = [event.external_event_id for event in events]
    payout_references: dict[str, set[str]] = {}
    for line in HumanitixPayoutLine.objects.filter(
        payout__organization=organization,
        payout__payout_date__gte=period_start,
        payout__payout_date__lte=period_end,
        external_event_id__in=event_ids,
    ).select_related("payout"):
        payout_references.setdefault(line.external_event_id, set()).add(
            line.payout.payout_reference
        )

    rows = []
    for event in events:
        try:
            summary = event.financial_summary
        except HumanitixEventFinancialSummary.DoesNotExist:
            continue
        native_gross = 0
        native_net = 0
        native_refunds = 0
        native_orders = 0
        for values in (summary.gateway_breakdown or {}).values():
            if not isinstance(values, dict):
                continue
            if values.get("classification") != "humanitix_native":
                continue
            native_gross += _cents(values.get("gross_sales"))
            native_net += _cents(values.get("net_sales"))
            native_refunds += abs(_cents(values.get("refunds")))
            native_orders += int(values.get("orders") or 0)
        if not any((native_gross, native_net, native_refunds)):
            continue
        mapping = mappings.get(event.external_event_id)
        revenue_after_refunds = native_gross - native_refunds
        estimated_fees = max(revenue_after_refunds - native_net, 0)
        rows.append(
            {
                "source_type": ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
                "source_id": event.external_event_id,
                "source_label": event.event_name,
                "mapping_status": "approved" if mapping else "missing",
                "event_name": (
                    mapping.event_tracking_option_name
                    if mapping and mapping.event_tracking_option_name
                    else event.event_name
                ),
                "project_name": (
                    mapping.project_tracking_option_name if mapping else ""
                ),
                "gross_cents": native_gross,
                "refunds_cents": -native_refunds,
                "platform_fee_cents": estimated_fees,
                "net_cash_contribution_cents": native_net,
                "native_order_count": native_orders,
                "gateway_breakdown": summary.gateway_breakdown,
                "payout_references": sorted(
                    payout_references.get(event.external_event_id, set())
                ),
                "source_hash": summary.source_hash,
                "event_start_at": event.start_at.isoformat() if event.start_at else None,
                "historical_costs_complete": False,
            }
        )
    return rows


def _source_for_xero_line(line: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_type": "xero_bank_transaction_line",
        "source_id": str(line.get("line_id") or ""),
        "bank_transaction_id": str(line.get("bank_transaction_id") or ""),
        "date": line.get("date"),
        "transaction_type": str(line.get("transaction_type") or ""),
        "account_code": str(line.get("account_code") or ""),
        "account_name": str(line.get("account_name") or ""),
        "description": str(line.get("description") or ""),
        "reference": str(line.get("reference") or ""),
        "contact_name": str(line.get("contact_name") or ""),
        "signed_cents": int(line.get("signed_cents") or 0),
    }


def _profitability_status(profit_cents: int) -> str:
    if profit_cents < 0:
        return "negative"
    if profit_cents == 0:
        return "break_even"
    return "positive"


def _add_contribution(
    groups: dict[tuple[str, str], dict[str, Any]],
    *,
    dimension_type: str,
    dimension_name: str,
    revenue_cents: int,
    cost_cents: int,
    sources: list[dict[str, Any]],
    flags: list[str],
    mapping_required: bool = False,
    humanitix_related: bool = False,
) -> None:
    dimension_name = str(dimension_name or "").strip()
    if not dimension_name:
        return
    key = (dimension_type, dimension_name.casefold())
    row = groups.setdefault(
        key,
        {
            "dimension_type": dimension_type,
            "dimension_name": dimension_name,
            "revenue_cents": 0,
            "cost_cents": 0,
            "sources": [],
            "validation_flags": [],
            "mapping_required": False,
            "humanitix_related": False,
        },
    )
    row["revenue_cents"] += int(revenue_cents)
    row["cost_cents"] += int(cost_cents)
    row["sources"].extend(sources)
    row["validation_flags"].extend(flags)
    row["mapping_required"] = row["mapping_required"] or mapping_required
    row["humanitix_related"] = row["humanitix_related"] or humanitix_related


def _dimension_report(
    *,
    cashflow: dict[str, Any],
    humanitix_revenue: list[dict[str, Any]],
    humanitix_profitability_included: bool,
) -> dict[str, Any]:
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    unmapped_sources = []
    for row in cashflow.get("rows") or []:
        revenue_cents = (
            int(row.get("gross_cents") or 0)
            + int(row.get("refunds_cents") or 0)
            + int(row.get("xero_other_income_cents") or 0)
        )
        cost_cents = int(row.get("stripe_fee_cents") or 0) + int(
            row.get("xero_cost_cents") or 0
        )
        sources = [
            {
                "source_type": str(row.get("source_type") or "stripe_ledger"),
                "source_id": str(row.get("source_id") or ""),
                "payout_ids": list(row.get("payout_ids") or []),
                "gross_cents": int(row.get("gross_cents") or 0),
                "refunds_cents": int(row.get("refunds_cents") or 0),
                "fee_cents": int(row.get("stripe_fee_cents") or 0),
            },
            *[_source_for_xero_line(line) for line in row.get("xero_lines") or []],
        ]
        mapping_required = row.get("mapping_status") == "missing"
        if not (row.get("event_name") or row.get("project_name")):
            unmapped_sources.append(
                {
                    "source_type": row.get("source_type"),
                    "source_id": row.get("source_id"),
                    "source_label": row.get("source_label"),
                    "revenue_cents": revenue_cents,
                    "cost_cents": cost_cents,
                    "classification": "mapping_required",
                    "sources": sources,
                }
            )
        for dimension_type, field in (
            ("event", "event_name"),
            ("project", "project_name"),
        ):
            _add_contribution(
                groups,
                dimension_type=dimension_type,
                dimension_name=row.get(field),
                revenue_cents=revenue_cents,
                cost_cents=cost_cents,
                sources=sources,
                flags=list(row.get("validation_flags") or []),
                mapping_required=mapping_required,
            )

    for row in cashflow.get("unmatched_xero_tracking") or []:
        sources = [
            _source_for_xero_line(line) for line in row.get("xero_lines") or []
        ]
        for dimension_type, field in (
            ("event", "event_name"),
            ("project", "project_name"),
        ):
            _add_contribution(
                groups,
                dimension_type=dimension_type,
                dimension_name=row.get(field),
                revenue_cents=int(row.get("xero_other_income_cents") or 0),
                cost_cents=int(row.get("xero_cost_cents") or 0),
                sources=sources,
                flags=[str(row.get("validation_flag") or "")],
            )

    for row in humanitix_revenue:
        revenue_cents = int(row.get("gross_cents") or 0) + int(
            row.get("refunds_cents") or 0
        )
        cost_cents = int(row.get("platform_fee_cents") or 0)
        sources = [
            {
                "source_type": ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
                "source_id": row["source_id"],
                "source_hash": row.get("source_hash") or "",
                "payout_references": row.get("payout_references") or [],
                "gateway_breakdown": row.get("gateway_breakdown") or {},
                "gross_cents": int(row.get("gross_cents") or 0),
                "refunds_cents": int(row.get("refunds_cents") or 0),
                "estimated_platform_fee_cents": cost_cents,
            }
        ]
        for dimension_type, field in (
            ("event", "event_name"),
            ("project", "project_name"),
        ):
            _add_contribution(
                groups,
                dimension_type=dimension_type,
                dimension_name=row.get(field),
                revenue_cents=revenue_cents,
                cost_cents=cost_cents,
                sources=sources,
                flags=["humanitix_historical_costs_incomplete"],
                mapping_required=row.get("mapping_status") == "missing",
                humanitix_related=True,
            )

    dimensions = {"events": [], "projects": []}
    for row in sorted(
        groups.values(),
        key=lambda item: (item["dimension_type"], item["dimension_name"].casefold()),
    ):
        profit_cents = row["revenue_cents"] - row["cost_cents"]
        excluded_by_policy = (
            row["humanitix_related"]
            and not humanitix_profitability_included
        )
        included = not row["mapping_required"] and not excluded_by_policy
        if row["mapping_required"]:
            status = "mapping_required"
        elif excluded_by_policy:
            status = "excluded_by_policy"
        else:
            status = _profitability_status(profit_cents)
        output = {
            **row,
            "profit_cents": profit_cents,
            "profitability_included": included,
            "profitability_status": status,
            "profit_margin_percent": (
                str(
                    (
                        Decimal(profit_cents)
                        * Decimal("100")
                        / Decimal(row["revenue_cents"])
                    ).quantize(Decimal("0.01"))
                )
                if included and row["revenue_cents"] > 0
                else None
            ),
            "tie_out_cents": (
                profit_cents
                - (row["revenue_cents"] - row["cost_cents"])
            ),
            "validation_flags": sorted(
                {item for item in row["validation_flags"] if item}
            ),
        }
        dimensions["events" if row["dimension_type"] == "event" else "projects"].append(output)

    summaries = {}
    for key, rows in dimensions.items():
        visible_revenue = sum(row["revenue_cents"] for row in rows)
        visible_costs = sum(row["cost_cents"] for row in rows)
        eligible = [row for row in rows if row["profitability_included"]]
        eligible_revenue = sum(row["revenue_cents"] for row in eligible)
        eligible_costs = sum(row["cost_cents"] for row in eligible)
        eligible_profit = eligible_revenue - eligible_costs
        summaries[key] = {
            "row_count": len(rows),
            "visible_revenue_cents": visible_revenue,
            "visible_cost_cents": visible_costs,
            "visible_profit_cents": visible_revenue - visible_costs,
            "eligible_revenue_cents": eligible_revenue,
            "eligible_cost_cents": eligible_costs,
            "eligible_profit_cents": eligible_profit,
            "profit_margin_percent": (
                str(
                    (
                        Decimal(eligible_profit)
                        * Decimal("100")
                        / Decimal(eligible_revenue)
                    ).quantize(Decimal("0.01"))
                )
                if eligible_revenue > 0
                else None
            ),
            "profitability_status": (
                _profitability_status(eligible_profit)
                if eligible_revenue > 0
                else "unavailable"
            ),
            "excluded_row_count": len(rows) - len(eligible),
            "tie_out_cents": eligible_profit - (eligible_revenue - eligible_costs),
        }
    return {
        "dimensions": dimensions,
        "summaries": summaries,
        "unmapped_sources": unmapped_sources,
    }


def _stripe_transfer_previews(
    records: list[StripePayoutReconciliation],
    *,
    profile: ReconciliationProfile,
    bank_transactions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    previews = []
    for record in records:
        matches = _matching_xero_transactions(record, profile, bank_transactions)
        previews.append(
            {
                "payout_id": record.payout_id,
                "existing_transactions": [
                    {
                        "bank_transaction_id": str(
                            transaction.get("BankTransactionID") or ""
                        ).strip()
                    }
                    for transaction, _basis in matches
                ],
            }
        )
    return previews


def _build_period(
    *,
    organization,
    profile: ReconciliationProfile,
    period_start: date,
    period_end: date,
    stripe_records: list[StripePayoutReconciliation],
    bank_transactions: list[dict[str, Any]],
    humanitix_transfer_ids: set[str],
    account_names_by_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    records = [
        record
        for record in stripe_records
        if record.arrival_date
        and period_start <= record.arrival_date <= period_end
    ]
    event_revenue = build_event_revenue_rollup(records)
    cashflow = build_event_cashflow_validation(
        event_revenue=event_revenue,
        bank_transactions=bank_transactions,
        payout_previews=_stripe_transfer_previews(
            records,
            profile=profile,
            bank_transactions=bank_transactions,
        ),
        profile=profile,
        period_start=period_start,
        period_end=period_end,
        excluded_transfer_transaction_ids=humanitix_transfer_ids,
        account_names_by_code=account_names_by_code,
    )
    humanitix_revenue = _humanitix_revenue_rollup(
        organization=organization,
        period_start=period_start,
        period_end=period_end,
    )
    report = _dimension_report(
        cashflow=cashflow,
        humanitix_revenue=humanitix_revenue,
        humanitix_profitability_included=(
            profile.humanitix_profitability_included
        ),
    )
    return {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        **report,
        "validation": {
            "status_counts": cashflow.get("status_counts") or {},
            "negative_count": int(cashflow.get("negative_count") or 0),
            "mapping_required_count": int(
                cashflow.get("mapping_required_count") or 0
            ),
            "xero_stripe_coding_incomplete_count": int(
                cashflow.get("xero_stripe_coding_incomplete_count") or 0
            ),
            "excluded_payout_transfer_line_count": len(
                cashflow.get("excluded_payout_transfer_lines") or []
            ),
        },
    }


def build_reconciliation_profitability_report(
    *,
    organization,
    period_start: date,
    period_end: date,
    bank_transactions: list[dict[str, Any]] | None = None,
    account_names_by_code: dict[str, str] | None = None,
) -> dict[str, Any]:
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=organization
    )
    bank_transactions = (
        fetch_xero_bank_transactions(profile)
        if bank_transactions is None
        else bank_transactions
    )
    stripe_records = list(
        StripePayoutReconciliation.objects.filter(
            organization=organization,
            arrival_date__gte=period_start,
            arrival_date__lte=period_end,
        ).order_by("arrival_date", "id")
    )
    humanitix_transfer_ids = {
        value
        for value in HumanitixPayout.objects.filter(
            organization=organization,
        ).exclude(xero_bank_transaction_id="").values_list(
            "xero_bank_transaction_id",
            flat=True,
        )
        if value
    }
    report = _build_period(
        organization=organization,
        profile=profile,
        period_start=period_start,
        period_end=period_end,
        stripe_records=stripe_records,
        bank_transactions=bank_transactions,
        humanitix_transfer_ids=humanitix_transfer_ids,
        account_names_by_code=account_names_by_code,
    )
    monthly = []
    for month_start, month_end in _month_periods(period_start, period_end):
        month = _build_period(
            organization=organization,
            profile=profile,
            period_start=month_start,
            period_end=month_end,
            stripe_records=stripe_records,
            bank_transactions=bank_transactions,
            humanitix_transfer_ids=humanitix_transfer_ids,
            account_names_by_code=account_names_by_code,
        )
        monthly.append(
            {
                "month": month_start.strftime("%Y-%m"),
                "period_start": month["period_start"],
                "period_end": month["period_end"],
                "events": month["summaries"]["events"],
                "projects": month["summaries"]["projects"],
            }
        )
    return {
        "schema_version": 1,
        "report_version": REPORT_VERSION,
        "report_type": "cashflow_profitability_estimate",
        "accounting_basis": (
            "Stripe payout ledgers plus tracked Xero Receive/Spend Money lines; "
            "this is a cashflow estimate, not an accrual profit and loss report."
        ),
        "limitations": [
            "Bills and journals absent from Xero bank transactions are not included.",
            "Untracked Xero lines cannot be assigned to an event or project.",
            "Humanitix historical costs are incomplete and excluded by default.",
        ],
        "policy": {
            "humanitix_profitability_included": (
                profile.humanitix_profitability_included
            ),
            "verified_by_slack_id": (
                profile.profitability_policy_verified_by_slack_id
            ),
            "verified_at": (
                profile.profitability_policy_verified_at.isoformat()
                if profile.profitability_policy_verified_at
                else None
            ),
        },
        **report,
        "monthly": monthly,
        "classification_counts": dict(
            sorted(
                Counter(
                    row["profitability_status"]
                    for rows in report["dimensions"].values()
                    for row in rows
                ).items()
            )
        ),
        "xero_writes": False,
    }


def _resolved_event_finance_accounts(
    accounts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    available = []
    for account in accounts:
        if str(account.get("Status") or "").strip().upper() == "ARCHIVED":
            continue
        available.append(
            {
                "account_code": str(account.get("Code") or "").strip(),
                "account_name": str(account.get("Name") or "").strip(),
                "account_type": str(account.get("Type") or "").strip().upper(),
            }
        )

    resolved = {}
    for spec in EVENT_FINANCE_CATEGORY_SPECS:
        match = None
        for alias in spec["account_names"]:
            match = next(
                (
                    account
                    for account in available
                    if account["account_name"].casefold() == alias.casefold()
                    and (
                        not account["account_type"]
                        or account["account_type"] == spec["xero_account_type"]
                    )
                ),
                None,
            )
            if match:
                break
        resolved[spec["key"]] = {
            "account_code": match["account_code"] if match else "",
            "account_name": match["account_name"] if match else "",
            "account_type": match["account_type"] if match else "",
            "match_basis": "exact_account_name" if match else "unresolved",
        }
    return resolved


def _event_catalog(
    *,
    organization,
    period_start: date,
    period_end: date,
) -> dict[str, dict[str, Any]]:
    mappings = {
        (mapping.source_type, mapping.source_id): mapping
        for mapping in ReconciliationMapping.objects.filter(
            organization=organization,
            source_type__in=(
                ReconciliationMapping.SOURCE_LUMA_EVENT,
                ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            ),
            active=True,
        )
    }
    events: dict[str, dict[str, Any]] = {}

    def add_event(
        *,
        source_type: str,
        source_id: str,
        event_name: str,
        event_url: str,
        start_at,
        extra: dict[str, Any],
    ) -> None:
        mapping = mappings.get((source_type, source_id))
        canonical_name = str(
            mapping.event_tracking_option_name if mapping else event_name
        ).strip()
        if not canonical_name:
            return
        key = canonical_name.casefold()
        row = events.setdefault(
            key,
            {
                "event_name": canonical_name,
                "start_at": None,
                "source_catalogs": [],
                "discovery_sources": [],
                "financial_sources": [],
            },
        )
        start_at_text = start_at.isoformat() if start_at else None
        if start_at_text and (
            not row["start_at"] or start_at_text < row["start_at"]
        ):
            row["start_at"] = start_at_text
        catalog_entry = {
            "source_type": source_type,
            "source_id": source_id,
            "source_event_name": event_name,
            "event_url": event_url,
            "start_at": start_at_text,
            "mapping_status": "approved" if mapping else "missing",
            **extra,
        }
        if not any(
            item["source_type"] == source_type and item["source_id"] == source_id
            for item in row["source_catalogs"]
        ):
            row["source_catalogs"].append(catalog_entry)
        if source_type not in row["discovery_sources"]:
            row["discovery_sources"].append(source_type)

    for event in LumaEventSelection.objects.filter(
        organization=organization,
        selected=True,
        start_at__date__gte=period_start,
        start_at__date__lte=period_end,
    ).order_by("start_at", "event_id"):
        add_event(
            source_type=ReconciliationMapping.SOURCE_LUMA_EVENT,
            source_id=event.event_id,
            event_name=event.event_name,
            event_url=event.event_url,
            start_at=event.start_at,
            extra={"selected": True},
        )

    for event in HumanitixEvent.objects.filter(
        organization=organization,
        start_at__date__gte=period_start,
        start_at__date__lte=period_end,
    ).order_by("start_at", "external_event_id"):
        add_event(
            source_type=ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
            source_id=event.external_event_id,
            event_name=event.event_name,
            event_url=event.event_url,
            start_at=event.start_at,
            extra={
                "published": event.published,
                "archived": event.archived,
            },
        )
    return events


def _event_finance_evidence(
    source: dict[str, Any],
    *,
    category_key: str,
    resolved_account: dict[str, Any],
) -> dict[str, Any] | None:
    source_type = str(source.get("source_type") or "")
    if (
        category_key == "ticket_sales"
        and source_type
        in {
            ReconciliationMapping.SOURCE_LUMA_EVENT,
            ReconciliationMapping.SOURCE_HUMANITIX_EVENT,
        }
        and int(source.get("gross_cents") or 0) > 0
    ):
        return {
            "source_type": source_type,
            "source_id": str(source.get("source_id") or ""),
            "amount_cents": int(source.get("gross_cents") or 0),
            "amount_basis": "provider_gross_ticket_sales",
            "payout_ids": list(source.get("payout_ids") or []),
            "payout_references": list(source.get("payout_references") or []),
        }

    if source_type != "xero_bank_transaction_line":
        return None
    expected_code = str(resolved_account.get("account_code") or "").casefold()
    if not expected_code or str(source.get("account_code") or "").casefold() != expected_code:
        return None
    spec = next(
        item for item in EVENT_FINANCE_CATEGORY_SPECS if item["key"] == category_key
    )
    if str(source.get("transaction_type") or "").upper() != spec["transaction_type"]:
        return None
    return {
        "source_type": source_type,
        "source_id": str(source.get("source_id") or ""),
        "bank_transaction_id": str(source.get("bank_transaction_id") or ""),
        "date": source.get("date"),
        "transaction_type": str(source.get("transaction_type") or ""),
        "account_code": str(source.get("account_code") or ""),
        "account_name": str(
            source.get("account_name")
            or resolved_account.get("account_name")
            or ""
        ),
        "amount_cents": abs(int(source.get("signed_cents") or 0)),
        "amount_basis": "tracked_xero_bank_transaction_line",
        "description": str(source.get("description") or ""),
        "reference": str(source.get("reference") or ""),
        "contact_name": str(source.get("contact_name") or ""),
    }


def build_reconciliation_event_finance_audit(
    *,
    organization,
    period_start: date,
    period_end: date,
    bank_transactions: list[dict[str, Any]] | None = None,
    accounts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Audit expected event revenue/cost categories using provider and Xero evidence."""
    profile = ReconciliationProfile.objects.select_related("xero_connection").get(
        organization=organization
    )
    bank_transactions = (
        fetch_xero_bank_transactions(profile)
        if bank_transactions is None
        else bank_transactions
    )
    accounts = fetch_xero_accounts(profile) if accounts is None else accounts
    resolved_accounts = _resolved_event_finance_accounts(accounts)
    account_names_by_code = {
        str(account.get("Code") or "").strip(): str(account.get("Name") or "").strip()
        for account in accounts
        if str(account.get("Code") or "").strip()
    }
    profitability = build_reconciliation_profitability_report(
        organization=organization,
        period_start=period_start,
        period_end=period_end,
        bank_transactions=bank_transactions,
        account_names_by_code=account_names_by_code,
    )
    events = _event_catalog(
        organization=organization,
        period_start=period_start,
        period_end=period_end,
    )

    for dimension in profitability.get("dimensions", {}).get("events", []):
        event_name = str(dimension.get("dimension_name") or "").strip()
        if not event_name:
            continue
        row = events.setdefault(
            event_name.casefold(),
            {
                "event_name": event_name,
                "start_at": None,
                "source_catalogs": [],
                "discovery_sources": [],
                "financial_sources": [],
            },
        )
        row["financial_sources"].extend(dimension.get("sources") or [])
        for source in dimension.get("sources") or []:
            source_type = str(source.get("source_type") or "")
            discovery_source = (
                "xero_tracking"
                if source_type == "xero_bank_transaction_line"
                else source_type
            )
            if discovery_source and discovery_source not in row["discovery_sources"]:
                row["discovery_sources"].append(discovery_source)

    missing_counts = Counter()
    output_events = []
    for row in events.values():
        categories = {}
        present_categories = []
        missing_categories = []
        for spec in EVENT_FINANCE_CATEGORY_SPECS:
            evidence = [
                item
                for item in (
                    _event_finance_evidence(
                        source,
                        category_key=spec["key"],
                        resolved_account=resolved_accounts[spec["key"]],
                    )
                    for source in row["financial_sources"]
                )
                if item is not None
            ]
            status = "present" if evidence else "missing"
            categories[spec["key"]] = {
                "label": spec["label"],
                "kind": spec["kind"],
                "status": status,
                "evidence_count": len(evidence),
                "evidence": evidence,
            }
            if evidence:
                present_categories.append(spec["key"])
            else:
                missing_categories.append(spec["key"])
                missing_counts[spec["key"]] += 1
        output_events.append(
            {
                "event_name": row["event_name"],
                "start_at": row["start_at"],
                "source_catalogs": sorted(
                    row["source_catalogs"],
                    key=lambda item: (item["source_type"], item["source_id"]),
                ),
                "discovery_sources": sorted(row["discovery_sources"]),
                "categories": categories,
                "present_categories": present_categories,
                "missing_categories": missing_categories,
                "completeness_status": (
                    "complete" if not missing_categories else "incomplete"
                ),
                "evidence_flags": sorted(
                    {
                        flag
                        for source in row["financial_sources"]
                        for flag in source.get("validation_flags") or []
                    }
                ),
            }
        )
    output_events.sort(
        key=lambda item: (
            item["start_at"] is None,
            item["start_at"] or "",
            item["event_name"].casefold(),
        )
    )
    complete_count = sum(
        item["completeness_status"] == "complete" for item in output_events
    )
    unresolved_categories = [
        key
        for key, account in resolved_accounts.items()
        if account["match_basis"] == "unresolved"
    ]
    return {
        "schema_version": 1,
        "audit_version": EVENT_FINANCE_AUDIT_VERSION,
        "audit_type": "event_finance_completeness",
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "required_categories": [
            {
                "key": spec["key"],
                "label": spec["label"],
                "kind": spec["kind"],
            }
            for spec in EVENT_FINANCE_CATEGORY_SPECS
        ],
        "resolved_accounts": resolved_accounts,
        "account_resolution_warnings": unresolved_categories,
        "events": output_events,
        "summary": {
            "event_count": len(output_events),
            "complete_count": complete_count,
            "incomplete_count": len(output_events) - complete_count,
            "missing_counts": {
                spec["key"]: missing_counts.get(spec["key"], 0)
                for spec in EVENT_FINANCE_CATEGORY_SPECS
            },
        },
        "evidence_basis": (
            "Ticket sales are present when Luma/Humanitix-linked Stripe revenue or "
            "a tracked Xero Ticket Sales receipt exists. Other categories require "
            "tracked Xero bank-transaction lines on the resolved chart-of-accounts code."
        ),
        "limitations": [
            "Missing means no tracked evidence was found in this period; it does not prove the event had no such revenue or cost.",
            "Bills and journals absent from Xero bank transactions are not included.",
            "Untracked Xero lines cannot be assigned to an event.",
            "Humanitix historical costs may be incomplete.",
            "Category evidence amounts are source observations and must not be added across provider and Xero views.",
        ],
        "xero_writes": False,
    }
