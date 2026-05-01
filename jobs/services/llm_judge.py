from __future__ import annotations

import json
from typing import Any

import requests

from jobs.conf import settings
from jobs.models import JobListing


def judge_top_candidates(candidates: list[JobListing], candidate_limit: int = 10) -> tuple[list[JobListing], dict[int, str]]:
    if not settings.llm_judge_enabled or not settings.llm_judge_api_key:
        return candidates, {}

    top_candidates = candidates[:candidate_limit]
    if len(top_candidates) < 2:
        return candidates, {}

    try:
        payload = build_request_payload(top_candidates)
        response = requests.post(
            f"{settings.llm_judge_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.llm_judge_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        judged = parse_judgement(content, top_candidates)
        if not judged:
            return candidates, {}

        ordered_ids, reasons = judged
        by_id = {job.id: job for job in candidates}
        ordered = [by_id[job_id] for job_id in ordered_ids if job_id in by_id]
        remaining = [job for job in candidates if job.id not in ordered_ids]
        return ordered + remaining, reasons
    except Exception:
        return candidates, {}


def build_request_payload(candidates: list[JobListing]) -> dict[str, Any]:
    return {
        "model": settings.llm_judge_model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You rank AI and startup jobs for an Australian Slack community. "
                    "Prefer strong AI relevance, Australian or Australia-eligible remote fit, startup/venture signal, "
                    "recent posts, complete descriptions, direct apply links, and variety across companies/sources. "
                    "Avoid generic, stale, low-information, or duplicate-looking roles. "
                    "Return only valid JSON."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "task": "Rank these 10 candidate jobs from best to weakest for today's top jobs list.",
                        "output_schema": {
                            "ranked_ids": ["job database id in best-to-weakest order"],
                            "reasons": {"job database id": "short reason under 90 characters"},
                        },
                        "jobs": [serialize_job(job) for job in candidates],
                    },
                    ensure_ascii=True,
                ),
            },
        ],
    }


def serialize_job(job: JobListing) -> dict[str, Any]:
    return {
        "id": job.id,
        "title": job.title,
        "company": job.company_name,
        "location": job.location,
        "source": job.source_name,
        "bucket": job.bucket,
        "ranking_score": job.ranking_score,
        "ai_score": job.ai_score,
        "startup_score": job.startup_score,
        "australia_score": job.australia_score,
        "remote_score": job.remote_score,
        "recency_score": job.recency_score,
        "summary": job.summary or job.why_selected,
        "description": (job.description or "")[:900],
    }


def parse_judgement(content: str, candidates: list[JobListing]) -> tuple[list[int], dict[int, str]] | None:
    data = json.loads(strip_code_fence(content))
    raw_ids = data.get("ranked_ids")
    if not isinstance(raw_ids, list):
        return None

    valid_ids = {job.id for job in candidates}
    ordered_ids: list[int] = []
    for value in raw_ids:
        try:
            job_id = int(value)
        except (TypeError, ValueError):
            continue
        if job_id in valid_ids and job_id not in ordered_ids:
            ordered_ids.append(job_id)

    if not ordered_ids:
        return None

    raw_reasons = data.get("reasons") if isinstance(data.get("reasons"), dict) else {}
    reasons: dict[int, str] = {}
    for key, value in raw_reasons.items():
        try:
            job_id = int(key)
        except (TypeError, ValueError):
            continue
        if job_id in valid_ids and isinstance(value, str):
            reasons[job_id] = value[:140]

    return ordered_ids, reasons


def strip_code_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text
