from __future__ import annotations

from datetime import date
from typing import Any

import requests

from jobs.conf import settings
from jobs.services.public_pages import collect_simple_jobs

API_URL = "https://api.adzuna.com/v1/api/jobs/au/search/1"
PUBLIC_SEARCH_URL = "https://www.adzuna.com.au/search?what={query}&where=Australia&sort_by=date"
QUERIES = ("machine learning", "AI engineer", "data scientist", "startup software engineer")


def collect_adzuna_jobs(per_query_limit: int = 10) -> list[dict[str, Any]]:
    if settings.adzuna_app_id and settings.adzuna_app_key:
        return collect_adzuna_api_jobs(per_query_limit)
    return collect_adzuna_public_jobs(per_query_limit)


def collect_adzuna_api_jobs(per_query_limit: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in QUERIES:
        response = requests.get(
            API_URL,
            params={
                "app_id": settings.adzuna_app_id,
                "app_key": settings.adzuna_app_key,
                "what": query,
                "where": "Australia",
                "sort_by": "date",
                "results_per_page": per_query_limit,
                "content-type": "application/json",
            },
            timeout=25,
        )
        response.raise_for_status()
        for item in response.json().get("results", []):
            mapped = map_adzuna_job(item, query)
            if not mapped:
                continue
            key = mapped["job_url"]
            if key in seen:
                continue
            seen.add(key)
            jobs.append(mapped)
    return jobs


def collect_adzuna_public_jobs(per_query_limit: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in QUERIES:
        page_jobs = collect_simple_jobs(
            url=PUBLIC_SEARCH_URL.format(query=query.replace(" ", "+")),
            source_name="Adzuna",
            source_type="broad_board_api",
            source_quality_score=0.66,
            limit=per_query_limit,
        )
        for job in page_jobs:
            if job["job_url"] in seen:
                continue
            seen.add(job["job_url"])
            jobs.append(job)
    return jobs


def map_adzuna_job(item: dict[str, Any], query: str) -> dict[str, Any] | None:
    title = item.get("title")
    url = item.get("redirect_url")
    if not title or not url:
        return None
    company = item.get("company", {}).get("display_name") if isinstance(item.get("company"), dict) else None
    location = item.get("location", {}).get("display_name") if isinstance(item.get("location"), dict) else None
    description = item.get("description")
    return {
        "run_date": date.today().isoformat(),
        "source_name": "Adzuna",
        "source_type": "broad_board_api",
        "source_quality_score": 0.66,
        "keyword": query,
        "title": title,
        "company_name": company,
        "location": location,
        "posted_text": item.get("created"),
        "date_posted": item.get("created"),
        "description": description,
        "job_url": url,
        "apply_url": url,
    }
