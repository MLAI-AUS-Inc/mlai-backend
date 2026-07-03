from __future__ import annotations

import re
from typing import Any

from jobs.services.logos import domain_for_company

STAGE_TERMS = (
    ("pre-seed", "Pre-seed"),
    ("seed", "Seed"),
    ("series a", "Series A"),
    ("series b", "Series B"),
    ("series c", "Series C+"),
    ("venture-backed", "Venture-backed"),
    ("venture backed", "Venture-backed"),
    ("scaleup", "Scaleup"),
    ("scale-up", "Scaleup"),
)

SIZE_PATTERNS = (
    (r"\b1[-\s]?10\b|\bunder 10\b", "1-10"),
    (r"\b11[-\s]?50\b", "11-50"),
    (r"\b51[-\s]?200\b", "51-200"),
    (r"\b201[-\s]?500\b", "201-500"),
    (r"\b501[-\s]?1000\b", "501-1000"),
    (r"\b1000\+\b|\b1001[-\s]?5000\b", "1000+"),
)

KNOWN_COMPANY_METADATA = {
    "airwallex": {"stage": "Growth", "size": "1000+", "quality": 0.95},
    "canva": {"stage": "Growth", "size": "1000+", "quality": 0.98},
    "deputy": {"stage": "Growth", "size": "201-500", "quality": 0.9},
    "go1": {"stage": "Growth", "size": "201-500", "quality": 0.88},
    "hatch": {"stage": "Growth", "size": "51-200", "quality": 0.86},
    "linktree": {"stage": "Growth", "size": "201-500", "quality": 0.9},
    "procurepro": {"stage": "Seed", "size": "11-50", "quality": 0.84},
    "secure code warrior": {"stage": "Growth", "size": "201-500", "quality": 0.9},
    "xero": {"stage": "Public/Scale", "size": "1000+", "quality": 0.88},
}


def enrich_company_metadata(job: dict[str, Any]) -> dict[str, Any]:
    company = normalize_company(job.get("company_name"))
    text = searchable_text(job)
    known = KNOWN_COMPANY_METADATA.get(company, {})

    stage = job.get("company_stage") or known.get("stage") or infer_stage(text)
    size = job.get("company_size") or known.get("size") or infer_size(text)
    domain = job.get("company_domain") or domain_for_company(job.get("company_name"))
    quality = float(job.get("company_quality_score") or known.get("quality") or 0.0)

    if job.get("source_type") in {"vc_portfolio", "startup_board"}:
        quality = max(quality, 0.82)
    if stage:
        quality = max(quality, 0.78)

    job.update(
        {
            "company_domain": domain,
            "company_stage": stage,
            "company_size": size,
            "company_quality_score": round(quality, 3),
        }
    )
    return job


def infer_stage(text: str) -> str | None:
    for needle, label in STAGE_TERMS:
        if needle in text:
            return label
    return None


def infer_size(text: str) -> str | None:
    for pattern, label in SIZE_PATTERNS:
        if re.search(pattern, text):
            return label
    return None


def searchable_text(job: dict[str, Any]) -> str:
    values = [
        job.get("title"),
        job.get("company_name"),
        job.get("location"),
        job.get("description"),
        job.get("source_name"),
        job.get("source_type"),
    ]
    return " ".join(str(value or "") for value in values).lower()


def normalize_company(company: str | None) -> str:
    return re.sub(r"\s+", " ", (company or "").strip().lower())
