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

    if source_name == "TopStartups.io":
        return parse_topstartups_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if source_name.startswith("Built In"):
        return parse_builtin_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if source_name == "AI Jobs Australia":
        return parse_aijobs_com_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if source_name == "ai-jobs.com.au":
        return parse_ai_jobs_au_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if source_name == "Matchstiq":
        return parse_matchstiq_jobs(soup, url, source_name, source_type, source_quality_score, limit)

    jobs = parse_json_ld_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if jobs:
        return jobs[:limit]

    jobs = parse_anchor_jobs(soup, url, source_name, source_type, source_quality_score, limit)
    if jobs:
        return jobs[:limit]

    return parse_text_jobs(soup, url, source_name, source_type, source_quality_score, limit)


def parse_topstartups_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    lines = visible_lines(soup)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        if len(jobs) >= limit:
            break
        if not looks_like_title(line):
            continue
        if index == 0 or not looks_like_company(lines[index - 1]):
            continue

        window = lines[index : index + 18]
        location = infer_location(window)
        posted = infer_posted(window)
        apply_url = find_nearby_link(soup, line, page_url)
        if not location or not posted or not apply_url:
            continue

        company = lines[index - 1]
        key = f"{line.lower()}|{company.lower()}|{apply_url}"
        if key in seen:
            continue
        seen.add(key)

        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                line,
                company,
                location,
                apply_url,
                " ".join(window),
                posted,
            )
        )

    return jobs[:limit]


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


def parse_matchstiq_jobs(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    lines = visible_lines(soup)
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, line in enumerate(lines):
        if len(jobs) >= limit:
            break
        title = clean_text(line)
        if not looks_like_title(title):
            continue
        if looks_like_location(title) or infer_posted([title]):
            continue

        window = lines[index + 1 : index + 7]
        company = first_company_after_title(window)
        location = infer_matchstiq_location(window)
        posted = infer_posted(window)
        if not company or not posted:
            continue

        if company.lower() in {"jobs", "companies", "post job"}:
            continue

        job_url = find_nearby_link(soup, title, page_url) or stable_listing_url(page_url, title, company)
        key = f"{title.lower()}|{company.lower()}|{location or ''}"
        if key in seen:
            continue
        seen.add(key)

        jobs.append(
            job_dict(
                source_name,
                source_type,
                source_quality_score,
                title,
                company,
                location,
                job_url,
                " ".join(window),
                posted,
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


def visible_lines(soup: BeautifulSoup) -> list[str]:
    lines = [clean_text(line) for line in soup.get_text("\n", strip=True).splitlines()]
    return [line for line in lines if line]


def looks_like_company(value: str) -> bool:
    text = clean_text(value)
    if not 2 <= len(text) <= 80:
        return False
    if len(text) > 45 or text.endswith("."):
        return False
    if looks_like_location(text) or infer_posted([text]):
        return False
    blocked = {"apply", "apply now", "learn more", "quick facts", "take action", "view job", "what they do"}
    return text.lower().strip(":") not in blocked


def find_nearby_link(soup: BeautifulSoup, title: str, page_url: str) -> str | None:
    title_link = None
    for link in soup.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True))
        if text == title:
            title_link = link
            break
    if title_link:
        href = title_link["href"]
        if href and href != "#":
            return urljoin(page_url, href)

    for link in soup.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True)).lower()
        href = link["href"]
        if text == "apply" and href and href != "#":
            return urljoin(page_url, href)
    for link in soup.find_all("a", href=True):
        text = clean_text(link.get_text(" ", strip=True)).lower()
        href = link["href"]
        if text in {"view job", "read more"} and looks_like_job_href(href):
            return urljoin(page_url, href)
    return None


def first_company_after_title(fragments: list[str]) -> str | None:
    for value in fragments:
        if looks_like_company(value):
            return value
    return None


def infer_matchstiq_location(fragments: list[str]) -> str | None:
    for value in fragments[1:]:
        text = clean_text(value)
        if len(text) > 80 or text.endswith("..."):
            continue
        if looks_like_location(text):
            return text
    return infer_location(fragments)


def stable_listing_url(page_url: str, title: str, company: str | None) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company or 'company'}-{title}".lower()).strip("-")
    return f"{page_url.rstrip('/')}?job={slug}"


def parse_company_location(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    parts = [clean_text(part) for part in re.split(r"\s*(?:•|\|)\s*", value, maxsplit=1)]
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
        "title": clean_scraped_title(title, company),
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
        "coordinator",
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


JOB_TITLE_TRAILING_BOILERPLATE = re.compile(
    r"\s+(?:Full[- ]?Time|Part[- ]?Time|Contract|Casual|Internship|Freelance)?\s*Job\s+"
    r"(?:Remote\s+in\s+[A-Za-z]+|in\s+[A-Za-z ,]+)$",
    re.I,
)


def clean_scraped_title(title: str, company: str | None) -> str:
    # Some boards (e.g. Company Brew) render an anchor's whole text as
    # "{company} {title} Full Time Job Remote in {region}" - strip the company
    # prefix and the trailing employment-type/region boilerplate so the stored
    # title is just the role name, not the source page's own layout text.
    text = clean_text(title)
    normalized_company = clean_text(company)
    if normalized_company and text.lower().startswith(normalized_company.lower() + " "):
        text = text[len(normalized_company):].strip()
    return JOB_TITLE_TRAILING_BOILERPLATE.sub("", text).strip()
