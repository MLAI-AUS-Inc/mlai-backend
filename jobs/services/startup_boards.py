from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from jobs.services.himalayas import parse_datetime
from jobs.services.logos import logo_url_for_company
from jobs.services.public_pages import clean_text
from jobs.services.public_pages import collect_simple_jobs
from jobs.services.rendered_jobs import collect_rendered_jobs

YC_JOBS_URL = "https://www.ycombinator.com/jobs"
MAIN_SEQUENCE_URL = "https://jobs.mseq.vc"
MAIN_SEQUENCE_API_URL = f"{MAIN_SEQUENCE_URL}/api/jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}


def collect_startup_jobs(limit: int = 25) -> list[dict]:
    return collect_rendered_jobs(
        url="https://startup.jobs/?q=AI",
        source_name="Startup.jobs",
        source_type="startup_board",
        source_quality_score=0.86,
        limit=limit,
    )


def collect_yc_jobs(limit: int = 25) -> list[dict]:
    response = requests.get(YC_JOBS_URL, headers=HEADERS, timeout=25)
    response.raise_for_status()
    return parse_yc_jobs_html(response.text, limit)


def collect_main_sequence_jobs(limit: int = 25) -> list[dict[str, Any]]:
    response = requests.get(
        MAIN_SEQUENCE_API_URL,
        params=main_sequence_params(limit),
        headers={**HEADERS, "Accept": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    data = response.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return [job for item in jobs[:limit] if (job := map_main_sequence_job(item))]


def main_sequence_params(limit: int) -> dict[str, Any]:
    return {
        "company": "all",
        "jobtype": "[]",
        "category": "[]",
        "secondary_category": "[]",
        "tags": "[]",
        "city": "[]",
        "state": "[]",
        "country": "[]",
        "remote_ok": "false",
        "remote_only": "false",
        "salary_timeframe": "",
        "salary_min": "",
        "salary_max": "",
        "keyword": "",
        "custom_fields": "{}",
        "limit": limit,
        "page": 1,
        "sortby": "newest",
    }


def map_main_sequence_job(item: dict[str, Any]) -> dict[str, Any] | None:
    title = clean_text(item.get("title"))
    if not title:
        return None

    company = clean_text(item.get("company_name") or nested_name(item.get("company"))) or None
    job_url = build_main_sequence_job_url(item)
    apply_url = item.get("apply_url") or job_url
    location = main_sequence_location(item)
    description = clean_html(item.get("description_html")) or clean_text(item.get("company_one_liner"))

    return {
        "run_date": date.today().isoformat(),
        "source_name": "Main Sequence Jobs",
        "source_type": "vc_portfolio",
        "source_quality_score": 0.88,
        "keyword": "main_sequence_jobs",
        "title": title,
        "company_name": company,
        "company_logo_url": item.get("company_logo") or logo_url_for_company(company),
        "company_domain": item.get("company_website"),
        "location": location,
        "remote_region": location if item.get("remote_only") or item.get("is_remote") else None,
        "posted_text": item.get("timeago") or item.get("published_at"),
        "date_posted": item.get("published_at") or item.get("created_at"),
        "description": description,
        "job_url": job_url,
        "apply_url": apply_url,
    }


def build_main_sequence_job_url(item: dict[str, Any]) -> str:
    job_id = item.get("id")
    slug = item.get("slug")
    company_slug = item.get("company_slug")
    if job_id and slug and company_slug:
        return f"{MAIN_SEQUENCE_URL}/job/{job_id}-{slug}-{company_slug}"
    if job_id and slug:
        return f"{MAIN_SEQUENCE_URL}/job/{job_id}-{slug}"
    return MAIN_SEQUENCE_URL


def main_sequence_location(item: dict[str, Any]) -> str | None:
    if item.get("remote_only"):
        return clean_text(item.get("remote_required_location")) or "Remote"
    if item.get("is_remote"):
        return clean_text(item.get("remote_required_location")) or "Hybrid"
    return clean_text(item.get("location_name") or nested_name(item.get("location"))) or None


def nested_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name") or value.get("label")
    return None


def parse_yc_jobs_html(html_text: str, limit: int = 25) -> list[dict[str, Any]]:
    text = html.unescape(html_text)
    jobs: list[dict[str, Any]] = []
    seen: set[int] = set()

    pattern = r'\{\s*"id"\s*:\s*\d+\s*,\s*"title"\s*:.*?,\s*"ctaUrl"\s*:\s*".*?"\s*\}'
    for raw_object in re.findall(pattern, text, re.S):
        if len(jobs) >= limit:
            break
        try:
            item = json.loads(raw_object)
        except json.JSONDecodeError:
            continue
        job = map_yc_job(item)
        if not job:
            continue
        job_id = int(item["id"])
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append(job)

    return jobs


def map_yc_job(item: dict[str, Any]) -> dict[str, Any] | None:
    title = clean_text(item.get("title"))
    job_url = item.get("url")
    if not title or not job_url:
        return None
    company = clean_text(item.get("companyName")) or None
    description = " ".join(
        value
        for value in (
            item.get("companyOneLiner"),
            item.get("prettyRole"),
            item.get("type"),
            item.get("salaryRange"),
            item.get("visa"),
            item.get("minExperience"),
        )
        if value
    )
    return {
        "run_date": date.today().isoformat(),
        "source_name": "YC Jobs",
        "source_type": "startup_board",
        "source_quality_score": 0.9,
        "keyword": "yc_jobs",
        "title": title,
        "company_name": company,
        "company_logo_url": item.get("companyLogoUrl") or logo_url_for_company(company),
        "company_stage": item.get("companyBatchName"),
        "location": clean_text(item.get("location")) or None,
        "posted_text": item.get("createdAt"),
        "date_posted": parse_datetime(item.get("createdAt")),
        "description": clean_text(description),
        "job_url": urljoin(YC_JOBS_URL, job_url),
        "apply_url": item.get("applyUrl") or item.get("ctaUrl") or urljoin(YC_JOBS_URL, job_url),
    }


def clean_html(value: str | None) -> str:
    if not value:
        return ""
    return clean_text(BeautifulSoup(value, "html.parser").get_text(" ", strip=True))
