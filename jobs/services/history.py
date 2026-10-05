"""Bounded query projection shared by the JSON and HTML Jobs history."""

from django.db.models import Prefetch

from jobs.models import JobListing, JobRun, SourceRunLog


def recent_job_runs(limit):
    return (
        JobRun.objects.filter(run_date__regex=r"^\d{4}-\d{2}-\d{2}$")
        .order_by("-run_date", "-created_at")
        .prefetch_related(
            Prefetch(
                "source_logs",
                queryset=SourceRunLog.objects.filter(status="error"),
                to_attr="history_source_errors",
            ),
            Prefetch(
                "jobs",
                queryset=JobListing.objects.filter(is_top_pick=True).order_by("rank"),
                to_attr="history_top_jobs",
            ),
        )[:limit]
    )
