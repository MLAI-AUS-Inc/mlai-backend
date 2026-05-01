from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

from jobs.conf import settings
from jobs.models import JobListing, JobRun

NOTION_API_URL = "https://api.notion.com/v1"


def notion_is_configured() -> bool:
    return bool(settings.notion_api_token and settings.notion_parent_page_id)


def publish_daily_jobs_page(run: JobRun, top_jobs: list[JobListing], all_jobs: list[JobListing]) -> str | None:
    if not notion_is_configured():
        return None

    parent, properties = build_parent_and_properties(f"Roo Jobs Daily - {format_run_date(run.run_date)}")
    payload = {
        "parent": parent,
        "properties": properties,
        "children": build_page_blocks(run, top_jobs, all_jobs),
    }

    response = requests.post(
        f"{NOTION_API_URL}/pages",
        headers=notion_headers(),
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("url")


def build_parent_and_properties(title: str) -> tuple[dict[str, str], dict[str, Any]]:
    parent_id = settings.notion_parent_page_id
    title_property = get_database_title_property(parent_id) if parent_id else None
    if title_property:
        return {"database_id": parent_id}, {title_property: {"title": rich_text(title)}}
    return {"page_id": parent_id}, {"title": {"title": rich_text(title)}}


def get_database_title_property(parent_id: str | None) -> str | None:
    if not parent_id:
        return None
    response = requests.get(
        f"{NOTION_API_URL}/databases/{parent_id}",
        headers=notion_headers(),
        timeout=15,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    database = response.json()
    properties = database.get("properties", {})
    for name, metadata in properties.items():
        if metadata.get("type") == "title":
            return name
    return None


def notion_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_api_token}",
        "Content-Type": "application/json",
        "Notion-Version": settings.notion_api_version,
    }


def build_page_blocks(run: JobRun, top_jobs: list[JobListing], all_jobs: list[JobListing]) -> list[dict[str, Any]]:
    notion_top_jobs = top_jobs[: settings.notion_top_pick_limit]
    blocks: list[dict[str, Any]] = [
        heading_1(f"Roo Jobs Daily - {format_run_date(run.run_date)}"),
        paragraph(
            f"Run {run.run_id}. Fetched {run.fetched_count}, matched {run.matched_count}, "
            f"deduped {run.deduped_count}, ranked {run.ranked_count}."
        ),
        heading_2(f"Top {len(notion_top_jobs)}"),
        jobs_table(notion_top_jobs, include_rank=True, include_logo=True),
    ]

    return blocks[:100]


def jobs_table(jobs: list[JobListing], include_rank: bool, include_logo: bool) -> dict[str, Any]:
    headers = ["Title", "Company", "Stage", "Remote", "Location", "Bucket", "Source", "Score", "Link"]
    if include_logo:
        headers.insert(2, "Logo")
    if include_rank:
        headers.insert(0, "#")

    rows = [table_row([plain_cell(value) for value in headers])]
    rows.extend(job_table_row(job, include_rank, include_logo) for job in jobs)

    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": len(headers),
            "has_column_header": True,
            "has_row_header": False,
            "children": rows,
        },
    }


def job_table_row(job: JobListing, include_rank: bool, include_logo: bool) -> dict[str, Any]:
    company = job.company_name or "Unknown company"
    location = job.location or "Location not listed"
    source = job.source_name or "Unknown source"
    url = job.apply_url or job.job_url
    cells = []

    if include_rank:
        cells.append(plain_cell(str(job.rank or "")))

    cells.extend(
        [
            plain_cell(job.title),
            plain_cell(company),
        ]
    )
    if include_logo:
        cells.append(link_cell("Logo", job.company_logo_url) if is_usable_logo_url(job.company_logo_url) else plain_cell(""))

    cells.extend(
        [
            plain_cell(job.company_stage or job.company_size or "", 24),
            plain_cell(remote_label(job.remote_eligibility), 28),
            plain_cell(location, 60),
            plain_cell(bucket_label(job.bucket)),
            plain_cell(source, 28),
            plain_cell(f"{job.ranking_score:.2f}"),
            link_cell("Read more", url),
        ]
    )
    return table_row(cells)


def table_row(cells: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"object": "block", "type": "table_row", "table_row": {"cells": cells}}


def plain_cell(text: str, limit: int = 80) -> list[dict[str, Any]]:
    return rich_text(truncate(text, limit))


def link_cell(text: str, url: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text, "link": {"url": url}}}]


def truncate(text: str, limit: int) -> str:
    value = text or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def is_usable_logo_url(url: str | None) -> bool:
    if not url:
        return False
    return url.startswith("https://")


def remote_label(value: str | None) -> str:
    if not value:
        return ""
    return value.replace("_", " ").title()


def heading_1(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": rich_text(text)}}


def heading_2(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": rich_text(text)}}


def heading_3(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": rich_text(text[:180])}}


def paragraph(text: str) -> dict[str, Any]:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": rich_text(text[:1900])}}


def bookmark(url: str) -> dict[str, Any]:
    return {"object": "block", "type": "bookmark", "bookmark": {"url": url}}


def rich_text(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": text}}]


def bucket_label(bucket: str | None) -> str:
    if not bucket:
        return "Matched"
    return bucket.replace("_", " ").title()


def format_run_date(run_date: str) -> str:
    try:
        date_value = datetime.fromisoformat(run_date)
        return date_value.strftime("%A, %d %B %Y").replace(", 0", ", ")
    except ValueError:
        return run_date
