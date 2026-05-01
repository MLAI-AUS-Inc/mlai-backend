from __future__ import annotations

from typing import Any


def build_job_summary(job: dict[str, Any]) -> str:
    parts: list[str] = []
    bucket = str(job.get("bucket") or "").replace("_", " ")
    stage = job.get("company_stage")
    size = job.get("company_size")

    if job.get("ai_score", 0) >= 0.35:
        parts.append("AI-relevant role")
    if job.get("startup_score", 0) >= 0.55:
        if stage:
            parts.append(f"{stage} startup signal")
        else:
            parts.append("strong startup signal")
    if job.get("australia_score", 0) >= 0.35:
        parts.append("Australia fit")
    if job.get("remote_eligibility") == "australia_eligible":
        parts.append("remote open to Australia/APAC/global")
    elif job.get("remote_score", 0) >= 0.35:
        parts.append("remote-friendly")
    if job.get("recency_score", 0) >= 0.7:
        parts.append("recent posting")
    if size:
        parts.append(f"{size} company size signal")
    if job.get("source_score", 0) >= 0.85:
        parts.append("high-signal source")

    if not parts and bucket:
        parts.append(f"matched {bucket}")

    return ", ".join(parts[:4]) or "good match for today"
