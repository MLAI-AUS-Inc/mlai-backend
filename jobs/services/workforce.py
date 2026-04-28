from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests

from jobs.services.logos import logo_url_for_company

BASE_URL = "https://www.workforceaustralia.gov.au"
API_URL = f"{BASE_URL}/api/v1/global/vacancies/"
QUERIES = ("AI", "machine learning", "data scientist", "startup software engineer")
RELEVANCE_PATTERNS = (
    r"\bAI\b",
    r"\bartificial intelligence\b",
    r"\bmachine learning\b",
    r"\bdata science\b",
    r"\bdata scientist\b",
    r"\bsoftware engineer\b",
    r"\bstartup\b",
    r"\bLLM\b",
    r"\bgenerative AI\b",
)
HEADERS = {
    "User-Agent": "RooJobsDaily/0.1 (+https://roo.jobs)",
    "Accept": "application/json",
}


def collect_workforce_jobs(per_query_limit: int = 10) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in QUERIES:
        for job in fetch_query_jobs(query, per_query_limit):
            key = job["job_url"]
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
    return jobs


def fetch_query_jobs(query: str, limit: int) -> list[dict[str, Any]]:
    response = requests.get(
        API_URL,
        params={"searchText": query, "pageSize": limit},
        headers=HEADERS,
        timeout=25,
    )
    response.raise_for_status()
    results = response.json().get("results", [])
    return [job for item in results if (job := map_workforce_job(item.get("result", item), query))]


def map_workforce_job(item: dict[str, Any], query: str) -> dict[str, Any] | None:
    title = item.get("title")
    vacancy_id = item.get("vacancyId")
    if not title or not vacancy_id:
        return None

    company = item.get("employerName") or None
    location = format_location(item)
    description = item.get("description")
    if not is_relevant(title, description):
        return None
    logo_url = urljoin(BASE_URL, item.get("logoUrl")) if item.get("logoUrl") else logo_url_for_company(company)
    job_url = f"{BASE_URL}/individuals/jobs/details/{vacancy_id}"

    return {
        "run_date": date.today().isoformat(),
        "source_name": "Workforce Australia",
        "source_type": "government_board",
        "source_quality_score": 0.62,
        "keyword": query,
        "title": title,
        "company_name": company,
        "company_logo_url": logo_url,
        "location": location,
        "posted_text": item.get("displayFromDate") or item.get("creationDate"),
        "date_posted": item.get("displayFromDate") or item.get("creationDate"),
        "description": description,
        "job_url": job_url,
        "apply_url": job_url,
    }


def format_location(item: dict[str, Any]) -> str | None:
    suburb = item.get("suburb")
    state = item.get("state")
    location = item.get("location", {}).get("label") if isinstance(item.get("location"), dict) else None
    if suburb and state:
        return f"{suburb.title()}, {state}"
    return location


def is_relevant(title: str, description: str | None) -> bool:
    text = f"{title} {description or ''}"
    return any(re.search(pattern, text, re.I) for pattern in RELEVANCE_PATTERNS)
