from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from jobs.services.logos import absolute_image_url, logo_url_for_company


def collect_getro_jobs(
    base_url: str,
    source_name: str,
    source_type: str = "vc_portfolio",
    source_quality_score: float = 0.9,
    limit: int = 40,
) -> list[dict[str, Any]]:
    response = requests.get(
        f"{base_url.rstrip('/')}/jobs",
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
            title_link = first_job_link(card)
        if not title_link:
            continue

        title = clean_text(title_link.get_text(" ", strip=True))
        job_url = urljoin(base_url, title_link.get("href", ""))
        if not title or not job_url or job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        company = extract_company(card)
        company_logo_url = extract_logo_url(card, base_url, company)
        fragments = [clean_text(value) for value in card.stripped_strings]
        fragments = [value for value in fragments if value]

        jobs.append(
            {
                "run_date": date.today().isoformat(),
                "source_name": source_name,
                "source_type": source_type,
                "source_quality_score": source_quality_score,
                "keyword": f"{source_name.lower()}_portfolio",
                "title": title,
                "company_name": company,
                "company_logo_url": company_logo_url,
                "location": extract_location(fragments, title, company),
                "posted_text": extract_posted_text(fragments),
                "description": clean_text(card.get_text(" ", strip=True)),
                "job_url": job_url,
                "apply_url": job_url,
            }
        )

    return jobs


def first_job_link(card):
    for link in card.find_all("a", href=True):
        href = link["href"]
        text = clean_text(link.get_text(" ", strip=True))
        if "/jobs/" in href and text and not text.lower().startswith("read more"):
            return link
    return None


def extract_company(card) -> str | None:
    for link in card.find_all("a", href=True):
        href = link["href"]
        if "/companies/" in href and "/jobs/" not in href:
            company = clean_text(link.get_text(" ", strip=True))
            if company:
                return company
    return None


def extract_logo_url(card, base_url: str, company: str | None) -> str | None:
    company_text = (company or "").lower()
    for image in card.find_all("img"):
        alt = (image.get("alt") or "").lower()
        src = image.get("src")
        if src and ("logo" in alt or (company_text and company_text in alt)):
            return absolute_image_url(src, base_url)
    return logo_url_for_company(company)


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
        if re.fullmatch(r"today|new|\d+\s+days?|\d+\s+hours?|\d+\s+months?|6\+\s+months", value, re.I):
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
        "new zealand",
        "singapore",
        "united states",
        "uk",
        "canada",
    )
    if any(term in text for term in location_terms):
        return True
    return bool(re.search(r"\b[A-Z]{2,3},\s", value))


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
