from __future__ import annotations

from datetime import date
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from jobs.services.logos import logo_url_for_company
from jobs.services.public_pages import clean_text, infer_location, infer_posted, looks_like_title

BASE_URL = "https://www.careerone.com.au"
QUERIES = ("ai", "machine-learning", "data-scientist", "startup-software-engineer")
HEADERS = {"User-Agent": "RooJobsDaily/0.1 (+https://roo.jobs)"}


def collect_careerone_jobs(per_query_limit: int = 10) -> list[dict[str, Any]]:
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
    url = f"{BASE_URL}/{quote_plus(query).replace('+', '-')}-jobs/in-australia"
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
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
