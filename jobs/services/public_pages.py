from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, NavigableString

from jobs.services.logos import logo_url_for_company

HEADERS = {"User-Agent": "RooJobsDaily/0.1 (+https://roo.jobs)"}


def collect_simple_jobs(
    url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int = 40,
) -> list[dict[str, Any]]:
    response = requests.get(url, headers=HEADERS, timeout=25)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    if source_name.startswith("Built In"):
        return parse_builtin_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if source_name == "AI Jobs Australia":
        return parse_aijobs_com_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if source_name == "ai-jobs.com.au":
        return parse_ai_jobs_au_jobs(soup, url, source_name, source_type, source_quality_score, limit)

    jobs = parse_json_ld_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if jobs:
        return jobs[:limit]

    jobs = parse_anchor_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if jobs:
        return jobs[:limit]

    return parse_text_jobs(soup, url, source_name, source_type, source_quality_score, limit)


def parse_builtin_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in soup.select('[data-id="job-card"], [data-id=job-card]'):
        if len(jobs) >= limit:
            break
        title_link = first_matching_link(card, lambda href, text: "/job/" in href and looks_like_title(text))
        if not title_link:
            continue
        title = clean_text(title_link.get_text(" ", strip=True))
        job_url = urljoin(page_url, title_link["href"])
        if job_url in seen:
            continue
        seen.add(job_url)
        company_link = first_matching_link(card, lambda href, text: "/company/" in href and bool(text))
        fragments = [clean_text(value) for value in card.stripped_strings if clean_text(value)]
        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                title,
                clean_text(company_link.get_text(" ", strip=True)) if company_link else infer_company(fragments, title),
                infer_location(fragments),
                job_url,
                " ".join(fragments),
                infer_posted(fragments),
            )
        )
    if jobs:
        return jobs[:limit]
    return parse_json_ld_list_items(soup, page_url, source_name, source_type, source_quality_score, limit)


def parse_aijobs_com_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        if len(jobs) >= limit:
            break
        title = clean_text(link.get_text(" ", strip=True))
        href = link["href"]
        if not looks_like_title(title):
            continue
        if not re.match(r"^/jobs/\d", href):
            continue
        if href.startswith("#") or href in {"/jobs", "/jobs/remote", "/registration/job-seeker"}:
            continue
        fragments = following_job_fragments(link, title)
        if not any(looks_like_location(value) or "remote" in value.lower() for value in fragments):
            continue
        job_url = urljoin(page_url, href)
        if job_url in seen:
            continue
        seen.add(job_url)
        company = infer_company([value for value in fragments if value != "Apply" and value != "Closed"], title)
        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                title,
                company,
                infer_location([value for value in fragments if value != title]),
                job_url,
                " ".join(fragments),
                infer_posted(fragments),
            )
        )
    return jobs[:limit]


def parse_ai_jobs_au_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    jobs: list[dict[str, Any]] = []
    seen: set[tuple[str, str | None]] = set()
    for index, line in enumerate(lines):
        if len(jobs) >= limit:
            break
        if line.startswith("Sign up to discover"):
            break
        if not looks_like_title(line):
            continue
        window = lines[index : index + 7]
        company, location = parse_company_location(window[1] if len(window) > 1 else None)
        if not location and len(window) > 3 and window[2] == "•" and looks_like_location(window[3]):
            location = window[3]
        if not company or not location:
            continue
        key = (line.lower(), company.lower())
        if key in seen:
            continue
        description = " ".join(window)
        if "future is hiring" in line.lower() or "Latest AI Jobs" in description:
            continue
        seen.add(key)
        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                line,
                company,
                location or infer_location(window),
                page_url,
                description,
                infer_posted(window),
            )
        )
    return jobs[:limit]


def parse_json_ld_list_items(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        for match in re.finditer(r'"name"\s*:\s*"([^"]+)".{0,300}?"url"\s*:\s*"([^"]+)"', text):
            if len(jobs) >= limit:
                break
            title = clean_text(match.group(1))
            if not looks_like_title(title):
                continue
            job_url = urljoin(page_url, match.group(2).replace("\\/", "/"))
            description = extract_after(text[match.end() : match.end() + 1000], r'"description"\s*:\s*"([^"]+)"')
            jobs.append(job_dict(source_name, source_type, source_quality_score, title, None, "Sydney, Australia", job_url, description or title))
    return jobs


def parse_json_ld_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text()
        for match in re.finditer(r'"title"\s*:\s*"([^"]+)"', text):
            if len(jobs) >= limit:
                break
            title = clean_text(match.group(1))
            company = extract_after(text[match.end() : match.end() + 1000], r'"name"\s*:\s*"([^"]+)"')
            location = extract_after(text[match.end() : match.end() + 2000], r'"addressLocality"\s*:\s*"([^"]+)"')
            jobs.append(job_dict(source_name, source_type, source_quality_score, title, company, location, page_url, text[:1200]))
    return jobs


def parse_anchor_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in soup.find_all("a", href=True):
        title = clean_text(link.get_text(" ", strip=True))
        href = link["href"]
        if len(jobs) >= limit:
            break
        if not looks_like_title(title) or not looks_like_job_href(href):
            continue
        url = urljoin(page_url, href)
        if url in seen:
            continue
        seen.add(url)
        container = nearest_content_container(link)
        fragments = [clean_text(value) for value in container.stripped_strings] if container else [title]
        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                title,
                infer_company(fragments, title),
                infer_location(fragments),
                url,
                " ".join(fragments),
                infer_posted(fragments),
            )
        )
    return jobs


def parse_text_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    lines = [line for line in lines if line]
    jobs: list[dict[str, Any]] = []

    for index, line in enumerate(lines):
        if len(jobs) >= limit:
            break
        if not looks_like_title(line):
            continue
        window = lines[index : index + 8]
        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                line,
                infer_company(window, line),
                infer_location(window),
                page_url,
                " ".join(window),
                infer_posted(window),
            )
        )
    return jobs


def nearest_content_container(link):
    for parent in link.parents:
        if parent.name in {"article", "li", "section"}:
            return parent
        if parent.name == "div" and len(parent.get_text(" ", strip=True)) > 80:
            return parent
    return link.parent


def first_matching_link(container, predicate):
    for link in container.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True))
        href = link["href"]
        if predicate(href, text):
            return link
    return None


def following_job_fragments(link, title: str) -> list[str]:
    fragments = [title]
    for element in link.next_elements:
        if element is link:
            continue
        if getattr(element, "name", None) == "a":
            href = element.get("href", "")
            text = clean_text(element.get_text(" ", strip=True))
            if re.match(r"^/jobs/\d", href) and text != title:
                break
        if isinstance(element, NavigableString):
            text = clean_text(str(element))
            if text and text not in fragments:
                fragments.append(text)
    return fragments


def parse_company_location(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = [clean_text(part) for part in re.split(r"\s*[•|]\s*", value, maxsplit=1)]
    if len(parts) == 2:
        return parts[0] or None, parts[1] or None
    return clean_text(value), None


def job_dict(
    source_name: str,
    source_type: str,
    source_quality_score: float,
    title: str,
    company: str | None,
    location: str | None,
    job_url: str,
    description: str,
    posted_text: str | None = None,
) -> dict[str, Any]:
    return {
        "run_date": date.today().isoformat(),
        "source_name": source_name,
        "source_type": source_type,
        "source_quality_score": source_quality_score,
        "keyword": source_name.lower().replace(" ", "_"),
        "title": clean_text(title),
        "company_name": company,
        "company_logo_url": logo_url_for_company(company),
        "location": location,
        "posted_text": posted_text,
        "description": clean_text(description)[:5000],
        "job_url": job_url,
        "apply_url": job_url,
    }


def looks_like_job_href(href: str) -> bool:
    text = href.lower()
    return any(token in text for token in ("/job", "/jobs", "job=", "apply"))


def looks_like_title(value: str) -> bool:
    if not 6 <= len(value) <= 110:
        return False
    bad_prefixes = ("search", "explore", "browse", "view all", "login", "sign", "save", "learn more", "apply", "latest")
    if value.lower().startswith(bad_prefixes):
        return False
    role_terms = (
        "engineer",
        "developer",
        "scientist",
        "analyst",
        "designer",
        "product",
        "data",
        "machine learning",
        "ai",
        "ml",
        "ops",
        "manager",
        "lead",
        "founding",
        "research",
        "sales",
        "marketing",
    )
    text = value.lower()
    for term in role_terms:
        if term in {"ai", "ml"}:
            if re.search(rf"\b{term}\b", text):
                return True
        elif term in text:
            return True
    return False


def infer_company(lines: list[str], title: str) -> str | None:
    for line in lines:
        if line == title:
            continue
        if 2 <= len(line) <= 60 and not looks_like_location(line) and not infer_posted([line]):
            return line
    return None


def infer_location(lines: list[str]) -> str | None:
    for line in lines:
        if looks_like_location(line):
            return line
    return None


def infer_posted(lines: list[str]) -> str | None:
    for line in lines:
        if re.search(r"\b(today|new|reposted|posted|ago|\d+\s*d|\d+\s+days?|over 30d|6\+\s+months)\b", line, re.I):
            return line[:80]
    return None


def looks_like_location(value: str) -> bool:
    text = value.lower()
    return any(
        term in text
        for term in (
            "australia",
            "sydney",
            "melbourne",
            "brisbane",
            "perth",
            "adelaide",
            "canberra",
            "remote",
            "auckland",
            "wellington",
            "new zealand",
            "worldwide",
            "apac",
        )
    )


def extract_after(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text)
    return clean_text(match.group(1)) if match else None


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
