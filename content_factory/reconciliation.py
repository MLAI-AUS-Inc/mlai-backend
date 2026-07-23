"""
Pull-based reconciliation between local ContentFactoryRun rows and content-factory.

The mlai↔content-factory seam is callback-driven: content-factory pushes
progress/terminal events from a durable outbox that retries for roughly two
hours (10 attempts, exponential backoff) and then parks the delivery in its
failed/ archive, never to be retried. If this backend is unreachable for
longer than that window, a run's terminal event is permanently lost and the
local run stays queued/running forever. The inverse ghost also exists: when
a dispatch response is lost, the local row is created under an invented
``vibe-marketing-*`` id for a run content-factory never accepted.

This sweep is the safety net for the pull direction:

- Local runs stuck in an active status past the stale threshold are probed
  via ``GET /api/runs/{run_id}`` and adopted (remote truth synced into the
  local snapshot) or, when content-factory has no record (404), failed
  honestly so they stop looking in-flight.
- ``vibe-marketing-*`` placeholder ids are failed without probing:
  content-factory never accepted the dispatch that would have minted a real
  id, and probing an unknown id is not free — content-factory's status read
  creates an empty artifact directory per unknown-id probe (the ghost-dir
  mechanism observed in production).
- Only workflows content-factory's durable status read actually covers are
  probed; for anything else a 404 proves nothing, so those runs are left to
  the callback spine.

Remote runs with no local record are already materialized by the callback
handlers' ``update_or_create`` spine whenever any event arrives; this module
deliberately does not try to discover them (content-factory exposes no run
listing), it only closes the local-side gaps.

The scheduler loop ticks every minute; each candidate run carries a
``reconciled_at`` stamp so it is probed at most once per probe interval, and
each tick probes at most a small batch. A healthy system does one cheap
indexed query per tick and nothing else.
"""

import logging
from datetime import timedelta
from typing import Optional

import requests

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

logger = logging.getLogger(__name__)

# Runs in these statuses are owned by content-factory: progress arrives via
# callbacks, so one that stops updating is either still (slowly) working,
# lost its terminal callback, or never existed remotely at all.
ACTIVE_RUN_STATUSES = (
    ContentFactoryRunStatus.QUEUED,
    ContentFactoryRunStatus.RUNNING,
)

# content-factory's GET /api/runs/{run_id} reads the durable store for these
# workflows only (main.py _load_durable_run). A 404 for any other workflow is
# inconclusive — the run may exist but be invisible to that endpoint — so the
# sweep never probes them.
DURABLY_READABLE_WORKFLOWS = frozenset(
    {
        "repo_scan",
        "article_system_setup",
        "direct_generate",
        "confirmed_topic",
        "article_revision",
    }
)

# Local run ids invented when a dispatch to content-factory failed or its
# response was lost (see _create_local_run in vibe_marketing_views). By
# construction content-factory never accepted these, so they are failed
# locally without a probe.
PLACEHOLDER_RUN_ID_PREFIX = "vibe-marketing-"

PLACEHOLDER_FAILURE_ERROR = (
    "content-factory never confirmed this dispatch (local placeholder run id); "
    "closed by the reconciliation sweep. Re-trigger the operation if it is still needed."
)
MISSING_REMOTE_FAILURE_ERROR = (
    "content-factory has no record of this run; its dispatch or run state was lost. "
    "Closed by the reconciliation sweep so it no longer appears in-flight. "
    "Re-trigger the operation if it is still needed."
)


def _setting_int(name: str, default: int) -> int:
    try:
        value = int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _stale_after() -> timedelta:
    return timedelta(minutes=_setting_int("CONTENT_FACTORY_RECONCILIATION_STALE_MINUTES", 45))


def _probe_interval() -> timedelta:
    return timedelta(minutes=_setting_int("CONTENT_FACTORY_RECONCILIATION_PROBE_INTERVAL_MINUTES", 15))


def _batch_limit() -> int:
    return _setting_int("CONTENT_FACTORY_RECONCILIATION_BATCH_LIMIT", 10)


def _remote_base_url() -> str:
    base_url = str(getattr(settings, "CONTENT_FACTORY_URL", "") or "").strip()
    if base_url:
        return base_url.rstrip("/")
    if getattr(settings, "IS_LOCAL_ENV", False):
        return "http://localhost:8001"
    return ""


def _remote_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = getattr(settings, "CONTENT_FACTORY_API_KEY", None)
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


def _stamp_reconciled(run_id: str, now) -> None:
    # .update() skips auto_now, so the probe stamp does not disturb the
    # updated_at staleness signal the sweep itself keys on.
    ContentFactoryRun.objects.filter(run_id=run_id).update(reconciled_at=now)


def _finalize_run_failure(run_id: str, *, error: str, outcome: str, now) -> bool:
    """
    Fail a stuck run, re-checking under lock that it is still active.

    A callback can land between candidate selection and this write; the
    status re-check makes the sweep lose that race instead of downgrading a
    freshly-updated run.
    """
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id).first()
        if run is None or run.status not in ACTIVE_RUN_STATUSES:
            return False
        result = dict(run.result or {})
        result["reconciliation"] = {"outcome": outcome, "checked_at": now.isoformat()}
        run.status = ContentFactoryRunStatus.FAILED
        run.error = error
        run.resume_available = False
        run.reconciled_at = now
        run.result = result
        run.save(
            update_fields=[
                "status",
                "error",
                "resume_available",
                "reconciled_at",
                "result",
                "updated_at",
            ]
        )
    return True


def _adopt_remote_payload(run: ContentFactoryRun, payload: dict) -> str:
    """
    Sync the remote run snapshot into the local row; returns the normalized
    status the run now carries.
    """
    from content_factory.service_views import _sync_content_factory_run_snapshot
    from content_factory.vibe_marketing_views import _normalize_remote_run_status

    normalized_status = _normalize_remote_run_status(payload.get("status"))
    sync_payload = dict(payload)
    sync_payload["workflow"] = str(payload.get("workflow") or run.workflow)
    sync_payload["status"] = normalized_status
    step_states = payload.get("step_states") or payload.get("steps") or {}
    if not isinstance(step_states, dict):
        step_states = {}
    _sync_content_factory_run_snapshot(
        run_id=run.run_id,
        data=sync_payload,
        step_states=step_states,
    )
    return normalized_status


def run_content_factory_reconciliation_sweep(*, limit: Optional[int] = None, now=None) -> dict:
    """
    One reconciliation pass. Idempotent and self-throttling, safe to tick
    every scheduler loop.
    """
    now = now or timezone.now()
    limit = limit if isinstance(limit, int) and limit > 0 else _batch_limit()
    stale_cutoff = now - _stale_after()
    probe_cutoff = now - _probe_interval()

    candidates = list(
        ContentFactoryRun.objects.filter(
            status__in=ACTIVE_RUN_STATUSES,
            updated_at__lt=stale_cutoff,
        )
        .filter(Q(reconciled_at__isnull=True) | Q(reconciled_at__lt=probe_cutoff))
        .filter(
            Q(run_id__startswith=PLACEHOLDER_RUN_ID_PREFIX)
            | Q(workflow__in=DURABLY_READABLE_WORKFLOWS)
        )
        .order_by("updated_at")[:limit]
    )

    summary = {
        "status": "completed",
        "checked": 0,
        "failed_placeholder": 0,
        "adopted": 0,
        "remote_active": 0,
        "failed_missing": 0,
        "errors": 0,
    }
    if not candidates:
        return summary

    base_url = _remote_base_url()
    headers = _remote_headers()

    for run in candidates:
        summary["checked"] += 1

        if run.run_id.startswith(PLACEHOLDER_RUN_ID_PREFIX):
            if _finalize_run_failure(
                run.run_id,
                error=PLACEHOLDER_FAILURE_ERROR,
                outcome="placeholder_never_dispatched",
                now=now,
            ):
                summary["failed_placeholder"] += 1
                logger.warning(
                    "Reconciliation closed placeholder run %s (workflow=%s, domain=%s): "
                    "dispatch was never confirmed by content-factory",
                    run.run_id,
                    run.workflow,
                    run.domain,
                )
            continue

        if not base_url:
            # Without a configured remote there is nothing to probe; leave
            # real-id runs untouched rather than guessing. Stamp the probe so
            # the warning repeats once per probe interval, not every tick.
            summary["errors"] += 1
            _stamp_reconciled(run.run_id, now)
            logger.warning(
                "Reconciliation cannot probe run %s: CONTENT_FACTORY_URL is not configured",
                run.run_id,
            )
            continue

        try:
            response = requests.get(
                f"{base_url}/api/runs/{run.run_id}",
                headers=headers,
                timeout=(3, 30),
            )
        except requests.RequestException as exc:
            summary["errors"] += 1
            _stamp_reconciled(run.run_id, now)
            logger.warning(
                "Reconciliation probe for run %s failed: %s", run.run_id, exc
            )
            continue

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if not isinstance(payload, dict) or not payload.get("status"):
                summary["errors"] += 1
                _stamp_reconciled(run.run_id, now)
                logger.warning(
                    "Reconciliation probe for run %s returned an unusable payload",
                    run.run_id,
                )
                continue
            try:
                normalized = _adopt_remote_payload(run, payload)
            except Exception:
                summary["errors"] += 1
                _stamp_reconciled(run.run_id, now)
                logger.exception("Reconciliation failed to adopt remote state for run %s", run.run_id)
                continue
            _stamp_reconciled(run.run_id, now)
            if normalized in ACTIVE_RUN_STATUSES:
                summary["remote_active"] += 1
            else:
                summary["adopted"] += 1
                logger.info(
                    "Reconciliation adopted remote state for run %s: %s -> %s",
                    run.run_id,
                    run.status,
                    normalized,
                )
        elif response.status_code == 404:
            if _finalize_run_failure(
                run.run_id,
                error=MISSING_REMOTE_FAILURE_ERROR,
                outcome="missing_on_remote",
                now=now,
            ):
                summary["failed_missing"] += 1
                logger.warning(
                    "Reconciliation closed run %s (workflow=%s, domain=%s): "
                    "content-factory returned 404",
                    run.run_id,
                    run.workflow,
                    run.domain,
                )
        else:
            summary["errors"] += 1
            _stamp_reconciled(run.run_id, now)
            logger.warning(
                "Reconciliation probe for run %s returned HTTP %s; leaving run untouched",
                run.run_id,
                response.status_code,
            )

    return summary
