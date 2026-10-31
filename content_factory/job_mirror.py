"""Copy ContentFactoryJob writes onto ContentFactoryRun's backend columns.

Robustness Phase 3 (R7 job/run merge), step 5.1. Each execution should have
one durable row. Until run-only writes, ContentFactoryJob stays authoritative
and this module keeps the run's backend columns (``delivery_phase``,
``job_meta``, billing, Slack thread and progress, topic selection) equal to it.

Behind ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED (default off):

- a ``post_save`` receiver mirrors every Job ``create``/``save``/
  ``get_or_create``/``update_or_create``;
- the queryset ``.update()`` sites, which skip signals, call
  :func:`mirror_jobs_to_runs` explicitly (tests/test_run_job_fold.py lists
  them, so a new one fails until it mirrors);
- a run created after its Job (the Job is keyed by the remote run id before
  the first callback materialises the run) is filled from that Job once.

A missing run is not an error: ``backfill_run_job_fields`` covers it later.
The mirror never writes ``ContentFactoryRun.status`` or any column CF
snapshots own, and uses a queryset update so the run's ``updated_at`` (run
recency, stall and reconciliation probes) is untouched. A mirror failure is
logged and never fails the Job write that triggered it.
"""
import logging

from django.conf import settings
from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# ContentFactoryJob attribute -> ContentFactoryRun column (design 4.1). The
# Run's own slack_user_id and domain are reused; job_id becomes run_id, or
# legacy_job_id when the two differ.
JOB_TO_RUN_FIELDS = {
    "status": "delivery_phase",
    "request_meta": "job_meta",
    "client_request_id": "client_request_id",
    "billing_source_job_id": "billing_source_run_id",
    "billing_amount": "billing_amount",
    "billing_status": "billing_status",
    "billing_ledger_id": "billing_ledger_id",
    "selected_keyword": "selected_keyword",
    "selection_reason": "selection_reason",
    "selection_data": "selection_data",
    "slack_channel_id": "slack_channel_id",
    "slack_root_message_ts": "slack_root_message_ts",
    "slack_thread_ts": "slack_thread_ts",
    "progress_message_ts": "progress_message_ts",
    "posted_progress_ids": "posted_progress_ids",
    "last_progress_milestone_index": "last_progress_milestone_index",
    "last_progress_milestone_key": "last_progress_milestone_key",
    "last_progress_updated_at": "last_progress_updated_at",
    "still_working_pinged_at": "still_working_pinged_at",
    "article_url": "article_url",
    "pr_url": "pr_url",
    "error_message": "job_error_message",
}
RUN_JOB_COLUMNS = (*JOB_TO_RUN_FIELDS.values(), "legacy_job_id")
_JSON_DEFAULTS = {"job_meta": dict, "selection_data": dict, "posted_progress_ids": list}
_BLANK_STRINGS = frozenset({
    "delivery_phase", "billing_status", "slack_channel_id", "slack_root_message_ts", "slack_thread_ts",
    "progress_message_ts", "last_progress_milestone_key", "legacy_job_id",
})
# Run column values that mean "no Job fact": what the 0006 columns hold before
# any mirror or backfill.
RUN_JOB_COLUMN_DEFAULTS = {
    column: (_JSON_DEFAULTS[column]() if column in _JSON_DEFAULTS
             else "" if column in _BLANK_STRINGS
             else 0 if column in {"billing_amount", "last_progress_milestone_index"}
             else None)
    for column in RUN_JOB_COLUMNS
}


def dual_write_enabled() -> bool:
    """True when Job writes are copied onto the run (ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED)."""
    return getattr(settings, "ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED", False) is True


def _job_fields_for(update_fields):
    """The Job attributes a save wrote, or None for every mapped field."""
    if update_fields is None:
        return None
    fields = set()
    for name in update_fields:
        if name in {"billing_ledger", "billing_ledger_id"}:
            fields.add("billing_ledger_id")
        elif name in JOB_TO_RUN_FIELDS or name == "job_id":
            fields.add(name)
    return fields


def saved_research_ledger_id(job):
    """The ledger id a research Job keeps only in request_meta, when it parses.

    The research dispatch stamps ``request_meta.roo_points_ledger_id`` but no
    ``billing_ledger`` (design 4.2, step 4).
    """
    from .billing_lineage import _ledger_id
    return _ledger_id((getattr(job, "request_meta", None) or {}).get("roo_points_ledger_id"))


def run_billing_ledger_id(job):
    """The run's billing ledger for a Job: its own, else its research stamp when that ledger exists."""
    ledger_id = getattr(job, "billing_ledger_id", None)
    if ledger_id is not None:
        return ledger_id
    stamped = saved_research_ledger_id(job)
    if stamped is None:
        return None
    from roo.models import Ledger
    return stamped if Ledger.objects.filter(pk=stamped).exists() else None


def run_values_for_job(job, *, run_id, job_fields=None):
    """Map a Job's values onto run columns (design 4.1).

    ``job_fields`` limits the copy to the Job attributes a save wrote; None
    copies every mapped field. ``legacy_job_id`` is the Job's id when it
    differs from ``run_id``, and blank otherwise.
    """
    values = {}
    for job_field, column in JOB_TO_RUN_FIELDS.items():
        if job_fields is not None and job_field not in job_fields:
            continue
        value = getattr(job, job_field, None)
        if value is None and column in _JSON_DEFAULTS:
            value = _JSON_DEFAULTS[column]()
        elif value is None and column in _BLANK_STRINGS:
            value = ""
        values[column] = value
    if job_fields is None or job_fields & {"billing_ledger_id", "request_meta"}:
        values["billing_ledger_id"] = run_billing_ledger_id(job)
    if job_fields is None or "job_id" in job_fields:
        job_id = str(job.job_id or "")
        values["legacy_job_id"] = "" if job_id == str(run_id or "") else job_id
    return values


def run_for_job_id(job_id, *, queryset=None):
    """The run carrying a Job's facts: ``run_id == job_id``, then ``legacy_job_id``."""
    from workflow_runs.models import ContentFactoryRun
    key = str(job_id or "").strip()
    if not key:
        return None
    runs = ContentFactoryRun.objects.all() if queryset is None else queryset
    return runs.filter(run_id=key).first() or runs.filter(legacy_job_id=key).order_by("pk").first()


def mirror_job_to_run(job, fields=None) -> bool:
    """Copy a Job's written fields onto its run, in the caller's transaction.

    ``fields`` are the Job field names a save wrote (``update_fields``); None
    copies every mapped field. A run that has never carried a Job fact gets a
    full copy from the Job's committed row instead, so a partial save never
    leaves it half filled. Returns True when a run was written. Does nothing
    with ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED off or when the run is missing.
    """
    if not dual_write_enabled() or job is None or not getattr(job, "pk", None):
        return False
    from workflow_runs.models import ContentFactoryRun
    try:
        with transaction.atomic():
            run = run_for_job_id(job.job_id, queryset=ContentFactoryRun.objects.only("pk", "run_id", "delivery_phase"))
            if run is None:
                return False
            job_fields = _job_fields_for(fields)
            if job_fields is not None and not run.delivery_phase:
                job_fields = None
                job = type(job).objects.filter(pk=job.pk).first() or job
            values = run_values_for_job(job, run_id=run.run_id, job_fields=job_fields)
            if not values:
                return False
            ContentFactoryRun.objects.filter(pk=run.pk).update(**values)
            return True
    except Exception:  # pragma: no cover - the Job stays authoritative; the backfill repairs drift
        logger.warning("content_factory_job_mirror_failed job_pk=%s", getattr(job, "pk", None), exc_info=True)
        return False


def mirror_jobs_to_runs(jobs, fields=None) -> int:
    """Mirror Jobs that a queryset ``.update()`` wrote (it sends no ``post_save``).

    ``jobs`` is evaluated only with dual write on, so a call costs no query
    when the flag is off. Returns how many runs were written.
    """
    if not dual_write_enabled():
        return 0
    return sum(1 for job in jobs if mirror_job_to_run(job, fields))


@receiver(post_save, sender="content_factory.ContentFactoryJob", dispatch_uid="content_factory_mirror_job_to_run")
def mirror_saved_job(sender, instance, raw=False, update_fields=None, **kwargs):
    """Mirror every saved Job onto its run (dual write)."""
    if raw or not dual_write_enabled():
        return
    mirror_job_to_run(instance, fields=update_fields)


@receiver(post_save, sender="workflow_runs.ContentFactoryRun", dispatch_uid="content_factory_fill_new_run_from_job")
def fill_new_run_from_job(sender, instance, created=False, raw=False, **kwargs):
    """Fill a newly created run from a Job written before it (dual write)."""
    if raw or not created or not dual_write_enabled():
        return
    from .models import ContentFactoryJob
    job = ContentFactoryJob.objects.filter(job_id=instance.run_id).first()
    if job is not None:
        mirror_job_to_run(job)
