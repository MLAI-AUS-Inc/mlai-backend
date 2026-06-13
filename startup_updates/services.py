from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from typing import Any, Iterable, Optional, Union

from django.db import transaction
from django.utils import timezone

from integrations import http_client
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
    ExternalServiceConnection,
    ExternalFinancialRecord,
    ExternalServiceConnectionStatus,
    ExternalServiceProvider,
)
from integrations.services.xero_scopes import (
    XERO_REPORT_SCOPE,
    xero_has_report_scope,
)
from integrations.services.gmail_scopes import (
    GMAIL_RECONNECT_WARNING,
    has_gmail_read_scope,
)
from startup_updates.models import (
    GmailMessageArtifact,
    GmailRelevanceLabel,
    GmailThreadArtifact,
    GoogleAnalyticsPropertySelection,
    LinearIssueArtifact,
    LinearProjectArtifact,
    LinearProjectSelection,
    LinearProjectUpdateArtifact,
    SlackChannelSelection,
    SlackThreadArtifact,
    StartupEvent,
    StartupManualDocument,
    StartupMetricObservation,
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    GmailSyncCursor,
    StartupProfile,
    UserStartupBinding,
)
from integrations.services.valley_harness import ValleyHarnessResult, notify_valley_run_created
from integrations.utils import normalize_domain


logger = logging.getLogger(__name__)

STARTUP_UPDATE_WORKFLOW = "startup_monthly_update"
DEFAULT_BACKFILL_MONTHS = 1
DEFAULT_CLASSIFICATION_BATCH_SIZE = 40
DEFAULT_ATTACHMENT_BYTES_LIMIT = 10 * 1024 * 1024
DEFAULT_MAX_SOURCE_THREADS = 40
SUPERSEDED_GMAIL_CONNECTION_ERROR = "Superseded by a newer Gmail connection."
MANUAL_DOCUMENTS_SOURCE = "manual_documents"
MAX_MANUAL_DOCUMENT_CONTEXT_CHARS = 40000
MAX_MANUAL_DOCUMENT_CONTEXT_CHARS_PER_DOCUMENT = 12000
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
    "revenue",
    "customer",
    "signed",
    "deal",
    "mrr",
    "arr",
    "cash",
    "kpi",
    "risk",
    "compliance",
    "security",
    "run rate",
    "invoice",
    "runway",
    "beta",
    "shipped",
    "release",
]
SLACK_HARD_IRRELEVANT_PATTERNS = [
    "has joined the channel",
    "has left the channel",
    "set the channel topic",
    "set the channel purpose",
    "archived the channel",
    "unpinned a message",
    "pinned a message",
    "daily standup",
    "standup reminder",
    "reminder:",
    "calendar reminder",
    "build passed",
    "build failed",
    "workflow run",
    "pull request opened",
    "pull request closed",
]
SLACK_LOW_SIGNAL_PATTERNS = [
    "thanks",
    "thank you",
    "sounds good",
    "nice",
    "ok",
    "okay",
    "done",
    "cool",
    "+1",
    "approved",
    "merged",
]
SLACK_AUTOMATION_AUTHOR_PATTERNS = [
    "bot",
    "github",
    "linear",
    "jira",
    "notion",
    "calendar",
    "standup",
    "zapier",
]
SLACK_COMPACT_MAX_MESSAGES = 40
SLACK_COMPACT_MAX_CHARS = 6000
GMAIL_COMPACT_MAX_MESSAGES = 24
GMAIL_COMPACT_MAX_CHARS = 12000
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
    "slack_backfill",
    "slack_relevance_classification",
    "slack_event_extraction",
    "linear_backfill",
    "linear_relevance_classification",
    "linear_event_extraction",
    "notion_backfill",
    "notion_relevance_classification",
    "notion_event_extraction",
    "google_analytics_backfill",
    "google_analytics_relevance_classification",
    "google_analytics_event_extraction",
    "timeline_merge",
    "candidate_curation",
    "founder_review",
    "draft_generation",
    "groundedness_review",
]
SOURCE_REPROCESS_STEPS = {
    "timeline_merge",
    "candidate_curation",
    "founder_review",
    "draft_generation",
    "groundedness_review",
}
SLACK_CLASSIFICATION_DOWNSTREAM_STEPS = {
    "slack_event_extraction",
    *SOURCE_REPROCESS_STEPS,
}
SLACK_STEP_KEYS = {
    "slack_backfill",
    "slack_relevance_classification",
    "slack_event_extraction",
}
LINEAR_CLASSIFICATION_DOWNSTREAM_STEPS = {
    "linear_event_extraction",
    *SOURCE_REPROCESS_STEPS,
}
LINEAR_STEP_KEYS = {
    "linear_backfill",
    "linear_relevance_classification",
    "linear_event_extraction",
}
NOTION_CLASSIFICATION_DOWNSTREAM_STEPS = {
    "notion_event_extraction",
    *SOURCE_REPROCESS_STEPS,
}
NOTION_STEP_KEYS = {
    "notion_backfill",
    "notion_relevance_classification",
    "notion_event_extraction",
}
GOOGLE_ANALYTICS_CLASSIFICATION_DOWNSTREAM_STEPS = {
    "google_analytics_event_extraction",
    *SOURCE_REPROCESS_STEPS,
}
GOOGLE_ANALYTICS_STEP_KEYS = {
    "google_analytics_backfill",
    "google_analytics_relevance_classification",
    "google_analytics_event_extraction",
}
LINEAR_COMPACT_MAX_ISSUES = 35
LINEAR_COMPACT_MAX_UPDATES = 8
LINEAR_COMPACT_MAX_CHARS = 9000
XERO_REPORTS_SCOPE = XERO_REPORT_SCOPE
XERO_REPORT_METRIC_KEYS = {
    "revenue",
    "revenueGrowthRate",
    "burnRate",
    "runway",
    "monthlyCosts",
    "operatingExpenses",
    "costOfSales",
}
XERO_DRAFT_METRIC_KEYS = (
    "revenue",
    "mrr",
    "burnRate",
    "runway",
    "monthlyCosts",
    "invoiceRevenue",
    "cashCollected",
    "revenueGrowthRate",
    "customerCount",
    "invoiceCount",
    "recurringInvoiceCount",
)
XERO_DRAFT_METRIC_LABELS = {
    "revenue": "Revenue",
    "activeUsers": "Active Users",
    "mrr": "MRR",
    "burnRate": "Burn Rate",
    "runway": "Runway",
    "monthlyCosts": "Monthly Costs",
    "operatingExpenses": "Operating Expenses",
    "costOfSales": "Cost of Sales",
    "invoiceRevenue": "Invoice Revenue",
    "cashCollected": "Cash Collected",
    "revenueGrowthRate": "Revenue Growth Rate",
    "customerCount": "Customer Count",
    "churn": "Churn",
    "invoiceCount": "Invoice Count",
    "recurringInvoiceCount": "Recurring Invoice Count",
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
        ExternalServiceProvider.LINEAR,
        ExternalServiceProvider.GOOGLE_ANALYTICS,
        MANUAL_DOCUMENTS_SOURCE,
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


def merge_source_warnings(
    *warning_groups: Optional[dict[str, list[str]]],
) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for warning_group in warning_groups:
        if not warning_group:
            continue
        for source, warnings in warning_group.items():
            source_key = str(source or "").strip().lower().replace("-", "_")
            if not source_key:
                continue
            for warning in warnings or []:
                warning_text = str(warning or "").strip()
                if warning_text and warning_text not in merged.setdefault(source_key, []):
                    merged[source_key].append(warning_text)
    return {source: warnings for source, warnings in merged.items() if warnings}


def coerce_startup_update_sources_for_gmail_scope(
    input_sources: Optional[list[str]],
    google_connection=None,
) -> tuple[list[str], object | None, dict[str, list[str]]]:
    selected_input_sources = normalize_startup_update_input_sources(input_sources)
    if "gmail" not in selected_input_sources:
        return selected_input_sources, None, {}
    if google_connection is None or has_gmail_read_scope(google_connection):
        return selected_input_sources, google_connection, {}

    fallback_sources = [source for source in selected_input_sources if source != "gmail"]
    warnings = {"gmail": [GMAIL_RECONNECT_WARNING]}
    if fallback_sources:
        return fallback_sources, None, warnings
    return selected_input_sources, google_connection, warnings


def startup_update_run_input_sources(run: ContentFactoryRun) -> list[str]:
    return normalize_startup_update_input_sources((run.run_request or {}).get("input_sources"))


def startup_update_run_matches_input_sources(
    run: ContentFactoryRun,
    input_sources: Optional[list[str]],
    *,
    google_connection_id: Optional[int],
) -> bool:
    if input_sources is None:
        return True

    requested = set(normalize_startup_update_input_sources(input_sources))
    existing = set(startup_update_run_input_sources(run))
    requested_uses_gmail = "gmail" in requested
    existing_uses_gmail = "gmail" in existing

    if not requested_uses_gmail:
        return not existing_uses_gmail and requested == existing

    if not existing_uses_gmail:
        return False

    run_google_connection_id = get_startup_update_run_google_connection_id(run)
    if google_connection_id is not None and run_google_connection_id not in (None, google_connection_id):
        return False
    return existing.issubset(requested)


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
        steps.extend(["slack_backfill", "slack_relevance_classification", "slack_event_extraction"])
    if ExternalServiceProvider.LINEAR in selected:
        steps.extend(["linear_backfill", "linear_relevance_classification", "linear_event_extraction"])
    if ExternalServiceProvider.NOTION in selected:
        steps.extend(["notion_backfill", "notion_relevance_classification", "notion_event_extraction"])
    if ExternalServiceProvider.GOOGLE_ANALYTICS in selected:
        steps.extend([
            "google_analytics_backfill",
            "google_analytics_relevance_classification",
            "google_analytics_event_extraction",
        ])
    steps.extend(["timeline_merge", "candidate_curation", "founder_review", "draft_generation", "groundedness_review"])
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
    slack_classification_was_added = "slack_relevance_classification" in added_steps
    linear_was_added = any(step in LINEAR_STEP_KEYS for step in added_steps)
    linear_classification_was_added = "linear_relevance_classification" in added_steps
    notion_was_added = any(step in NOTION_STEP_KEYS for step in added_steps)
    notion_classification_was_added = "notion_relevance_classification" in added_steps
    ga_was_added = any(step in GOOGLE_ANALYTICS_STEP_KEYS for step in added_steps)
    ga_classification_was_added = "google_analytics_relevance_classification" in added_steps

    update_fields: list[str] = []
    if previous_step_order != desired_step_order:
        run.step_order = desired_step_order
        update_fields.append("step_order")

    if not run.current_step or run.current_step not in desired_step_order:
        run.current_step = desired_step_order[0]
        update_fields.append("current_step")
    elif "slack_backfill" in added_steps and run.current_step in SOURCE_REPROCESS_STEPS:
        run.current_step = "slack_backfill"
        update_fields.append("current_step")
    elif slack_classification_was_added and run.current_step in SLACK_CLASSIFICATION_DOWNSTREAM_STEPS:
        run.current_step = "slack_relevance_classification"
        update_fields.append("current_step")
    elif "linear_backfill" in added_steps and run.current_step in SOURCE_REPROCESS_STEPS:
        run.current_step = "linear_backfill"
        update_fields.append("current_step")
    elif linear_classification_was_added and run.current_step in LINEAR_CLASSIFICATION_DOWNSTREAM_STEPS:
        run.current_step = "linear_relevance_classification"
        update_fields.append("current_step")
    elif "notion_backfill" in added_steps and run.current_step in SOURCE_REPROCESS_STEPS:
        run.current_step = "notion_backfill"
        update_fields.append("current_step")
    elif notion_classification_was_added and run.current_step in NOTION_CLASSIFICATION_DOWNSTREAM_STEPS:
        run.current_step = "notion_relevance_classification"
        update_fields.append("current_step")
    elif "google_analytics_backfill" in added_steps and run.current_step in SOURCE_REPROCESS_STEPS:
        run.current_step = "google_analytics_backfill"
        update_fields.append("current_step")
    elif ga_classification_was_added and run.current_step in GOOGLE_ANALYTICS_CLASSIFICATION_DOWNSTREAM_STEPS:
        run.current_step = "google_analytics_relevance_classification"
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
        reset_steps = set(SOURCE_REPROCESS_STEPS)
        if slack_classification_was_added:
            reset_steps.update(SLACK_CLASSIFICATION_DOWNSTREAM_STEPS)
        downstream_steps = ContentFactoryRunStep.objects.filter(
            run=run,
            step_key__in=reset_steps,
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

    if linear_was_added:
        reset_steps = set(SOURCE_REPROCESS_STEPS)
        if linear_classification_was_added:
            reset_steps.update(LINEAR_CLASSIFICATION_DOWNSTREAM_STEPS)
        downstream_steps = ContentFactoryRunStep.objects.filter(
            run=run,
            step_key__in=reset_steps,
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

    if notion_was_added:
        reset_steps = set(SOURCE_REPROCESS_STEPS)
        if notion_classification_was_added:
            reset_steps.update(NOTION_CLASSIFICATION_DOWNSTREAM_STEPS)
        downstream_steps = ContentFactoryRunStep.objects.filter(
            run=run,
            step_key__in=reset_steps,
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

    if ga_was_added:
        reset_steps = set(SOURCE_REPROCESS_STEPS)
        if ga_classification_was_added:
            reset_steps.update(GOOGLE_ANALYTICS_CLASSIFICATION_DOWNSTREAM_STEPS)
        downstream_steps = ContentFactoryRunStep.objects.filter(
            run=run,
            step_key__in=reset_steps,
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


def parse_startup_update_target_month(
    value: Optional[Union[str, date, datetime]] = None,
    *,
    reference: Optional[Union[date, datetime]] = None,
) -> date:
    current_month = _month_start(reference)
    if value is None or value == "":
        return current_month
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = str(value or "").strip()
        try:
            parsed = date.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("target_month must use YYYY-MM-01 format.") from exc
    if parsed.day != 1:
        raise ValueError("target_month must be the first day of a month.")
    target_month = date(parsed.year, parsed.month, 1)
    if target_month > current_month:
        raise ValueError("target_month cannot be in the future.")
    return target_month


def _aware_utc_datetime(day: date, boundary: time) -> datetime:
    return datetime.combine(day, boundary, tzinfo=dt_timezone.utc)


def build_startup_update_target_windows(
    target_month: Optional[Union[str, date, datetime]] = None,
    *,
    reference: Optional[datetime] = None,
) -> dict[str, Any]:
    now = reference or timezone.now()
    if timezone.is_naive(now):
        now = timezone.make_aware(now, timezone=dt_timezone.utc)
    month = parse_startup_update_target_month(target_month, reference=now)
    month_end = _month_end(month)
    narrative_start = _aware_utc_datetime(month, time.min)
    narrative_month_end = _aware_utc_datetime(month_end, time.max)
    narrative_end = min(now, narrative_month_end)
    financial_start = _previous_month_start(month)
    return {
        "target_month": month,
        "narrative_start": narrative_start,
        "narrative_end": narrative_end,
        "narrative_start_date": narrative_start.date(),
        "narrative_end_date": narrative_end.date(),
        "financial_start_date": financial_start,
        "financial_end_date": narrative_end.date(),
    }


def get_startup_update_run_target_month(run: ContentFactoryRun) -> Optional[date]:
    run_request = run.run_request or {}
    raw_value = run_request.get("target_month") or run_request.get("current_month")
    if not raw_value:
        draft_months = run_request.get("draft_months") or []
        if isinstance(draft_months, (list, tuple)) and draft_months:
            raw_value = draft_months[-1]
    if not raw_value:
        return None
    try:
        parsed = date.fromisoformat(str(raw_value))
    except ValueError:
        return None
    if parsed.day != 1:
        return None
    return date(parsed.year, parsed.month, 1)


def startup_update_run_matches_target_month(run: ContentFactoryRun, target_month: date) -> bool:
    return get_startup_update_run_target_month(run) == _month_start(target_month)


def set_startup_update_run_target_month(
    run: ContentFactoryRun,
    target_month: Optional[Union[str, date, datetime]] = None,
    *,
    reference: Optional[datetime] = None,
    window_months: Optional[int] = None,
) -> ContentFactoryRun:
    windows = build_startup_update_target_windows(target_month, reference=reference)
    month = windows["target_month"]
    run_request = dict(run.run_request or {})
    if window_months is not None:
        run_request["window_months"] = int(window_months)
    run_request["target_month"] = month.isoformat()
    run_request["current_month"] = month.isoformat()
    run_request["draft_months"] = [month.isoformat()]
    run_request["backfill_window_start"] = windows["narrative_start"].isoformat()
    run_request["backfill_window_end"] = windows["narrative_end"].isoformat()
    run.run_request = run_request
    run.save(update_fields=["run_request", "updated_at"])
    return run


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


def _month_end(month: date) -> date:
    next_month = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    return next_month - timedelta(days=1)


def _xero_schedule(record: ExternalFinancialRecord) -> dict[str, Any]:
    payload = record.raw_payload if isinstance(record.raw_payload, dict) else {}
    schedule = payload.get("Schedule") if isinstance(payload.get("Schedule"), dict) else payload.get("schedule")
    return schedule if isinstance(schedule, dict) else {}


def _xero_schedule_date(record: ExternalFinancialRecord, *keys: str) -> Optional[date]:
    schedule = _xero_schedule(record)
    for key in keys:
        value = schedule.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        parsed = None
        try:
            from django.utils.dateparse import parse_date

            parsed = parse_date(str(value))
        except Exception:
            parsed = None
        if parsed:
            return parsed
    return None


def _xero_repeating_invoice_active_in_month(record: ExternalFinancialRecord, month: date) -> bool:
    status = str(record.status or "").upper()
    if status in {"DRAFT", "DELETED", "VOIDED"}:
        return False
    month_start = _month_start(month)
    month_end = _month_end(month_start)
    start = _xero_schedule_date(record, "StartDate", "start_date")
    if start is None and record.transaction_date and record.transaction_date <= month_end:
        start = record.transaction_date
    end = _xero_schedule_date(record, "EndDate", "end_date")
    if start and start > month_end:
        return False
    if end and end < month_start:
        return False
    return True


def _report_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", "."}:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return -amount if negative and amount > 0 else amount


def _normalize_report_label(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _iter_xero_report_entries(rows: Any, *, section: str = "") -> Iterable[dict[str, Any]]:
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_type = str(row.get("RowType") or row.get("row_type") or "").strip()
        title = str(row.get("Title") or row.get("title") or "").strip()
        cells = row.get("Cells") or row.get("cells") or []
        values = [
            str((cell or {}).get("Value") or (cell or {}).get("value") or "").strip()
            for cell in cells
            if isinstance(cell, dict)
        ]
        label = title or (values[0] if values else "")
        amount = None
        for value in reversed(values[1:] or values):
            amount = _report_decimal(value)
            if amount is not None:
                break
        if label and amount is not None:
            yield {
                "label": label,
                "normalized_label": _normalize_report_label(label),
                "amount": amount,
                "section": section,
                "normalized_section": _normalize_report_label(section),
                "row_type": row_type,
            }
        nested_rows = row.get("Rows") or row.get("rows")
        child_section = " > ".join(part for part in [section, title] if part)
        yield from _iter_xero_report_entries(nested_rows, section=child_section)


def _xero_report_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    reports = payload.get("Reports") or payload.get("reports") or []
    report = reports[0] if reports and isinstance(reports[0], dict) else {}
    return list(_iter_xero_report_entries(report.get("Rows") or report.get("rows") or []))


def _xero_report_entry_labels(entries: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for entry in entries:
        label = str(entry.get("label") or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels[:40]


def _find_xero_report_amount(entries: list[dict[str, Any]], labels: Iterable[str]) -> Optional[dict[str, Any]]:
    normalized_labels = {_normalize_report_label(label) for label in labels}
    for entry in entries:
        if entry["normalized_label"] in normalized_labels:
            return entry
    for entry in entries:
        if any(label in entry["normalized_label"] for label in normalized_labels if label):
            return entry
    return None


def _positive_xero_report_entry(entry: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not entry or entry.get("amount") is None:
        return None
    return {
        **entry,
        "amount": abs(entry["amount"]),
    }


def _xero_monthly_cost_entry(
    *,
    cost_of_sales: Optional[dict[str, Any]],
    operating_expenses: Optional[dict[str, Any]],
    total_expenses: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    cost_of_sales = _positive_xero_report_entry(cost_of_sales)
    operating_expenses = _positive_xero_report_entry(operating_expenses)
    total_expenses = _positive_xero_report_entry(total_expenses)
    if cost_of_sales and operating_expenses:
        return {
            "label": "Cost of Sales + Operating Expenses",
            "normalized_label": "cost of sales + operating expenses",
            "amount": cost_of_sales["amount"] + operating_expenses["amount"],
            "section": "",
            "normalized_section": "",
            "row_type": "calculated",
            "component_labels": [cost_of_sales.get("label"), operating_expenses.get("label")],
            "component_amounts": [str(cost_of_sales["amount"]), str(operating_expenses["amount"])],
        }
    return total_expenses or operating_expenses or cost_of_sales


def _parse_xero_profit_and_loss_report(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _xero_report_entries(payload)
    revenue = _find_xero_report_amount(entries, ["Total Income", "Total Revenue", "Income"])
    net = _find_xero_report_amount(entries, ["Net Profit", "Net Loss", "Net Profit/(Loss)", "Net Profit / (Loss)"])
    cost_of_sales = _find_xero_report_amount(
        entries,
        [
            "Total Cost of Sales",
            "Total Cost of Goods Sold",
            "Total Direct Costs",
            "Cost of Sales",
            "Cost of Goods Sold",
        ],
    )
    operating_expenses = _find_xero_report_amount(
        entries,
        [
            "Total Operating Expenses",
            "Operating Expenses",
        ],
    )
    total_expenses = _find_xero_report_amount(
        entries,
        [
            "Total Expenses",
            "Expenses",
            "Total Expense",
        ],
    )
    return {
        "entries": entries,
        "revenue": revenue,
        "net": net,
        "cost_of_sales": _positive_xero_report_entry(cost_of_sales),
        "operating_expenses": _positive_xero_report_entry(operating_expenses or total_expenses),
        "monthly_costs": _xero_monthly_cost_entry(
            cost_of_sales=cost_of_sales,
            operating_expenses=operating_expenses,
            total_expenses=total_expenses,
        ),
    }


def _parse_xero_balance_sheet_report(payload: dict[str, Any]) -> dict[str, Any]:
    entries = _xero_report_entries(payload)
    cash = _find_xero_report_amount(
        entries,
        [
            "Total Bank",
            "Total Cash",
            "Cash and Cash Equivalents",
            "Total Cash and Cash Equivalents",
            "Cash at Bank",
            "Total Cash at Bank",
        ],
    )
    if cash is None:
        bank_entries = [
            entry for entry in entries
            if "bank" in entry["normalized_section"] and entry["normalized_label"].startswith("total")
        ]
        cash = bank_entries[-1] if bank_entries else None
    return {
        "entries": entries,
        "cash": cash,
    }


def _xero_connection_currency(connection, records: list[ExternalFinancialRecord]) -> str:
    currencies = sorted({
        record.currency
        for record in records
        if record.connection_id == connection.id and record.currency
    })
    return currencies[0] if len(currencies) == 1 else ""


def _xero_report_metadata(
    *,
    source_metric: str,
    warnings: list[str],
    report_name: str,
    start_date: Optional[date],
    end_date: date,
    entry: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    return {
        "source_metric": source_metric,
        "warnings": warnings,
        "source_record_count": 0,
        "report_name": report_name,
        "report_start_date": start_date.isoformat() if start_date else None,
        "report_end_date": end_date.isoformat(),
        "report_row_label": entry.get("label") if entry else "",
        "report_row_section": entry.get("section") if entry else "",
        **(extra or {}),
    }


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
    connections = list(
        ExternalServiceConnection.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.XERO,
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("id")
    )
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

    def delete_report_metrics(month: date) -> None:
        StartupMetricObservation.objects.filter(
            organization=organization,
            source_provider=ExternalServiceProvider.XERO,
            period_month=month,
            metric_key__in=XERO_REPORT_METRIC_KEYS,
        ).delete()

    for currency in currencies or [""]:
        currency_recurring = records_for_currency(recurring_records, currency)
        current_recurring_metrics = [
            record for record in currency_recurring
            if _xero_repeating_invoice_active_in_month(record, current_month)
        ]
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

            customer_ids = {
                record.merchant_name
                for record in monthly_invoices + monthly_payments
                if str(record.merchant_name or "").strip()
            }
            if customer_ids:
                save_metric(
                    month=month,
                    key="customerCount",
                    name="Customer count",
                    value_text=str(len(customer_ids)),
                    value_number=Decimal(len(customer_ids)),
                    unit="count",
                    records_for_metric=monthly_invoices + monthly_payments,
                    summary="Unique Xero customer contacts with sales invoices or payments in the month.",
                    metadata=_metric_summary_metadata(
                        source_metric="xero_customer_count",
                        warnings=warnings,
                        records=monthly_invoices + monthly_payments,
                        extra={"customer_names": sorted(customer_ids)},
                    ),
                )

            month_summaries.setdefault(month.isoformat(), {})[currency or "unknown"] = {
                "invoice_revenue": str(invoice_revenue),
                "cash_collected": str(cash_collected),
                "invoice_count": len(monthly_invoices),
                "payment_count": len(monthly_payments),
                "customer_count": len(customer_ids),
            }

    report_months = [
        current_month,
        _previous_month_start(current_month),
        _previous_month_start(_previous_month_start(current_month)),
    ]
    report_metrics_available = False
    for month in report_months:
        delete_report_metrics(month)
    for connection in connections:
        if not xero_has_report_scope(connection.scopes):
            warnings.append(
                "Xero reports scope is missing; reconnect Xero to calculate Revenue, Burn Rate, Runway, and Revenue Growth from accounting reports."
            )
            continue
        profit_and_loss_by_month: dict[date, dict[str, Any]] = {}
        try:
            from integrations.services.external_connectors import fetch_xero_accounting_report

            for month in report_months:
                month_payload = fetch_xero_accounting_report(
                    connection,
                    "ProfitAndLoss",
                    params={"fromDate": month.isoformat(), "toDate": _month_end(month).isoformat()},
                )
                profit_and_loss_by_month[month] = _parse_xero_profit_and_loss_report(month_payload)
            balance_payload = fetch_xero_accounting_report(
                connection,
                "BalanceSheet",
                params={"date": _month_end(current_month).isoformat()},
            )
        except http_client.HTTPError as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code == 403:
                warnings.append(
                    "Xero reports permission was denied; reconnect Xero with Profit and Loss and Balance Sheet report scopes to calculate Revenue, Burn Rate, Runway, and Revenue Growth."
                )
            else:
                warnings.append(f"Xero reports could not be fetched: {str(exc) or 'request failed'}")
            continue
        except Exception as exc:
            warnings.append(f"Xero reports could not be fetched: {str(exc) or 'request failed'}")
            continue

        report_metrics_available = True
        currency = _xero_connection_currency(connection, records)
        current_report = profit_and_loss_by_month.get(current_month) or {}
        previous_report = profit_and_loss_by_month.get(_previous_month_start(current_month)) or {}
        current_revenue = current_report.get("revenue")
        previous_revenue = previous_report.get("revenue")
        monthly_costs = current_report.get("monthly_costs")
        operating_expenses = current_report.get("operating_expenses")
        cost_of_sales = current_report.get("cost_of_sales")
        current_report_labels = _xero_report_entry_labels(current_report.get("entries") or [])
        for report_month, report in profit_and_loss_by_month.items():
            revenue = report.get("revenue")
            if not revenue:
                continue
            report_labels = _xero_report_entry_labels(report.get("entries") or [])
            save_metric(
                month=report_month,
                key="revenue",
                name="Revenue",
                value_text=_format_money(revenue["amount"], currency),
                value_number=revenue["amount"],
                unit=currency,
                records_for_metric=[],
                summary="Revenue calculated from Xero Profit and Loss total income.",
                metadata=_xero_report_metadata(
                    source_metric="xero_profit_and_loss_revenue",
                    warnings=warnings,
                    report_name="ProfitAndLoss",
                    start_date=report_month,
                    end_date=_month_end(report_month),
                    entry=revenue,
                    extra={
                        "connection_id": connection.id,
                        "source_currency": currency,
                        "parsed_row_labels": report_labels,
                        "calculation_basis": "profit_and_loss_total_income",
                    },
                ),
            )
        if monthly_costs:
            save_metric(
                month=current_month,
                key="monthlyCosts",
                name="Monthly costs",
                value_text=_format_money(monthly_costs["amount"], currency),
                value_number=monthly_costs["amount"],
                unit=currency,
                records_for_metric=[],
                summary="Monthly costs calculated from Xero Profit and Loss expense rows.",
                metadata=_xero_report_metadata(
                    source_metric="xero_profit_and_loss_monthly_costs",
                    warnings=warnings,
                    report_name="ProfitAndLoss",
                    start_date=current_month,
                    end_date=_month_end(current_month),
                    entry=monthly_costs,
                    extra={
                        "connection_id": connection.id,
                        "source_currency": currency,
                        "parsed_row_labels": current_report_labels,
                        "calculation_basis": "cost_of_sales_plus_operating_expenses_when_available_otherwise_total_expenses",
                        "component_labels": monthly_costs.get("component_labels", []),
                        "component_amounts": monthly_costs.get("component_amounts", []),
                    },
                ),
            )
        if operating_expenses:
            save_metric(
                month=current_month,
                key="operatingExpenses",
                name="Operating expenses",
                value_text=_format_money(operating_expenses["amount"], currency),
                value_number=operating_expenses["amount"],
                unit=currency,
                records_for_metric=[],
                summary="Operating expenses calculated from Xero Profit and Loss expense rows.",
                metadata=_xero_report_metadata(
                    source_metric="xero_profit_and_loss_operating_expenses",
                    warnings=warnings,
                    report_name="ProfitAndLoss",
                    start_date=current_month,
                    end_date=_month_end(current_month),
                    entry=operating_expenses,
                    extra={
                        "connection_id": connection.id,
                        "source_currency": currency,
                        "parsed_row_labels": current_report_labels,
                        "calculation_basis": "profit_and_loss_operating_or_total_expenses",
                    },
                ),
            )
        if cost_of_sales:
            save_metric(
                month=current_month,
                key="costOfSales",
                name="Cost of sales",
                value_text=_format_money(cost_of_sales["amount"], currency),
                value_number=cost_of_sales["amount"],
                unit=currency,
                records_for_metric=[],
                summary="Cost of sales calculated from Xero Profit and Loss cost rows.",
                metadata=_xero_report_metadata(
                    source_metric="xero_profit_and_loss_cost_of_sales",
                    warnings=warnings,
                    report_name="ProfitAndLoss",
                    start_date=current_month,
                    end_date=_month_end(current_month),
                    entry=cost_of_sales,
                    extra={
                        "connection_id": connection.id,
                        "source_currency": currency,
                        "parsed_row_labels": current_report_labels,
                        "calculation_basis": "profit_and_loss_cost_of_sales",
                    },
                ),
            )
        if current_revenue and previous_revenue and previous_revenue["amount"] > 0:
            growth = (current_revenue["amount"] - previous_revenue["amount"]) / previous_revenue["amount"]
            save_metric(
                month=current_month,
                key="revenueGrowthRate",
                name="Revenue growth rate",
                value_text=_format_percent(growth),
                value_number=growth,
                unit="ratio",
                records_for_metric=[],
                summary="Revenue growth calculated from current and previous Xero Profit and Loss total income.",
                metadata=_xero_report_metadata(
                    source_metric="xero_profit_and_loss_revenue_growth",
                    warnings=warnings,
                    report_name="ProfitAndLoss",
                    start_date=_previous_month_start(current_month),
                    end_date=_month_end(current_month),
                    entry=current_revenue,
                    extra={
                        "connection_id": connection.id,
                        "source_currency": currency,
                        "previous_month": _previous_month_start(current_month).isoformat(),
                        "previous_revenue": str(previous_revenue["amount"]),
                        "current_revenue": str(current_revenue["amount"]),
                        "parsed_row_labels": current_report_labels,
                        "calculation_basis": "current_month_total_income_minus_previous_month_total_income_divided_by_previous_month_total_income",
                    },
                ),
            )
        current_net = current_report.get("net")
        current_burn: Optional[Decimal] = None
        if current_net:
            label = _normalize_report_label(current_net.get("label"))
            amount = current_net["amount"]
            if "loss" in label and amount > 0:
                current_burn = amount
            elif amount < 0:
                current_burn = abs(amount)
        if current_burn and current_burn > 0:
            save_metric(
                month=current_month,
                key="burnRate",
                name="Burn rate",
                value_text=_format_money(current_burn, currency),
                value_number=current_burn,
                unit=currency,
                records_for_metric=[],
                summary="Burn rate calculated as positive net accounting loss from Xero Profit and Loss.",
                metadata=_xero_report_metadata(
                    source_metric="xero_profit_and_loss_accounting_burn",
                    warnings=warnings,
                    report_name="ProfitAndLoss",
                    start_date=current_month,
                    end_date=_month_end(current_month),
                    entry=current_net,
                    extra={
                        "connection_id": connection.id,
                        "source_currency": currency,
                        "parsed_row_labels": current_report_labels,
                        "calculation_basis": "positive_net_loss",
                    },
                ),
            )
        trailing_burns: list[Decimal] = []
        for month in report_months:
            net_entry = (profit_and_loss_by_month.get(month) or {}).get("net")
            if not net_entry:
                continue
            amount = net_entry["amount"]
            label = _normalize_report_label(net_entry.get("label"))
            if "loss" in label and amount > 0:
                trailing_burns.append(amount)
            elif amount < 0:
                trailing_burns.append(abs(amount))
        balance = _parse_xero_balance_sheet_report(balance_payload)
        cash_entry = balance.get("cash")
        if cash_entry and cash_entry["amount"] > 0 and trailing_burns:
            average_burn = sum(trailing_burns, Decimal("0")) / Decimal(len(trailing_burns))
            if average_burn > 0:
                runway_months = cash_entry["amount"] / average_burn
                runway_value_number = runway_months.quantize(Decimal("0.0001"))
                runway_text = f"{_format_decimal(runway_months, places='0.1')} months"
                save_metric(
                    month=current_month,
                    key="runway",
                    name="Runway",
                    value_text=runway_text,
                    value_number=runway_value_number,
                    unit="months",
                    records_for_metric=[],
                    summary="Runway calculated from Xero Balance Sheet cash divided by trailing accounting burn.",
                    metadata=_xero_report_metadata(
                        source_metric="xero_balance_sheet_runway",
                        warnings=warnings,
                        report_name="BalanceSheet",
                        start_date=None,
                        end_date=_month_end(current_month),
                        entry=cash_entry,
                        extra={
                            "connection_id": connection.id,
                            "source_currency": currency,
                            "cash_balance": str(cash_entry["amount"]),
                            "average_burn": str(average_burn),
                            "burn_month_count": len(trailing_burns),
                            "parsed_row_labels": _xero_report_entry_labels(balance.get("entries") or []),
                            "calculation_basis": "cash_balance_divided_by_trailing_positive_net_loss",
                        },
                    ),
                )

    if report_metrics_available:
        month_summaries.setdefault(current_month.isoformat(), {}).setdefault("reports", {})["available"] = True

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


def build_linear_run_context(*, organization: Organization, selected_project_ids: Optional[list[str]] = None) -> dict:
    queryset = LinearProjectSelection.objects.filter(
        organization=organization,
        selected=True,
    ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
    if selected_project_ids:
        queryset = queryset.filter(linear_project_id__in=selected_project_ids)
    projects = [
        {
            "project_id": selection.linear_project_id,
            "project_name": selection.project_name,
            "status": selection.project_status,
            "health": selection.project_health,
            "last_synced_at": selection.last_synced_at.isoformat() if selection.last_synced_at else None,
        }
        for selection in queryset.order_by("project_name", "linear_project_id")
    ]
    project_ids = [project["project_id"] for project in projects]
    artifacts = LinearProjectArtifact.objects.filter(
        organization=organization,
        linear_project_id__in=project_ids,
    )
    issue_count = LinearIssueArtifact.objects.filter(organization=organization, project__in=artifacts).count()
    update_count = LinearProjectUpdateArtifact.objects.filter(organization=organization, project__in=artifacts).count()
    return {
        "source": "linear",
        "purpose": "selected_project_management_context",
        "warning": "Linear is project-management context only and is not financial truth.",
        "selected_project_ids": project_ids,
        "selected_project_count": len(projects),
        "selected_projects": projects,
        "cached_project_count": artifacts.count(),
        "cached_issue_count": issue_count,
        "cached_update_count": update_count,
        "warnings": [] if projects else ["Linear was selected but no projects are selected."],
    }


def latest_external_connection_for_startup(
    *,
    user,
    organization: Organization,
    provider: str,
) -> Optional[ExternalServiceConnection]:
    connection = (
        user.external_service_connections.filter(
            provider=provider,
            organization=organization,
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )
    if connection is not None:
        return connection
    return (
        user.external_service_connections.filter(provider=provider)
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )


def build_notion_run_context(*, organization: Organization) -> dict:
    connection = (
        ExternalServiceConnection.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.NOTION,
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )
    sync_cursor = connection.sync_cursor if connection is not None and isinstance(connection.sync_cursor, dict) else {}
    return {
        "source": "notion",
        "purpose": "founder_workspace_context",
        "scope": "whole_accessible_workspace",
        "connection_id": connection.id if connection else None,
        "workspace": connection.account_label if connection else "",
        "last_synced_at": connection.last_synced_at.isoformat() if connection and connection.last_synced_at else None,
        "index_partial": bool(sync_cursor.get("startup_update_index_partial", True)) if connection else True,
        "warnings": [] if connection else ["Notion was selected but no Notion connection is available."],
    }


def build_google_analytics_run_context(*, organization: Organization) -> dict:
    connection = (
        ExternalServiceConnection.objects.filter(
            organization=organization,
            provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
        )
        .exclude(status=ExternalServiceConnectionStatus.DISCONNECTED)
        .order_by("-updated_at", "-id")
        .first()
    )
    selected_properties = []
    if connection is not None:
        selected_properties = [
            {
                "property_id": selection.property_id,
                "property_display_name": selection.property_display_name,
                "account_id": selection.account_id,
                "account_display_name": selection.account_display_name,
            }
            for selection in GoogleAnalyticsPropertySelection.objects.filter(
                connection=connection,
                selected=True,
            ).order_by("property_display_name", "property_id")
        ]
    property_ids = [item["property_id"] for item in selected_properties]
    warnings = []
    if connection is None:
        warnings.append("Google Analytics was selected but no Google Analytics connection is available.")
    elif not property_ids:
        warnings.append("Google Analytics was selected but no property is selected.")
    return {
        "source": "google_analytics",
        "purpose": "selected_web_product_analytics_context",
        "warning": "Google Analytics is deterministic web/product analytics context and is not financial truth.",
        "connection_id": connection.id if connection else None,
        "account_label": connection.account_label if connection else "",
        "selected_property_ids": property_ids,
        "selected_property_count": len(property_ids),
        "selected_properties": selected_properties,
        "last_synced_at": connection.last_synced_at.isoformat() if connection and connection.last_synced_at else None,
        "warnings": warnings,
    }


def build_external_context_for_sources(
    *,
    organization: Organization,
    input_sources: Optional[list[str]],
    start_date: date,
    end_date: date,
    source_warnings: Optional[dict[str, list[str]]] = None,
    manual_document_ids: Optional[list[str]] = None,
    manual_summary: Optional[str] = None,
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
    if ExternalServiceProvider.LINEAR in selected:
        context["linear"] = build_linear_run_context(
            organization=organization,
            selected_project_ids=None,
        )
    if ExternalServiceProvider.NOTION in selected:
        context["notion"] = build_notion_run_context(organization=organization)
    if ExternalServiceProvider.GOOGLE_ANALYTICS in selected:
        context["google_analytics"] = build_google_analytics_run_context(organization=organization)
    if MANUAL_DOCUMENTS_SOURCE in selected:
        context[MANUAL_DOCUMENTS_SOURCE] = build_manual_documents_run_context(
            organization=organization,
            document_ids=manual_document_ids,
            summary=manual_summary,
        )
    if warnings_by_source.get("gmail"):
        context["gmail"] = {
            "source_unavailable": True,
            "needs_reconnect": True,
            "warnings": list(warnings_by_source["gmail"]),
        }
    return context


def _normalize_manual_document_id_list(document_ids: Optional[list[str]]) -> list[str]:
    if not document_ids:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_id in document_ids:
        value = str(raw_id or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _manual_document_context_excerpt(text: str, remaining_chars: int) -> tuple[str, int]:
    limit = min(MAX_MANUAL_DOCUMENT_CONTEXT_CHARS_PER_DOCUMENT, max(remaining_chars, 0))
    if limit <= 0:
        return "", 0
    excerpt = str(text or "").strip()[:limit].strip()
    return excerpt, len(excerpt)


def build_manual_documents_run_context(
    *,
    organization: Organization,
    document_ids: Optional[list[str]],
    summary: Optional[str] = None,
) -> dict[str, Any]:
    normalized_ids = _normalize_manual_document_id_list(document_ids)
    context: dict[str, Any] = {
        "summary": str(summary or "").strip(),
        "documents": [],
        "warnings": [],
    }
    if not normalized_ids:
        if not context["summary"]:
            context["warnings"].append("Manual documents were selected but no uploaded documents or summary were provided.")
        return context

    documents_by_id = {
        str(document.id): document
        for document in StartupManualDocument.objects.filter(
            organization=organization,
            id__in=normalized_ids,
        ).order_by("-created_at")
    }
    remaining_chars = MAX_MANUAL_DOCUMENT_CONTEXT_CHARS
    for document_id in normalized_ids:
        document = documents_by_id.get(document_id)
        if document is None:
            context["warnings"].append(f"Manual document {document_id} was not found for this startup.")
            continue

        text_excerpt = ""
        excerpt_chars = 0
        if document.extraction_status == "processed" and document.extracted_text.strip():
            text_excerpt, excerpt_chars = _manual_document_context_excerpt(document.extracted_text, remaining_chars)
            remaining_chars -= excerpt_chars
        else:
            context["warnings"].append(
                f"{document.original_filename} could not be used as extracted text "
                f"({document.extraction_status or 'unknown'})."
            )

        context["documents"].append(
            {
                "id": str(document.id),
                "filename": document.original_filename,
                "content_type": document.content_type,
                "file_size_bytes": document.file_size_bytes,
                "extraction_status": document.extraction_status,
                "parse_notes": document.parse_notes,
                "text_size_chars": document.text_size_chars,
                "text_excerpt": text_excerpt,
                "text_excerpt_chars": excerpt_chars,
                "uploaded_at": document.created_at.isoformat() if document.created_at else None,
            }
        )

    if remaining_chars <= 0:
        context["warnings"].append("Manual document context was truncated before being sent to the draft generator.")
    return context


def refresh_startup_update_run_source_context(
    *,
    run: ContentFactoryRun,
    organization: Organization,
    input_sources: Optional[list[str]],
    start_date: date,
    end_date: date,
    source_warnings: Optional[dict[str, list[str]]] = None,
    manual_document_ids: Optional[list[str]] = None,
    manual_summary: Optional[str] = None,
) -> ContentFactoryRun:
    run_request = dict(run.run_request or {})
    selected_input_sources = normalize_startup_update_input_sources(input_sources)
    reconcile_startup_update_run_source_steps(run=run, input_sources=selected_input_sources)
    if selected_input_sources:
        run_request["input_sources"] = list(selected_input_sources)
    if MANUAL_DOCUMENTS_SOURCE in set(selected_input_sources):
        document_ids = (
            _normalize_manual_document_id_list(manual_document_ids)
            if manual_document_ids is not None
            else _normalize_manual_document_id_list(run_request.get("manual_document_ids"))
        )
        summary = str(
            manual_summary
            if manual_summary is not None
            else run_request.get("manual_summary") or ""
        ).strip()
        run_request["manual_document_ids"] = document_ids
        run_request["manual_summary"] = summary
    else:
        run_request.pop("manual_document_ids", None)
        run_request.pop("manual_summary", None)
    if ExternalServiceProvider.SLACK in set(selected_input_sources):
        run_request["slack_channel_ids"] = [
            selection.channel_id
            for selection in SlackChannelSelection.objects.filter(
                organization=organization,
                selected=True,
            ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        ]
    if ExternalServiceProvider.LINEAR in set(selected_input_sources):
        run_request["linear_project_ids"] = [
            selection.linear_project_id
            for selection in LinearProjectSelection.objects.filter(
                organization=organization,
                selected=True,
            ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        ]
    if ExternalServiceProvider.NOTION in set(selected_input_sources):
        binding = (
            UserStartupBinding.objects.select_related("user")
            .filter(id=run_request.get("binding_id"), organization=organization)
            .first()
            or organization.user_startup_bindings.select_related("user").first()
        )
        notion_connection = (
            latest_external_connection_for_startup(
                user=binding.user,
                organization=organization,
                provider=ExternalServiceProvider.NOTION,
            )
            if binding is not None
            else None
        )
        if notion_connection is not None:
            if notion_connection.organization_id != organization.id:
                notion_connection.organization = organization
                notion_connection.save(update_fields=["organization", "updated_at"])
            run_request["notion_connection_id"] = notion_connection.id
    if ExternalServiceProvider.GOOGLE_ANALYTICS in set(selected_input_sources):
        binding = (
            UserStartupBinding.objects.select_related("user")
            .filter(id=run_request.get("binding_id"), organization=organization)
            .first()
            or organization.user_startup_bindings.select_related("user").first()
        )
        ga_connection = (
            latest_external_connection_for_startup(
                user=binding.user,
                organization=organization,
                provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
            )
            if binding is not None
            else None
        )
        if ga_connection is not None:
            if ga_connection.organization_id != organization.id:
                ga_connection.organization = organization
                ga_connection.save(update_fields=["organization", "updated_at"])
            run_request["google_analytics_connection_id"] = ga_connection.id
            run_request["google_analytics_property_ids"] = [
                selection.property_id
                for selection in GoogleAnalyticsPropertySelection.objects.filter(
                    connection=ga_connection,
                    selected=True,
                ).order_by("property_display_name", "property_id")
            ]
    external_context = build_external_context_for_sources(
        organization=organization,
        input_sources=selected_input_sources,
        start_date=start_date,
        end_date=end_date,
        source_warnings=source_warnings,
        manual_document_ids=run_request.get("manual_document_ids"),
        manual_summary=run_request.get("manual_summary"),
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


def record_valley_dispatch_result(run: ContentFactoryRun, dispatch_result: ValleyHarnessResult | object) -> None:
    meta = _get_run_meta(run)
    meta["last_dispatch_attempt_at"] = timezone.now().isoformat()
    raw_status_code = getattr(dispatch_result, "status_code", None)
    status_code = raw_status_code if isinstance(raw_status_code, int) else None
    if bool(dispatch_result):
        payload = getattr(dispatch_result, "payload", None)
        response_payload = payload if isinstance(payload, dict) else {}
        meta["dispatch_status"] = "queued"
        meta["last_dispatch_job_id"] = response_payload.get("job_id") or ""
        meta["last_dispatch_error"] = ""
        meta["last_dispatch_error_kind"] = ""
        meta["last_dispatch_status_code"] = status_code
    else:
        meta["dispatch_status"] = "failed"
        meta["last_dispatch_error"] = str(getattr(dispatch_result, "detail", "") or "Valley dispatch failed.")[:300]
        meta["last_dispatch_error_kind"] = str(getattr(dispatch_result, "failure_kind", "") or "unknown")
        meta["last_dispatch_status_code"] = status_code
    _set_run_meta(run, meta)
    run.save(update_fields=["result", "updated_at"])


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
        "organization_kind": profile.organization_kind,
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
    target_month: Optional[date] = None,
    input_sources: Optional[list[str]] = None,
) -> Optional[ContentFactoryRun]:
    runs = _iter_startup_update_runs(
        organization=organization,
        statuses=OPEN_RUN_STATUSES,
    )
    target = _month_start(target_month) if target_month is not None else None

    def matches_target(run: ContentFactoryRun) -> bool:
        return target is None or startup_update_run_matches_target_month(run, target)

    def matches_sources(run: ContentFactoryRun) -> bool:
        return startup_update_run_matches_input_sources(
            run,
            input_sources,
            google_connection_id=google_connection_id,
        )

    if google_connection_id is None:
        for run in runs:
            if matches_target(run) and matches_sources(run):
                return run
        return None

    legacy_candidate = None
    for run in runs:
        run_google_connection_id = get_startup_update_run_google_connection_id(run)
        if run_google_connection_id == google_connection_id:
            if matches_target(run) and matches_sources(run):
                return run
            continue
        if (
            run_google_connection_id is None
            and legacy_candidate is None
            and matches_target(run)
            and matches_sources(run)
        ):
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
    target_month: Optional[Union[str, date, datetime]] = None,
    manual_document_ids: Optional[list[str]] = None,
    manual_summary: Optional[str] = None,
    force_regenerate: bool = False,
) -> ContentFactoryRun:
    now = timezone.now()
    windows = build_startup_update_target_windows(target_month, reference=now)
    selected_target_month = windows["target_month"]
    selected_input_sources = normalize_startup_update_input_sources(input_sources)
    google_connection = binding.google_connection or getattr(binding.user, "google_connection", None)
    selected_input_sources, google_connection, gmail_scope_warnings = coerce_startup_update_sources_for_gmail_scope(
        selected_input_sources,
        google_connection,
    )
    source_warnings = merge_source_warnings(source_warnings, gmail_scope_warnings)
    selected_source_set = set(selected_input_sources)
    google_connection_id = google_connection.id if google_connection else None
    step_order = build_startup_update_step_order(selected_input_sources)

    existing = get_open_startup_update_run(
        organization=organization,
        google_connection_id=google_connection_id,
        target_month=selected_target_month,
        input_sources=selected_input_sources,
    )
    if existing:
        pin_startup_update_run_connection(existing, google_connection_id)
        set_startup_update_run_target_month(
            existing,
            selected_target_month,
            reference=now,
            window_months=window_months,
        )
        refresh_startup_update_run_source_context(
            run=existing,
            organization=organization,
            input_sources=selected_input_sources,
            start_date=windows["financial_start_date"],
            end_date=windows["financial_end_date"],
            source_warnings=source_warnings,
            manual_document_ids=manual_document_ids,
            manual_summary=manual_summary,
        )
        supersede_conflicting_startup_update_runs(
            organization=organization,
            google_connection_id=google_connection_id,
            keep_run_id=existing.run_id,
        )
        if force_regenerate and not (existing.run_request or {}).get("force_regenerate"):
            existing_request = dict(existing.run_request or {})
            existing_request["force_regenerate"] = True
            existing.run_request = existing_request
            existing.save(update_fields=["run_request", "updated_at"])
        return existing

    backfill_start = windows["narrative_start"]
    backfill_end = windows["narrative_end"]
    current_month = selected_target_month
    months = [selected_target_month]
    financial_start_date = windows["financial_start_date"]
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
        "target_month": current_month.isoformat(),
        "draft_months": [item.isoformat() for item in months],
        "current_month": current_month.isoformat(),
        "backfill_window_start": backfill_start.isoformat(),
        "backfill_window_end": backfill_end.isoformat(),
        "startup_context": startup_context,
    }
    run_request["input_sources"] = list(selected_input_sources)
    run_request["force_regenerate"] = bool(force_regenerate)
    if MANUAL_DOCUMENTS_SOURCE in selected_source_set:
        run_request["manual_document_ids"] = _normalize_manual_document_id_list(manual_document_ids)
        run_request["manual_summary"] = str(manual_summary or "").strip()
    if ExternalServiceProvider.NOTION in selected_source_set:
        notion_connection = latest_external_connection_for_startup(
            user=binding.user,
            organization=organization,
            provider=ExternalServiceProvider.NOTION,
        )
        if notion_connection is not None:
            if notion_connection.organization_id != organization.id:
                notion_connection.organization = organization
                notion_connection.save(update_fields=["organization", "updated_at"])
            run_request["notion_connection_id"] = notion_connection.id
    selected_ga_property_ids = []
    if ExternalServiceProvider.GOOGLE_ANALYTICS in selected_source_set:
        ga_connection = latest_external_connection_for_startup(
            user=binding.user,
            organization=organization,
            provider=ExternalServiceProvider.GOOGLE_ANALYTICS,
        )
        if ga_connection is not None:
            if ga_connection.organization_id != organization.id:
                ga_connection.organization = organization
                ga_connection.save(update_fields=["organization", "updated_at"])
            run_request["google_analytics_connection_id"] = ga_connection.id
            selected_ga_property_ids = [
                selection.property_id
                for selection in GoogleAnalyticsPropertySelection.objects.filter(
                    connection=ga_connection,
                    selected=True,
                ).order_by("property_display_name", "property_id")
            ]
            run_request["google_analytics_property_ids"] = selected_ga_property_ids
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
    selected_linear_projects = []
    if ExternalServiceProvider.LINEAR in selected_source_set:
        selected_linear_projects = [
            selection.linear_project_id
            for selection in LinearProjectSelection.objects.filter(
                organization=organization,
                selected=True,
            ).exclude(connection__status=ExternalServiceConnectionStatus.DISCONNECTED)
        ]
        run_request["linear_project_ids"] = selected_linear_projects
    external_context = build_external_context_for_sources(
        organization=organization,
        input_sources=selected_input_sources,
        start_date=financial_start_date,
        end_date=windows["financial_end_date"],
        source_warnings=source_warnings,
        manual_document_ids=run_request.get("manual_document_ids"),
        manual_summary=run_request.get("manual_summary"),
    )
    if external_context:
        if ExternalServiceProvider.SLACK in external_context:
            external_context[ExternalServiceProvider.SLACK]["selected_channel_ids"] = selected_slack_channels
        if ExternalServiceProvider.LINEAR in external_context:
            external_context[ExternalServiceProvider.LINEAR]["selected_project_ids"] = selected_linear_projects
        if ExternalServiceProvider.GOOGLE_ANALYTICS in external_context:
            external_context[ExternalServiceProvider.GOOGLE_ANALYTICS]["selected_property_ids"] = selected_ga_property_ids
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
                "backfill_window_end": backfill_end,
            },
        )
    if ExternalServiceProvider.XERO in selected_source_set:
        refresh_startup_update_run_source_context(
            run=run,
            organization=organization,
            input_sources=selected_input_sources,
            start_date=financial_start_date,
            end_date=windows["financial_end_date"],
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
        def _dispatch_to_valley() -> None:
            record_valley_dispatch_result(run, notify_valley_run_created(run.run_id))

        transaction.on_commit(_dispatch_to_valley)
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


def _slack_thread_haystack(thread: SlackThreadArtifact) -> str:
    participant_summary = thread.participant_summary or {}
    participants = participant_summary.get("participants") if isinstance(participant_summary, dict) else []
    payload_text = " ".join(
        str(item.get("cleaned_text") or item.get("text") or "")
        for item in (thread.message_payloads or [])
        if isinstance(item, dict)
    )
    return " ".join(
        [
            thread.channel_name or "",
            thread.channel_id or "",
            thread.cleaned_text or "",
            payload_text,
            " ".join(str(item or "") for item in (participants or [])),
        ]
    ).lower()


def _slack_message_payload_text(payload: dict[str, Any]) -> str:
    return str(payload.get("cleaned_text") or payload.get("text") or "").strip()


def _is_short_slack_acknowledgement(text: str) -> bool:
    normalized = re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()
    if not normalized:
        return True
    if normalized in {item.replace("+", "").strip() for item in SLACK_LOW_SIGNAL_PATTERNS}:
        return True
    words = normalized.split()
    return len(words) <= 3 and normalized in {"ok", "okay", "yes", "yep", "done", "nice", "cool", "thanks", "thank you"}


def score_slack_thread_for_profile(profile: StartupProfile, thread: SlackThreadArtifact) -> tuple[int, list[str], str, bool]:
    haystack = _slack_thread_haystack(thread)
    profile_signals = _profile_signal_lists(profile)
    allowlist_terms = (
        profile_signals["company_aliases"]
        + profile_signals["positive_keywords"]
        + profile_signals["founder_names"]
        + profile_signals["team_names"]
        + profile_signals["investor_names"]
        + profile_signals["customer_names"]
        + profile_signals["prospect_names"]
        + profile_signals["competitor_names"]
        + HIGH_SIGNAL_TERMS
    )
    allowlist_reasons = []
    if _match_any(allowlist_terms, haystack):
        allowlist_reasons.append("allowlist_profile_or_high_signal_term")

    payloads = [item for item in (thread.message_payloads or []) if isinstance(item, dict)]
    texts = [_slack_message_payload_text(item) for item in payloads]
    non_empty_texts = [text for text in texts if text]
    hard_reasons = []
    if not str(thread.cleaned_text or "").strip() and not non_empty_texts:
        hard_reasons.append("hard_filtered_empty_thread")
    if non_empty_texts and all(_is_short_slack_acknowledgement(text) for text in non_empty_texts):
        hard_reasons.append("hard_filtered_acknowledgement_only")
    if any(pattern in haystack for pattern in SLACK_HARD_IRRELEVANT_PATTERNS):
        hard_reasons.append("hard_filtered_slack_system_or_routine_automation")

    author_names = [
        str(item.get("author_name") or item.get("author_id") or "").lower()
        for item in payloads
    ]
    automation_authors = [
        name for name in author_names
        if any(pattern in name for pattern in SLACK_AUTOMATION_AUTHOR_PATTERNS)
    ]
    if automation_authors and len(automation_authors) >= max(len(author_names), 1):
        hard_reasons.append("hard_filtered_automation_author")

    if hard_reasons and not allowlist_reasons:
        return 0, _uniq(hard_reasons), GmailRelevanceLabel.IRRELEVANT, True

    score = 45
    reasons = []
    if hard_reasons and allowlist_reasons:
        reasons.append("allowlist_override_hard_filter")
    if any(pattern in haystack for pattern in SLACK_LOW_SIGNAL_PATTERNS):
        score -= 15
        reasons.append("matched_low_signal_slack_pattern")
    if _match_any(profile_signals["company_aliases"] + profile_signals["positive_keywords"], haystack):
        score += 15
        reasons.append("matched_company_alias_or_positive_keyword")
    if _match_any(profile_signals["founder_names"] + profile_signals["team_names"], haystack):
        score += 10
        reasons.append("matched_founder_or_team_name")
    if _match_any(profile_signals["investor_names"], haystack):
        score += 20
        reasons.append("matched_investor_name")
    if _match_any(profile_signals["customer_names"] + profile_signals["prospect_names"], haystack):
        score += 20
        reasons.append("matched_customer_or_prospect_name")
    if _match_any(profile_signals["competitor_names"], haystack):
        score += 10
        reasons.append("matched_competitor_name")
    if _match_any(HIGH_SIGNAL_TERMS, haystack):
        score += 20
        reasons.append("matched_high_signal_term")
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
    return score, _uniq([*reasons, *allowlist_reasons]), label, False


def apply_slack_profile_scoring(
    profile: StartupProfile,
    thread: SlackThreadArtifact,
    *,
    persist: bool = True,
) -> tuple[int, list[str], str, bool]:
    score, reasons, label, hard_filtered = score_slack_thread_for_profile(profile, thread)
    thread.heuristic_score = score
    thread.heuristic_reasons = reasons
    if hard_filtered and thread.classified_at is None:
        thread.relevance_label = GmailRelevanceLabel.IRRELEVANT
        thread.relevance_score = 0.0
        thread.relevance_reason = ", ".join(reasons)
        thread.needs_extraction = False
        thread.classified_at = timezone.now()
    if persist:
        update_fields = [
            "heuristic_score",
            "heuristic_reasons",
            "updated_at",
        ]
        if hard_filtered:
            update_fields.extend([
                "relevance_label",
                "relevance_score",
                "relevance_reason",
                "needs_extraction",
                "classified_at",
            ])
        thread.save(update_fields=[*dict.fromkeys(update_fields)])
    return thread.heuristic_score, thread.heuristic_reasons, thread.relevance_label, hard_filtered


def _compact_text(value: str, *, max_chars: int) -> tuple[str, bool]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    quote_markers = [
        "\nOn ",
        "\nFrom:",
        "\nSent:",
        "\n-----Original Message-----",
    ]
    cut_at = len(text)
    for marker in quote_markers:
        index = text.find(marker)
        if index > 0:
            cut_at = min(cut_at, index)
    lines = []
    for line in text[:cut_at].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue
        if stripped.lower() in {"unsubscribe", "view in browser"}:
            continue
        lines.append(stripped)
    compacted = "\n".join(lines).strip()
    truncated = False
    if len(compacted) > max_chars:
        compacted = compacted[:max_chars].rstrip()
        truncated = True
    return compacted, truncated or cut_at < len(text)


def _payload_is_high_signal(payload: dict[str, Any], profile: StartupProfile) -> bool:
    haystack = " ".join(
        [
            str(payload.get("subject") or ""),
            str(payload.get("from_address") or ""),
            str(payload.get("author_name") or ""),
            _slack_message_payload_text(payload),
        ]
    ).lower()
    profile_signals = _profile_signal_lists(profile)
    terms = (
        profile_signals["company_aliases"]
        + profile_signals["positive_keywords"]
        + profile_signals["founder_names"]
        + profile_signals["team_names"]
        + profile_signals["investor_names"]
        + profile_signals["customer_names"]
        + profile_signals["prospect_names"]
        + HIGH_SIGNAL_TERMS
    )
    return _match_any(terms, haystack)


def compact_gmail_thread_bundle(
    thread: GmailThreadArtifact,
    *,
    profile: StartupProfile,
    attachments: list[Any],
) -> dict[str, Any]:
    payloads = [item for item in (thread.message_payloads or []) if isinstance(item, dict)]
    important_ids = {
        str(item.get("message_id") or "")
        for item in payloads
        if _payload_is_high_signal(item, profile)
    }
    if not important_ids and payloads:
        important_ids.add(str(payloads[-1].get("message_id") or ""))

    kept_payloads = []
    omitted_count = 0
    used_chars = 0
    for index, payload in enumerate(payloads):
        message_id = str(payload.get("message_id") or "")
        keep = (
            message_id in important_ids
            or index == 0
            or index >= len(payloads) - 3
            or len(kept_payloads) < 4
        )
        if not keep:
            omitted_count += 1
            continue
        compacted_text, truncated = _compact_text(
            str(payload.get("cleaned_text") or ""),
            max_chars=2500,
        )
        if used_chars + len(compacted_text) > GMAIL_COMPACT_MAX_CHARS and kept_payloads:
            omitted_count += 1
            continue
        compacted_payload = {**payload, "cleaned_text": compacted_text}
        if truncated:
            compacted_payload["compression_note"] = "quoted_or_long_text_trimmed"
        kept_payloads.append(compacted_payload)
        used_chars += len(compacted_text)
        if len(kept_payloads) >= GMAIL_COMPACT_MAX_MESSAGES:
            omitted_count += max(len(payloads) - index - 1, 0)
            break

    cleaned_text = "\n\n".join(
        str(item.get("cleaned_text") or "").strip()
        for item in kept_payloads
        if str(item.get("cleaned_text") or "").strip()
    )
    compression_notes = []
    if omitted_count:
        compression_notes.append(f"omitted_{omitted_count}_low_signal_or_over_budget_messages")
    return {
        "gmail_thread_id": thread.gmail_thread_id,
        "source_message_ids": thread.source_message_ids or [],
        "source_message_count": thread.source_message_count,
        "cleaned_text": cleaned_text or thread.cleaned_text[:GMAIL_COMPACT_MAX_CHARS],
        "participant_summary": {
            **(thread.participant_summary or {}),
            "compression": {
                "original_message_count": len(payloads),
                "kept_message_count": len(kept_payloads),
                "omitted_message_count": omitted_count,
            },
        },
        "message_payloads": kept_payloads,
        "attachments": attachments,
        "omitted_message_count": omitted_count,
        "compression_notes": compression_notes,
    }


def compact_slack_thread_bundle(thread: SlackThreadArtifact) -> dict[str, Any]:
    payloads = [item for item in (thread.message_payloads or []) if isinstance(item, dict)]
    hint_ids = set()
    extraction_hints = thread.extraction_hints or {}
    if isinstance(extraction_hints, dict):
        hint_ids = {str(item or "") for item in extraction_hints.get("important_message_ids") or []}

    kept_payloads = []
    omitted_count = 0
    used_chars = 0
    for index, payload in enumerate(payloads):
        message_id = str(payload.get("message_id") or "")
        text = _slack_message_payload_text(payload)
        high_signal = any(term in text.lower() for term in HIGH_SIGNAL_TERMS)
        keep = (
            message_id in hint_ids
            or high_signal
            or index == 0
            or index >= len(payloads) - 5
            or len(kept_payloads) < 6
        )
        if not keep:
            omitted_count += 1
            continue
        compacted_text, truncated = _compact_text(text, max_chars=1000)
        if used_chars + len(compacted_text) > SLACK_COMPACT_MAX_CHARS and kept_payloads:
            omitted_count += 1
            continue
        compacted_payload = {**payload, "cleaned_text": compacted_text}
        if truncated:
            compacted_payload["compression_note"] = "long_text_trimmed"
        kept_payloads.append(compacted_payload)
        used_chars += len(compacted_text)
        if len(kept_payloads) >= SLACK_COMPACT_MAX_MESSAGES:
            omitted_count += max(len(payloads) - index - 1, 0)
            break

    lines = []
    for item in kept_payloads:
        posted = item.get("posted_at") or item.get("message_ts") or ""
        author = item.get("author_name") or item.get("author_id") or "Slack user"
        text = item.get("cleaned_text") or ""
        if text:
            lines.append(f"[{posted}] {author}: {text}")

    compression_notes = []
    if omitted_count:
        compression_notes.append(f"omitted_{omitted_count}_low_signal_or_over_budget_messages")
    return {
        "slack_thread_id": f"slack:{thread.channel_id}:{thread.thread_ts}",
        "channel_id": thread.channel_id,
        "channel_name": thread.channel_name,
        "thread_ts": thread.thread_ts,
        "source_message_ids": thread.source_message_ids or [],
        "source_message_count": thread.source_message_count,
        "cleaned_text": "\n".join(lines) or thread.cleaned_text[:SLACK_COMPACT_MAX_CHARS],
        "participant_summary": {
            **(thread.participant_summary or {}),
            "compression": {
                "original_message_count": len(payloads),
                "kept_message_count": len(kept_payloads),
                "omitted_message_count": omitted_count,
            },
        },
        "message_payloads": kept_payloads,
        "heuristic_score": thread.heuristic_score,
        "heuristic_reasons": thread.heuristic_reasons or [],
        "relevance_score": thread.relevance_score,
        "relevance_reason": thread.relevance_reason,
        "extraction_hints": extraction_hints if isinstance(extraction_hints, dict) else {},
        "omitted_message_count": omitted_count,
        "compression_notes": compression_notes,
    }


def _linear_project_public_id(project: LinearProjectArtifact) -> str:
    return f"linear:project:{project.linear_project_id}"


def _linear_issue_public_id(issue: LinearIssueArtifact) -> str:
    return f"linear:issue:{issue.identifier or issue.linear_issue_id}"


def _linear_update_public_id(update: LinearProjectUpdateArtifact) -> str:
    return f"linear:update:{update.linear_project_update_id}"


def compact_linear_project_bundle(project: LinearProjectArtifact) -> dict[str, Any]:
    extraction_hints = project.extraction_hints if isinstance(project.extraction_hints, dict) else {}
    important_issue_ids = {str(item or "") for item in extraction_hints.get("important_issue_ids") or []}
    important_update_ids = {str(item or "") for item in extraction_hints.get("important_update_ids") or []}

    issue_queryset = project.issues.order_by("-updated_at_linear", "-id")
    update_queryset = project.project_updates.order_by("-updated_at_linear", "-id")
    issues = []
    omitted_issue_count = 0
    for index, issue in enumerate(issue_queryset):
        public_id = _linear_issue_public_id(issue)
        high_signal = any(term in " ".join([issue.title or "", issue.description or ""]).lower() for term in HIGH_SIGNAL_TERMS)
        keep = (
            public_id in important_issue_ids
            or issue.identifier in important_issue_ids
            or high_signal
            or index < LINEAR_COMPACT_MAX_ISSUES
        )
        if not keep:
            omitted_issue_count += 1
            continue
        description, truncated = _compact_text(issue.description or "", max_chars=1000)
        issues.append(
            {
                "issue_id": public_id,
                "identifier": issue.identifier,
                "title": issue.title,
                "description": description,
                "state_name": issue.state_name,
                "state_type": issue.state_type,
                "priority": issue.priority,
                "priority_label": issue.priority_label,
                "assignee_name": issue.assignee_name,
                "labels": issue.label_names or [],
                "due_date": issue.due_date.isoformat() if issue.due_date else None,
                "updated_at": issue.updated_at_linear.isoformat() if issue.updated_at_linear else None,
                "url": issue.url,
                "compression_note": "long_description_trimmed" if truncated else "",
            }
        )
        if len(issues) >= LINEAR_COMPACT_MAX_ISSUES:
            omitted_issue_count += max(issue_queryset.count() - index - 1, 0)
            break

    updates = []
    omitted_update_count = 0
    used_chars = 0
    for index, update in enumerate(update_queryset):
        public_id = _linear_update_public_id(update)
        body, truncated = _compact_text(update.body or "", max_chars=1800)
        keep = public_id in important_update_ids or index < LINEAR_COMPACT_MAX_UPDATES
        if not keep:
            omitted_update_count += 1
            continue
        if used_chars + len(body) > LINEAR_COMPACT_MAX_CHARS and updates:
            omitted_update_count += 1
            continue
        updates.append(
            {
                "update_id": public_id,
                "body": body,
                "health": update.health,
                "author_name": update.author_name,
                "updated_at": update.updated_at_linear.isoformat() if update.updated_at_linear else None,
                "url": update.url,
                "compression_note": "long_body_trimmed" if truncated else "",
            }
        )
        used_chars += len(body)

    source_record_ids = [_linear_project_public_id(project)]
    source_record_ids.extend(item["update_id"] for item in updates)
    source_record_ids.extend(item["issue_id"] for item in issues)
    compression_notes = []
    if omitted_issue_count:
        compression_notes.append(f"omitted_{omitted_issue_count}_low_signal_or_over_budget_issues")
    if omitted_update_count:
        compression_notes.append(f"omitted_{omitted_update_count}_over_budget_updates")

    return {
        "linear_project_id": _linear_project_public_id(project),
        "project_id": project.linear_project_id,
        "project_name": project.name,
        "description": project.description[:1500],
        "status_name": project.status_name,
        "status_type": project.status_type,
        "health": project.health,
        "progress": project.progress,
        "scope": project.scope,
        "priority": project.priority,
        "lead_name": project.lead_name,
        "team_names": project.team_names or [],
        "start_date": project.start_date.isoformat() if project.start_date else None,
        "target_date": project.target_date.isoformat() if project.target_date else None,
        "url": project.url,
        "source_record_ids": source_record_ids,
        "issues": issues,
        "updates": updates,
        "issue_count": project.issues.count(),
        "update_count": project.project_updates.count(),
        "heuristic_score": project.heuristic_score,
        "heuristic_reasons": project.heuristic_reasons or [],
        "relevance_score": project.relevance_score,
        "relevance_reason": project.relevance_reason,
        "extraction_hints": extraction_hints,
        "omitted_issue_count": omitted_issue_count,
        "omitted_update_count": omitted_update_count,
        "compression_notes": compression_notes,
    }


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


def _draft_item_from_xero_metric(metric: StartupMetricObservation) -> dict[str, Any]:
    value_number = str(metric.value_number) if metric.value_number is not None else None
    return {
        "metric_key": metric.metric_key,
        "key": metric.metric_key,
        "label": XERO_DRAFT_METRIC_LABELS.get(metric.metric_key) or metric.metric_name,
        "value": metric.value_text,
        "value_text": metric.value_text,
        "value_number": value_number,
        "unit": metric.unit,
        "source_provider": ExternalServiceProvider.XERO,
        "source_metric_id": metric.id,
        "source_record_ids": metric.source_record_ids or [],
        "source_metadata": {
            **(metric.source_metadata or {}),
            "draft_merge_basis": "xero_backed_metric_observation",
        },
        "summary": metric.summary,
    }


def merge_xero_metrics_into_structured_memo(
    *,
    organization: Organization,
    month: date,
    structured_memo: dict,
    evidence_metric_ids: Optional[list[int]] = None,
) -> tuple[dict, list[int]]:
    month_start = _month_start(month)
    memo = dict(structured_memo or {})
    metrics = list(
        StartupMetricObservation.objects.filter(
            organization=organization,
            source_provider=ExternalServiceProvider.XERO,
            period_month=month_start,
            metric_key__in=XERO_DRAFT_METRIC_KEYS,
        )
        .order_by("metric_key", "unit", "-observed_at", "-updated_at", "-id")
    )
    if not metrics:
        return memo, evidence_metric_ids or []

    by_key: dict[str, list[StartupMetricObservation]] = {}
    for metric in metrics:
        by_key.setdefault(metric.metric_key, []).append(metric)

    xero_items: list[dict[str, Any]] = []
    merged_metric_ids: list[int] = []
    source_notes = list(memo.get("source_notes") or []) if isinstance(memo.get("source_notes"), list) else []
    for key in XERO_DRAFT_METRIC_KEYS:
        key_metrics = by_key.get(key) or []
        if not key_metrics:
            continue
        units = {metric.unit for metric in key_metrics}
        if len(key_metrics) > 1 and len(units) > 1:
            note = f"Xero provided multiple currencies for {XERO_DRAFT_METRIC_LABELS.get(key, key)}; review source records before using a combined value."
            if note not in source_notes:
                source_notes.append(note)
            continue
        metric = key_metrics[0]
        xero_items.append(_draft_item_from_xero_metric(metric))
        merged_metric_ids.append(metric.id)

    if xero_items:
        memo["kpi_snapshot"] = _merge_kpi_snapshot(memo.get("kpi_snapshot"), xero_items)
    if source_notes:
        memo["source_notes"] = source_notes
    return memo, _unique_id_list(evidence_metric_ids, merged_metric_ids)


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
    replace: bool = False,
) -> MonthlyUpdateDraft:
    month_start = _month_start(month)
    existing_draft = MonthlyUpdateDraft.objects.filter(
        organization=organization,
        month=month_start,
    ).first()
    # On an explicit "Run again" regenerate we replace the previous run's draft
    # outright instead of merging, so stale dot points and metrics don't linger.
    # Writes that belong to the *same* run (e.g. investor + community drafts
    # submitted together) are still merged so they don't clobber each other.
    replace_existing_draft = (
        replace
        and existing_draft is not None
        and (run is None or existing_draft.run_id != run.pk)
    )
    if existing_draft is not None and not replace_existing_draft:
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
