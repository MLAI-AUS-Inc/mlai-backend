from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone

from jobs.models import JobDisqualifierCandidate, JobFeedback, JobListing, JobRun


@dataclass(frozen=True)
class ParsedFeedback:
    feedback_type: str
    rank: int | None = None
    reason: str = ""
    keyword: str = ""


def parse_feedback_command(text: str) -> ParsedFeedback | None:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if not value:
        return None

    good_match = re.match(r"^(?:/job-good|good)(?:\s+#?(\d+))?$", value, re.I)
    if good_match:
        return ParsedFeedback("good", rank=_int_or_none(good_match.group(1)))

    flag_match = re.match(r"^(?:/job-flag|bad|flag)(?:\s+#?(\d+))?(?:\s+(.+))?$", value, re.I)
    if flag_match:
        return ParsedFeedback("flag", rank=_int_or_none(flag_match.group(1)), reason=(flag_match.group(2) or "").strip()[:255])

    disqualify_match = re.match(r"^(?:/job-disqualify|disqualify)\s+(.+)$", value, re.I)
    if disqualify_match:
        keyword = normalize_keyword(disqualify_match.group(1))
        if keyword:
            return ParsedFeedback("disqualify", keyword=keyword)

    return None


def record_feedback(
    *,
    run_id: str | None,
    text: str,
    slack_user_id: str = "",
    slack_channel_id: str = "",
    slack_message_ts: str = "",
) -> JobFeedback:
    parsed = parse_feedback_command(text)
    if not parsed:
        raise ValueError("Unrecognised jobs feedback command")

    run = JobRun.objects.filter(run_id=run_id).first() if run_id else None
    job = None
    if run and parsed.rank:
        job = JobListing.objects.filter(run=run, is_top_pick=True, rank=parsed.rank).first()

    feedback = JobFeedback.objects.create(
        run=run,
        job=job,
        feedback_type=parsed.feedback_type,
        rank=parsed.rank,
        reason=parsed.reason,
        keyword=parsed.keyword,
        raw_text=text,
        slack_user_id=slack_user_id,
        slack_channel_id=slack_channel_id,
        slack_message_ts=slack_message_ts,
    )
    return feedback


def update_disqualifier_candidates(*, min_signals: int = 3) -> dict[str, int]:
    pending = JobFeedback.objects.filter(feedback_type="disqualify", processed_at__isnull=True).exclude(keyword="")
    seen = 0
    promoted = 0
    now = timezone.now()

    for feedback in pending:
        candidate, _created = JobDisqualifierCandidate.objects.get_or_create(
            keyword=feedback.keyword,
            defaults={"category": "community", "severity": "penalize", "penalty": 0.08},
        )
        candidate.signal_count += 1
        candidate.confidence = min(1.0, candidate.signal_count / max(1, min_signals))
        candidate.last_seen_at = now
        if candidate.status == "review" and candidate.signal_count >= min_signals:
            candidate.status = "active"
            promoted += 1
        candidate.save(update_fields=["signal_count", "confidence", "last_seen_at", "status", "updated_at"])
        feedback.processed_at = now
        feedback.save(update_fields=["processed_at"])
        seen += 1

    return {"processed": seen, "promoted": promoted}


def prune_old_feedback(*, retention_days: int = 90) -> int:
    cutoff = timezone.now() - timedelta(days=max(1, int(retention_days)))
    deleted_count, _details = JobFeedback.objects.filter(created_at__lt=cutoff).delete()
    return int(deleted_count)


def active_disqualifier_candidates() -> list[JobDisqualifierCandidate]:
    try:
        return list(JobDisqualifierCandidate.objects.filter(status="active").order_by("keyword"))
    except Exception:
        return []


def normalize_keyword(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = text.strip("`'\" ")
    return text[:255]


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None
