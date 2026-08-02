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
    fetch_xero_bank_transactions,
)


REPORT_VERSION = "reconciliation-profitability-v1"


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
