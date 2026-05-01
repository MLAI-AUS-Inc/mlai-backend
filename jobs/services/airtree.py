from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://jobs.airtree.vc"


def collect_airtree_jobs(limit: int = 40) -> list[dict[str, Any]]:
    response = requests.get(
        f"{BASE_URL}/jobs",
        headers={"User-Agent": "RooJobsDaily/0.1 (+https://roo.jobs)"},
        timeout=25,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    cards = soup.select('[data-testid="job-list-item"], .job-card')
    jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for card in cards:
        if len(jobs) >= limit:
            break

        title_link = card.select_one('[data-testid="job-title-link"]')
        if not title_link:
            continue

        title = clean_text(title_link.get_text(" ", strip=True))
        job_url = urljoin(BASE_URL, title_link.get("href", ""))
        if not title or not job_url or job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        company = extract_company(card)
        fragments = [clean_text(value) for value in card.stripped_strings]
        fragments = [value for value in fragments if value]
        location = extract_location(fragments, title, company)
        posted_text = extract_posted_text(fragments)

        jobs.append(
            {
                "run_date": date.today().isoformat(),
                "source_name": "Airtree Jobs",
                "source_type": "vc_portfolio",
                "source_quality_score": 0.95,
                "keyword": "airtree_portfolio",
                "title": title,
                "company_name": company,
                "location": location,
                "posted_text": posted_text,
                "description": clean_text(card.get_text(" ", strip=True)),
                "job_url": job_url,
                "apply_url": job_url,
            }
        )

    return jobs


def extract_company(card) -> str | None:
    for link in card.find_all("a", href=True):
        href = link["href"]
        if "/companies/" in href and "/jobs/" not in href:
            company = clean_text(link.get_text(" ", strip=True))
            if company:
                return company
    return None


def extract_location(fragments: list[str], title: str, company: str | None) -> str | None:
    ignored = {title, company or "", "Read more", "about", "at"}
    for value in fragments:
        if value in ignored:
            continue
        if looks_like_location(value):
            return value.replace(" ; ", "; ")
    return None


def extract_posted_text(fragments: list[str]) -> str | None:
    for value in fragments:
        if re.fullmatch(r"today|new|\d+\s+days?|\d+\s+hours?", value, re.I):
            return value
    return None


def looks_like_location(value: str) -> bool:
    text = value.lower()
    location_terms = (
        "australia",
        "sydney",
        "melbourne",
        "brisbane",
        "perth",
        "adelaide",
        "canberra",
        "remote",
        "united states",
        "singapore",
        "new zealand",
        "uk",
        "canada",
    )
    if any(term in text for term in location_terms):
        return True
    return bool(re.search(r"\b[A-Z]{2,3},\s", value))


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
