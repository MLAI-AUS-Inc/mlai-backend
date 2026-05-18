from __future__ import annotations

import time
from datetime import date
from typing import Any

import requests

from jobs.conf import settings

API_URL = "https://api.adzuna.com/v1/api/jobs/au/search/1"
QUERIES = ("machine learning", "AI engineer", "data scientist", "startup software engineer")
RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}
MAX_RETRIES = 3


def collect_adzuna_jobs(per_query_limit: int = 10) -> list[dict[str, Any]]:
    if settings.adzuna_app_id and settings.adzuna_app_key:
        return collect_adzuna_api_jobs(per_query_limit)
    return []


def collect_adzuna_api_jobs(per_query_limit: int) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in QUERIES:
        response = get_adzuna_api_response(query, per_query_limit)
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


def get_adzuna_api_response(query: str, per_query_limit: int) -> requests.Response:
    params = {
        "app_id": settings.adzuna_app_id,
        "app_key": settings.adzuna_app_key,
        "what": query,
        "where": "Australia",
        "sort_by": "date",
        "results_per_page": per_query_limit,
        "content-type": "application/json",
    }
    with requests.Session() as session:
        session.trust_env = False
        for attempt in range(1, MAX_RETRIES + 1):
            response = session.get(API_URL, params=params, timeout=25)
            if response.status_code not in RETRY_STATUS_CODES or attempt == MAX_RETRIES:
                response.raise_for_status()
                return response
            delay = retry_delay_seconds(response, attempt)
            time.sleep(delay)
    raise RuntimeError("Adzuna API request retry loop exited unexpectedly")


def retry_delay_seconds(response: requests.Response, attempt: int) -> int:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(int(retry_after), 60)
    return min(2**attempt, 30)


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
