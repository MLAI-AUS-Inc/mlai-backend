from __future__ import annotations

from datetime import datetime

from jobs.conf import settings
from jobs.models import JobListing


def _slack_service():
    from integrations.services.slack import SlackService

    return SlackService


def format_slack_message(run_date: str, top_jobs: list[JobListing], full_list_url: str) -> dict:
    try:
        date_value = datetime.fromisoformat(run_date)
        parsed_date = date_value.strftime("%A, %d %B %Y").replace(", 0", ", ")
    except ValueError:
        parsed_date = run_date
    lines = [
        f"Today's best AI + startup jobs for {parsed_date}",
        "Fresh roles with the strongest Australia, remote, AI, and startup fit.",
        "",
    ]

    for job in top_jobs[: settings.jobs_top_pick_limit]:
        title = job.title or "Untitled role"
        company = job.company_name or "Unknown company"
        location = job.location or "Location not listed"
        why = job.why_selected or "good match for today"
        link = job.apply_url or job.job_url
        lines.append(f"{job.rank}. {title} - {company} - {location}")
        lines.append(f"   {why}")
        lines.append(f"   <{link}|Read more>")

    lines.extend(["", f"More opportunities: <{full_list_url}|View all matched jobs>"])
    return {"channel": settings.slack_jobs_channel, "text": "\n".join(lines)}


def post_slack_message(payload: dict) -> tuple[bool, str | None]:
    channel = str(payload.get("channel") or settings.slack_jobs_channel)
    slack_service = _slack_service()
    channel_id = channel if not channel.startswith("#") else slack_service.get_channel_id_by_name(channel[1:])
    if not channel_id:
        return False, f"Slack channel not found for {channel}"
    success, _ts = slack_service.send_message(channel_id, payload.get("text", ""))
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
