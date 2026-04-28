from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

KNOWN_COMPANY_DOMAINS = {
    "airwallex": "airwallex.com",
    "canva": "canva.com",
    "canonical": "canonical.com",
    "hatch": "hatch.team",
    "interview kickstart": "interviewkickstart.com",
    "linktree": "linktr.ee",
    "micromine australia pty ltd": "micromine.com",
    "motorola solutions": "motorolasolutions.com",
    "secure code warrior": "securecodewarrior.com",
    "tether operations limited": "tether.to",
    "xero": "xero.com",
}


def logo_url_for_company(company_name: str | None, existing_url: str | None = None) -> str | None:
    if existing_url:
        return existing_url
    domain = domain_for_company(company_name)
    if not domain:
        return None
    return f"https://logo.clearbit.com/{domain}"


def domain_for_company(company_name: str | None) -> str | None:
    normalized = normalize_company(company_name)
    if not normalized:
        return None
    if normalized in KNOWN_COMPANY_DOMAINS:
        return KNOWN_COMPANY_DOMAINS[normalized]
    compact = re.sub(r"\b(pty|ltd|limited|inc|llc|plc|co|company|group|australia)\b", "", normalized)
    compact = re.sub(r"[^a-z0-9]+", "", compact)
    if len(compact) < 3:
        return None
    return f"{compact}.com"


def normalize_company(company_name: str | None) -> str:
    return re.sub(r"\s+", " ", (company_name or "").strip().lower())


def absolute_image_url(src: str | None, base_url: str) -> str | None:
    if not src or src.startswith("data:"):
        return None
    return urljoin(base_url, src)


def hostname_from_url(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
