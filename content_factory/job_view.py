"""Read a Content Factory Job's backend facts from its run.

Robustness Phase 3 (R7 job/run merge), step 5.3. :func:`job_view` returns a
small read-only object with ContentFactoryJob's attribute names, filled from
the run columns that ``job_mirror`` and ``backfill_run_job_fields`` keep.
Its ``status`` is the run's ``delivery_phase``, never the CF-owned run status.

Readers switch through :func:`read_job_facts`:

- both flags off: the Job read only, exactly as before;
- ARTICLES_RUN_JOB_SHADOW_READ_ENABLED: the Job read is served, the run read
  runs beside it, and a mismatch is logged with field names and counts only;
- ARTICLES_RUN_JOB_READ_ENABLED: the run read is served.

Rollback is turning the flag off. Writes still go to the Job until run-only
writes (5.4); a view refuses ``save()``.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger(__name__)

# Job attributes a view carries. created_at and updated_at are the run's own
# (the approved schema has no copy of the Job's), so parity checks skip them.
JOB_VIEW_FIELDS = (
    "job_id", "slack_user_id", "domain", "status", "request_meta", "client_request_id",
    "billing_source_job_id", "billing_amount", "billing_status", "billing_ledger_id",
    "selected_keyword", "selection_reason", "selection_data", "slack_channel_id",
    "slack_root_message_ts", "slack_thread_ts", "progress_message_ts", "posted_progress_ids",
    "last_progress_milestone_index", "last_progress_milestone_key", "last_progress_updated_at",
    "still_working_pinged_at", "article_url", "pr_url", "error_message", "created_at", "updated_at",
)
TIMESTAMP_FIELDS = frozenset({"created_at", "updated_at"})
COMPARED_FIELDS = tuple(field for field in JOB_VIEW_FIELDS if field not in TIMESTAMP_FIELDS)


def shadow_read_enabled() -> bool:
    """True when Job reads are compared with run reads (ARTICLES_RUN_JOB_SHADOW_READ_ENABLED)."""
    return getattr(settings, "ARTICLES_RUN_JOB_SHADOW_READ_ENABLED", False) is True


def read_enabled() -> bool:
    """True when Job reads are served from the run (ARTICLES_RUN_JOB_READ_ENABLED)."""
    return getattr(settings, "ARTICLES_RUN_JOB_READ_ENABLED", False) is True


class RunJobView:
    """A read-only, Job-shaped view of one run's backend columns."""

    __slots__ = ("run", *JOB_VIEW_FIELDS)

    def __init__(self, run):
        from .job_mirror import JOB_TO_RUN_FIELDS
        values = {"run": run, "job_id": run.legacy_job_id or run.run_id,
                  "slack_user_id": run.slack_user_id, "domain": run.domain,
                  "created_at": run.created_at, "updated_at": run.updated_at}
        for job_field, column in JOB_TO_RUN_FIELDS.items():
            values[job_field] = getattr(run, column)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("RunJobView is read-only; write the ContentFactoryJob")

    @property
    def run_id(self):
        return self.run.run_id

    @property
    def billing_ledger(self):
        return self.run.billing_ledger

    def save(self, *args, **kwargs):
        raise TypeError("RunJobView is read-only; write the ContentFactoryJob")

    def __repr__(self):
        return f"<RunJobView {self.job_id} ({self.status})>"


def job_runs():
    """Runs that carry a Job's facts: a mirror or the backfill set their phase."""
    from workflow_runs.models import ContentFactoryRun
    return ContentFactoryRun.objects.exclude(delivery_phase="")


def job_view(run_or_id):
    """The Job-shaped view of a run, or of the run carrying ``job_id``.

    Returns None where ``ContentFactoryJob.objects.filter(job_id=...).first()``
    would: no run carries that Job's facts.
    """
    from workflow_runs.models import ContentFactoryRun
    if isinstance(run_or_id, ContentFactoryRun):
        return RunJobView(run_or_id) if run_or_id.delivery_phase else None
    key = str(run_or_id or "").strip()
    if not key:
        return None
    runs = job_runs()
    run = runs.filter(run_id=key, legacy_job_id="").first() or runs.filter(legacy_job_id=key).order_by("pk").first()
    return RunJobView(run) if run is not None else None


def job_views(runs):
    """Job-shaped views of the runs that carry a Job."""
    return [RunJobView(run) for run in runs if run.delivery_phase]


def thread_runs(channel_id, thread_ts):
    """Runs whose Job was posted in a Slack thread (root or reply ts)."""
    return job_runs().filter(slack_channel_id=channel_id).filter(
        Q(slack_thread_ts=thread_ts) | Q(slack_root_message_ts=thread_ts))


def _job_value(job, field):
    if field == "billing_ledger_id" and getattr(job, "billing_ledger_id", None) is None:
        # The run also carries a research Job's ledger stamp (job_mirror.run_billing_ledger_id).
        from .job_mirror import saved_research_ledger_id
        return saved_research_ledger_id(job)
    return getattr(job, field, None)


def job_field_mismatches(job, view):
    """Names of the compared fields where a Job and its run view differ."""
    if job is None or view is None:
        return [] if job is None and view is None else ["missing_run" if view is None else "missing_job"]
    return [field for field in COMPARED_FIELDS if _job_value(job, field) != getattr(view, field, None)]


def value_mismatches(job_value, run_value):
    """Names of what differs between a Job-backed and a run-backed read result.

    Dicts compare by key, records by field, and anything else as ``value``.
    """
    if hasattr(job_value, "job_id") or hasattr(run_value, "job_id"):
        return job_field_mismatches(job_value, run_value)
    if isinstance(job_value, dict) and isinstance(run_value, dict):
        return sorted(key for key in set(job_value) | set(run_value) if job_value.get(key) != run_value.get(key))
    return [] if job_value == run_value else ["value"]


def read_job_facts(site, *, from_job, from_run, compare=value_mismatches):
    """Serve one Job read according to the 5.3 flags.

    ``from_job`` and ``from_run`` return the same shape of result. A shadow
    read runs in its own savepoint and never affects the served value; only
    the site, the mismatching names and their count are logged.
    """
    if read_enabled():
        return from_run()
    value = from_job()
    if shadow_read_enabled():
        try:
            with transaction.atomic():
                mismatched = compare(value, from_run())
        except Exception:
            logger.warning("content_factory_run_job_shadow_read_failed site=%s", site, exc_info=True)
        else:
            if mismatched:
                logger.warning("content_factory_run_job_shadow_mismatch site=%s fields=%s count=%d",
                               site, ",".join(sorted(mismatched)), len(mismatched))
    return value


def read_job_flag(site, *, from_job, from_run):
    """A yes/no Job read (an exists or guard check): only truthiness is compared and served."""
    return bool(read_job_facts(site, from_job=lambda: bool(from_job()), from_run=lambda: bool(from_run())))


def read_thread_jobs(site, channel_id, thread_ts, resolve):
    """Resolve a Slack thread from its Jobs, or from their runs (5.3 flags).

    ``resolve(jobs, get_job)`` gets the thread's Job-shaped records and a
    lookup for one more record by job id. The Job path issues the same
    queries the thread resolvers always did.
    """
    from .models import ContentFactoryJob

    def from_job():
        jobs = list(ContentFactoryJob.objects.filter(slack_channel_id=channel_id).filter(
            Q(slack_thread_ts=thread_ts) | Q(slack_root_message_ts=thread_ts)))
        return resolve(jobs, lambda job_id: ContentFactoryJob.objects.filter(job_id=job_id).first())

    def from_run():
        return resolve(job_views(thread_runs(channel_id, thread_ts)), job_view)

    return read_job_facts(site, from_job=from_job, from_run=from_run)
