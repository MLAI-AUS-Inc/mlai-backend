"""Backfill, compare and undo the ContentFactoryJob facts folded onto runs.

Robustness Phase 3 (R7 job/run merge), steps 4.2, 4.3 and the 5.2 parity
gate. The management commands ``backfill_run_job_fields`` and
``compare_run_job_fields`` are thin wrappers over this module.

The backfill only fills empty run columns. It never overwrites a non-empty
value (a differing one is counted as a conflict), never deletes a Job, and
creates a run only for a Job that has none, tagged in ``result`` so ``--undo``
can find it. Reports carry field names and counts only: no ids, domains,
founder names or emails.
"""
from collections import Counter
from datetime import datetime

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .job_mirror import JOB_TO_RUN_FIELDS, RUN_JOB_COLUMN_DEFAULTS, RUN_JOB_COLUMNS, run_values_for_job

BACKFILL_RESULT_KEY = "backfilled_from_job"
DISPATCH_TOKEN_PREFIX = "vibe-dispatch:"
DEFAULT_BATCH_SIZE = 500
RESEARCH_ACTION = "content_island_topic_generation"

# Design 3.3: the run status a created run gets for its Job's phase.
PHASE_TO_RUN_STATUS = {
    "queued": "queued",
    "researching": "running",
    "generating": "running",
    "awaiting_confirmation": "awaiting_confirmation",
    "awaiting_delivery_mode": "awaiting_delivery_mode",
    "awaiting_approval": "awaiting_approval",
    "blocked": "blocked",
    "auth_required": "blocked",
    "pr_opened": "completed",
    "needs_review": "completed",
    "confirmed": "completed",
    "cancelled": "cancelled",
    "completed": "completed",
    "error": "failed",
}


def run_status_for_phase(phase) -> str:
    """The run status for a Job phase (design 3.3); run vocabulary passes through."""
    from workflow_runs.status import normalize_run_status
    value = str(phase or "").strip().lower()
    return PHASE_TO_RUN_STATUS.get(value) or normalize_run_status(value)


def infer_workflow(job) -> str:
    """The workflow of a Job that never had a run, from its request_meta (design 4.2)."""
    meta = job.request_meta if isinstance(job.request_meta, dict) else {}
    kind = str(meta.get("type") or "").strip()
    if kind == "scan":
        return "repo_scan"
    if kind == "scaffold_articles":
        return "article_system_setup"
    topic = meta.get("topic") or meta.get("target_keyword") or meta.get("confirmed_keyword") or job.selected_keyword
    if meta.get("source_run_id") and topic:
        return "confirmed_topic"
    if (meta.get("roo_points_action") == RESEARCH_ACTION or "requested_topic_count" in meta
            or "daily_topic_policy" in meta or str(job.client_request_id or "").startswith("automation-research:")):
        return "auto_discovery"
    return "direct_generate"


def is_token_job(job) -> bool:
    """A Job still keyed by a dispatch token rather than a remote run id."""
    job_id = str(job.job_id or "")
    return job_id.startswith(DISPATCH_TOKEN_PREFIX) or (bool(job_id) and job_id == str(job.client_request_id or ""))


def _empty(column, value) -> bool:
    return value in (None, "") or value == RUN_JOB_COLUMN_DEFAULTS[column]


class RunMatch:
    """How a Job's run was found: ``run`` may be None (create one), or the Job is skipped."""

    def __init__(self, run=None, how="missing", owner_job=None):
        self.run = run
        self.how = how
        self.owner_job = owner_job


def match_run_for_job(job, *, runs):
    """Find a Job's run (design 4.2 step 2): run_id, then its dispatch token.

    A token Job matches the run carrying the token as ``legacy_job_id`` or
    ``run_request.client_request_id`` (the run ``bind_dispatch_token_run``
    renamed). When a Job with that run's own id exists, the token Job is an
    orphan: the real carrier keeps the run.
    """
    from .models import ContentFactoryJob
    run = runs.filter(run_id=job.job_id).first()
    if run is not None:
        return RunMatch(run, "run_id")
    if not is_token_job(job):
        return RunMatch()
    candidates = list(runs.filter(legacy_job_id=job.job_id).order_by("pk")[:2]) or list(
        runs.filter(run_request__client_request_id=job.job_id).order_by("pk")[:2])
    if len(candidates) > 1:
        return RunMatch(how="ambiguous")
    if not candidates:
        return RunMatch()
    run = candidates[0]
    owner = ContentFactoryJob.objects.filter(job_id=run.run_id).exclude(pk=job.pk).first()
    if owner is not None:
        return RunMatch(run, "orphan", owner_job=owner)
    return RunMatch(run, "token")


def fill_plan(job, run):
    """Values to write into a run's empty columns, and the conflicting column names.

    An empty run column takes the Job's value, even an empty one of another
    shape ("" for None), so the view reads exactly what the Job holds. A
    non-empty run value that differs is never overwritten: it is a conflict.
    """
    values = run_values_for_job(job, run_id=run.run_id)
    fills, conflicts = {}, []
    for column, value in values.items():
        current = getattr(run, column)
        if current == value:
            continue
        if _empty(column, current):
            fills[column] = value
        else:
            conflicts.append(column)
    return fills, conflicts


def orphan_conflicts(job, owner_job, run):
    """Columns where an orphan token Job disagrees with the real carrier (never copied)."""
    mine = run_values_for_job(job, run_id=run.run_id)
    theirs = run_values_for_job(owner_job, run_id=run.run_id)
    return [column for column, value in mine.items()
            if column != "legacy_job_id" and not _empty(column, value) and value != theirs.get(column)]


class BackfillReport:
    """Counts only. ``lines()`` is what the command prints."""

    def __init__(self, *, apply):
        self.apply = apply
        self.counts = Counter()
        self.conflicts = Counter()
        self.create_workflows = Counter()
        self.create_statuses = Counter()

    def lines(self):
        mode = "apply" if self.apply else "dry run (pass --apply to write)"
        verb = "Created" if self.apply else "Would create"
        fill_verb = "Filled" if self.apply else "Would fill"
        lines = [f"Mode: {mode}"]
        for key in ("jobs_scanned", "matched_by_run_id", "matched_by_token", "orphan_token_jobs",
                    "ambiguous_token_jobs", "jobs_without_run", "create_skipped", "create_races"):
            lines.append(f"{key}: {self.counts[key]}")
        lines.append(f"{verb} runs: {self.counts['runs_created']}")
        lines.append(f"  by workflow: {_counter_text(self.create_workflows)}")
        lines.append(f"  by status: {_counter_text(self.create_statuses)}")
        lines.append(f"  active (queued/running; the reconciliation sweep probes these): {self.counts['created_active']}")
        lines.append(f"{fill_verb} runs: {self.counts['runs_filled']} ({self.counts['columns_filled']} columns)")
        lines.append(f"{'Linked' if self.apply else 'Would link'} written articles: {self.counts['articles_linked']}")
        lines.append(f"Conflicts (kept the run's value): {_counter_text(self.conflicts)}")
        return lines


def _counter_text(counter):
    return ", ".join(f"{key}={counter[key]}" for key in sorted(counter)) or "none"


def _created_run_kwargs(job, *, at):
    """A tagged run for a Job that never had one (design 4.2 step 3)."""
    values = run_values_for_job(job, run_id=job.job_id)
    return {
        "run_id": job.job_id,
        "workflow": infer_workflow(job),
        "domain": job.domain or "",
        "slack_user_id": job.slack_user_id or "",
        "status": run_status_for_phase(job.status),
        "current_step": "",
        "run_request": {},
        "result": {BACKFILL_RESULT_KEY: {"at": at.isoformat(), "job_pk": job.pk}},
        **values,
    }


def _link_articles(job, run, report):
    from .models import WrittenArticle
    articles = WrittenArticle.objects.filter(job_id=job.pk)
    report.conflicts["written_article_run"] += articles.exclude(run__isnull=True).exclude(run_id=run.pk).count()
    unlinked = articles.filter(run__isnull=True)
    if report.apply:
        report.counts["articles_linked"] += unlinked.update(run=run)
    else:
        report.counts["articles_linked"] += unlinked.count()


def _backfill_job(job, report, *, at, create, create_since):
    from workflow_runs.models import ContentFactoryRun
    runs = ContentFactoryRun.objects.select_for_update() if report.apply else ContentFactoryRun.objects.all()
    match = match_run_for_job(job, runs=runs)
    if match.how == "ambiguous":
        report.counts["ambiguous_token_jobs"] += 1
        return
    if match.how == "orphan":
        report.counts["orphan_token_jobs"] += 1
        report.conflicts.update(orphan_conflicts(job, match.owner_job, match.run))
        return
    if match.run is None:
        report.counts["jobs_without_run"] += 1
        if not create or (create_since is not None and job.created_at < create_since):
            report.counts["create_skipped"] += 1
            return
        kwargs = _created_run_kwargs(job, at=at)
        report.create_workflows[kwargs["workflow"]] += 1
        report.create_statuses[kwargs["status"]] += 1
        report.counts["created_active"] += kwargs["status"] in {"queued", "running"}
        if not report.apply:
            report.counts["runs_created"] += 1
            report.counts["articles_linked"] += job.articles.filter(run__isnull=True).count()
            return
        try:
            with transaction.atomic():
                run = ContentFactoryRun.objects.create(**kwargs)
        except IntegrityError:
            # A callback created the run meanwhile; a re-run fills it.
            report.counts["create_races"] += 1
            return
        ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=job.created_at, updated_at=at)
        report.counts["runs_created"] += 1
        _link_articles(job, run, report)
        return
    report.counts["matched_by_run_id" if match.how == "run_id" else "matched_by_token"] += 1
    run = match.run
    if report.apply:
        # The run is locked now; read the Job's committed row so a callback
        # that wrote it meanwhile is not copied stale.
        job = type(job).objects.filter(pk=job.pk).first() or job
    fills, conflicts = fill_plan(job, run)
    report.conflicts.update(conflicts)
    if fills:
        report.counts["runs_filled"] += 1
        report.counts["columns_filled"] += len(fills)
        if report.apply:
            ContentFactoryRun.objects.filter(pk=run.pk).update(**fills)
    _link_articles(job, run, report)


def backfill_run_job_fields(*, apply=False, batch_size=DEFAULT_BATCH_SIZE, create=True, create_since=None, now=None):
    """Copy every Job's facts into its run's empty columns (design 4.2).

    Jobs are read by pk in batches, each batch in its own transaction with
    the written runs locked. Re-running changes nothing new. Returns the
    :class:`BackfillReport`.
    """
    from .models import ContentFactoryJob
    report = BackfillReport(apply=apply)
    at = now or timezone.now()
    last_pk = 0
    while True:
        batch = list(ContentFactoryJob.objects.filter(pk__gt=last_pk).order_by("pk")[:batch_size])
        if not batch:
            break
        with transaction.atomic():
            for job in batch:
                report.counts["jobs_scanned"] += 1
                _backfill_job(job, report, at=at, create=create, create_since=create_since)
        last_pk = batch[-1].pk
    return report


def _backfill_time(run):
    tag = (run.result or {}).get(BACKFILL_RESULT_KEY) if isinstance(run.result, dict) else None
    try:
        return datetime.fromisoformat(str(tag.get("at"))) if isinstance(tag, dict) else None
    except ValueError:
        return None


def undo_backfill(*, apply=False, batch_size=DEFAULT_BATCH_SIZE):
    """Reverse the backfill (design 4.3).

    Deletes only the runs it created (tagged ``result.backfilled_from_job``)
    that nothing updated since (``updated_at`` still equals the backfill
    time), then blanks the new run columns and ``WrittenArticle.run``.
    Returns counts.
    """
    from workflow_runs.models import ContentFactoryRun
    from .models import WrittenArticle
    counts, deleted = Counter(), []
    tagged = ContentFactoryRun.objects.filter(result__has_key=BACKFILL_RESULT_KEY).order_by("pk")
    last_pk = 0
    while True:
        batch = list(tagged.filter(pk__gt=last_pk)[:batch_size])
        if not batch:
            break
        with transaction.atomic():
            for run in batch:
                at = _backfill_time(run)
                current = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first() if apply else run
                if current is None:
                    continue
                if at is None or current.updated_at != at:
                    counts["created_runs_kept_updated"] += 1
                    continue
                counts["created_runs_deleted"] += 1
                deleted.append(current.pk)
                if apply:
                    current.delete()
        last_pk = batch[-1].pk

    last_pk = 0
    while True:
        pks = list(ContentFactoryRun.objects.filter(_filled_runs(), pk__gt=last_pk).exclude(pk__in=deleted)
                   .order_by("pk").values_list("pk", flat=True)[:batch_size])
        if not pks:
            break
        counts["runs_blanked"] += len(pks)
        if apply:
            with transaction.atomic():
                ContentFactoryRun.objects.filter(pk__in=pks).update(**_blank_values())
        last_pk = pks[-1]
    linked = WrittenArticle.objects.filter(run__isnull=False)
    counts["articles_unlinked"] = linked.update(run=None) if apply else linked.count()
    return counts


def _filled_runs():
    """Runs holding any Job fact: a new column away from its default."""
    condition = Q()
    for column, default in RUN_JOB_COLUMN_DEFAULTS.items():
        condition |= Q(**{f"{column}__isnull": False}) if default is None else ~Q(**{column: default})
    return condition


def _blank_values():
    return {column: (type(default)() if isinstance(default, (dict, list)) else default)
            for column, default in RUN_JOB_COLUMN_DEFAULTS.items()}


class CompareReport:
    """Per-field mismatch counts between each Job and its run."""

    def __init__(self):
        self.counts = Counter()
        self.mismatches = Counter()

    def lines(self):
        lines = [f"{key}: {self.counts[key]}" for key in (
            "jobs_compared", "matched", "missing_run", "unfilled_run", "orphan_token_jobs",
            "ambiguous_token_jobs", "jobs_with_mismatch")]
        lines.append(f"Mismatches by field: {_counter_text(self.mismatches)}")
        return lines


def compare_run_job_fields(*, batch_size=DEFAULT_BATCH_SIZE, updated_since=None):
    """Count per-field mismatches between Jobs and their runs (the 5.2 parity gate)."""
    from workflow_runs.models import ContentFactoryRun
    from .job_view import RunJobView, job_field_mismatches
    from .models import ContentFactoryJob
    report = CompareReport()
    jobs = ContentFactoryJob.objects.order_by("pk")
    if updated_since is not None:
        jobs = jobs.filter(updated_at__gte=updated_since)
    last_pk = 0
    while True:
        batch = list(jobs.filter(pk__gt=last_pk)[:batch_size])
        if not batch:
            break
        for job in batch:
            report.counts["jobs_compared"] += 1
            match = match_run_for_job(job, runs=ContentFactoryRun.objects.all())
            if match.how == "ambiguous":
                report.counts["ambiguous_token_jobs"] += 1
            elif match.how == "orphan":
                report.counts["orphan_token_jobs"] += 1
            elif match.run is None:
                report.counts["missing_run"] += 1
            elif not match.run.delivery_phase:
                report.counts["unfilled_run"] += 1
            else:
                report.counts["matched"] += 1
                fields = job_field_mismatches(job, RunJobView(match.run))
                if fields:
                    report.counts["jobs_with_mismatch"] += 1
                    report.mismatches.update(fields)
        last_pk = batch[-1].pk
    return report


__all__ = [
    "BACKFILL_RESULT_KEY", "JOB_TO_RUN_FIELDS", "RUN_JOB_COLUMNS", "backfill_run_job_fields",
    "compare_run_job_fields", "infer_workflow", "match_run_for_job", "run_status_for_phase", "undo_backfill",
]
