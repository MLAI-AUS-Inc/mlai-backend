from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from jobs.conf import settings
from jobs.services.adzuna import collect_adzuna_jobs
from jobs.services.careerone import collect_careerone_jobs
from jobs.services.getro import collect_getro_jobs
from jobs.services.himalayas import collect_himalayas_jobs
from jobs.services.indeed import collect_indeed_jobs
from jobs.services.jobs_playwright import collect_jobs_for_keyword
from jobs.services.public_pages import collect_simple_jobs
from jobs.services.startup_boards import collect_startup_jobs, collect_wellfound_jobs, collect_yc_jobs
from jobs.services.workforce import collect_workforce_jobs

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")


@dataclass(frozen=True)
class SourceConfig:
    name: str
    source_type: str
    quality_score: float
    enabled: bool
    keywords: tuple[str, ...]


PHASE_1_SOURCES: tuple[SourceConfig, ...] = (
    SourceConfig(
        name="SEEK",
        source_type="broad_board",
        quality_score=0.72,
        enabled=True,
        keywords=(
            "machine learning engineer",
            "AI engineer",
            "data scientist",
            "MLOps engineer",
            "startup product manager",
            "startup software engineer",
            "remote AI engineer",
            "remote machine learning",
        ),
    ),
    # Broad boards that need explicit permitted API/feed paths before enabling.
    SourceConfig("LinkedIn Jobs", "broad_board", 0.78, False, ("AI", "machine learning", "startup")),
    SourceConfig("Indeed Australia", "broad_board", 0.7, False, ("AI", "machine learning", "startup")),
    SourceConfig("Wellfound", "startup_board", 0.9, False, ("AI", "machine learning", "remote startup")),
    SourceConfig("Startup.jobs", "startup_board", 0.86, True, ("AI", "software engineer", "product")),
    SourceConfig("TopStartups.io", "startup_board", 0.78, False, ("AI", "machine learning", "Australia")),
    SourceConfig("Built In Melbourne", "startup_board", 0.78, True, ("AI", "data", "software")),
    SourceConfig("Built In Sydney", "startup_board", 0.78, False, ("AI", "data", "software")),
    SourceConfig("Airtree Jobs", "vc_portfolio", 0.95, True, ("AI", "data", "software")),
    SourceConfig("Blackbird Jobs", "vc_portfolio", 0.95, True, ("AI", "data", "software")),
    SourceConfig("Antler Jobs", "vc_portfolio", 0.84, True, ("AI", "product", "software")),
    SourceConfig("Innovation Bay Jobs", "vc_portfolio", 0.82, True, ("AI", "product", "software")),
    SourceConfig("Main Sequence Jobs", "vc_portfolio", 0.88, False, ("AI", "deep tech", "software")),
    SourceConfig("AI Jobs Australia", "ai_board", 0.86, True, ("AI", "machine learning", "data")),
    SourceConfig("ai-jobs.com.au", "ai_board", 0.86, False, ("AI", "machine learning", "data")),
    SourceConfig("Company Brew", "startup_board", 0.76, True, ("AI", "startup", "software")),
    SourceConfig("Matchstiq", "startup_board", 0.76, False, ("AI", "startup", "software")),
    SourceConfig("LaunchVic", "startup_board", 0.74, False, ("startup", "AI", "software")),
    SourceConfig("Himalayas", "remote_board", 0.82, True, ("AI", "machine learning", "startup")),
    # Tier 2 expansion sources are registered but disabled until we add an
    # allowed API/feed path or custom connector.
    SourceConfig("Jora", "broad_board", 0.58, False, ("AI", "startup", "machine learning")),
    SourceConfig("Adzuna", "broad_board_api", 0.66, False, ("AI", "startup", "machine learning")),
    SourceConfig("CareerOne", "broad_board", 0.56, True, ("AI", "startup", "machine learning")),
    SourceConfig("Glassdoor", "broad_board", 0.5, False, ("AI", "startup", "machine learning")),
    SourceConfig("Workforce Australia", "government_board", 0.62, True, ("AI", "startup", "machine learning")),
    SourceConfig("YC Jobs", "startup_board", 0.9, False, ("AI", "remote", "startup")),
    SourceConfig("Matchbox", "startup_board", 0.7, False, ("AI", "startup", "software")),
    SourceConfig("Startup Galaxy", "startup_board", 0.68, False, ("AI", "startup", "software")),
    SourceConfig("Recruiter AI Niche Boards", "ai_board", 0.6, False, ("AI", "machine learning", "data")),
)

GETRO_BOARDS = {
    "Airtree Jobs": "https://jobs.airtree.vc",
    "Blackbird Jobs": "https://jobs.blackbird.vc",
    "Antler Jobs": "https://careers.antler.co",
    "Innovation Bay Jobs": "https://jobs.innovationbay.com",
}

PUBLIC_PAGE_SOURCES = {
    "TopStartups.io": "https://topstartups.io/jobs/",
    "Built In Melbourne": "https://www.builtinmelbourne.com/jobs",
    "Built In Sydney": "https://www.builtinsydney.com/jobs",
    "AI Jobs Australia": "https://www.aijobs.com/jobs/in-australia",
    "ai-jobs.com.au": "https://ai-jobs.com.au/",
    "Company Brew": "https://companybrew.com/jobs",
    "Matchstiq": "https://matchstiq.io/jobs",
    "LaunchVic": "https://launchvic.org/about/jobs/",
}


def melbourne_today() -> str:
    return datetime.now(MELBOURNE_TZ).date().isoformat()


def date_posted_from_days(days_ago: int | None) -> datetime | None:
    if days_ago is None:
        return None
    return datetime.now(MELBOURNE_TZ) - timedelta(days=days_ago)


def collect_from_source(
    source: SourceConfig,
    max_pages: int | None = None,
    per_keyword_limit: int | None = None,
) -> list[dict[str, Any]]:
    if source.name == "Wellfound":
        return collect_wellfound_jobs(limit=per_keyword_limit or 25)

    if source.name == "Startup.jobs":
        return collect_startup_jobs(limit=per_keyword_limit or 25)

    if source.name == "YC Jobs":
        return collect_yc_jobs(limit=per_keyword_limit or 25)

    if source.name == "Adzuna":
        return collect_adzuna_jobs(per_query_limit=per_keyword_limit or 10)

    if source.name == "Indeed Australia":
        return collect_indeed_jobs(per_query_limit=per_keyword_limit or 10)

    if source.name == "CareerOne":
        return collect_careerone_jobs(per_query_limit=per_keyword_limit or 10)

    if source.name == "Workforce Australia":
        return collect_workforce_jobs(per_query_limit=per_keyword_limit or 10)

    if source.name in GETRO_BOARDS:
        return collect_getro_jobs(
            base_url=GETRO_BOARDS[source.name],
            source_name=source.name,
            source_type=source.source_type,
            source_quality_score=source.quality_score,
            limit=per_keyword_limit or 40,
        )

    if source.name == "Himalayas":
        return collect_himalayas_jobs(per_query_limit=per_keyword_limit or 20)

    if source.name in PUBLIC_PAGE_SOURCES:
        return collect_simple_jobs(
            url=PUBLIC_PAGE_SOURCES[source.name],
            source_name=source.name,
            source_type=source.source_type,
            source_quality_score=source.quality_score,
            limit=per_keyword_limit or 40,
        )

    if source.name != "SEEK":
        return []

    results: list[dict[str, Any]] = []
    for keyword in source.keywords:
        jobs = collect_jobs_for_keyword(
            keyword=keyword,
            max_pages=max_pages or settings.jobs_seek_max_pages,
            per_keyword_limit=per_keyword_limit or settings.jobs_seek_per_keyword_limit,
            headless=settings.jobs_scrape_headless,
        )
        for job in jobs:
            job["source_type"] = source.source_type
            job["source_quality_score"] = source.quality_score
        results.extend(jobs)
    return results
