import logging
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
from integrations.services.valley_harness import notify_valley_run_created
from integrations.utils import normalize_domain


logger = logging.getLogger(__name__)

STARTUP_UPDATE_WORKFLOW = "startup_monthly_update"
DEFAULT_BACKFILL_MONTHS = 1
DEFAULT_CLASSIFICATION_BATCH_SIZE = 40
DEFAULT_ATTACHMENT_BYTES_LIMIT = 10 * 1024 * 1024
SUPERSEDED_GMAIL_CONNECTION_ERROR = "Superseded by a newer Gmail connection."
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
) -> ContentFactoryRun:
    google_connection = binding.google_connection or getattr(binding.user, "google_connection", None)
    google_connection_id = google_connection.id if google_connection else None

    existing = get_open_startup_update_run(
        organization=organization,
        google_connection_id=google_connection_id,
    )
    if existing:
        pin_startup_update_run_connection(existing, google_connection_id)
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
            "startup_context": startup_context,
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
