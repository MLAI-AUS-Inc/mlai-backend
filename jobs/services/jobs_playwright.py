from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import quote_plus

from jobs.services.logos import absolute_image_url, logo_url_for_company

BASE_URL = "https://www.seek.com.au"


def build_search_url(keyword: str, page: int = 1) -> str:
    slug = quote_plus(keyword).replace("+", "-")
    if page <= 1:
        return f"{BASE_URL}/{slug}-jobs"
    return f"{BASE_URL}/{slug}-jobs?page={page}"


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def parse_posted_days(posted_text: str) -> int | None:
    text = (posted_text or "").lower().strip()

    if "today" in text or text == "new":
        return 0

    match = re.search(r"(\d+)\s*d", text)
    if match:
        return int(match.group(1))

    return None


def extract_card_links(page) -> list[str]:
    selectors = [
        'article[data-automation="normalJob"] a[data-automation="job-list-item-link-overlay"]',
        'article[data-automation="premiumJob"] a[data-automation="job-list-item-link-overlay"]',
    ]

    hrefs: list[str] = []
    seen: set[str] = set()

    for selector in selectors:
        links = page.locator(selector)
        count = links.count()

        for i in range(count):
            href = links.nth(i).get_attribute("href")
            if not href:
                continue

            url = href if href.startswith("http") else f"{BASE_URL}{href}"
            if url in seen:
                continue

            seen.add(url)
            hrefs.append(url)

    return hrefs


def extract_text(page, selectors: list[str]) -> str:
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() > 0:
            text = clean_text(locator.inner_text())
            if text:
                return text
    return ""


def extract_description(page) -> str:
    selectors = [
        '[data-automation="jobAdDetails"]',
        '[data-automation="jobDescription"]',
        'div[data-automation="job-detail-page"]',
        "main",
    ]

    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count() > 0:
            text = clean_text(locator.inner_text())
            if text and len(text) > 80:
                return text[:5000]

    body_text = clean_text(page.locator("body").inner_text())
    return body_text[:5000]


def extract_company_logo_url(page, company_name: str | None) -> str | None:
    selectors = [
        '[data-automation*="company"] img',
        '[data-automation*="advertiser"] img',
        'img[alt*="logo" i]',
        "img",
    ]
    company_text = (company_name or "").lower()
    for selector in selectors:
        images = page.locator(selector)
        for index in range(min(images.count(), 20)):
            image = images.nth(index)
            alt = (image.get_attribute("alt") or "").lower()
            src = image.get_attribute("src")
            if not src:
                continue
            if "logo" in alt or (company_text and company_text in alt):
                return absolute_image_url(src, BASE_URL)
    return logo_url_for_company(company_name)


def extract_job_detail(detail_page, job_url: str, keyword: str) -> dict[str, Any] | None:
    title = extract_text(
        detail_page,
        [
            '[data-automation="job-detail-title"]',
            '[data-automation="jobTitle"]',
            "h1",
        ],
    )

    company = extract_text(
        detail_page,
        [
            '[data-automation="advertiser-name"]',
            '[data-automation="jobCompany"]',
        ],
    )

    location = extract_text(
        detail_page,
        [
            '[data-automation="job-detail-location"]',
            '[data-automation="jobLocation"]',
        ],
    )

    work_type = extract_text(
        detail_page,
        [
            '[data-automation="job-detail-work-type"]',
        ],
    )

    salary = extract_text(
        detail_page,
        [
            '[data-automation="job-detail-salary"]',
        ],
    )

    posted_text = extract_text(
        detail_page,
        [
            '[data-automation="job-detail-date"]',
            '[data-automation="jobListingDate"]',
        ],
    )

    if not posted_text:
        spans = detail_page.locator("span")
        for i in range(spans.count()):
            text = clean_text(spans.nth(i).inner_text())
            if re.search(r"(posted\s+(today|\d+d ago))|(today)|(\d+d ago)", text, re.I):
                posted_text = text
                break

    description = extract_description(detail_page)
    company_logo_url = extract_company_logo_url(detail_page, company)

    if not title:
        return None

    return {
        "run_date": date.today().isoformat(),
        "source_name": "SEEK",
        "keyword": keyword,
        "title": title,
        "company_name": company,
        "company_logo_url": company_logo_url,
        "location": location,
        "salary": salary,
        "work_type": work_type,
        "posted_text": posted_text,
        "posted_days_ago": parse_posted_days(posted_text),
        "description": description,
        "job_url": job_url,
    }


def collect_jobs_for_keyword(
    keyword: str,
    max_pages: int = 3,
    per_keyword_limit: int = 20,
    headless: bool = False,
) -> list[dict[str, Any]]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError("Playwright is not installed for the SEEK connector") from exc
    except Exception as exc:
        raise RuntimeError(
            "Playwright could not be initialized for the SEEK connector. "
            "Ensure browser binaries are installed with `python -m playwright install chromium`."
        ) from exc

    results: list[dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            detail_page = context.new_page()

            try:
                for page_num in range(1, max_pages + 1):
                    search_url = build_search_url(keyword, page_num)
                    page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)

                    try:
                        page.wait_for_selector(
                            'article[data-automation="normalJob"], article[data-automation="premiumJob"]',
                            timeout=10000,
                        )
                    except PlaywrightTimeoutError:
                        continue

                    job_links = extract_card_links(page)

                    for job_url in job_links:
                        if len(results) >= per_keyword_limit:
                            return results

                        detail_page.goto(job_url, wait_until="domcontentloaded", timeout=30000)
                        detail_page.wait_for_timeout(4000)

                        job = extract_job_detail(detail_page, job_url, keyword)
                        if job:
                            results.append(job)

                return results

            finally:
                context.close()
                browser.close()
    except Exception as exc:
        raise RuntimeError(
            "Playwright failed while scraping SEEK. "
            "Ensure Chromium is installed and the runtime can launch it."
        ) from exc
