from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

from jobs.services.logos import logo_url_for_company
from jobs.services.public_pages import infer_posted

BASE_URL = "https://au.indeed.com"
QUERIES = ("AI engineer", "machine learning engineer", "data scientist", "startup software engineer")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}


class IndeedBlockedError(RuntimeError):
    pass


def build_search_url(query: str) -> str:
    return f"{BASE_URL}/jobs?q={quote_plus(query)}&l=Australia&sort=date"


def collect_indeed_jobs(per_query_limit: int = 10) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    session = requests.Session()
    session.headers.update(HEADERS)
    for query in QUERIES:
        page_jobs = fetch_query_jobs(session, query, per_query_limit)
        for job in page_jobs:
            key = job["job_url"]
            if key in seen:
                continue
            seen.add(key)
            jobs.append(job)

    return jobs


def fetch_query_jobs(session: requests.Session, query: str, limit: int) -> list[dict[str, Any]]:
    response = session.get(build_search_url(query), timeout=25)
    if response.status_code in {403, 429}:
        raise IndeedBlockedError(f"Indeed returned HTTP {response.status_code}")
    response.raise_for_status()
    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    if is_blocked_page(html, soup):
        raise IndeedBlockedError("Indeed returned a verification/block page")

    jobs: list[dict[str, Any]] = []
    for card in find_job_cards(soup):
        if len(jobs) >= limit:
            break
        job = parse_card(card, query)
        if job:
            jobs.append(job)

    return jobs


def find_job_cards(soup: BeautifulSoup):
    selectors = (
        "div.job_seen_beacon",
        "div[data-jk]",
        "td.resultContent",
        ".jobsearch-ResultsList .result",
    )
    seen_ids: set[int] = set()
    for selector in selectors:
        for card in soup.select(selector):
            marker = id(card)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            yield card


def parse_card(card, query: str) -> dict[str, Any] | None:
    title_link = first_select(card, ("a.jcs-JobTitle", "h2.jobTitle a", "a[data-jk]", "a[href*='/rc/clk']"))
    if not title_link:
        return None

    title = clean_text(title_link.get_text(" ", strip=True))
    href = title_link.get("href")
    if not title or not href:
        return None

    job_url = urljoin(BASE_URL, href)
    company = text_from_first(card, ("span[data-testid='company-name']", "span.companyName", "[data-testid='company-name']"))
    location = text_from_first(card, ("div[data-testid='text-location']", "div.companyLocation", "[data-testid='text-location']"))
    salary = text_from_first(card, ("div.metadata.salary-snippet-container", ".salary-snippet-container", "[data-testid='attribute_snippet_testid']"))
    description = clean_text(card.get_text(" ", strip=True))
    fragments = [clean_text(part) for part in description.split("\n") if clean_text(part)]

    return {
        "run_date": date.today().isoformat(),
        "source_name": "Indeed Australia",
        "source_type": "broad_board",
        "source_quality_score": 0.7,
        "keyword": query,
        "title": title,
        "company_name": company,
        "company_logo_url": logo_url_for_company(company),
        "location": location,
        "salary": salary,
        "posted_text": infer_posted(fragments) or infer_posted([description]),
        "description": description[:5000],
        "job_url": job_url,
        "apply_url": job_url,
    }


def is_blocked_page(html: str, soup: BeautifulSoup) -> bool:
    lowered = html.lower()
    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    page_text = clean_text(soup.get_text(" ", strip=True)).lower()
    signals = (
        "just a moment",
        "captcha",
        "verify you are human",
        "access denied",
        "blocked",
        "unusual traffic",
    )
    return any(signal in lowered or signal in title or signal in page_text for signal in signals)


def first_select(card, selectors: tuple[str, ...]):
    for selector in selectors:
        match = card.select_one(selector)
        if match:
            return match
    return None


def text_from_first(card, selectors: tuple[str, ...]) -> str | None:
    match = first_select(card, selectors)
    if not match:
        return None
    value = clean_text(match.get_text(" ", strip=True))
    return value or None


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
