from __future__ import annotations

from jobs.services.public_pages import collect_simple_jobs
from jobs.services.rendered_jobs import collect_rendered_jobs


def collect_wellfound_jobs(limit: int = 25) -> list[dict]:
    return collect_rendered_jobs(
        url="https://wellfound.com/jobs",
        source_name="Wellfound",
        source_type="startup_board",
        source_quality_score=0.9,
        limit=limit,
    )


def collect_startup_jobs(limit: int = 25) -> list[dict]:
    return collect_rendered_jobs(
        url="https://startup.jobs/?q=AI",
        source_name="Startup.jobs",
        source_type="startup_board",
        source_quality_score=0.86,
        limit=limit,
    )


def collect_yc_jobs(limit: int = 25) -> list[dict]:
    return collect_simple_jobs(
        url="https://www.workatastartup.com/jobs",
        source_name="YC Jobs",
        source_type="startup_board",
        source_quality_score=0.9,
        limit=limit,
    )
