from __future__ import annotations

from datetime import datetime

from jobs.conf import settings
from jobs.models import JobListing
from jobs.services.final_screening import apply_publish_screen


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

    screened_jobs = [job for job in top_jobs if apply_publish_screen(job)][: settings.jobs_top_pick_limit]
    for display_rank, job in enumerate(screened_jobs[: settings.jobs_top_pick_limit], start=1):
        title = job.title or "Untitled role"
        company = job.company_name or "Unknown company"
        location = job.location or "Location not listed"
        why = job.why_selected or "good match for today"
        link = job.apply_url or job.job_url
        lines.append(f"{display_rank}. {title} - {company} - {location}")
        lines.append(f"   {why}")
        lines.append(f"   <{link}|Apply now>")

    lines.extend(["", f"More opportunities: <{full_list_url}|View all matched jobs>", "", feedback_footer()])
    return {
        "channel": settings.slack_jobs_channel,
        "text": "\n".join(lines),
        "blocks": build_slack_blocks(parsed_date, screened_jobs, full_list_url),
    }


def build_slack_blocks(run_date_label: str, jobs: list[JobListing], full_list_url: str) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"Today's best AI + startup jobs for {run_date_label}",
                "emoji": True,
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "Fresh roles with the strongest Australia, remote, AI, and startup fit.",
            },
        },
    ]

    for display_rank, job in enumerate(jobs, start=1):
        title = slack_escape(job.title or "Untitled role")
        company = slack_escape(job.company_name or "Unknown company")
        location = slack_escape(job.location or "Location not listed")
        source = slack_escape(job.source_name or "Unknown source")
        why = slack_escape(job.why_selected or "good match for today")
        link = job.apply_url or job.job_url
        link_text = f"<{link}|Apply now>" if link else "Not listed"
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*#{display_rank} {title}*"},
                    "fields": [
                        {"type": "mrkdwn", "text": f"*Company:*\n{company}"},
                        {"type": "mrkdwn", "text": f"*Location:*\n{location}"},
                        {"type": "mrkdwn", "text": f"*Source:*\n{source}"},
                        {"type": "mrkdwn", "text": f"*Why selected:*\n{why}"},
                        {"type": "mrkdwn", "text": f"*Job link:*\n{link_text}"},
                    ],
                },
            ]
        )

    blocks.extend(
        [
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*More opportunities:* <{full_list_url}|View all matched jobs>"},
            },
            {"type": "context", "elements": [{"type": "mrkdwn", "text": feedback_footer()}]},
        ]
    )
    return blocks[:50]


def slack_escape(value: str) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def feedback_footer() -> str:
    return (
        "Help Roo improve tomorrow's jobs: reply in this thread with examples like "
        "`good #2`, `bad #5 not AI`, `bad #4 location restricted`, "
        "`bad #6 generic software role`, `/job-disqualify Remote, USA`, "
        "`/job-disqualify PhD scholarship`, or `/job-disqualify EU only`."
    )


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
