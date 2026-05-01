from __future__ import annotations

import re
from datetime import date
from html import unescape
from typing import Any

import requests

from jobs.services.logos import logo_url_for_company

BASE_URL = "https://himalayas.app"
SEARCH_URL = f"{BASE_URL}/jobs/api/search"

DEFAULT_QUERIES = (
    "machine learning",
    "AI engineer",
    "data science",
    "MLOps",
    "startup",
)


def collect_himalayas_jobs(per_query_limit: int = 20) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    for query in DEFAULT_QUERIES:
        page_jobs = fetch_search_page(query=query, page=1)
        for item in page_jobs[:per_query_limit]:
            mapped = map_himalayas_job(item, query)
            if not mapped:
                continue
            key = mapped.get("job_url") or mapped.get("apply_url")
            if not key or key in seen:
                continue
            seen.add(key)
            jobs.append(mapped)

    return jobs


def fetch_search_page(query: str, page: int = 1) -> list[dict[str, Any]]:
    response = requests.get(
        SEARCH_URL,
        params={"q": query, "worldwide": "true", "sort": "recent", "page": page},
        headers={"User-Agent": "RooJobsDaily/0.1 (+https://roo.jobs)"},
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("jobs", "data", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def map_himalayas_job(item: dict[str, Any], query: str) -> dict[str, Any] | None:
    title = clean_text(item.get("title"))
    company = clean_text(item.get("companyName") or item.get("company"))
    if not title:
        return None

    job_url = item.get("url") or item.get("jobUrl") or item.get("himalayasUrl")
    if job_url and job_url.startswith("/"):
        job_url = f"{BASE_URL}{job_url}"
    if not job_url:
        guid = item.get("guid") or item.get("slug") or item.get("id")
        if isinstance(guid, str) and guid.startswith("http"):
            job_url = guid
        else:
            job_url = f"{BASE_URL}/jobs/{guid}" if guid else item.get("applicationLink")

    restrictions = item.get("locationRestrictions") or []
    location = format_location(restrictions, item.get("timezoneRestriction"))
    description = clean_html(item.get("description") or item.get("excerpt"))
    salary = format_salary(item)
    if salary:
        description = f"{description} Salary: {salary}".strip()

    return {
        "run_date": date.today().isoformat(),
        "source_name": "Himalayas",
        "source_type": "remote_board",
        "source_quality_score": 0.82,
        "keyword": query,
        "title": title,
        "company_name": company or None,
        "company_logo_url": extract_logo_url(item, company),
        "location": location,
        "remote_region": location,
        "posted_text": item.get("pubDate") or item.get("publishedAt"),
        "date_posted": parse_datetime(item.get("pubDate") or item.get("publishedAt")),
        "description": description,
        "job_url": job_url,
        "apply_url": item.get("applicationLink") or job_url,
    }


def format_location(restrictions: Any, timezones: Any) -> str:
    if isinstance(restrictions, list) and restrictions:
        joined = ", ".join(str(value) for value in restrictions[:4])
        return f"Remote - {joined}"
    if isinstance(timezones, list) and timezones:
        joined = ", ".join(str(value) for value in timezones[:4])
        return f"Remote - {joined}"
    return "Remote - worldwide"


def format_salary(item: dict[str, Any]) -> str | None:
    min_salary = item.get("minSalary")
    max_salary = item.get("maxSalary")
    currency = item.get("currency")
    if min_salary and max_salary and currency:
        return f"{min_salary}-{max_salary} {currency}"
    return None


def extract_logo_url(item: dict[str, Any], company: str | None) -> str | None:
    for key in ("companyLogo", "companyLogoUrl", "company_logo_url", "logo", "logoUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    company_data = item.get("company")
    if isinstance(company_data, dict):
        for key in ("logo", "logoUrl", "image", "avatarUrl"):
            value = company_data.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    return logo_url_for_company(company)


def parse_datetime(value):
    if not value:
        return None
    try:
        from datetime import datetime

        if isinstance(value, (int, float)):
            timestamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(timestamp)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (OSError, TypeError, ValueError):
        return None


def clean_html(value: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return clean_text(unescape(text))


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
