import uuid
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Union

from django.db import transaction
from django.utils import timezone

from core.models import (
    ContentFactoryApprovalState,
    ContentFactoryRun,
    ContentFactoryRunStatus,
    Organization,
)
from integrations.models import (
    GmailMessageArtifact,
    GmailRelevanceLabel,
    MonthlyUpdateDraft,
    MonthlyUpdateDraftStatus,
    GmailSyncCursor,
    StartupProfile,
    UserStartupBinding,
)
from integrations.utils import normalize_domain


STARTUP_UPDATE_WORKFLOW = "startup_monthly_update"
DEFAULT_BACKFILL_MONTHS = 6
DEFAULT_CLASSIFICATION_BATCH_SIZE = 40
DEFAULT_ATTACHMENT_BYTES_LIMIT = 10 * 1024 * 1024
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
OPEN_RUN_STATUSES = {
    ContentFactoryRunStatus.QUEUED,
    ContentFactoryRunStatus.RUNNING,
    ContentFactoryRunStatus.BLOCKED,
    ContentFactoryRunStatus.AWAITING_CONFIRMATION,
    ContentFactoryRunStatus.AWAITING_APPROVAL,
    ContentFactoryRunStatus.AWAITING_DELIVERY_MODE,
    ContentFactoryRunStatus.APPROVAL_REQUIRED,
}
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


def _serialize_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return value


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
        "summary": metric.summary or "",
    }


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


def get_open_startup_update_run(*, organization: Organization) -> Optional[ContentFactoryRun]:
    return (
        ContentFactoryRun.objects.filter(
            workflow=STARTUP_UPDATE_WORKFLOW,
            domain=organization.domain,
            status__in=list(OPEN_RUN_STATUSES),
        )
        .order_by("-updated_at")
        .first()
    )


def create_startup_update_run(
    *,
    organization: Organization,
    binding: UserStartupBinding,
    window_months: int = DEFAULT_BACKFILL_MONTHS,
) -> ContentFactoryRun:
    existing = get_open_startup_update_run(organization=organization)
    if existing:
        return existing

    now = timezone.now()
    backfill_start = now - timedelta(days=30 * int(window_months))
    current_month = _month_start(now)
    months = iter_recent_month_starts(3, reference=now)
    google_connection = binding.google_connection or getattr(binding.user, "google_connection", None)
    profile = getattr(organization, "startup_profile", None)
    run = ContentFactoryRun.objects.create(
        run_id=f"startup-update-{uuid.uuid4()}",
        workflow=STARTUP_UPDATE_WORKFLOW,
        domain=organization.domain,
        slack_user_id=str(binding.user_id),
        status=ContentFactoryRunStatus.QUEUED,
        current_step=RUN_STEP_ORDER[0],
        approval_state=ContentFactoryApprovalState.NOT_REQUIRED,
        step_order=RUN_STEP_ORDER,
        run_request={
            "organization_id": organization.id,
            "startup_profile_id": profile.id if profile else None,
            "binding_id": binding.id,
            "google_connection_id": google_connection.id if google_connection else None,
            "window_months": int(window_months),
            "classification_batch_size": DEFAULT_CLASSIFICATION_BATCH_SIZE,
            "attachment_bytes_limit": DEFAULT_ATTACHMENT_BYTES_LIMIT,
            "draft_months": [item.isoformat() for item in months],
            "current_month": current_month.isoformat(),
            "backfill_window_start": backfill_start.isoformat(),
            "backfill_window_end": now.isoformat(),
        },
        result={},
        acceptance_summary={},
        verification_summary={},
    )
    GmailSyncCursor.objects.get_or_create(
        organization=organization,
        google_connection=google_connection,
        defaults={
            "backfill_window_start": backfill_start,
            "backfill_window_end": now,
        },
    )
    return run


def score_message_for_profile(profile: StartupProfile, artifact: GmailMessageArtifact) -> tuple[int, list[str], str]:
    haystack = " ".join(
        [
            artifact.subject or "",
            artifact.snippet or "",
            artifact.cleaned_text or "",
            artifact.from_address or "",
            " ".join(artifact.to_addresses or []),
            " ".join(artifact.cc_addresses or []),
        ]
    ).lower()

    score = 50
    reasons = []

    if any(pattern in haystack for pattern in LOW_SIGNAL_PATTERNS):
        score -= 40
        reasons.append("matched_low_signal_pattern")

    domain_aliases = [normalize_domain(item) for item in (profile.domain_aliases or [])]
    company_aliases = [item.lower() for item in (profile.company_aliases or [])]
    founder_names = [item.lower() for item in (profile.founder_names or [])]
    team_names = [item.lower() for item in (profile.team_names or [])]
    investor_domains = [normalize_domain(item) for item in (profile.investor_domains or [])]
    investor_names = [item.lower() for item in (profile.investor_names or [])]
    customer_domains = [normalize_domain(item) for item in (profile.customer_domains or [])]
    customer_names = [item.lower() for item in (profile.customer_names or [])]
    prospect_domains = [normalize_domain(item) for item in (profile.prospect_domains or [])]
    prospect_names = [item.lower() for item in (profile.prospect_names or [])]
    competitor_domains = [normalize_domain(item) for item in (profile.competitor_domains or [])]
    competitor_names = [item.lower() for item in (profile.competitor_names or [])]
    positive_keywords = [item.lower() for item in (profile.positive_keywords or [])]
    negative_keywords = [item.lower() for item in (profile.negative_keywords or [])]

    if any(alias and alias in haystack for alias in company_aliases + positive_keywords):
        score += 15
        reasons.append("matched_company_alias_or_positive_keyword")

    if any(name and name in haystack for name in founder_names + team_names):
        score += 15
        reasons.append("matched_founder_or_team_name")

    if any(name and name in haystack for name in investor_names):
        score += 20
        reasons.append("matched_investor_name")

    if any(name and name in haystack for name in customer_names + prospect_names):
        score += 15
        reasons.append("matched_customer_or_prospect_name")

    if any(name and name in haystack for name in competitor_names):
        score += 10
        reasons.append("matched_competitor_name")

    if any(term in haystack for term in HIGH_SIGNAL_TERMS):
        score += 15
        reasons.append("matched_high_signal_term")

    participant_values = [artifact.from_address or ""] + list(artifact.to_addresses or []) + list(artifact.cc_addresses or [])
    participant_domains = []
    for value in participant_values:
        if "@" not in value:
            continue
        participant_domains.append(normalize_domain(value.split("@")[-1]))

    if any(domain and domain in participant_domains for domain in domain_aliases):
        score += 25
        reasons.append("matched_company_domain")

    if any(domain and domain in participant_domains for domain in investor_domains):
        score += 20
        reasons.append("matched_investor_domain")

    if any(domain and domain in participant_domains for domain in customer_domains + prospect_domains):
        score += 15
        reasons.append("matched_customer_or_prospect_domain")

    if any(domain and domain in participant_domains for domain in competitor_domains):
        score += 10
        reasons.append("matched_competitor_domain")

    if any(keyword and keyword in haystack for keyword in negative_keywords):
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
        ("Asks", memo.get("asks") or []),
        ("Highlights", memo.get("highlights") or []),
        ("Lowlights / Risks", memo.get("lowlights") or []),
        ("Product / GTM / Team / Fundraising", memo.get("operations") or []),
        ("Next 30 Days", memo.get("next_30_days") or []),
    ]

    lines = []
    if title:
        lines.append(f"# {title}")
        lines.append("")
    if topline:
        lines.append("## Topline")
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
    rendered_markdown = render_monthly_update_markdown(structured_memo)
    title = str((structured_memo or {}).get("title") or "").strip()
    draft, _ = MonthlyUpdateDraft.objects.update_or_create(
        organization=organization,
        month=_month_start(month),
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
