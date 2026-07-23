from __future__ import annotations

import ipaddress
import re
import socket
from datetime import date
from typing import Any
from urllib.parse import urljoin, urlsplit

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
EXTERNAL_FETCH_TIMEOUT = (5, 10)
EXTERNAL_FETCH_MAX_BYTES = 512 * 1024
EXTERNAL_FETCH_MAX_REDIRECTS = 3


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
    current_url = url
    try:
        for redirect_count in range(EXTERNAL_FETCH_MAX_REDIRECTS + 1):
            if not _is_safe_external_url(current_url):
                return None

            response = requests.get(
                current_url,
                headers=HTML_HEADERS,
                timeout=EXTERNAL_FETCH_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or redirect_count == EXTERNAL_FETCH_MAX_REDIRECTS:
                        return None
                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                html = _read_limited_response_text(response)
                if html is None:
                    return None
                return extract_company_from_job_posting_page(html)
            finally:
                response.close()
    except (requests.RequestException, OSError, ValueError):
        return None
    return None


def _is_safe_external_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        return False

    try:
        addresses = {ipaddress.ip_address(hostname)}
    except ValueError:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = {
            ipaddress.ip_address(sockaddr[0])
            for _family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }

    return bool(addresses) and all(address.is_global for address in addresses)


def _read_limited_response_text(response: requests.Response) -> str | None:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > EXTERNAL_FETCH_MAX_BYTES:
                return None
        except ValueError:
            return None

    body = bytearray()
    for chunk in response.iter_content(chunk_size=16 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > EXTERNAL_FETCH_MAX_BYTES:
            return None
    return body.decode(response.encoding or "utf-8", errors="replace")


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
