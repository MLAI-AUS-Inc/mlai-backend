from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

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
HTML_HEADERS = {
    "User-Agent": "RooJobsDaily/0.1 (+https://roo.jobs)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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
    if not company:
        # Some listings (usually recruiter-sourced) come through with no employer name
        # at all. Try the cheap path first: the description sometimes has a plain-text
        # "Company: X" label. Only fall back to following the embedded apply link (an
        # extra network fetch) when that's not there.
        company = extract_company_label_from_text(item.get("description"))
    if not company:
        apply_link = extract_apply_link(item.get("description"))
        if apply_link:
            company = fetch_external_company_name(apply_link)
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


def extract_company_label_from_text(description: str | None) -> str | None:
    if not description:
        return None
    # Descriptions come through as either plain text or HTML paragraphs depending on
    # the original listing source, so normalize to plain text before matching.
    text = BeautifulSoup(description, "html.parser").get_text(" ") if "<" in description else description
    match = re.search(r"\bCompany:\s*([^\n]+?)(?:\s+Location:|\s+View more detail|$)", text, re.I)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip().rstrip(".")
    return None


def extract_apply_link(description: str | None) -> str | None:
    if not description or "<" not in description:
        return None
    link = BeautifulSoup(description, "html.parser").find("a", href=True)
    return link["href"] if link else None


def fetch_external_company_name(url: str) -> str | None:
    try:
        response = requests.get(url, headers=HTML_HEADERS, timeout=15, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException:
        return None
    return extract_company_from_job_posting_page(response.text)


def extract_company_from_job_posting_page(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        if '"JobPosting"' not in text:
            continue
        match = re.search(r'"hiringOrganization"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', text)
        if match:
            return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


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
