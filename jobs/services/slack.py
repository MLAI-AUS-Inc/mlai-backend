from __future__ import annotations

from datetime import datetime

from jobs.conf import settings
from jobs.models import JobListing
from jobs.services.final_screening import apply_publish_screen


def _slack_service():
    from integrations.services.slack import SlackService

    return SlackService


def format_slack_message(
    run_date: str,
    top_jobs: list[JobListing],
    full_list_url: str,
    *,
    matched_count: int | None = None,
) -> dict:
    try:
        date_value = datetime.fromisoformat(run_date)
        parsed_date = date_value.strftime("%A, %d %B %Y").replace(", 0", ", ")
    except ValueError:
        parsed_date = run_date

    screened_jobs = [job for job in top_jobs if apply_publish_screen(job)][: settings.jobs_top_pick_limit]
    digest_title = _digest_title(parsed_date, len(screened_jobs))
    lines = [digest_title, ""]
    for display_rank, job in enumerate(screened_jobs, start=1):
        lines.append(_job_heading(job, display_rank))
        lines.append(_job_details(job))
        lines.append("")
    lines.append(_full_list_footer(full_list_url, matched_count))

    return {
        "channel": settings.slack_jobs_channel,
        "text": "\n".join(lines),
        "blocks": build_slack_blocks(
            parsed_date,
            screened_jobs,
            full_list_url,
            matched_count=matched_count,
        ),
    }


def build_slack_blocks(
    run_date_label: str,
    jobs: list[JobListing],
    full_list_url: str,
    *,
    matched_count: int | None = None,
) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": _digest_title(run_date_label, len(jobs)),
                "emoji": True,
            },
        },
    ]
    for display_rank, job in enumerate(jobs, start=1):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{_job_heading(job, display_rank)}\n{_job_details(job)}",
                },
            }
        )
    if not jobs:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No screened matches available._",
                },
            }
        )
    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": _full_list_footer(full_list_url, matched_count),
                }
            ],
        },
    )
    return blocks


def _digest_title(run_date_label: str, job_count: int) -> str:
    noun = "job" if job_count == 1 else "jobs"
    return f"Top {job_count} AI + startup {noun} · {run_date_label}"


def _job_heading(job: JobListing, display_rank: int) -> str:
    title = slack_escape(job.title or "Untitled role", limit=140)
    company = slack_escape(job.company_name or "Unknown company", limit=80)
    link = job.apply_url or job.job_url
    linked_title = f"<{link}|{title}>" if link else title
    return f"*{display_rank}. {linked_title}* — {company}"


def _job_details(job: JobListing) -> str:
    location = slack_escape(job.location or "Location not listed", limit=100)
    why = slack_escape(job.why_selected or "Good match for today", limit=240)
    return f"{location} · {why}"


def _full_list_footer(full_list_url: str, matched_count: int | None) -> str:
    if matched_count is None:
        summary = "More opportunities"
    else:
        noun = "job" if matched_count == 1 else "jobs"
        summary = f"{matched_count} matched {noun} today"
    if not full_list_url:
        return summary
    return f"{summary} · <{full_list_url}|View all matched jobs →>"


def slack_escape(value: str, *, limit: int | None = None) -> str:
    escaped = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if limit is None or len(escaped) <= limit:
        return escaped

    truncated = escaped[: max(0, limit - 1)].rstrip()
    if truncated.rfind("&") > truncated.rfind(";"):
        truncated = truncated[: truncated.rfind("&")].rstrip()
    return truncated + "…"


def post_slack_message(payload: dict) -> tuple[bool, str | None]:
    channel = str(payload.get("channel") or settings.slack_jobs_channel)
    slack_service = _slack_service()
    channel_id = channel if not channel.startswith("#") else slack_service.get_channel_id_by_name(channel[1:])
    if not channel_id:
        return False, f"Slack channel not found for {channel}"
    success, _ts = slack_service.send_message(channel_id, payload.get("text", ""), blocks=payload.get("blocks"))
    if not success:
        return False, f"Slack message send failed for {channel}"
    return True, None


def post_failure_alert(run_id: str, error_message: str) -> bool:
    text = (
        "Roo Jobs Daily failed\n"
        f"Run: {run_id}\n"
        f"Error: {error_message[:500]}\n"
        f"Status: {settings.public_base_url}/api/v1/jobs/runs/{run_id}"
    )
    success, _error = post_slack_message({"channel": settings.slack_jobs_channel, "text": text})
    return success
