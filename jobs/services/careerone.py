from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from jobs.services.logos import logo_url_for_company
from jobs.services.public_pages import clean_text, infer_location, infer_posted, looks_like_title

BASE_URL = "https://www.careerone.com.au"
QUERIES = ("ai", "machine-learning", "data-scientist", "startup-software-engineer")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": BASE_URL,
}
RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}
MAX_RETRIES = 3
CAREERONE_ENABLED = True
logger = logging.getLogger(__name__)


def collect_careerone_jobs(per_query_limit: int = 10) -> list[dict[str, Any]]:
    if not CAREERONE_ENABLED:
        return []
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in QUERIES:
        query_jobs = fetch_query_jobs(query, per_query_limit)
        for job in query_jobs:
            key = job["job_url"]
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)
    return jobs


def fetch_query_jobs(query: str, limit: int) -> list[dict[str, Any]]:
    url = f"{BASE_URL}/{quote_plus(query).replace('+', '-')}-jobs/in-australia"
    response = get_careerone_response(url, query)
    soup = BeautifulSoup(response.text, "html.parser")

    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.select('a[href^="/jobview/"]'):
        if len(jobs) >= limit:
            break
        title = clean_text(link.get_text(" ", strip=True))
        if title.lower() == "view job":
            title = title_from_card(link)
        if not looks_like_title(title):
            continue
        job_url = urljoin(BASE_URL, link["href"])
        if job_url in seen:
            continue
        seen.add(job_url)
        card = nearest_job_card(link)
        fragments = [clean_text(value) for value in card.stripped_strings] if card else [title]
        fragments = [value for value in fragments if value and value.lower() not in {"save", "view job"}]
        company = infer_company_from_fragments(fragments, title)
        jobs.append(
            {
                "run_date": date.today().isoformat(),
                "source_name": "CareerOne",
                "source_type": "broad_board",
                "source_quality_score": 0.56,
                "keyword": query.replace("-", " "),
                "title": title,
                "company_name": company,
                "company_logo_url": logo_url_for_company(company),
                "location": infer_location(fragments),
                "posted_text": infer_posted(fragments),
                "description": " ".join(fragments)[:5000],
                "job_url": job_url,
                "apply_url": job_url,
            }
        )
    return jobs


def get_careerone_response(url: str, query: str) -> requests.Response:
    with requests.Session() as session:
        session.trust_env = False
        session.headers.update(HEADERS)
        for attempt in range(1, MAX_RETRIES + 1):
            response = session.get(url, timeout=25)
            if response.status_code not in RETRY_STATUS_CODES or attempt == MAX_RETRIES:
                response.raise_for_status()
                return response
            delay = retry_delay_seconds(response, attempt)
            logger.warning(
                "CareerOne query %s returned HTTP %s; retrying in %ss",
                query,
                response.status_code,
                delay,
            )
            time.sleep(delay)
    raise RuntimeError("CareerOne request retry loop exited unexpectedly")


def retry_delay_seconds(response: requests.Response, attempt: int) -> int:
    retry_after = response.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        return min(int(retry_after), 60)
    return min(2**attempt, 30)


def nearest_job_card(link):
    for parent in link.parents:
        text = clean_text(parent.get_text(" ", strip=True))
        if parent.name == "div" and "Posted" in text and len(text) > 120:
            return parent
    return link.parent


def title_from_card(link) -> str:
    card = nearest_job_card(link)
    if not card:
        return ""
    for candidate in card.find_all("a", href=True):
        href = candidate.get("href", "")
        text = clean_text(candidate.get_text(" ", strip=True))
        if href.startswith("/jobview/") and text.lower() != "view job" and looks_like_title(text):
            return text
    fragments = [clean_text(value) for value in card.stripped_strings]
    for value in fragments:
        if looks_like_title(value):
            return value
    return ""


def infer_company_from_fragments(fragments: list[str], title: str) -> str | None:
    for index, value in enumerate(fragments):
        if value == title and index + 1 < len(fragments):
            company = fragments[index + 1]
            if company and not infer_location([company]) and not infer_posted([company]):
                return company
    return None
