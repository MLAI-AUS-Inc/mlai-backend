from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobs.conf import settings
from jobs.services.logos import logo_url_for_company
from jobs.services.public_pages import infer_company, infer_location, infer_posted, looks_like_title


class RenderedSourceBlockedError(RuntimeError):
    pass


def collect_rendered_jobs(
    url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int = 25,
    wait_ms: int = 5000,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RenderedSourceBlockedError("Playwright is not installed for rendered source connector") from exc

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=settings.jobs_scrape_headless)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                page.wait_for_timeout(wait_ms)
                html = page.content()
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        raise RenderedSourceBlockedError(
            "Playwright failed while scraping a rendered jobs source. "
            "Ensure Chromium is installed and the runtime can launch it."
        ) from exc

    return parse_rendered_jobs(
        html=html,
        page_url=url,
        source_name=source_name,
        source_type=source_type,
        source_quality_score=source_quality_score,
        limit=limit,
    )


def parse_rendered_jobs(
    html: str,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    if is_blocked_html(html, soup):
        raise RenderedSourceBlockedError("Rendered source is blocked by CAPTCHA/anti-bot page")

    return extract_rendered_jobs_from_soup(soup, page_url, source_name, source_type, source_quality_score, limit)


def extract_rendered_jobs_from_soup(
    soup: BeautifulSoup,
    page_url: str,
    source_name: str,
    source_type: str,
    source_quality_score: float,
    limit: int,
) -> list[dict[str, Any]]:
    page_text = soup.get_text(" ", strip=True).lower()
    if any(signal in page_text for signal in ("access denied", "forbidden", "verify you are human", "captcha")):
        raise RenderedSourceBlockedError("Rendered source is blocked by CAPTCHA/anti-bot page")
    jobs: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for link in soup.find_all("a", href=True):
        if len(jobs) >= limit:
            break

        href = link["href"]
        title = clean_title(link.get_text(" ", strip=True))
        if not looks_like_job_link(href, page_url) or not looks_like_title(title):
            continue

        job_url = urljoin(page_url, href)
        if job_url in seen_urls:
            continue
        seen_urls.add(job_url)

        container = nearest_container(link)
        fragments = [clean_text(value) for value in container.stripped_strings] if container else [title]
        fragments = [value for value in fragments if value]
        company = infer_company(fragments, title)
        if source_name == "Startup.jobs":
            company = company_from_startup_jobs_href(href, title) or company

        jobs.append(
            {
                "run_date": date.today().isoformat(),
                "source_name": source_name,
                "source_type": source_type,
                "source_quality_score": source_quality_score,
                "keyword": source_name.lower().replace(" ", "_"),
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


def is_blocked_html(html: str, soup: BeautifulSoup) -> bool:
    lowered = html.lower()
    page_text = soup.get_text(" ", strip=True).lower()
    signals = (
        "captcha-delivery.com",
        "datadome",
        "verify you are human",
        "access denied",
        "forbidden",
        "captcha",
    )
    return any(signal in lowered or signal in page_text for signal in signals)


def looks_like_job_link(href: str, page_url: str) -> bool:
    text = href.lower()
    host_hint = page_url.lower()
    if "wellfound.com" in host_hint:
        return "/jobs/" in text or "/company/" in text
    if "startup.jobs" in host_hint:
        excluded_prefixes = (
            "/roles/",
            "/locations/",
            "/company/",
            "/companies/",
            "/tags",
            "/collections",
            "/employers",
            "/students",
            "/salaries",
            "/trends",
            "/job-boards",
            "/remote-jobs",
            "/part-time-jobs",
            "/internships",
        )
        return text.startswith("/") and not text.startswith(excluded_prefixes) and bool(re.search(r"-\d{5,}", text))
    return "/jobs/" in text or "/job/" in text


def nearest_container(link):
    for parent in link.parents:
        if parent.name in {"article", "li", "section"}:
            return parent
        if parent.name == "div" and len(parent.get_text(" ", strip=True)) > 90:
            return parent
    return link.parent


def clean_title(value: str | None) -> str:
    text = clean_text(value)
    text = re.sub(r"\s+at\s+.+$", "", text, flags=re.I)
    return text[:140]


def company_from_startup_jobs_href(href: str, title: str) -> str | None:
    slug = href.strip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    title_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if title_slug and slug.startswith(title_slug + "-"):
        company_slug = slug[len(title_slug) + 1 :]
    else:
        parts = slug.split("-")
        company_slug = "-".join(parts[-2:]) if len(parts) >= 2 else ""
    if not company_slug:
        return None
    return " ".join(word.capitalize() for word in company_slug.split("-") if word)


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()
