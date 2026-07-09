from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from jobs.conf import settings
from jobs.services.job_scoring import is_target_role_title, rerank_for_relevance, score_job
from jobs.services.location_eligibility import classify_with_rules, searchable_text
from jobs.services.summaries import build_job_summary


def screen_job_for_publish(job: Any) -> tuple[bool, str | None, dict[str, Any]]:
    payload = _job_payload(job)
    if not is_target_role_title(payload.get("title")):
        return False, "outside allowed role families", payload
    if payload.get("remote_eligibility") == "restricted_remote":
        return False, "location restricted for Australian candidates", payload
    location_rule = classify_with_rules(searchable_text(payload))
    if location_rule and location_rule.is_restricted:
        return False, location_rule.reason or "location restricted for Australian candidates", payload
    if _is_stale(payload.get("date_posted"), payload.get("posted_text")):
        return False, "older than the configured freshness window", payload

    rescored = rerank_for_relevance(score_job(payload))
    if rescored.get("post_score_status") != "accepted":
        return False, str(rescored.get("post_score_status") or "failed relevance screen"), rescored
    if not rescored.get("bucket"):
        return False, "does not match an Australia or eligible remote bucket", rescored

    rescored["summary"] = build_job_summary(rescored)
    return True, None, rescored


def apply_publish_screen(job: Any) -> bool:
    accepted, _reason, rescored = screen_job_for_publish(job)
    if not accepted:
        return False

    for field in (
        "ai_score",
        "startup_score",
        "australia_score",
        "remote_score",
        "recency_score",
        "source_score",
        "quality_score",
        "ranking_score",
        "bucket",
        "summary",
    ):
        setattr(job, field, rescored.get(field))
    job.why_selected = rescored["summary"]
    return True


def _job_payload(job: Any) -> dict[str, Any]:
    fields = (
        "title",
        "company_name",
        "company_stage",
        "company_size",
        "company_quality_score",
        "location",
        "country",
        "description",
        "job_url",
        "apply_url",
        "source_name",
        "source_type",
        "date_posted",
        "posted_text",
        "remote_eligibility",
        "remote_eligibility_score",
        "ranking_penalty",
    )
    payload = {field: getattr(job, field, None) for field in fields}
    payload["source_quality_score"] = getattr(job, "source_score", 0.0)
    return payload


def _is_stale(date_posted: Any, posted_text: str | None) -> bool:
    cutoff = datetime.now(dt_timezone.utc) - timedelta(hours=settings.jobs_freshness_hours)
    if date_posted:
        if isinstance(date_posted, str):
            try:
                date_posted = datetime.fromisoformat(date_posted.replace("Z", "+00:00"))
            except ValueError:
                date_posted = None
        if isinstance(date_posted, datetime):
            if date_posted.tzinfo is None:
                date_posted = date_posted.replace(tzinfo=dt_timezone.utc)
            return date_posted.astimezone(dt_timezone.utc) < cutoff

    age_match = re.search(
        r"\b(\d+)\s*(h|hr|hrs|hour|hours|d|day|days|w|wk|wks|week|weeks|mo|mos|month|months)\b",
        str(posted_text or ""),
        re.I,
    )
    if not age_match:
        return False
    unit = age_match.group(2).lower()
    if unit.startswith("mo"):
        hours_per_unit = 24 * 30
    elif unit.startswith("w"):
        hours_per_unit = 24 * 7
    elif unit.startswith("d"):
        hours_per_unit = 24
    else:
        hours_per_unit = 1
    return int(age_match.group(1)) * hours_per_unit > settings.jobs_freshness_hours
