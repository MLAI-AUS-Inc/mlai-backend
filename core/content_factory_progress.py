from __future__ import annotations

from datetime import timedelta
from typing import Optional

from django.utils import timezone

from integrations.services.slack import SlackService

CONTENT_FACTORY_ARTICLE_COST_POINTS = 6
QUIET_RUN_THRESHOLD = timedelta(minutes=10)

MILESTONE_SUMMARIES = {
    "queued": "Preparing the paid run and loading context.",
    "research_started": "Researching candidate topics.",
    "candidate_pool_ready": "Shortlisting the strongest opportunities.",
    "awaiting_confirmation": "Research complete. Waiting for your topic choice.",
    "research_locked": "Research and outline locked.",
    "draft_grounded": "Draft written and grounded to sources.",
    "finishing_pass": "Running final checks and packaging delivery.",
    "completed": "Run complete.",
}


def resolve_job_thread_context(job, data=None):
    payload = data or {}
    channel_id = (job.slack_channel_id or "") or str(payload.get("slack_channel_id") or "").strip()
    root_message_ts = (
        (job.slack_root_message_ts or "")
        or str(payload.get("slack_root_message_ts") or "").strip()
        or str(payload.get("root_message_ts") or "").strip()
    )
    thread_ts = (job.slack_thread_ts or "") or str(payload.get("slack_thread_ts") or "").strip() or root_message_ts
    if not root_message_ts:
        root_message_ts = thread_ts or ""
    return channel_id, root_message_ts, thread_ts


def is_discovery_workflow(job) -> bool:
    request_meta = job.request_meta or {}
    return not bool(request_meta.get("topic") or request_meta.get("source_run_id"))


def active_watchdog_status(job) -> bool:
    return str(job.status or "").strip() in {"queued", "researching", "confirmed", "generating"}


def live_card_summary_for_job(job) -> str:
    status_value = str(job.status or "").strip()
    milestone_key = str(job.last_progress_milestone_key or "").strip()

    if status_value == "error":
        return job.error_message or "The run hit an error."
    if status_value == "completed":
        return "Run complete."
    if status_value == "awaiting_confirmation":
        return MILESTONE_SUMMARIES["awaiting_confirmation"]
    if milestone_key:
        return MILESTONE_SUMMARIES.get(milestone_key, milestone_key.replace("_", " ").capitalize())
    return MILESTONE_SUMMARIES["queued"]


def _workflow_stages(job) -> list[tuple[str, str]]:
    stages = [
        ("preparing", "Preparing run"),
        ("researching", "Researching"),
    ]
    if is_discovery_workflow(job):
        stages.append(("awaiting_confirmation", "Awaiting your decision"))
    stages.extend(
        [
            ("writing", "Writing draft"),
            ("final_checks", "Final checks"),
            ("complete", "Complete"),
        ]
    )
    return stages


def _current_stage(job) -> str:
    status_value = str(job.status or "").strip()
    milestone_key = str(job.last_progress_milestone_key or "").strip()

    if status_value == "queued":
        return "preparing"
    if status_value == "researching":
        return "researching"
    if status_value == "awaiting_confirmation":
        return "awaiting_confirmation"
    if status_value in {"confirmed", "generating", "awaiting_delivery_mode", "awaiting_approval"}:
        if milestone_key == "finishing_pass":
            return "final_checks"
        return "writing"
    if status_value == "completed":
        return "complete"
    if status_value == "error":
        if milestone_key == "finishing_pass":
            return "final_checks"
        if milestone_key in {"research_locked", "draft_grounded"}:
            return "writing"
        if milestone_key in {"research_started", "candidate_pool_ready"}:
            return "researching"
        return "preparing"
    return "preparing"


def build_live_card_blocks(
    job,
    *,
    summary_text: Optional[str] = None,
    quiet_note: Optional[str] = None,
    failed: bool = False,
) -> list[dict]:
    summary = str(summary_text or live_card_summary_for_job(job)).strip()
    current_stage = _current_stage(job)
    stages = []
    seen_current = False

    for stage_key, label in _workflow_stages(job):
        if failed and stage_key == current_stage:
            icon = "❌"
        elif stage_key == current_stage:
            icon = "⏳"
            seen_current = True
        elif not seen_current:
            icon = "✅"
        else:
            icon = "⬜"
        stages.append(f"{icon} {label}")

    stage_lines = "\n".join(stages)
    text = (
        f"*Content Factory for {job.domain}*\n\n"
        f"*Now:* {summary}\n\n"
        f"{stage_lines}"
    )
    if quiet_note:
        text += f"\n\n_{quiet_note}_"

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": text,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"💳 This paid run costs {CONTENT_FACTORY_ARTICLE_COST_POINTS} Roo points. "
                        "They were deducted when the run started."
                    ),
                }
            ],
        },
    ]


def upsert_live_progress_card(
    job,
    *,
    data=None,
    summary_text: Optional[str] = None,
    quiet_note: Optional[str] = None,
    failed: bool = False,
) -> tuple[bool, Optional[str]]:
    channel_id, _root_message_ts, thread_ts = resolve_job_thread_context(job, data=data)
    if not channel_id or not thread_ts:
        return False, None

    fallback_text = summary_text or live_card_summary_for_job(job)
    blocks = build_live_card_blocks(job, summary_text=summary_text, quiet_note=quiet_note, failed=failed)

    progress_ts = str(job.progress_message_ts or "").strip()
    if progress_ts:
        updated = SlackService.update_message(channel_id, progress_ts, fallback_text, blocks=blocks)
        return updated, progress_ts if updated else None

    send_result = SlackService.send_message(
        channel_id,
        fallback_text,
        blocks=blocks,
        thread_ts=thread_ts,
    )
    if isinstance(send_result, tuple):
        success, message_ts = send_result
    else:
        success = bool(send_result)
        message_ts = None
    if success and message_ts:
        job.progress_message_ts = message_ts
        job.save(update_fields=["progress_message_ts", "updated_at"])
        return True, message_ts
    return success, message_ts


def maybe_send_still_working_ping(job) -> bool:
    if not active_watchdog_status(job):
        return False
    if not str(job.progress_message_ts or "").strip():
        return False
    if not job.last_progress_updated_at:
        return False

    now = timezone.now()
    if now - job.last_progress_updated_at < QUIET_RUN_THRESHOLD:
        return False

    if job.still_working_pinged_at and job.still_working_pinged_at >= job.last_progress_updated_at:
        return False

    quiet_note = "Still working. Deep research and verification can take a few minutes."
    sent, _message_ts = upsert_live_progress_card(job, quiet_note=quiet_note)
    if not sent:
        return False

    job.still_working_pinged_at = now
    job.save(update_fields=["still_working_pinged_at", "updated_at"])
    return True
