from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime as django_parse_datetime

from jobs.conf import settings
from jobs.models import JobListing, JobRun, SeekJob, SourceRunLog
from jobs.services.company_metadata import enrich_company_metadata
from jobs.services.job_scoring import clean_job_title, clean_text, normalize_url, normalize_words, score_job, why_selected
from jobs.services.llm_judge import judge_top_candidates
from jobs.services.logos import logo_url_for_company
from jobs.services.notion import publish_daily_jobs_page
from jobs.services.remote import infer_remote_eligibility
from jobs.services.slack import format_slack_message, post_failure_alert, post_slack_message
from jobs.services.sources import GETRO_BOARDS, PHASE_1_SOURCES, PUBLIC_PAGE_SOURCES, collect_from_source, date_posted_from_days, melbourne_today
from jobs.services.summaries import build_job_summary

logger = logging.getLogger(__name__)

TERMINAL_COMPLETED_STATUSES = [
    "completed",
    "completed_no_results",
    "completed_with_source_errors",
    "completed_no_results_with_source_errors",
    "completed_with_publish_errors",
]


def _jobs_schedule_timezone():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    candidate = settings.jobs_schedule_timezone.strip() or "Australia/Melbourne"
    try:
        return ZoneInfo(candidate)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid jobs schedule timezone: {candidate}") from exc


def _jobs_schedule_local_now(now: datetime | None = None) -> datetime:
    current = now or timezone.now()
    return current.astimezone(_jobs_schedule_timezone())


def _scheduler_config_errors() -> list[str]:
    errors: list[str] = []

    try:
        _jobs_schedule_timezone()
    except ValueError as exc:
        errors.append(str(exc))

    if not 0 <= settings.jobs_schedule_hour <= 23:
        errors.append("JOBS_SCHEDULE_HOUR must be between 0 and 23")
    if not 0 <= settings.jobs_schedule_minute <= 59:
        errors.append("JOBS_SCHEDULE_MINUTE must be between 0 and 59")
    if settings.jobs_retry_attempts < 1:
        errors.append("JOBS_RETRY_ATTEMPTS must be at least 1")
    if settings.jobs_retry_delay_seconds < 0:
        errors.append("JOBS_RETRY_DELAY_SECONDS must be 0 or greater")
    if settings.jobs_failure_stop_after_days < 1:
        errors.append("JOBS_FAILURE_STOP_AFTER_DAYS must be at least 1")
    if settings.jobs_scheduler_max_pages < 1:
        errors.append("JOBS_SCHEDULER_MAX_PAGES must be at least 1")
    if settings.jobs_scheduler_per_keyword_limit < 1:
        errors.append("JOBS_SCHEDULER_PER_KEYWORD_LIMIT must be at least 1")

    return errors


def validate_scheduler_configuration() -> None:
    errors = _scheduler_config_errors()
    if errors:
        raise ValueError("; ".join(errors))


def create_run(
    run_date: str | None = None,
    *,
    collect_live: bool = True,
    post_to_slack: bool = False,
    post_to_notion: bool = True,
    source_names: list[str] | None = None,
    max_pages: int | None = None,
    per_keyword_limit: int | None = None,
    trigger_source: str = "manual_api",
) -> JobRun:
    run_date = run_date or melbourne_today()
    run_id = f"{run_date}-{uuid.uuid4().hex[:8]}"
    full_list_url = f"{settings.public_base_url}/api/v1/jobs/daily/{run_date}"
    return JobRun.objects.create(
        run_id=run_id,
        run_date=run_date,
        status="queued",
        full_list_url=full_list_url,
        collect_live=collect_live,
        post_to_slack=post_to_slack,
        post_to_notion=post_to_notion,
        source_names=source_names,
        max_pages=max_pages,
        per_keyword_limit=per_keyword_limit,
        trigger_source=trigger_source,
    )


def latest_run_for_date(run_date: str) -> JobRun | None:
    return JobRun.objects.filter(run_date=run_date).order_by("-started_at", "-id").first()


def seek_row_to_raw_job(row: SeekJob) -> dict[str, Any]:
    return {
        "run_date": row.run_date,
        "source_name": row.source_name,
        "source_type": "broad_board",
        "source_quality_score": 0.72,
        "keyword": row.keyword,
        "title": row.title,
        "company_name": row.company_name,
        "company_logo_url": row.company_logo_url,
        "company_domain": row.company_domain,
        "company_stage": row.company_stage,
        "company_size": row.company_size,
        "company_quality_score": row.company_quality_score,
        "location": row.location,
        "posted_text": row.posted_text,
        "description": row.description,
        "job_url": row.job_url,
    }


def collect_existing_seek_jobs(run_date: str) -> list[dict[str, Any]]:
    return [seek_row_to_raw_job(row) for row in SeekJob.objects.filter(run_date=run_date)]


def normalize_raw_job(raw: dict[str, Any], run: JobRun) -> dict[str, Any]:
    location = clean_text(raw.get("location"))
    location_lower = location.lower()
    is_remote = "remote" in location_lower or "work from home" in location_lower

    country = "Australia" if any(
        term in location_lower
        for term in ("australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra")
    ) else None

    raw_date_posted = raw.get("date_posted") or date_posted_from_days(raw.get("posted_days_ago"))
    normalized_date_posted = normalize_posted_datetime(raw_date_posted)

    return {
        "run_id": run.run_id,
        "run_date": run.run_date,
        "title": clean_job_title(raw.get("title")),
        "company_name": clean_text(raw.get("company_name")) or None,
        "company_logo_url": logo_url_for_company(
            clean_text(raw.get("company_name")) or None,
            clean_text(raw.get("company_logo_url")) or None,
        ),
        "company_domain": clean_text(raw.get("company_domain")) or None,
        "company_stage": clean_text(raw.get("company_stage")) or None,
        "company_size": clean_text(raw.get("company_size")) or None,
        "company_quality_score": raw.get("company_quality_score") or 0.0,
        "location": location or None,
        "is_remote": is_remote,
        "remote_region": "Global/APAC" if is_remote else None,
        "remote_eligibility": None,
        "remote_eligibility_score": 0.0,
        "country": country,
        "city": None,
        "job_url": raw.get("job_url"),
        "apply_url": raw.get("apply_url") or raw.get("job_url"),
        "source_name": raw.get("source_name") or "Unknown",
        "source_type": raw.get("source_type") or "broad_board",
        "date_posted": normalized_date_posted,
        "posted_text": clean_text(raw.get("posted_text")) or None,
        "description": clean_text(raw.get("description")) or None,
        "source_quality_score": raw.get("source_quality_score"),
    }


def normalize_posted_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, str):
        parsed = django_parse_datetime(value)
        if not parsed:
            return None
        value = parsed
    if not isinstance(value, datetime):
        return None
    if timezone.is_naive(value):
        return timezone.make_aware(value, _jobs_schedule_timezone())
    return value.astimezone(_jobs_schedule_timezone())


def is_valid_job_target(raw: dict[str, Any]) -> bool:
    job_url = normalize_url(raw.get("job_url"))
    apply_url = normalize_url(raw.get("apply_url"))
    source_name = clean_text(raw.get("source_name")) or ""
    listing_urls = {
        normalize_url(PUBLIC_PAGE_SOURCES.get(source_name)),
        normalize_url(GETRO_BOARDS.get(source_name)),
        normalize_url(f"{GETRO_BOARDS.get(source_name, '').rstrip('/')}/jobs" if source_name in GETRO_BOARDS else None),
    }
    listing_urls.discard("")
    if job_url in listing_urls or apply_url in listing_urls:
        return False
    return bool(job_url)


def fetch_raw_jobs(
    run: JobRun,
    collect_live: bool,
    source_names: list[str] | None,
    max_pages: int | None,
    per_keyword_limit: int | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    raw_jobs: list[dict[str, Any]] = []
    failed_sources: list[str] = []
    allowed_sources = {name.lower() for name in source_names} if source_names else None

    if collect_live:
        for source in PHASE_1_SOURCES:
            if not source.enabled:
                continue
            if allowed_sources and source.name.lower() not in allowed_sources:
                continue

            log = SourceRunLog.objects.create(run=run, source_name=source.name, status="running")
            try:
                jobs = collect_from_source(source, max_pages=max_pages, per_keyword_limit=per_keyword_limit)
                raw_jobs.extend(jobs)
                log.status = "ok"
                log.fetched_count = len(jobs)
            except Exception as exc:
                log.status = "error"
                log.error_message = str(exc)
                failed_sources.append(f"{source.name}: {exc}")
            finally:
                log.completed_at = timezone.now()
                log.save(update_fields=["status", "fetched_count", "error_message", "completed_at"])

    if not raw_jobs and (not collect_live or not source_names):
        raw_jobs.extend(collect_existing_seek_jobs(run.run_date))

    return raw_jobs, failed_sources


def _build_run_status(*, top_jobs_count: int, source_errors: list[str], slack_error: str | None) -> str:
    if slack_error:
        return "completed_with_publish_errors"
    if source_errors and top_jobs_count:
        return "completed_with_source_errors"
    if source_errors:
        return "completed_no_results_with_source_errors"
    if top_jobs_count:
        return "completed"
    return "completed_no_results"


def _summarize_run_issues(source_errors: list[str], slack_error: str | None) -> str | None:
    issues: list[str] = []
    if source_errors:
        issues.append("Source errors: " + "; ".join(source_errors[:5]))
    if slack_error:
        issues.append(f"Slack publish error: {slack_error}")
    return " | ".join(issues) if issues else None


def enqueue_run_from_request(validated: dict[str, Any], *, trigger_source: str = "manual_api") -> JobRun:
    return create_run(
        collect_live=validated.get("collect_live", True),
        post_to_slack=validated.get("post_to_slack", False),
        post_to_notion=validated.get("post_to_notion", True),
        source_names=validated.get("sources"),
        max_pages=validated.get("max_pages"),
        per_keyword_limit=validated.get("per_keyword_limit"),
        trigger_source=trigger_source,
    )


def claim_queued_run() -> JobRun | None:
    with transaction.atomic():
        run = (
            JobRun.objects.select_for_update(skip_locked=True)
            .filter(status="queued")
            .order_by("created_at", "id")
            .first()
        )
        if not run:
            return None
        run.status = "running"
        run.claimed_at = timezone.now()
        run.started_at = run.started_at or run.claimed_at
        run.save(update_fields=["status", "claimed_at", "started_at", "updated_at"])
        return run


def process_next_queued_run() -> dict[str, Any]:
    run = claim_queued_run()
    if not run:
        return {"status": "skipped", "reason": "no_queued_runs"}

    run_daily_jobs(
        run.run_id,
        collect_live=run.collect_live,
        post_to_slack=run.post_to_slack,
        post_to_notion=run.post_to_notion,
        source_names=run.source_names,
        max_pages=run.max_pages,
        per_keyword_limit=run.per_keyword_limit,
    )
    refreshed = JobRun.objects.get(pk=run.pk)
    return {
        "status": refreshed.status,
        "run_id": refreshed.run_id,
        "trigger_source": refreshed.trigger_source,
    }


def insert_matched_jobs(run: JobRun, raw_jobs: list[dict[str, Any]]) -> list[JobListing]:
    inserted: list[JobListing] = []
    for raw in raw_jobs:
        if not raw.get("title") or not raw.get("job_url") or not is_valid_job_target(raw):
            continue

        normalized = normalize_raw_job(raw, run)
        enriched = enrich_company_metadata(normalized)
        enriched = infer_remote_eligibility(enriched)
        scored = score_job(enriched)
        if not scored.get("bucket"):
            continue
        summary = build_job_summary(scored)

        row = JobListing(
            run=run,
            run_date=run.run_date,
            title=scored["title"],
            company_name=scored.get("company_name"),
            company_logo_url=scored.get("company_logo_url"),
            company_domain=scored.get("company_domain"),
            company_stage=scored.get("company_stage"),
            company_size=scored.get("company_size"),
            company_quality_score=scored.get("company_quality_score"),
            location=scored.get("location"),
            is_remote=scored.get("is_remote", False),
            remote_region=scored.get("remote_region"),
            remote_eligibility=scored.get("remote_eligibility"),
            remote_eligibility_score=scored.get("remote_eligibility_score"),
            country=scored.get("country"),
            city=scored.get("city"),
            job_url=scored["job_url"],
            apply_url=scored.get("apply_url"),
            source_name=scored["source_name"],
            source_type=scored.get("source_type"),
            date_posted=scored.get("date_posted"),
            posted_text=scored.get("posted_text"),
            description=scored.get("description"),
            ai_score=scored["ai_score"],
            startup_score=scored["startup_score"],
            australia_score=scored["australia_score"],
            remote_score=scored["remote_score"],
            recency_score=scored["recency_score"],
            source_score=scored["source_score"],
            quality_score=scored["quality_score"],
            ranking_score=scored["ranking_score"],
            bucket=scored["bucket"],
            summary=summary,
            why_selected=summary or why_selected(scored),
            dedupe_key=scored["dedupe_key"],
        )
        try:
            row.save()
            inserted.append(row)
        except IntegrityError:
            continue

    return inserted


def select_top_jobs(run: JobRun, limit: int | None = None) -> list[JobListing]:
    limit = limit or settings.jobs_top_pick_limit
    candidates = list(JobListing.objects.filter(run=run).order_by("-ranking_score", "id"))
    candidates, llm_reasons = judge_top_candidates(candidates, candidate_limit=10)

    selected: list[JobListing] = []
    companies: set[str] = set()
    title_company_pairs: set[str] = set()
    sources: dict[str, int] = {}
    buckets: set[str] = set()

    def can_pick(job: JobListing, strict: bool) -> bool:
        company = (job.company_name or "").lower()
        pair = f"{normalize_words(job.title)}|{normalize_words(job.company_name)}"
        if company and company in companies:
            return False
        if pair in title_company_pairs:
            return False
        max_per_source = 2 if strict else 3
        if sources.get(job.source_name, 0) >= max_per_source:
            return False
        return True

    def pick(job: JobListing) -> None:
        selected.append(job)
        if job.company_name:
            companies.add(job.company_name.lower())
        title_company_pairs.add(f"{normalize_words(job.title)}|{normalize_words(job.company_name)}")
        sources[job.source_name] = sources.get(job.source_name, 0) + 1
        if job.bucket:
            buckets.add(job.bucket)

    for strict in (True, False):
        for job in candidates:
            if len(selected) >= limit:
                break
            if job in selected or not can_pick(job, strict):
                continue
            if strict and len(selected) < 4 and job.bucket in buckets and len(buckets) < 4:
                continue
            pick(job)
        if len(selected) >= limit:
            break

    for job in candidates:
        if len(selected) >= limit:
            break
        company = (job.company_name or "").lower()
        pair = f"{normalize_words(job.title)}|{normalize_words(job.company_name)}"
        if job in selected or (company and company in companies) or pair in title_company_pairs:
            continue
        pick(job)

    for index, job in enumerate(selected, start=1):
        job.is_top_pick = True
        job.rank = index
        if job.id in llm_reasons:
            job.why_selected = llm_reasons[job.id]
            job.summary = llm_reasons[job.id]
        job.save(update_fields=["is_top_pick", "rank", "why_selected", "summary"])
    return selected


def run_daily_jobs(
    run_id: str,
    collect_live: bool = True,
    post_to_slack: bool = False,
    post_to_notion: bool = True,
    source_names: list[str] | None = None,
    max_pages: int | None = None,
    per_keyword_limit: int | None = None,
) -> None:
    run = JobRun.objects.get(run_id=run_id)
    try:
        if run.status != "running":
            run.status = "running"
            run.started_at = timezone.now()
            run.save(update_fields=["status", "started_at", "updated_at"])

        raw_jobs, source_errors = fetch_raw_jobs(run, collect_live, source_names, max_pages, per_keyword_limit)
        run.fetched_count = len(raw_jobs)

        if collect_live and source_errors and not raw_jobs:
            raise RuntimeError("All live job sources failed. " + "; ".join(source_errors[:5]))

        matched = insert_matched_jobs(run, raw_jobs)
        run.matched_count = len(matched)
        run.deduped_count = JobListing.objects.filter(run=run).count()

        top_jobs = select_top_jobs(run)
        run.ranked_count = len(top_jobs)

        slack_error: str | None = None
        if post_to_notion and top_jobs:
            all_jobs = list(JobListing.objects.filter(run=run).order_by("-is_top_pick", "rank", "-ranking_score"))
            notion_url = publish_daily_jobs_page(run, top_jobs, all_jobs)
            if notion_url:
                run.full_list_url = notion_url

        if post_to_slack and top_jobs:
            payload = format_slack_message(run.run_date, top_jobs, run.full_list_url or "")
            posted, slack_error = post_slack_message(payload)
            if posted:
                run.slack_posted_at = timezone.now()
            else:
                logger.error("Jobs Slack publish failed for run %s: %s", run.run_id, slack_error)

        if source_errors:
            logger.warning("Jobs run %s completed with source errors: %s", run.run_id, "; ".join(source_errors))

        run.status = _build_run_status(
            top_jobs_count=len(top_jobs),
            source_errors=source_errors,
            slack_error=slack_error,
        )
        run.error_message = _summarize_run_issues(source_errors, slack_error)
        run.completed_at = timezone.now()
        run.save()
        if run.status == "completed_no_results_with_source_errors":
            try:
                post_failure_alert(run.run_id, run.error_message or "Run completed with source errors")
            except Exception:
                logger.exception("Failed to send source error alert for jobs run %s", run.run_id)
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = timezone.now()
        run.save(update_fields=["status", "error_message", "completed_at", "updated_at"])
        try:
            post_failure_alert(run.run_id, str(exc))
        except Exception:
            pass
        raise


def run_daily_jobs_scheduler(now: datetime | None = None) -> dict[str, Any]:
    queued_result = process_next_queued_run()
    if queued_result.get("status") != "skipped":
        return {"status": "ok", "queued_run": queued_result}

    if not settings.jobs_scheduler_enabled:
        return {"status": "skipped", "reason": "scheduler_disabled"}

    errors = _scheduler_config_errors()
    if errors:
        logger.error("Jobs scheduler misconfigured: %s", "; ".join(errors))
        return {"status": "failed", "reason": "invalid_scheduler_config", "errors": errors}

    local_now = _jobs_schedule_local_now(now)
    run_date = local_now.date().isoformat()
    schedule_hour = settings.jobs_schedule_hour
    schedule_minute = settings.jobs_schedule_minute

    if (local_now.hour, local_now.minute) < (schedule_hour, schedule_minute):
        return {"status": "skipped", "reason": "before_schedule_window", "run_date": run_date}

    open_statuses = ["queued", "running"]

    if JobRun.objects.filter(
        run_date=run_date,
        trigger_source="daily_scheduler",
        status__in=open_statuses + TERMINAL_COMPLETED_STATUSES,
    ).exists():
        return {"status": "skipped", "reason": "run_already_exists", "run_date": run_date}

    failed_days = 0
    for previous_run_date in (
        JobRun.objects.filter(run_date__lt=run_date)
        .order_by("-run_date")
        .values_list("run_date", flat=True)
        .distinct()
    ):
        statuses = set(JobRun.objects.filter(run_date=previous_run_date).values_list("status", flat=True))
        if statuses & set(TERMINAL_COMPLETED_STATUSES):
            break
        if statuses and statuses <= {"failed"}:
            failed_days += 1
            if failed_days >= settings.jobs_failure_stop_after_days:
                logger.error(
                    "Jobs scheduler halted after %s consecutive failed days. Latest failed day: %s",
                    failed_days,
                    previous_run_date,
                )
                return {
                    "status": "halted",
                    "reason": "consecutive_failed_days",
                    "run_date": run_date,
                    "failed_days": failed_days,
                }
        else:
            break

    failed_runs = list(
        JobRun.objects.filter(run_date=run_date, trigger_source="daily_scheduler", status="failed")
        .order_by("-completed_at", "-id")
    )
    failed_count = len(failed_runs)
    if failed_count >= settings.jobs_retry_attempts:
        return {
            "status": "skipped",
            "reason": "max_attempts_reached",
            "run_date": run_date,
            "failed_attempts": failed_count,
        }

    last_failed_run = failed_runs[0] if failed_runs else None
    if last_failed_run and last_failed_run.completed_at:
        retry_after = last_failed_run.completed_at + timedelta(seconds=settings.jobs_retry_delay_seconds)
        if timezone.now() < retry_after:
            return {
                "status": "skipped",
                "reason": "retry_backoff",
                "run_date": run_date,
                "failed_attempts": failed_count,
                "retry_after": retry_after.isoformat(),
            }

    run = create_run(
        run_date=run_date,
        collect_live=True,
        post_to_slack=settings.jobs_scheduler_post_to_slack,
        post_to_notion=settings.jobs_scheduler_post_to_notion,
        max_pages=settings.jobs_scheduler_max_pages,
        per_keyword_limit=settings.jobs_scheduler_per_keyword_limit,
        trigger_source="daily_scheduler",
    )
    run_daily_jobs(
        run.run_id,
        collect_live=run.collect_live,
        post_to_slack=run.post_to_slack,
        post_to_notion=run.post_to_notion,
        source_names=run.source_names,
        max_pages=run.max_pages,
        per_keyword_limit=run.per_keyword_limit,
    )
    refreshed = JobRun.objects.get(pk=run.pk)
    return {
        "status": refreshed.status,
        "run_id": refreshed.run_id,
        "run_date": refreshed.run_date,
        "attempt": failed_count + 1,
    }
