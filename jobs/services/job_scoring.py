from __future__ import annotations

import re
from datetime import datetime, timezone as dt_timezone
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

AI_TERMS = (
    "artificial intelligence",
    "genai",
    "generative ai",
    "large language model",
    "large language models",
    "machine learning",
    "deep learning",
    "data science",
    "data scientist",
    "data engineer",
    "computer vision",
    "research scientist",
    "research engineer",
    "ai engineer",
    "ml engineer",
    "machine learning engineer",
    "mlops",
    "llm",
    "llms",
    "nlp",
    " ai ",
    " ml ",
)

STARTUP_TERMS = (
    "startup",
    "scaleup",
    "scale-up",
    "venture-backed",
    "venture backed",
    "vc-backed",
    "vc backed",
    "portfolio company",
    "seed",
    "series a",
    "series b",
    "series c",
    "founding",
    "founder",
    "founder-led",
    "early stage",
    "early-stage",
    "high growth",
    "high-growth",
)

AUSTRALIA_TERMS = (
    "australia",
    "sydney",
    "melbourne",
    "brisbane",
    "perth",
    "adelaide",
    "canberra",
    "hobart",
    "darwin",
    "nsw",
    "vic",
    "qld",
    "wa",
    "sa",
    "act",
)

REMOTE_TERMS = (
    "remote",
    "work from home",
    "work-from-home",
    "anywhere",
    "apac",
    "anz",
    "global",
    "worldwide",
)

SOURCE_MULTIPLIERS = {
    "vc_portfolio": 1.0,
    "startup_board": 0.9,
    "remote_board": 0.78,
    "ai_board": 0.84,
    "broad_board": 0.62,
    "aggregator": 0.45,
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def searchable_text(job: dict[str, Any]) -> str:
    return f" {clean_text(job.get('title'))} {clean_text(job.get('company_name'))} {clean_text(job.get('location'))} {clean_text(job.get('description'))} ".lower()


def term_score(text: str, terms: tuple[str, ...]) -> float:
    hits = 0
    for term in terms:
        if len(term.strip()) <= 3:
            if re.search(rf"\b{re.escape(term.strip())}\b", text, re.I):
                hits += 1
        elif term in text:
            hits += 1
    return min(1.0, hits / 3)


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    kept_query = "&".join(
        f"{key}={values[0]}"
        for key, values in sorted(query.items())
        if key.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}
    )
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", kept_query, ""))


def normalize_words(value: str | None) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    stop_words = {
        "senior",
        "sr",
        "lead",
        "principal",
        "remote",
        "hybrid",
        "contract",
        "full",
        "time",
        "we",
        "re",
        "looking",
        "for",
        "a",
        "an",
    }
    return " ".join(word for word in text.split() if word not in stop_words)


def clean_job_title(title: str | None) -> str:
    text = clean_text(title)
    text = re.sub(r"^we['’]?re looking for (an?|the)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s+@\s+.+$", "", text)
    return text[:140]


def dedupe_key(job: dict[str, Any]) -> str:
    canonical_url = normalize_url(job.get("apply_url") or job.get("job_url"))
    title = normalize_words(job.get("title"))
    company = normalize_words(job.get("company_name"))
    location = normalize_words(job.get("location"))
    if canonical_url:
        return f"{title}|{company}|{canonical_url}"
    return f"{title}|{company}|{location}"


def infer_bucket(ai_score: float, startup_score: float, australia_score: float, remote_score: float) -> str | None:
    if australia_score >= 0.35 and ai_score >= 0.35:
        return "australian_ai"
    if australia_score >= 0.35 and startup_score >= 0.35:
        return "australian_startup"
    if remote_score >= 0.35 and ai_score >= 0.35:
        return "remote_ai"
    if remote_score >= 0.35 and startup_score >= 0.35:
        return "remote_startup"
    return None


def recency_score(date_posted: datetime | str | None, posted_text: str | None) -> float:
    text = (posted_text or "").lower()
    if "today" in text or "new" == text.strip():
        return 1.0
    days_match = re.search(r"(\d+)\s*d", text)
    if days_match:
        days = int(days_match.group(1))
        return max(0.0, 1.0 - (days / 7))
    if not date_posted:
        return 0.35
    date_posted = coerce_datetime(date_posted)
    if not date_posted:
        return 0.35
    age_days = max(0, (datetime.now(dt_timezone.utc) - date_posted).days)
    return max(0.0, 1.0 - (age_days / 7))


def parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def coerce_datetime(value: datetime | str | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, str):
        value = parse_datetime(value)
        if not value:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def quality_score(job: dict[str, Any]) -> float:
    score = 0.0
    if clean_text(job.get("title")):
        score += 0.2
    if clean_text(job.get("company_name")):
        score += 0.18
    if clean_text(job.get("location")):
        score += 0.12
    if clean_text(job.get("description")):
        score += min(0.35, len(clean_text(job.get("description"))) / 3000)
    if job.get("job_url"):
        score += 0.15
    return min(1.0, score)


def score_job(job: dict[str, Any]) -> dict[str, Any]:
    text = searchable_text(job)
    location_text = f" {clean_text(job.get('location'))} {clean_text(job.get('country'))} ".lower()
    ai_score = term_score(text, AI_TERMS)
    startup_score = term_score(text, STARTUP_TERMS)
    australia_score = term_score(location_text, AUSTRALIA_TERMS)
    if australia_score > 0:
        australia_score = max(australia_score, 0.7)
    remote_score = term_score(text, REMOTE_TERMS)
    if "remote" in clean_text(job.get("location")).lower():
        remote_score = max(remote_score, 0.75)

    source_type = job.get("source_type") or "broad_board"
    source_score = max(float(job.get("source_quality_score") or 0), SOURCE_MULTIPLIERS.get(source_type, 0.5))
    if source_type in {"vc_portfolio", "startup_board"}:
        startup_score = max(startup_score, 0.7)
    if job.get("company_stage"):
        startup_score = max(startup_score, 0.65)
    if job.get("company_quality_score", 0) >= 0.85:
        startup_score = max(startup_score, 0.55)
    posted_score = recency_score(job.get("date_posted"), job.get("posted_text"))
    complete_score = quality_score(job)
    remote_score = max(remote_score, float(job.get("remote_eligibility_score") or 0))
    bucket = infer_bucket(ai_score, startup_score, australia_score, remote_score)

    ranking_score = (
        ai_score * 0.24
        + startup_score * 0.17
        + australia_score * 0.18
        + remote_score * 0.1
        + posted_score * 0.14
        + source_score * 0.1
        + float(job.get("company_quality_score") or 0) * 0.03
        + complete_score * 0.04
    )

    job.update(
        {
            "ai_score": ai_score,
            "startup_score": startup_score,
            "australia_score": australia_score,
            "remote_score": remote_score,
            "recency_score": posted_score,
            "source_score": source_score,
            "quality_score": complete_score,
            "ranking_score": round(ranking_score, 4),
            "bucket": bucket,
            "dedupe_key": dedupe_key(job),
        }
    )
    return job


def why_selected(job: dict[str, Any]) -> str:
    reasons: list[str] = []
    if job.get("ai_score", 0) >= 0.35:
        reasons.append("strong AI relevance")
    if job.get("startup_score", 0) >= 0.35:
        reasons.append("startup signal")
    if job.get("australia_score", 0) >= 0.35:
        reasons.append("Australia fit")
    if job.get("remote_score", 0) >= 0.35:
        reasons.append("remote-friendly")
    if job.get("recency_score", 0) >= 0.7:
        reasons.append("recent posting")
    if job.get("source_score", 0) >= 0.85:
        reasons.append("high-signal source")
    return ", ".join(reasons[:3]) or "good match for today"
