from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Union

from django.db import transaction
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    ContentFactoryRunStep,
    ContentFactoryRunStepAttempt,
    ContentFactoryStepStatus,
)
from integrations.models import (
    ExternalFinancialRecord,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from startup_updates.models import (
    GmailMessageArtifact,
    GmailRelevanceLabel,
    SlackChannelSelection,
    StartupEvent,
    StartupMetricObservation,
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    GmailSyncCursor,
    StartupProfile,
    UserStartupBinding,
)
from integrations.services.valley_harness import notify_valley_run_created
from integrations.utils import normalize_domain


logger = logging.getLogger(__name__)

STARTUP_UPDATE_WORKFLOW = "startup_monthly_update"
DEFAULT_BACKFILL_MONTHS = 1
DEFAULT_CLASSIFICATION_BATCH_SIZE = 40
DEFAULT_ATTACHMENT_BYTES_LIMIT = 10 * 1024 * 1024
DEFAULT_MAX_SOURCE_THREADS = 40
SUPERSEDED_GMAIL_CONNECTION_ERROR = "Superseded by a newer Gmail connection."
MERGEABLE_DRAFT_LIST_SECTIONS = (
    "financial_performance",
    "highlights",
    "lowlights",
    "operations",
    "learnings",
    "next_30_days",
    "asks",
)
HIGH_SIGNAL_TERMS = [
    "arr",
    "mrr",
    "runway",
    "burn",
    "churn",
    "renewal",
    "expansion",
    "pilot",
    "contract",
    "launch",
    "hiring",
    "hire",
    "board",
    "investor",
    "fundraise",
    "fundraising",
    "term sheet",
    "partnership",
    "outage",
    "incident",
]
LOW_SIGNAL_PATTERNS = [
    "unsubscribe",
    "receipt",
    "invoice",
    "verification code",
    "one-time password",
    "otp",
    "calendar invitation",
    "zoom meeting",
    "google calendar",
    "newsletter",
    "promotion",
    "social",
]
HARD_IRRELEVANT_TEXT_PATTERNS = [
    "magic link",
    "verification code",
    "one-time password",
    "password reset",
    "weekly digest",
    "newsletter",
    "recommended for you",
    "top posts",
    "unsubscribe",
    "invitation",
    "calendar invitation",
    "receipt",
    "payment received",
    "order confirmation",
]
HARD_IRRELEVANT_SENDER_LOCALPARTS = {
    "noreply",
    "no-reply",
    "donotreply",
    "mailer-daemon",
    "notifications",
}
HARD_IRRELEVANT_GMAIL_LABELS = {
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_FORUMS",
}
HARD_IRRELEVANT_PRECEDENCE_VALUES = {"bulk", "list", "junk"}
OPEN_RUN_STATUSES = {
    ContentFactoryRunStatus.QUEUED,
    ContentFactoryRunStatus.RUNNING,
    ContentFactoryRunStatus.BLOCKED,
    ContentFactoryRunStatus.AWAITING_CONFIRMATION,
    ContentFactoryRunStatus.AWAITING_APPROVAL,
    ContentFactoryRunStatus.AWAITING_DELIVERY_MODE,
    ContentFactoryRunStatus.APPROVAL_REQUIRED,
}
STARTUP_UPDATE_CANCELLABLE_STATUSES = OPEN_RUN_STATUSES | {
    ContentFactoryRunStatus.FAILED,
    ContentFactoryRunStatus.DENIED,
}
RUN_CANCEL_BACKUPS_KEY = "_cancel_backups"
VALLEY_META_KEY = "_valley_meta"
RUN_STEP_ORDER = [
    "profile_resolution",
    "gmail_backfill",
    "relevance_classification",
    "thread_hydration",
    "event_extraction",
    "timeline_merge",
    "draft_generation",
    "groundedness_review",
]
SOURCE_REPROCESS_STEPS = {
    "timeline_merge",
    "draft_generation",
    "groundedness_review",
}
SLACK_STEP_KEYS = {
    "slack_backfill",
    "slack_event_extraction",
}


def normalize_startup_update_input_sources(input_sources: Optional[list[str]]) -> list[str]:
    allowed = {
        "gmail",
        ExternalServiceProvider.XERO,
        ExternalServiceProvider.BANK_FEED,
        ExternalServiceProvider.STRIPE,
        ExternalServiceProvider.NOTION,
        ExternalServiceProvider.GOOGLE_DRIVE,
        ExternalServiceProvider.SLACK,
    }
    if not input_sources:
        return ["gmail"]
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_source in input_sources:
        source = str(raw_source or "").strip().lower().replace("-", "_")
        if source not in allowed or source in seen:
            continue
        seen.add(source)
        normalized.append(source)
    return normalized or ["gmail"]


def gmail_required_for_sources(input_sources: Optional[list[str]]) -> bool:
    return "gmail" in normalize_startup_update_input_sources(input_sources)


def build_startup_update_step_order(input_sources: Optional[list[str]]) -> list[str]:
    selected = set(normalize_startup_update_input_sources(input_sources))
    steps = ["profile_resolution"]
    if "gmail" in selected:
        steps.extend([
            "gmail_backfill",
            "relevance_classification",
            "thread_hydration",
            "event_extraction",
        ])
    if ExternalServiceProvider.SLACK in selected:
        steps.extend(["slack_backfill", "slack_event_extraction"])
    steps.extend(["timeline_merge", "draft_generation", "groundedness_review"])
    return steps


def reconcile_startup_update_run_source_steps(
    *,
    run: ContentFactoryRun,
    input_sources: Optional[list[str]],
) -> ContentFactoryRun:
    selected_input_sources = normalize_startup_update_input_sources(input_sources)
    desired_step_order = build_startup_update_step_order(selected_input_sources)
    previous_step_order = list(run.step_order or [])
    previous_steps = set(previous_step_order)
    added_steps = [step for step in desired_step_order if step not in previous_steps]
    slack_was_added = any(step in SLACK_STEP_KEYS for step in added_steps)

    update_fields: list[str] = []
    if previous_step_order != desired_step_order:
        run.step_order = desired_step_order
        update_fields.append("step_order")

    if not run.current_step or run.current_step not in desired_step_order:
        run.current_step = desired_step_order[0]
        update_fields.append("current_step")
    elif slack_was_added and run.current_step in SOURCE_REPROCESS_STEPS:
        run.current_step = "slack_backfill"
        update_fields.append("current_step")

    if update_fields:
        run.save(update_fields=[*dict.fromkeys(update_fields), "updated_at"])

    existing_steps = {
        step.step_key: step
        for step in ContentFactoryRunStep.objects.filter(run=run)
    }
    for index, step_key in enumerate(desired_step_order):
        step = existing_steps.get(step_key)
        if step is None:
            ContentFactoryRunStep.objects.create(
                run=run,
                step_key=step_key,
                display_order=index,
                required=True,
                status=ContentFactoryStepStatus.PENDING,
            )
            continue
        if step.display_order != index:
            step.display_order = index
            step.save(update_fields=["display_order"])

    if slack_was_added:
        downstream_steps = ContentFactoryRunStep.objects.filter(
            run=run,
            step_key__in=SOURCE_REPROCESS_STEPS,
        )
        ContentFactoryRunStepAttempt.objects.filter(step__in=downstream_steps).delete()
        downstream_steps.update(
            status=ContentFactoryStepStatus.PENDING,
            attempts=0,
            message="",
            started_at=None,
            completed_at=None,
            error="",
            artifacts=[],
        )

    return run


def _uniq(values: Iterable[str]) -> list[str]:
    deduped = []
    seen = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def _append_note(existing: str, addition: str) -> str:
    existing_text = str(existing or "").strip()
    addition_text = str(addition or "").strip()
    if not addition_text:
        return existing_text
    if not existing_text:
        return addition_text
    if addition_text.lower() in existing_text.lower():
        return existing_text
    return f"{existing_text}\n\n{addition_text}"


def _match_any(values: Iterable[str], haystack: str) -> bool:
    return any(value and value in haystack for value in values)


def _competitor_name_domain_lists(raw_competitors) -> tuple[list[str], list[str]]:
    names = []
    domains = []
    for item in raw_competitors or []:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            domain = normalize_domain(item.get("domain") or item.get("url") or "")
            if name:
                names.append(name)
            if domain:
                domains.append(domain)
            continue
        text = str(item or "").strip()
        if not text:
            continue
        normalized = normalize_domain(text)
        if "." in normalized and " " not in normalized:
            domains.append(normalized)
        else:
            names.append(text)
    return _uniq(names), _uniq(domains)


def _month_start(value: Optional[Union[date, datetime]] = None) -> date:
    if value is None:
        value = timezone.now().date()
    if isinstance(value, datetime):
        value = value.date()
    return date(value.year, value.month, 1)


def iter_recent_month_starts(count: int, *, reference: Optional[Union[date, datetime]] = None) -> list[date]:
    current = _month_start(reference)
    months = []
    for offset in range(count):
        year = current.year
        month = current.month - offset
        while month <= 0:
            month += 12
            year -= 1
        months.append(date(year, month, 1))
    return list(reversed(months))


def build_bank_feed_run_context(*, organization: Organization, start_date: date, end_date: date) -> dict:
    records = (
        ExternalFinancialRecord.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.BANK_FEED,
            record_type=ExternalFinancialRecord.RECORD_BANK_TRANSACTION,
            status__iexact="posted",
            transaction_date__gte=start_date,
            transaction_date__lte=end_date,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        .select_related("financial_account")
        .order_by("-transaction_date", "-posted_at", "-id")
    )
    cash_received = Decimal("0")
    cash_spent = Decimal("0")
    currencies = set()
    notable = []

    for record in records:
        amount = record.amount or Decimal("0")
        if record.currency:
            currencies.add(record.currency)
        is_debit = record.direction.lower() == "debit" or amount < 0
        if is_debit:
            cash_spent += abs(amount)
        else:
            cash_received += abs(amount)
        notable.append(
            {
                "date": record.transaction_date.isoformat() if record.transaction_date else None,
                "description": record.merchant_name or record.description,
                "account": record.financial_account.account_label if record.financial_account else record.external_account_id,
                "amount": str(amount),
                "currency": record.currency,
                "direction": record.direction,
                "category": record.category,
            }
        )

    notable = sorted(
        notable,
        key=lambda item: abs(Decimal(str(item.get("amount") or "0"))),
        reverse=True,
    )[:10]
    return {
        "source": "bank_feed",
        "purpose": "cash_movement_context",
        "warning": "Bank Feed is cash validation context only and should not be used to calculate MRR by default.",
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "cash_received": str(cash_received),
        "cash_spent": str(cash_spent),
        "transaction_count": records.count(),
        "currencies": sorted(currencies),
        "notable_transactions": notable,
    }


def _monthly_normalized_xero_amount(record: ExternalFinancialRecord) -> Optional[Decimal]:
    amount = record.amount
    if amount is None:
        return None
    payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
    schedule = payload.get("Schedule") if isinstance(payload.get("Schedule"), dict) else payload.get("schedule")
    schedule = schedule if isinstance(schedule, dict) else {}
    unit = str(schedule.get("Unit") or schedule.get("unit") or "").upper()
    try:
        period = Decimal(str(schedule.get("Period") or schedule.get("period") or "1"))
    except Exception:
        period = Decimal("1")
    if period <= 0:
        period = Decimal("1")
    if unit in {"MONTHLY", "MONTH"}:
        return amount / period
    if unit in {"YEARLY", "YEAR"}:
        return amount / (Decimal("12") * period)
    if unit in {"WEEKLY", "WEEK"}:
        return amount * Decimal("52") / Decimal("12") / period
    if unit in {"DAILY", "DAY"}:
        return amount * Decimal("365") / Decimal("12") / period
    return amount


def build_xero_run_context(*, organization: Organization, start_date: date, end_date: date) -> dict:
    records = (
        ExternalFinancialRecord.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.XERO,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-transaction_date", "-posted_at", "-id")
    )
    recurring_records = list(records.filter(record_type=ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE)[:20])
    invoice_records = list(
        records.filter(
            record_type=ExternalFinancialRecord.RECORD_XERO_INVOICE,
            transaction_date__gte=start_date,
            transaction_date__lte=end_date,
        )[:20]
    )
    payment_records = list(
        records.filter(
            record_type=ExternalFinancialRecord.RECORD_XERO_PAYMENT,
            transaction_date__gte=start_date,
            transaction_date__lte=end_date,
        )
    )
    mrr = sum(
        (value for value in (_monthly_normalized_xero_amount(record) for record in recurring_records) if value is not None),
        Decimal("0"),
    )
    cash_collected = sum((abs(record.amount or Decimal("0")) for record in payment_records), Decimal("0"))
    currencies = sorted({record.currency for record in recurring_records + invoice_records + payment_records if record.currency})
    warnings = [
        "Use Xero as accounting validation and non-Stripe recurring revenue context; Stripe subscription data wins where both sources overlap."
    ]
    if len(currencies) > 1:
        warnings.append("Xero records include multiple currencies; do not combine them into one MRR value.")

    def summarize(record: ExternalFinancialRecord) -> dict:
        return {
            "date": record.transaction_date.isoformat() if record.transaction_date else None,
            "description": record.description,
            "contact": record.merchant_name,
            "amount": str(record.amount or Decimal("0")),
            "currency": record.currency,
            "status": record.status,
            "record_type": record.record_type,
        }

    return {
        "source": "xero",
        "purpose": "accounting_revenue_context",
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "estimated_mrr_from_repeating_invoices": str(mrr),
        "cash_collected": str(cash_collected),
        "currencies": currencies,
        "warnings": warnings,
        "recurring_invoices": [summarize(record) for record in recurring_records[:10]],
        "recent_sales_invoices": [summarize(record) for record in invoice_records[:10]],
        "recent_payments": [summarize(record) for record in payment_records[:10]],
    }


def _previous_month_start(month: date) -> date:
    if month.month == 1:
        return date(month.year - 1, 12, 1)
    return date(month.year, month.month - 1, 1)


def _decimal_sum(records: Iterable[ExternalFinancialRecord]) -> Decimal:
    return sum((record.amount or Decimal("0") for record in records), Decimal("0"))


def _format_decimal(value: Decimal, *, places: str = "0.01") -> str:
    return str(value.quantize(Decimal(places)))


def _format_money(value: Decimal, currency: str) -> str:
    amount = _format_decimal(value)
    return f"{currency} {amount}" if currency else amount


def _format_percent(value: Decimal) -> str:
    return f"{_format_decimal(value * Decimal('100'))}%"


def _record_ids(records: Iterable[ExternalFinancialRecord]) -> list[str]:
    return [str(record.external_record_id or record.id) for record in records]


def _metric_summary_metadata(
    *,
    source_metric: str,
    warnings: list[str],
    records: list[ExternalFinancialRecord],
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "source_metric": source_metric,
        "warnings": warnings,
        "source_record_count": len(records),
        **(extra or {}),
    }


def publish_xero_metric_observations(
    *,
    organization: Organization,
    run: Optional[ContentFactoryRun],
    start_date: date,
    end_date: date,
) -> dict[str, Any]:
    records = list(
        ExternalFinancialRecord.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.XERO,
        )
        .exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("transaction_date", "posted_at", "id")
    )
    recurring_records = [
        record for record in records
        if record.record_type == ExternalFinancialRecord.RECORD_XERO_REPEATING_INVOICE
    ]
    invoice_records = [
        record for record in records
        if record.record_type == ExternalFinancialRecord.RECORD_XERO_INVOICE
        and record.transaction_date
        and start_date <= record.transaction_date <= end_date
    ]
    payment_records = [
        record for record in records
        if record.record_type == ExternalFinancialRecord.RECORD_XERO_PAYMENT
        and record.transaction_date
        and start_date <= record.transaction_date <= end_date
    ]
    currencies = sorted({record.currency for record in recurring_records + invoice_records + payment_records if record.currency})
    warnings = [
        "Xero financial metrics are deterministic accounting context. Do not infer additional financial values."
    ]
    if len(currencies) > 1:
        warnings.append("Xero records include multiple currencies; metrics are emitted per currency.")

    current_month = _month_start(end_date)
    month_summaries: dict[str, dict[str, Any]] = {}
    saved_metric_ids: list[int] = []

    def records_for_currency(items: list[ExternalFinancialRecord], currency: str) -> list[ExternalFinancialRecord]:
        return [record for record in items if (record.currency or "") == currency]

    def records_for_month(items: list[ExternalFinancialRecord], month: date, currency: str) -> list[ExternalFinancialRecord]:
        return [
            record for record in items
            if (record.currency or "") == currency
            and record.transaction_date
            and _month_start(record.transaction_date) == month
        ]

    def save_metric(
        *,
        month: date,
        key: str,
        name: str,
        value_text: str,
        value_number: Optional[Decimal],
        unit: str,
        records_for_metric: list[ExternalFinancialRecord],
        summary: str,
        metadata: dict[str, Any],
        confidence: float = 1.0,
    ) -> None:
        metric, _created = StartupMetricObservation.objects.update_or_create(
            organization=organization,
            source_thread=None,
            source_provider=ExternalServiceProvider.XERO,
            metric_key=key,
            period_month=month,
            unit=unit,
            defaults={
                "run": run,
                "metric_name": name,
                "value_text": value_text,
                "value_number": value_number,
                "observed_at": timezone.now(),
                "confidence": confidence,
                "evidence_message_ids": [],
                "evidence_attachment_ids": [],
                "source_record_ids": _record_ids(records_for_metric),
                "source_metadata": metadata,
                "summary": summary,
            },
        )
        saved_metric_ids.append(metric.id)

    for currency in currencies or [""]:
        currency_recurring = records_for_currency(recurring_records, currency)
        current_recurring_for_month = records_for_month(currency_recurring, current_month, currency)
        current_recurring_metrics = current_recurring_for_month or currency_recurring
        current_mrr = sum(
            (value for value in (_monthly_normalized_xero_amount(record) for record in current_recurring_metrics) if value is not None),
            Decimal("0"),
        )
        if current_recurring_metrics:
            save_metric(
                month=current_month,
                key="mrr",
                name="MRR",
                value_text=_format_money(current_mrr, currency),
                value_number=current_mrr,
                unit=currency,
                records_for_metric=current_recurring_metrics,
                summary="MRR calculated from active Xero repeating invoices.",
                metadata=_metric_summary_metadata(source_metric="xero_repeating_invoice_mrr", warnings=warnings, records=current_recurring_metrics),
            )
            save_metric(
                month=current_month,
                key="recurringInvoiceCount",
                name="Recurring invoice count",
                value_text=str(len(current_recurring_metrics)),
                value_number=Decimal(len(current_recurring_metrics)),
                unit="count",
                records_for_metric=current_recurring_metrics,
                summary="Active Xero repeating invoice count.",
                metadata=_metric_summary_metadata(source_metric="xero_recurring_invoice_count", warnings=warnings, records=current_recurring_metrics),
            )

            previous_month = _previous_month_start(current_month)
            previous_recurring = records_for_month(currency_recurring, previous_month, currency)
            previous_mrr = sum(
                (value for value in (_monthly_normalized_xero_amount(record) for record in previous_recurring) if value is not None),
                Decimal("0"),
            )
            if previous_mrr > 0:
                growth = (current_mrr - previous_mrr) / previous_mrr
                save_metric(
                    month=current_month,
                    key="revenueGrowthRate",
                    name="MRR growth rate",
                    value_text=_format_percent(growth),
                    value_number=growth,
                    unit="ratio",
                    records_for_metric=current_recurring_metrics + previous_recurring,
                    summary="MRR growth calculated from current and previous Xero repeating invoice MRR.",
                    metadata=_metric_summary_metadata(
                        source_metric="xero_mrr_growth_rate",
                        warnings=warnings,
                        records=current_recurring_metrics + previous_recurring,
                        extra={"previous_month": previous_month.isoformat(), "previous_mrr": str(previous_mrr)},
                    ),
                )

        months_with_activity = sorted({
            _month_start(record.transaction_date)
            for record in invoice_records + payment_records
            if record.transaction_date and (record.currency or "") == currency
        })
        for month in months_with_activity:
            monthly_invoices = records_for_month(invoice_records, month, currency)
            monthly_payments = records_for_month(payment_records, month, currency)
            invoice_revenue = _decimal_sum(monthly_invoices)
            cash_collected = sum((abs(record.amount or Decimal("0")) for record in monthly_payments), Decimal("0"))

            if monthly_invoices:
                save_metric(
                    month=month,
                    key="invoiceRevenue",
                    name="Invoice revenue",
                    value_text=_format_money(invoice_revenue, currency),
                    value_number=invoice_revenue,
                    unit=currency,
                    records_for_metric=monthly_invoices,
                    summary="Sales invoice revenue calculated from Xero authorised and paid invoices.",
                    metadata=_metric_summary_metadata(source_metric="xero_invoice_revenue", warnings=warnings, records=monthly_invoices),
                )
                save_metric(
                    month=month,
                    key="invoiceCount",
                    name="Invoice count",
                    value_text=str(len(monthly_invoices)),
                    value_number=Decimal(len(monthly_invoices)),
                    unit="count",
                    records_for_metric=monthly_invoices,
                    summary="Sales invoice count from Xero.",
                    metadata=_metric_summary_metadata(source_metric="xero_invoice_count", warnings=warnings, records=monthly_invoices),
                )

            if monthly_payments:
                save_metric(
                    month=month,
                    key="cashCollected",
                    name="Cash collected",
                    value_text=_format_money(cash_collected, currency),
                    value_number=cash_collected,
                    unit=currency,
                    records_for_metric=monthly_payments,
                    summary="Cash collected calculated from Xero payments.",
                    metadata=_metric_summary_metadata(source_metric="xero_cash_collected", warnings=warnings, records=monthly_payments),
                )

            month_summaries.setdefault(month.isoformat(), {})[currency or "unknown"] = {
                "invoice_revenue": str(invoice_revenue),
                "cash_collected": str(cash_collected),
                "invoice_count": len(monthly_invoices),
                "payment_count": len(monthly_payments),
            }

    return {
        "source": "xero",
        "published_metric_ids": saved_metric_ids,
        "published_metric_count": len(saved_metric_ids),
        "months": month_summaries,
        "currencies": currencies,
        "warnings": warnings,
        "needs_review": bool(len(currencies) > 1),
    }


def build_slack_run_context(*, organization: Organization, selected_channel_ids: Optional[list[str]] = None) -> dict:
    queryset = SlackChannelSelection.objects.filter(
        organization=organization,
        selected=True,
    ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
    if selected_channel_ids:
        queryset = queryset.filter(channel_id__in=selected_channel_ids)
    channels = [
        {
            "channel_id": selection.channel_id,
            "channel_name": selection.channel_name,
            "is_private": bool(selection.is_private),
            "last_synced_at": selection.last_synced_at.isoformat() if selection.last_synced_at else None,
        }
        for selection in queryset.order_by("channel_name", "channel_id")
    ]
    return {
        "source": "slack",
        "purpose": "selected_channel_operating_context",
        "selected_channel_ids": [channel["channel_id"] for channel in channels],
        "selected_channel_count": len(channels),
        "selected_channels": channels,
        "warnings": [] if channels else ["Slack was selected but no channels are selected."],
    }


def build_external_context_for_sources(
    *,
    organization: Organization,
    input_sources: Optional[list[str]],
    start_date: date,
    end_date: date,
    source_warnings: Optional[dict[str, list[str]]] = None,
) -> dict[str, Any]:
    selected = set(input_sources or [])
    context: dict[str, Any] = {}
    warnings_by_source = source_warnings or {}
    if ExternalServiceProvider.BANK_FEED in selected:
        context["bank_feed"] = build_bank_feed_run_context(
            organization=organization,
            start_date=start_date,
            end_date=end_date,
        )
    if ExternalServiceProvider.XERO in selected:
        xero_context = build_xero_run_context(
            organization=organization,
            start_date=start_date,
            end_date=end_date,
        )
        if warnings_by_source.get(ExternalServiceProvider.XERO):
            xero_context.setdefault("warnings", []).extend(warnings_by_source[ExternalServiceProvider.XERO])
            xero_context["needs_review"] = True
        context["xero"] = xero_context
    if ExternalServiceProvider.SLACK in selected:
        context["slack"] = build_slack_run_context(
            organization=organization,
            selected_channel_ids=None,
        )
    return context


def refresh_startup_update_run_source_context(
    *,
    run: ContentFactoryRun,
    organization: Organization,
    input_sources: Optional[list[str]],
    start_date: date,
    end_date: date,
    source_warnings: Optional[dict[str, list[str]]] = None,
) -> ContentFactoryRun:
    run_request = dict(run.run_request or {})
    selected_input_sources = normalize_startup_update_input_sources(input_sources)
    reconcile_startup_update_run_source_steps(run=run, input_sources=selected_input_sources)
    if selected_input_sources:
        run_request["input_sources"] = list(selected_input_sources)
    if ExternalServiceProvider.SLACK in set(selected_input_sources):
        run_request["slack_channel_ids"] = [
            selection.channel_id
            for selection in SlackChannelSelection.objects.filter(
                organization=organization,
                selected=True,
            ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        ]
    external_context = build_external_context_for_sources(
        organization=organization,
        input_sources=selected_input_sources,
        start_date=start_date,
        end_date=end_date,
        source_warnings=source_warnings,
    )
    if ExternalServiceProvider.XERO in set(selected_input_sources or []):
        xero_metrics = publish_xero_metric_observations(
            organization=organization,
            run=run,
            start_date=start_date,
            end_date=end_date,
        )
        external_context.setdefault("xero", {}).setdefault("warnings", []).extend(xero_metrics.get("warnings", []))
        external_context.setdefault("xero", {})["published_metrics"] = xero_metrics
        if xero_metrics.get("needs_review"):
            external_context.setdefault("xero", {})["needs_review"] = True
    if external_context:
        run_request["external_context"] = external_context
    run.run_request = run_request
    run.save(update_fields=["run_request", "updated_at"])
    return run


def _serialize_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _get_run_result_payload(run: ContentFactoryRun) -> dict:
    payload = run.result or {}
    return payload if isinstance(payload, dict) else {}


def _set_run_result_payload(run: ContentFactoryRun, payload: dict) -> None:
    run.result = dict(payload or {})


def _get_run_meta(run: ContentFactoryRun) -> dict:
    meta = _get_run_result_payload(run).get(VALLEY_META_KEY) or {}
    return meta if isinstance(meta, dict) else {}


def _set_run_meta(run: ContentFactoryRun, meta: dict) -> None:
    payload = dict(_get_run_result_payload(run))
    payload[VALLEY_META_KEY] = dict(meta or {})
    _set_run_result_payload(run, payload)


def get_startup_update_run_cancel_backups(run: ContentFactoryRun) -> dict:
    backups = _get_run_result_payload(run).get(RUN_CANCEL_BACKUPS_KEY) or {}
    if not isinstance(backups, dict):
        backups = {}
    return {
        "drafts": dict(backups.get("drafts") or {}),
        "events": dict(backups.get("events") or {}),
        "metrics": dict(backups.get("metrics") or {}),
    }


def set_startup_update_run_cancel_backups(run: ContentFactoryRun, backups: dict) -> None:
    payload = dict(_get_run_result_payload(run))
    payload[RUN_CANCEL_BACKUPS_KEY] = {
        "drafts": dict((backups or {}).get("drafts") or {}),
        "events": dict((backups or {}).get("events") or {}),
        "metrics": dict((backups or {}).get("metrics") or {}),
    }
    _set_run_result_payload(run, payload)


def _serialize_event(event) -> dict:
    return {
        "id": event.id,
        "canonical_key": event.canonical_key,
        "event_type": event.event_type,
        "title": event.title,
        "summary": event.summary,
        "event_date": event.event_date.isoformat() if event.event_date else None,
        "month_bucket": event.month_bucket.isoformat(),
        "date_precision": event.date_precision,
        "sentiment": event.sentiment,
        "investor_importance": event.investor_importance,
        "quantitative_facts": event.quantitative_facts or [],
        "evidence_message_ids": event.evidence_message_ids or [],
        "evidence_attachment_ids": event.evidence_attachment_ids or [],
        "source_thread_ids": event.source_thread_ids or [],
        "confidence": event.confidence,
        "status": event.status,
        "needs_review": event.needs_review,
        "merge_notes": event.merge_notes or "",
    }


def _serialize_metric(metric) -> dict:
    return {
        "id": metric.id,
        "metric_key": metric.metric_key,
        "metric_name": metric.metric_name,
        "value_text": metric.value_text,
        "value_number": str(metric.value_number) if metric.value_number is not None else None,
        "unit": metric.unit,
        "observed_at": _serialize_datetime(metric.observed_at),
        "period_month": metric.period_month.isoformat(),
        "confidence": metric.confidence,
        "evidence_message_ids": metric.evidence_message_ids or [],
        "evidence_attachment_ids": metric.evidence_attachment_ids or [],
        "source_provider": metric.source_provider or "",
        "source_record_ids": metric.source_record_ids or [],
        "source_metadata": metric.source_metadata or {},
        "summary": metric.summary or "",
    }


def _message_haystack(artifact: GmailMessageArtifact) -> str:
    return " ".join(
        [
            getattr(artifact, "subject", "") or "",
            getattr(artifact, "snippet", "") or "",
            getattr(artifact, "cleaned_text", "") or "",
            getattr(artifact, "from_address", "") or "",
            " ".join(getattr(artifact, "to_addresses", []) or []),
            " ".join(getattr(artifact, "cc_addresses", []) or []),
            " ".join(getattr(artifact, "bcc_addresses", []) or []),
            " ".join(getattr(artifact, "reply_to_addresses", []) or []),
        ]
    ).lower()


def _participant_domains(artifact: GmailMessageArtifact) -> list[str]:
    participant_values = [getattr(artifact, "from_address", "") or ""]
    participant_values.extend(getattr(artifact, "to_addresses", []) or [])
    participant_values.extend(getattr(artifact, "cc_addresses", []) or [])
    participant_values.extend(getattr(artifact, "bcc_addresses", []) or [])
    participant_values.extend(getattr(artifact, "reply_to_addresses", []) or [])

    participant_domains = []
    for value in participant_values:
        if "@" not in value:
            continue
        participant_domains.append(normalize_domain(value.split("@")[-1]))
    return participant_domains


def _normalize_sender_localpart(value: str) -> str:
    localpart = str(value or "").split("@", 1)[0].lower()
    return "".join(char for char in localpart if char.isalnum())


def _header_values(artifact: GmailMessageArtifact) -> dict[str, str]:
    raw = getattr(artifact, "header_values", {}) or {}
    values = {}
    for key, value in raw.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key:
            values[normalized_key] = str(value or "").strip()
    return values


def _profile_signal_lists(profile: StartupProfile) -> dict[str, list[str]]:
    return {
        "domain_aliases": [normalize_domain(item) for item in (profile.domain_aliases or [])],
        "company_aliases": [item.lower() for item in (profile.company_aliases or [])],
        "founder_names": [item.lower() for item in (profile.founder_names or [])],
        "team_names": [item.lower() for item in (profile.team_names or [])],
        "investor_domains": [normalize_domain(item) for item in (profile.investor_domains or [])],
        "investor_names": [item.lower() for item in (profile.investor_names or [])],
        "customer_domains": [normalize_domain(item) for item in (profile.customer_domains or [])],
        "customer_names": [item.lower() for item in (profile.customer_names or [])],
        "prospect_domains": [normalize_domain(item) for item in (profile.prospect_domains or [])],
        "prospect_names": [item.lower() for item in (profile.prospect_names or [])],
        "competitor_domains": [normalize_domain(item) for item in (profile.competitor_domains or [])],
        "competitor_names": [item.lower() for item in (profile.competitor_names or [])],
        "positive_keywords": [item.lower() for item in (profile.positive_keywords or [])],
        "negative_keywords": [item.lower() for item in (profile.negative_keywords or [])],
    }


def _allowlist_override_reasons(
    *,
    haystack: str,
    participant_domains: list[str],
    profile_signals: dict[str, list[str]],
) -> list[str]:
    reasons = []
    if _match_any(profile_signals["company_aliases"] + profile_signals["positive_keywords"], haystack):
        reasons.append("allowlist_company_alias_or_positive_keyword")
    if _match_any(profile_signals["founder_names"] + profile_signals["team_names"], haystack):
        reasons.append("allowlist_founder_or_team_name")
    if _match_any(profile_signals["investor_names"], haystack):
        reasons.append("allowlist_investor_name")
    if _match_any(profile_signals["customer_names"] + profile_signals["prospect_names"], haystack):
        reasons.append("allowlist_customer_or_prospect_name")
    if _match_any(HIGH_SIGNAL_TERMS, haystack):
        reasons.append("allowlist_high_signal_term")
    if any(domain and domain in participant_domains for domain in profile_signals["domain_aliases"]):
        reasons.append("allowlist_company_domain")
    if any(domain and domain in participant_domains for domain in profile_signals["investor_domains"]):
        reasons.append("allowlist_investor_domain")
    if any(domain and domain in participant_domains for domain in profile_signals["customer_domains"] + profile_signals["prospect_domains"]):
        reasons.append("allowlist_customer_or_prospect_domain")
    return _uniq(reasons)


def _hard_irrelevant_reasons(artifact: GmailMessageArtifact, *, haystack: str) -> list[str]:
    reasons = []
    header_values = _header_values(artifact)
    label_ids = {str(item or "").strip().upper() for item in (getattr(artifact, "label_ids", []) or [])}

    if HARD_IRRELEVANT_GMAIL_LABELS.intersection(label_ids):
        reasons.append("hard_filtered_gmail_category")

    if header_values.get("list-id") or header_values.get("list-unsubscribe"):
        reasons.append("hard_filtered_bulk_header")

    precedence = header_values.get("precedence", "").lower()
    if precedence in HARD_IRRELEVANT_PRECEDENCE_VALUES:
        reasons.append("hard_filtered_bulk_header")

    auto_submitted = header_values.get("auto-submitted", "").lower()
    if auto_submitted and auto_submitted != "no":
        reasons.append("hard_filtered_auto_submitted")

    if _normalize_sender_localpart(getattr(artifact, "from_address", "") or "") in {
        "".join(char for char in item if char.isalnum()) for item in HARD_IRRELEVANT_SENDER_LOCALPARTS
    }:
        reasons.append("hard_filtered_no_reply_sender")

    if _match_any(HARD_IRRELEVANT_TEXT_PATTERNS, haystack):
        reasons.append("hard_filtered_low_signal_pattern")

    return _uniq(reasons)


def seed_startup_profile(profile: StartupProfile) -> StartupProfile:
    org = profile.organization
    config = getattr(org, "content_config", None)
    competitor_names, competitor_domains = _competitor_name_domain_lists(getattr(org, "competitors", []) or [])

    company_aliases = list(profile.company_aliases or [])
    if not company_aliases:
        company_aliases = _uniq([org.name, getattr(config, "brand_name", "") if config else ""])

    domain_aliases = list(profile.domain_aliases or [])
    if not domain_aliases:
        domain_aliases = _uniq([org.domain])

    positive_keywords = list(profile.positive_keywords or [])
    if not positive_keywords:
        positive_keywords = _uniq(
            list(getattr(org, "seed_keywords", []) or [])
            + company_aliases
            + list(profile.product_names or [])
        )

    fields_to_update = []
    defaults = {
        "company_aliases": company_aliases,
        "domain_aliases": domain_aliases,
        "competitor_names": list(profile.competitor_names or []) or competitor_names,
        "competitor_domains": list(profile.competitor_domains or []) or competitor_domains,
        "positive_keywords": positive_keywords,
    }
    for field_name, value in defaults.items():
        if getattr(profile, field_name) != value:
            setattr(profile, field_name, value)
            fields_to_update.append(field_name)

    if not profile.notes and config and getattr(config, "company_context", ""):
        profile.notes = str(config.company_context or "")
        fields_to_update.append("notes")

    if fields_to_update:
        fields_to_update.append("updated_at")
        profile.save(update_fields=fields_to_update)

    return profile


def sync_startup_profile_from_company(
    *,
    startup_profile: StartupProfile,
    organization: Organization,
    company,
    user,
) -> StartupProfile:
    startup_profile = seed_startup_profile(startup_profile)
    config = getattr(organization, "content_config", None)

    company_name = str(getattr(company, "name", "") or "").strip()
    if company_name and organization.name != company_name:
        organization.name = company_name
        organization.save(update_fields=["name"])

    founder_name = str(getattr(user, "full_name", "") or "").strip()
    competitor_names, competitor_domains = _competitor_name_domain_lists(
        getattr(organization, "competitors", []) or []
    )
    seed_keywords = list(getattr(organization, "seed_keywords", []) or [])

    update_fields = []
    merged_values = {
        "company_aliases": _uniq(
            [
                *(startup_profile.company_aliases or []),
                company_name,
                organization.name,
                getattr(config, "brand_name", "") if config else "",
            ]
        ),
        "domain_aliases": _uniq(
            [
                *(startup_profile.domain_aliases or []),
                organization.domain,
                normalize_domain(getattr(company, "domain", "") or ""),
            ]
        ),
        "founder_names": _uniq([*(startup_profile.founder_names or []), founder_name]),
        "team_names": _uniq([*(startup_profile.team_names or []), founder_name]),
        "competitor_names": _uniq([*(startup_profile.competitor_names or []), *competitor_names]),
        "competitor_domains": _uniq([*(startup_profile.competitor_domains or []), *competitor_domains]),
        "positive_keywords": _uniq(
            [
                *(startup_profile.positive_keywords or []),
                *seed_keywords,
                company_name,
                organization.name,
                getattr(config, "brand_name", "") if config else "",
                *(startup_profile.product_names or []),
            ]
        ),
    }

    for field_name, value in merged_values.items():
        if getattr(startup_profile, field_name) != value:
            setattr(startup_profile, field_name, value)
            update_fields.append(field_name)

    merged_notes = _append_note(
        startup_profile.notes,
        getattr(config, "company_context", "") if config else "",
    )
    if startup_profile.notes != merged_notes:
        startup_profile.notes = merged_notes
        update_fields.append("notes")

    if update_fields:
        update_fields.append("updated_at")
        startup_profile.save(update_fields=update_fields)

    return startup_profile


def build_startup_context_snapshot(
    *,
    organization: Organization,
    profile: StartupProfile,
) -> dict:
    return {
        "organization_id": organization.id,
        "domain": organization.domain,
        "company_name": organization.name,
        "company_aliases": list(profile.company_aliases or []),
        "domain_aliases": list(profile.domain_aliases or []),
        "product_names": list(profile.product_names or []),
        "founder_names": list(profile.founder_names or []),
        "team_names": list(profile.team_names or []),
        "investor_names": list(profile.investor_names or []),
        "investor_domains": list(profile.investor_domains or []),
        "competitor_names": list(profile.competitor_names or []),
        "competitor_domains": list(profile.competitor_domains or []),
        "customer_names": list(profile.customer_names or []),
        "customer_domains": list(profile.customer_domains or []),
        "prospect_names": list(profile.prospect_names or []),
        "prospect_domains": list(profile.prospect_domains or []),
        "positive_keywords": list(profile.positive_keywords or []),
        "negative_keywords": list(profile.negative_keywords or []),
        "kpi_definitions": list(profile.kpi_definitions or []),
        "default_currency": profile.default_currency,
        "notes": profile.notes,
        "stage": profile.stage,
    }


def resolve_or_create_profile(*, domain: str) -> tuple[Organization, StartupProfile]:
    normalized_domain = normalize_domain(domain)
    organization, _ = Organization.objects.get_or_create(
        domain=normalized_domain,
        defaults={"name": normalized_domain},
    )
    profile, _ = StartupProfile.objects.get_or_create(organization=organization)
    profile = seed_startup_profile(profile)
    return organization, profile


def bind_user_to_startup(*, user, organization: Organization, google_connection=None, role: str = "", is_default_for_gmail: bool = True) -> UserStartupBinding:
    with transaction.atomic():
        if is_default_for_gmail:
            UserStartupBinding.objects.filter(user=user, is_default_for_gmail=True).exclude(
                organization=organization
            ).update(is_default_for_gmail=False)

        binding, _ = UserStartupBinding.objects.update_or_create(
            user=user,
            organization=organization,
            defaults={
                "google_connection": google_connection,
                "role": role or "",
                "is_default_for_gmail": bool(is_default_for_gmail),
            },
        )
    return binding


def get_default_binding_for_domain(*, user, domain: str) -> Optional[UserStartupBinding]:
    normalized_domain = normalize_domain(domain)
    return (
        UserStartupBinding.objects.select_related("organization", "google_connection")
        .filter(user=user, organization__domain=normalized_domain)
        .first()
    )


def get_default_gmail_binding(*, user) -> Optional[UserStartupBinding]:
    bindings = UserStartupBinding.objects.select_related("organization", "google_connection").filter(user=user)
    default_binding = bindings.filter(is_default_for_gmail=True).order_by("-updated_at").first()
    if default_binding is not None:
        return default_binding

    candidates = list(bindings.order_by("-updated_at")[:2])
    if len(candidates) == 1:
        return candidates[0]
    return None


def get_startup_update_run_google_connection_id(run: ContentFactoryRun) -> Optional[int]:
    value = (run.run_request or {}).get("google_connection_id")
    if value in ("", None):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def pin_startup_update_run_connection(run: ContentFactoryRun, google_connection_id: Optional[int]) -> Optional[int]:
    if google_connection_id is None:
        return None
    current_id = get_startup_update_run_google_connection_id(run)
    if current_id == int(google_connection_id):
        return current_id

    run_request = dict(run.run_request or {})
    run_request["google_connection_id"] = int(google_connection_id)
    run.run_request = run_request
    run.save(update_fields=["run_request", "updated_at"])
    return int(google_connection_id)


def _iter_startup_update_runs(*, organization: Organization, statuses: Optional[Iterable[str]] = None) -> list[ContentFactoryRun]:
    queryset = ContentFactoryRun.objects.filter(
        workflow=STARTUP_UPDATE_WORKFLOW,
        domain=organization.domain,
    )
    if statuses is not None:
        queryset = queryset.filter(status__in=list(statuses))
    return list(queryset.order_by("-updated_at"))


def get_open_startup_update_run(
    *,
    organization: Organization,
    google_connection_id: Optional[int] = None,
) -> Optional[ContentFactoryRun]:
    runs = _iter_startup_update_runs(
        organization=organization,
        statuses=OPEN_RUN_STATUSES,
    )
    if google_connection_id is None:
        return runs[0] if runs else None

    legacy_candidate = None
    for run in runs:
        run_google_connection_id = get_startup_update_run_google_connection_id(run)
        if run_google_connection_id == google_connection_id:
            return run
        if run_google_connection_id is None and legacy_candidate is None:
            legacy_candidate = run
    return legacy_candidate


def get_latest_startup_update_run(
    *,
    organization: Organization,
    google_connection_id: Optional[int] = None,
) -> Optional[ContentFactoryRun]:
    runs = _iter_startup_update_runs(organization=organization)
    if google_connection_id is None:
        return runs[0] if runs else None

    legacy_candidate = None
    for run in runs:
        run_google_connection_id = get_startup_update_run_google_connection_id(run)
        if run_google_connection_id == google_connection_id:
            return run
        if run_google_connection_id is None and legacy_candidate is None:
            legacy_candidate = run
    return legacy_candidate


def supersede_conflicting_startup_update_runs(
    *,
    organization: Organization,
    google_connection_id: Optional[int],
    keep_run_id: Optional[str] = None,
    error_message: str = SUPERSEDED_GMAIL_CONNECTION_ERROR,
) -> int:
    if google_connection_id is None:
        return 0

    updated = 0
    for run in _iter_startup_update_runs(organization=organization, statuses=OPEN_RUN_STATUSES):
        if keep_run_id and run.run_id == keep_run_id:
            continue

        run_google_connection_id = get_startup_update_run_google_connection_id(run)
        if run_google_connection_id in (None, google_connection_id):
            continue

        run.status = ContentFactoryRunStatus.FAILED
        run.error = error_message
        run.resume_available = False
        run.save(update_fields=["status", "error", "resume_available", "updated_at"])
        updated += 1

    return updated


def create_startup_update_run(
    *,
    organization: Organization,
    binding: UserStartupBinding,
    window_months: int = DEFAULT_BACKFILL_MONTHS,
    input_sources: Optional[list[str]] = None,
    source_warnings: Optional[dict[str, list[str]]] = None,
) -> ContentFactoryRun:
    selected_input_sources = normalize_startup_update_input_sources(input_sources)
    selected_source_set = set(selected_input_sources)
    google_connection = binding.google_connection or getattr(binding.user, "google_connection", None)
    if not gmail_required_for_sources(selected_input_sources):
        google_connection = None
    google_connection_id = google_connection.id if google_connection else None
    step_order = build_startup_update_step_order(selected_input_sources)

    existing = get_open_startup_update_run(
        organization=organization,
        google_connection_id=google_connection_id,
    )
    if existing:
        pin_startup_update_run_connection(existing, google_connection_id)
        now = timezone.now()
        months = iter_recent_month_starts(3, reference=now)
        refresh_startup_update_run_source_context(
            run=existing,
            organization=organization,
            input_sources=selected_input_sources,
            start_date=_previous_month_start(months[0]),
            end_date=now.date(),
            source_warnings=source_warnings,
        )
        supersede_conflicting_startup_update_runs(
            organization=organization,
            google_connection_id=google_connection_id,
            keep_run_id=existing.run_id,
        )
        return existing

    now = timezone.now()
    backfill_start = now - timedelta(days=30 * int(window_months))
    current_month = _month_start(now)
    months = iter_recent_month_starts(3, reference=now)
    financial_start_date = min(backfill_start.date(), _previous_month_start(months[0]))
    profile = getattr(organization, "startup_profile", None)
    startup_context = (
        build_startup_context_snapshot(organization=organization, profile=profile)
        if profile is not None
        else {}
    )
    supersede_conflicting_startup_update_runs(
        organization=organization,
        google_connection_id=google_connection_id,
    )
    run_request = {
        "organization_id": organization.id,
        "startup_profile_id": profile.id if profile else None,
        "binding_id": binding.id,
        "google_connection_id": google_connection.id if google_connection else None,
        "window_months": int(window_months),
        "classification_batch_size": DEFAULT_CLASSIFICATION_BATCH_SIZE,
        "attachment_bytes_limit": DEFAULT_ATTACHMENT_BYTES_LIMIT,
        "max_source_threads": DEFAULT_MAX_SOURCE_THREADS,
        "draft_months": [item.isoformat() for item in months],
        "current_month": current_month.isoformat(),
        "backfill_window_start": backfill_start.isoformat(),
        "backfill_window_end": now.isoformat(),
        "startup_context": startup_context,
    }
    run_request["input_sources"] = list(selected_input_sources)
    selected_slack_channels = []
    if ExternalServiceProvider.SLACK in selected_source_set:
        selected_slack_channels = [
            selection.channel_id
            for selection in SlackChannelSelection.objects.filter(
                organization=organization,
                selected=True,
            ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        ]
        run_request["slack_channel_ids"] = selected_slack_channels
    external_context = build_external_context_for_sources(
        organization=organization,
        input_sources=selected_input_sources,
        start_date=financial_start_date,
        end_date=now.date(),
        source_warnings=source_warnings,
    )
    if external_context:
        if ExternalServiceProvider.SLACK in external_context:
            external_context[ExternalServiceProvider.SLACK]["selected_channel_ids"] = selected_slack_channels
        run_request["external_context"] = external_context

    run = ContentFactoryRun.objects.create(
        run_id=f"startup-update-{uuid.uuid4()}",
        workflow=STARTUP_UPDATE_WORKFLOW,
        domain=organization.domain,
        slack_user_id=str(binding.user_id),
        status=ContentFactoryRunStatus.QUEUED,
        current_step=step_order[0],
        approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
        step_order=step_order,
        run_request=run_request,
        result={},
        acceptance_summary={},
        verification_summary={},
    )
    reconcile_startup_update_run_source_steps(run=run, input_sources=selected_input_sources)
    if google_connection is not None and "gmail" in selected_source_set:
        GmailSyncCursor.objects.get_or_create(
            organization=organization,
            google_connection=google_connection,
            defaults={
                "backfill_window_start": backfill_start,
                "backfill_window_end": now,
            },
        )
    if ExternalServiceProvider.XERO in selected_source_set:
        refresh_startup_update_run_source_context(
            run=run,
            organization=organization,
            input_sources=selected_input_sources,
            start_date=financial_start_date,
            end_date=now.date(),
            source_warnings=source_warnings,
        )
    return run


def _iso_date(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _iso_datetime(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_cancel_backup_for_draft(draft: MonthlyUpdateDraft) -> dict:
    return {
        "run_id": getattr(draft.run, "run_id", None),
        "month": draft.month.isoformat(),
        "status": draft.status,
        "title": draft.title,
        "model_name": draft.model_name,
        "groundedness_status": draft.groundedness_status,
        "structured_memo": draft.structured_memo or {},
        "rendered_markdown": draft.rendered_markdown or "",
        "evidence_event_ids": list(draft.evidence_event_ids or []),
        "evidence_metric_ids": list(draft.evidence_metric_ids or []),
        "carry_forward_event_ids": list(draft.carry_forward_event_ids or []),
        "groundedness_notes": draft.groundedness_notes or "",
    }


def build_cancel_backup_for_event(event: StartupEvent) -> dict:
    return {
        "run_id": getattr(event.run, "run_id", None),
        "canonical_key": event.canonical_key,
        "event_type": event.event_type,
        "title": event.title,
        "summary": event.summary or "",
        "event_date": _iso_date(event.event_date),
        "month_bucket": event.month_bucket.isoformat(),
        "date_precision": event.date_precision,
        "sentiment": event.sentiment or "",
        "investor_importance": event.investor_importance,
        "quantitative_facts": list(event.quantitative_facts or []),
        "evidence_message_ids": list(event.evidence_message_ids or []),
        "evidence_attachment_ids": list(event.evidence_attachment_ids or []),
        "source_thread_ids": list(event.source_thread_ids or []),
        "confidence": event.confidence,
        "status": event.status,
        "needs_review": bool(event.needs_review),
        "merge_notes": event.merge_notes or "",
    }


def build_cancel_backup_for_metric(metric: StartupMetricObservation) -> dict:
    return {
        "run_id": getattr(metric.run, "run_id", None),
        "source_thread_id": metric.source_thread_id,
        "metric_key": metric.metric_key,
        "metric_name": metric.metric_name,
        "value_text": metric.value_text,
        "value_number": str(metric.value_number) if metric.value_number is not None else None,
        "unit": metric.unit or "",
        "observed_at": _iso_datetime(metric.observed_at),
        "period_month": metric.period_month.isoformat(),
        "confidence": metric.confidence,
        "evidence_message_ids": list(metric.evidence_message_ids or []),
        "evidence_attachment_ids": list(metric.evidence_attachment_ids or []),
        "source_provider": metric.source_provider or "gmail",
        "source_record_ids": list(metric.source_record_ids or []),
        "source_metadata": metric.source_metadata or {},
        "summary": metric.summary or "",
    }


def _restore_cancelled_run_drafts(*, organization: Organization, backups: dict) -> int:
    restored = 0
    for snapshot in (backups or {}).values():
        month_value = date.fromisoformat(str(snapshot["month"]))
        previous_run = ContentFactoryRun.objects.filter(run_id=snapshot.get("run_id") or "").first()
        MonthlyUpdateDraft.objects.update_or_create(
            organization=organization,
            month=month_value,
            defaults={
                "run": previous_run,
                "status": snapshot.get("status", MonthlyUpdateDraftStatus.DRAFT),
                "title": snapshot.get("title", ""),
                "model_name": snapshot.get("model_name", ""),
                "groundedness_status": snapshot.get("groundedness_status", "pending"),
                "structured_memo": snapshot.get("structured_memo") or {},
                "rendered_markdown": snapshot.get("rendered_markdown", ""),
                "evidence_event_ids": snapshot.get("evidence_event_ids") or [],
                "evidence_metric_ids": snapshot.get("evidence_metric_ids") or [],
                "carry_forward_event_ids": snapshot.get("carry_forward_event_ids") or [],
                "groundedness_notes": snapshot.get("groundedness_notes", ""),
            },
        )
        restored += 1
    return restored


def _restore_cancelled_run_events(*, organization: Organization, backups: dict) -> int:
    restored = 0
    for snapshot in (backups or {}).values():
        previous_run = ContentFactoryRun.objects.filter(run_id=snapshot.get("run_id") or "").first()
        StartupEvent.objects.update_or_create(
            organization=organization,
            canonical_key=snapshot["canonical_key"],
            defaults={
                "run": previous_run,
                "event_type": snapshot["event_type"],
                "title": snapshot["title"],
                "summary": snapshot.get("summary", ""),
                "event_date": snapshot.get("event_date"),
                "month_bucket": date.fromisoformat(str(snapshot["month_bucket"])),
                "date_precision": snapshot.get("date_precision", "day"),
                "sentiment": snapshot.get("sentiment", ""),
                "investor_importance": snapshot.get("investor_importance", 3),
                "quantitative_facts": snapshot.get("quantitative_facts") or [],
                "evidence_message_ids": snapshot.get("evidence_message_ids") or [],
                "evidence_attachment_ids": snapshot.get("evidence_attachment_ids") or [],
                "source_thread_ids": snapshot.get("source_thread_ids") or [],
                "confidence": snapshot.get("confidence", 0.0),
                "status": snapshot.get("status", "open"),
                "needs_review": bool(snapshot.get("needs_review", False)),
                "merge_notes": snapshot.get("merge_notes", ""),
            },
        )
        restored += 1
    return restored


def _restore_cancelled_run_metrics(*, organization: Organization, backups: dict) -> int:
    restored = 0
    for snapshot in (backups or {}).values():
        previous_run = ContentFactoryRun.objects.filter(run_id=snapshot.get("run_id") or "").first()
        StartupMetricObservation.objects.update_or_create(
            organization=organization,
            source_thread_id=snapshot.get("source_thread_id"),
            source_provider=snapshot.get("source_provider") or "gmail",
            metric_key=snapshot["metric_key"],
            period_month=date.fromisoformat(str(snapshot["period_month"])),
            value_text=snapshot["value_text"],
            defaults={
                "run": previous_run,
                "metric_name": snapshot["metric_name"],
                "value_number": Decimal(snapshot["value_number"]) if snapshot.get("value_number") not in (None, "") else None,
                "unit": snapshot.get("unit", ""),
                "observed_at": snapshot.get("observed_at"),
                "confidence": snapshot.get("confidence", 0.0),
                "evidence_message_ids": snapshot.get("evidence_message_ids") or [],
                "evidence_attachment_ids": snapshot.get("evidence_attachment_ids") or [],
                "source_record_ids": snapshot.get("source_record_ids") or [],
                "source_metadata": snapshot.get("source_metadata") or {},
                "summary": snapshot.get("summary", ""),
            },
        )
        restored += 1
    return restored


def cancel_startup_update_run(
    *,
    run_id: str,
    organization: Organization,
    binding_id: int,
    google_connection_id: Optional[int],
    cancelled_by_user_id: int,
) -> dict:
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(
            run_id=run_id,
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=organization.domain,
        ).first()
        if run is None:
            raise ContentFactoryRun.DoesNotExist(run_id)

        run_binding_id = (run.run_request or {}).get("binding_id")
        if run_binding_id and int(run_binding_id) != int(binding_id):
            raise PermissionError("Run does not belong to the active binding.")

        run_google_connection_id = get_startup_update_run_google_connection_id(run)
        if (
            google_connection_id is not None
            and run_google_connection_id is not None
            and int(run_google_connection_id) != int(google_connection_id)
        ):
            raise PermissionError("Run does not belong to the active Gmail connection.")

        if run.status == ContentFactoryRunStatus.COMPLETED:
            return {
                "run": run,
                "cancel_applied": False,
                "cleanup": {"drafts_deleted": 0, "events_deleted": 0, "metrics_deleted": 0},
            }

        if run.status == ContentFactoryRunStatus.CANCELLED:
            return {
                "run": run,
                "cancel_applied": False,
                "cleanup": {"drafts_deleted": 0, "events_deleted": 0, "metrics_deleted": 0},
            }

        if run.status not in STARTUP_UPDATE_CANCELLABLE_STATUSES:
            return {
                "run": run,
                "cancel_applied": False,
                "cleanup": {"drafts_deleted": 0, "events_deleted": 0, "metrics_deleted": 0},
            }

        backups = get_startup_update_run_cancel_backups(run)
        _restore_cancelled_run_drafts(organization=organization, backups=backups.get("drafts") or {})
        _restore_cancelled_run_events(organization=organization, backups=backups.get("events") or {})
        _restore_cancelled_run_metrics(organization=organization, backups=backups.get("metrics") or {})

        drafts_deleted, _ = MonthlyUpdateDraft.objects.filter(run=run).delete()
        events_deleted, _ = StartupEvent.objects.filter(run=run).delete()
        metrics_deleted, _ = StartupMetricObservation.objects.filter(run=run).delete()

        meta = _get_run_meta(run)
        meta["retry_count"] = 0
        meta["last_error"] = ""
        meta["lease_owner"] = None
        meta["lease_expires_at"] = None
        meta["last_heartbeat_at"] = None
        meta["dead_letters"] = []
        cancelled_at = timezone.now()
        meta["cancellation"] = {
            "cancelled_at": cancelled_at.isoformat(),
            "cancelled_by_user_id": int(cancelled_by_user_id),
            "cancel_reason": "user_requested",
        }
        _set_run_meta(run, meta)

        run.status = ContentFactoryRunStatus.CANCELLED
        run.error = "Cancelled by user."
        run.resume_available = False
        run.save(update_fields=["status", "result", "error", "resume_available", "updated_at"])

        run.steps.filter(status=ContentFactoryStepStatus.RUNNING).update(
            status=ContentFactoryStepStatus.CANCELLED,
            message="Cancelled by user.",
            completed_at=cancelled_at,
            error="Cancelled by user.",
        )
        ContentFactoryRunStepAttempt.objects.filter(
            step__run=run,
            status=ContentFactoryStepStatus.RUNNING,
        ).update(
            status=ContentFactoryStepStatus.CANCELLED,
            message="Cancelled by user.",
            completed_at=cancelled_at,
            error="Cancelled by user.",
        )

        return {
            "run": run,
            "cancel_applied": True,
            "cleanup": {
                "drafts_deleted": int(drafts_deleted),
                "events_deleted": int(events_deleted),
                "metrics_deleted": int(metrics_deleted),
            },
        }


def maybe_start_startup_update_for_google_connection(
    *,
    user,
    google_connection,
    window_months: int = DEFAULT_BACKFILL_MONTHS,
) -> Optional[ContentFactoryRun]:
    if google_connection is None:
        return None

    binding = get_default_gmail_binding(user=user)
    if binding is None:
        return None

    if binding.google_connection_id != google_connection.id:
        binding.google_connection = google_connection
        binding.save(update_fields=["google_connection", "updated_at"])

    organization = binding.organization
    resolve_or_create_profile(domain=organization.domain)
    existing_run = get_open_startup_update_run(
        organization=organization,
        google_connection_id=google_connection.id,
    )
    run = create_startup_update_run(
        organization=organization,
        binding=binding,
        window_months=window_months,
    )
    reused_existing_run = existing_run is not None
    logger.info(
        "google_oauth_startup_update_run_%s",
        "reused" if reused_existing_run else "created",
        extra={
            "user_id": user.pk,
            "organization_id": organization.id,
            "run_id": run.run_id,
            "reused": reused_existing_run,
        },
    )
    if existing_run is None:
        transaction.on_commit(lambda: notify_valley_run_created(run.run_id))
    return run


def score_message_for_profile(profile: StartupProfile, artifact: GmailMessageArtifact) -> tuple[int, list[str], str]:
    haystack = _message_haystack(artifact)
    participant_domains = _participant_domains(artifact)
    profile_signals = _profile_signal_lists(profile)
    allowlist_reasons = _allowlist_override_reasons(
        haystack=haystack,
        participant_domains=participant_domains,
        profile_signals=profile_signals,
    )
    hard_irrelevant_reasons = _hard_irrelevant_reasons(artifact, haystack=haystack)

    if hard_irrelevant_reasons and not allowlist_reasons:
        return 0, hard_irrelevant_reasons, GmailRelevanceLabel.IRRELEVANT

    score = 50
    reasons = []

    if hard_irrelevant_reasons and allowlist_reasons:
        reasons.append("allowlist_override_hard_filter")

    if any(pattern in haystack for pattern in LOW_SIGNAL_PATTERNS):
        score -= 40
        reasons.append("matched_low_signal_pattern")

    if _match_any(profile_signals["company_aliases"] + profile_signals["positive_keywords"], haystack):
        score += 15
        reasons.append("matched_company_alias_or_positive_keyword")

    if _match_any(profile_signals["founder_names"] + profile_signals["team_names"], haystack):
        score += 15
        reasons.append("matched_founder_or_team_name")

    if _match_any(profile_signals["investor_names"], haystack):
        score += 20
        reasons.append("matched_investor_name")

    if _match_any(profile_signals["customer_names"] + profile_signals["prospect_names"], haystack):
        score += 15
        reasons.append("matched_customer_or_prospect_name")

    if _match_any(profile_signals["competitor_names"], haystack):
        score += 10
        reasons.append("matched_competitor_name")

    if _match_any(HIGH_SIGNAL_TERMS, haystack):
        score += 15
        reasons.append("matched_high_signal_term")

    if any(domain and domain in participant_domains for domain in profile_signals["domain_aliases"]):
        score += 25
        reasons.append("matched_company_domain")

    if any(domain and domain in participant_domains for domain in profile_signals["investor_domains"]):
        score += 20
        reasons.append("matched_investor_domain")

    if any(
        domain and domain in participant_domains
        for domain in profile_signals["customer_domains"] + profile_signals["prospect_domains"]
    ):
        score += 15
        reasons.append("matched_customer_or_prospect_domain")

    if any(domain and domain in participant_domains for domain in profile_signals["competitor_domains"]):
        score += 10
        reasons.append("matched_competitor_domain")

    if _match_any(profile_signals["negative_keywords"], haystack):
        score -= 20
        reasons.append("matched_negative_keyword")

    score = max(0, min(100, score))

    if score >= 80:
        label = GmailRelevanceLabel.RELEVANT
    elif score <= 20:
        label = GmailRelevanceLabel.IRRELEVANT
    else:
        label = GmailRelevanceLabel.AMBIGUOUS

    return score, _uniq(reasons), label


def apply_profile_scoring(
    profile: StartupProfile,
    artifact: GmailMessageArtifact,
    *,
    persist: bool = True,
) -> tuple[int, list[str], str]:
    score, reasons, label = score_message_for_profile(profile, artifact)
    artifact.heuristic_score = score
    artifact.heuristic_reasons = reasons
    if artifact.classified_at is None:
        artifact.relevance_label = label
    artifact.needs_thread_context = artifact.relevance_label in {
        GmailRelevanceLabel.RELEVANT,
        GmailRelevanceLabel.AMBIGUOUS,
    }

    if persist:
        update_fields = [
            "heuristic_score",
            "heuristic_reasons",
            "needs_thread_context",
            "updated_at",
        ]
        if artifact.classified_at is None:
            update_fields.insert(2, "relevance_label")
        artifact.save(update_fields=update_fields)

    return artifact.heuristic_score, artifact.heuristic_reasons, artifact.relevance_label


def build_timeline_payload(*, organization: Organization) -> dict:
    months = iter_recent_month_starts(6)
    event_queryset = organization.startup_events.order_by("month_bucket", "-investor_importance", "title")
    metric_queryset = organization.startup_metric_observations.order_by("period_month", "metric_key")

    grouped = {month.isoformat(): {"events": [], "metrics": []} for month in months}
    for event in event_queryset:
        bucket = event.month_bucket.isoformat()
        grouped.setdefault(bucket, {"events": [], "metrics": []})
        grouped[bucket]["events"].append(_serialize_event(event))
    for metric in metric_queryset:
        bucket = metric.period_month.isoformat()
        grouped.setdefault(bucket, {"events": [], "metrics": []})
        grouped[bucket]["metrics"].append(_serialize_metric(metric))

    return {
        "organization_id": organization.id,
        "domain": organization.domain,
        "months": grouped,
    }


def render_monthly_update_markdown(structured_memo: dict) -> str:
    memo = structured_memo or {}
    title = str(memo.get("title") or "").strip()
    topline = str(memo.get("topline") or "").strip()
    sections = [
        ("KPI Snapshot", memo.get("kpi_snapshot") or []),
        ("Financial Performance", memo.get("financial_performance") or []),
        ("Asks", memo.get("asks") or []),
        ("Highlights", memo.get("highlights") or []),
        ("Lowlights / Risks", memo.get("lowlights") or []),
        ("Product / GTM / Team / Fundraising", memo.get("operations") or []),
        ("Learnings", memo.get("learnings") or []),
        ("Next 30 Days", memo.get("next_30_days") or []),
    ]

    lines = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    if topline:
        lines.append("## Executive Summary")
        lines.append(topline)
        lines.append("")

    for heading, items in sections:
        lines.append(f"## {heading}")
        if items:
            for item in items:
                if isinstance(item, dict):
                    label = str(item.get("label") or item.get("name") or "").strip()
                    value = str(item.get("value") or item.get("text") or item.get("summary") or "").strip()
                    if label and value:
                        lines.append(f"- **{label}:** {value}")
                    elif value:
                        lines.append(f"- {value}")
                    elif label:
                        lines.append(f"- {label}")
                else:
                    text = str(item or "").strip()
                    if text:
                        lines.append(f"- {text}")
        else:
            lines.append("- None noted.")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _normalize_draft_similarity_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", value.lower())).strip()


def _draft_bullet_text(item: Any) -> str:
    if isinstance(item, dict):
        parts = [
            item.get("label"),
            item.get("name"),
            item.get("text"),
            item.get("value"),
            item.get("summary"),
        ]
        return " ".join(str(part).strip() for part in parts if str(part or "").strip())
    return str(item or "").strip()


def _draft_bullets_are_similar(existing_item: Any, incoming_item: Any) -> bool:
    existing_text = _normalize_draft_similarity_text(_draft_bullet_text(existing_item))
    incoming_text = _normalize_draft_similarity_text(_draft_bullet_text(incoming_item))
    if not existing_text or not incoming_text:
        return False
    if existing_text == incoming_text:
        return True

    existing_tokens = set(existing_text.split())
    incoming_tokens = set(incoming_text.split())
    if not existing_tokens or not incoming_tokens:
        return False

    overlap = len(existing_tokens & incoming_tokens)
    union = len(existing_tokens | incoming_tokens)
    shorter = min(len(existing_tokens), len(incoming_tokens))
    token_similarity = overlap / union if union else 0
    containment = overlap / shorter if shorter else 0
    return token_similarity >= 0.62 or (containment >= 0.8 and overlap >= 3)


def _normalize_draft_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [item for item in value if _draft_bullet_text(item)]
    if _draft_bullet_text(value):
        return [value]
    return []


def _merge_draft_list_section(existing_items: Any, incoming_items: Any) -> tuple[list[Any], dict[str, int]]:
    merged = list(_normalize_draft_list(existing_items))
    incoming = _normalize_draft_list(incoming_items)
    refreshed_indexes: set[int] = set()
    added = 0

    for incoming_item in incoming:
        matched_index = None
        for index, existing_item in enumerate(merged):
            if index in refreshed_indexes:
                continue
            if _draft_bullets_are_similar(existing_item, incoming_item):
                matched_index = index
                break

        if matched_index is None:
            merged.append(incoming_item)
            added += 1
            continue

        merged[matched_index] = incoming_item
        refreshed_indexes.add(matched_index)

    return merged, {
        "refreshed": len(refreshed_indexes),
        "added": added,
        "preserved": max(len(_normalize_draft_list(existing_items)) - len(refreshed_indexes), 0),
    }


def _draft_metric_key(item: Any) -> str:
    if not isinstance(item, dict):
        return _normalize_draft_similarity_text(str(item or ""))
    metric_key = str(item.get("metric_key") or item.get("key") or "").strip()
    if metric_key:
        return f"metric:{metric_key.lower()}"
    return f"label:{_normalize_draft_similarity_text(str(item.get('label') or item.get('name') or ''))}"


def _merge_kpi_snapshot(existing_items: Any, incoming_items: Any) -> list[Any]:
    merged = list(existing_items or []) if isinstance(existing_items, list) else []
    index_by_key = {
        _draft_metric_key(item): index
        for index, item in enumerate(merged)
        if _draft_metric_key(item)
    }

    for incoming_item in incoming_items if isinstance(incoming_items, list) else []:
        key = _draft_metric_key(incoming_item)
        if key and key in index_by_key:
            merged[index_by_key[key]] = incoming_item
        else:
            if key:
                index_by_key[key] = len(merged)
            merged.append(incoming_item)
    return merged


def _unique_id_list(*values: Optional[list[int]]) -> list[int]:
    seen: set[int] = set()
    merged: list[int] = []
    for value in values:
        for item in value or []:
            try:
                normalized = int(item)
            except (TypeError, ValueError):
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return merged


def _merge_groundedness_notes(existing_notes: str, incoming_notes: str, stats: dict[str, int]) -> str:
    merge_note = (
        "Regenerated and merged with existing monthly update: "
        f"{stats.get('refreshed', 0)} bullets refreshed, "
        f"{stats.get('added', 0)} added, "
        f"{stats.get('preserved', 0)} preserved."
    )
    notes: list[str] = []
    for note in (existing_notes, incoming_notes, merge_note):
        text = str(note or "").strip()
        if text and text not in notes:
            notes.append(text)
    return "\n".join(notes)


def merge_monthly_update_structured_memo(existing_memo: dict, incoming_memo: dict) -> tuple[dict, dict[str, int]]:
    existing = dict(existing_memo or {})
    incoming = dict(incoming_memo or {})
    merged: dict[str, Any] = dict(existing)
    stats = {"refreshed": 0, "added": 0, "preserved": 0}

    for key, value in incoming.items():
        if key in MERGEABLE_DRAFT_LIST_SECTIONS or key == "kpi_snapshot":
            continue
        if value in ("", None, [], {}):
            continue
        merged[key] = value

    for section in MERGEABLE_DRAFT_LIST_SECTIONS:
        merged_items, section_stats = _merge_draft_list_section(
            existing.get(section),
            incoming.get(section),
        )
        if merged_items:
            merged[section] = merged_items
        for stat_key, value in section_stats.items():
            stats[stat_key] += value

    merged["kpi_snapshot"] = _merge_kpi_snapshot(
        existing.get("kpi_snapshot"),
        incoming.get("kpi_snapshot"),
    )

    if existing.get("source_notes") or incoming.get("source_notes"):
        merged["source_notes"] = list(dict.fromkeys(
            str(item).strip()
            for item in [
                *(existing.get("source_notes") or []),
                *(incoming.get("source_notes") or []),
            ]
            if str(item or "").strip()
        ))

    return merged, stats


def upsert_monthly_update_draft(
    *,
    organization: Organization,
    month: date,
    run: Optional[ContentFactoryRun],
    structured_memo: dict,
    model_name: str,
    status: str = MonthlyUpdateDraftStatus.DRAFT,
    groundedness_status: str = "pending",
    evidence_event_ids: Optional[list[int]] = None,
    evidence_metric_ids: Optional[list[int]] = None,
    carry_forward_event_ids: Optional[list[int]] = None,
    groundedness_notes: str = "",
) -> MonthlyUpdateDraft:
    month_start = _month_start(month)
    existing_draft = MonthlyUpdateDraft.objects.filter(
        organization=organization,
        month=month_start,
    ).first()
    if existing_draft is not None:
        structured_memo, merge_stats = merge_monthly_update_structured_memo(
            existing_draft.structured_memo or {},
            structured_memo or {},
        )
        evidence_event_ids = _unique_id_list(existing_draft.evidence_event_ids, evidence_event_ids)
        evidence_metric_ids = _unique_id_list(existing_draft.evidence_metric_ids, evidence_metric_ids)
        carry_forward_event_ids = _unique_id_list(existing_draft.carry_forward_event_ids, carry_forward_event_ids)
        groundedness_notes = _merge_groundedness_notes(
            existing_draft.groundedness_notes,
            groundedness_notes,
            merge_stats,
        )

    rendered_markdown = render_monthly_update_markdown(structured_memo)
    title = str((structured_memo or {}).get("title") or "").strip()
    draft, _ = MonthlyUpdateDraft.objects.update_or_create(
        organization=organization,
        month=month_start,
        defaults={
            "run": run,
            "status": status,
            "title": title,
            "model_name": model_name or "",
            "groundedness_status": groundedness_status,
            "structured_memo": structured_memo or {},
            "rendered_markdown": rendered_markdown,
            "evidence_event_ids": evidence_event_ids or [],
            "evidence_metric_ids": evidence_metric_ids or [],
            "carry_forward_event_ids": carry_forward_event_ids or [],
            "groundedness_notes": groundedness_notes or "",
        },
    )
    return draft
