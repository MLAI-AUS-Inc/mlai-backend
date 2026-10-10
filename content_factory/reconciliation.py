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
from django.utils.dateparse import parse_datetime

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
        "article_generation",
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
    return timedelta(minutes=_setting_int("CONTENT_FACTORY_RECONCILIATION_STALE_MINUTES", 10))


def _probe_interval() -> timedelta:
    return timedelta(minutes=_setting_int("CONTENT_FACTORY_RECONCILIATION_PROBE_INTERVAL_MINUTES", 2))


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
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    from .website_contract import connection_contract, WebsiteAuthorityError
    original = ContentFactoryRun.objects.filter(run_id=run_id).values("organization_id", "run_request").first()
    if original is None:
        return False
    request = original["run_request"] or {}
    try:
        binding = connection_contract(request)
    except WebsiteAuthorityError:
        return False
    operation = None
    with transaction.atomic():
        # Use lifecycle order. Taking the run first would deadlock against a
        # disconnect holding the operation while cancelling that same run.
        if original["organization_id"] is not None:
            organization = Organization.objects.select_for_update().filter(pk=original["organization_id"]).first()
            if organization is None:
                return False
        if binding:
            website = WebsiteConnection.objects.select_for_update().filter(pk=binding["website_connection_id"],
                organization_id=original["organization_id"]).first()
            if website is None or website.generation != binding["connection_generation"] or website.state != "connected":
                return False
            if binding.get("repository_id") is not None and binding["repository_id"] != website.repository_id:
                return False
            if request.get("operation_id"):
                operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=request["operation_id"],
                    connection=website, generation=website.generation, action="workflow").first()
                if operation is None or operation.state in {"cancelled", "deleted", "denied", "completed"}:
                    return False
                if request.get("operation_attempt") != operation.payload.get("attempt", 1):
                    return False
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id).first()
        if run is None or run.status not in ACTIVE_RUN_STATUSES or run.organization_id != original["organization_id"]:
            return False
        try:
            current_binding = connection_contract(run.run_request or {})
        except WebsiteAuthorityError:
            return False
        if current_binding != binding or any((run.run_request or {}).get(key) != request.get(key)
                for key in ("operation_id", "operation_attempt", "deletion_epoch", "domain", "github_repo")):
            return False
        result = dict(run.result or {})
        result["reconciliation"] = {"outcome": outcome, "checked_at": now.isoformat()}
        result["reconciliation_refund_pending"] = True
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
        if operation is not None:
            operation.state = "cancelled"
            operation.receipt = {**(operation.receipt or {}), "status": "failed", "reason_code": outcome, "run_id": run.run_id}
            operation.save(update_fields=["state", "receipt", "updated_at"])
    try:
        _refund_failed_run(run, reason=outcome)
    except Exception:
        logger.warning("content_factory_reconciliation_refund_pending run_id=%s", run.run_id)
    return True


def _adopt_remote_payload(run: ContentFactoryRun, payload: dict) -> str:
    """
    Sync the remote run snapshot into the local row; returns the normalized
    status the run now carries.
    """
    from content_factory.service_views import _sync_content_factory_run_snapshot
    from workflow_runs.status import normalize_run_status

    normalized_status = normalize_run_status(payload.get("status"))
    sync_payload = dict(payload)
    sync_payload["workflow"] = str(payload.get("workflow") or run.workflow)
    sync_payload["status"] = normalized_status
    step_states = payload.get("step_states") or payload.get("steps") or {}
    if not isinstance(step_states, dict):
        step_states = {}
    from .website_connections import authority_guard, needs_repository_authority, scoped_run_contract
    from .website_contract import WebsiteAuthorityError, connection_contract
    from .portable_drafts import portable_run_update_allowed
    if needs_repository_authority({"workflow": run.workflow}) and not portable_run_update_allowed(run, sync_payload):
        try:
            original = scoped_run_contract(run)
            with authority_guard(original, action="read"):
                binding = connection_contract(original)
                # Worker status responses omit these model columns. Retain
                # the original authorized identity, including recovery after
                # an older sparse projection cleared the denormalized column.
                for key in ("domain", "github_repo"):
                    saved = str(original.get(key) or getattr(run, key, "") or "")
                    remote_request = sync_payload.get("run_request")
                    for observation in (sync_payload, remote_request if isinstance(remote_request, dict) else {}):
                        observed = str(observation.get(key) or "")
                        if observed and observed.casefold() != saved.casefold():
                            raise WebsiteAuthorityError("website_scope_mismatch", "The worker snapshot does not match the saved website identity.")
                    sync_payload[key] = saved
                if not sync_payload.get("slack_user_id"):
                    sync_payload["slack_user_id"] = original.get("slack_user_id") or run.slack_user_id
                sync_payload.update(binding)
                sync_payload["run_request"] = {**(run.run_request or {}), **(sync_payload.get("run_request") or {}), **binding}
                observed, _ = _sync_content_factory_run_snapshot(run_id=run.run_id, data=sync_payload, step_states=step_states)
                from .website_operations import observe_workflow_status
                observe_workflow_status(observed, sync_payload)
        except WebsiteAuthorityError as denied:
            if run.status in {"cancelled", "denied"} or denied.code in {
                    "website_scope_mismatch", "run_cancelled", "website_operation_cancelled",
                    "website_data_deleted", "website_connection_deleted"}:
                return run.status
            from .run_observations import proven_run, observation_payload
            from .service_views import _apply_run_snapshot
            sync_payload.setdefault("domain", run.domain)
            if proven_run(run.run_id, sync_payload) is None:
                return run.status
            safe = observation_payload(sync_payload, run)
            safe["result"] = {**safe.get("result", {}), "authority_denied": {"code": denied.code, "message": str(denied)}}
            response = _apply_run_snapshot(run.run_id, safe)
            if response.status_code >= 400:
                return run.status
            run.refresh_from_db()
            return run.status
    else:
        _sync_content_factory_run_snapshot(run_id=run.run_id, data=sync_payload, step_states=step_states)
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
        "recovery_requested": 0,
        "failed_stalled": 0,
    }
    summary.update(_reconcile_orphan_operations(limit=limit, now=now))
    summary["settled_restart_receipts"] = _reconcile_restart_receipts(limit=limit, now=now)
    summary["redriven"] = _redrive_fixed_runs(limit=limit, now=now)
    summary["refusal_alerts"] = report_repeated_refusals(limit=limit, now=now)
    _settle_sweep_refunds(limit=limit)
    if not candidates:
        return summary

    base_url = _remote_base_url()
    headers = _remote_headers()
    recovery_supported = None

    for run in candidates:
        summary["checked"] += 1

        from .dispatch_binding import run_is_dispatch_token_keyed
        if run_is_dispatch_token_keyed(run):
            from .dispatch_models import ContentFactoryDispatchOutbox
            if ContentFactoryDispatchOutbox.objects.filter(client_request_id=run.run_id,
                    state__in=["pending", "delivering", "refund_pending", "awaiting_resolution"]).exists():
                continue

        if run.run_id.startswith(PLACEHOLDER_RUN_ID_PREFIX):
            from .dispatch_models import ContentFactoryDispatchOutbox
            if ContentFactoryDispatchOutbox.objects.filter(client_request_id=run.run_request.get("client_request_id", run.run_id),
                    state__in=["pending", "delivering", "refund_pending", "awaiting_resolution"]).exists():
                continue
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
                from workflow_runs.status import normalize_run_status
                remote_status = normalize_run_status(payload.get("status"))
                if remote_status in ACTIVE_RUN_STATUSES and remote_heartbeat_stale(payload, now=now):
                    if recovery_supported is None:
                        recovery_supported = worker_recovery_supported(base_url=base_url, headers=headers)
                    if not recovery_supported:
                        summary["remote_active"] += 1
                        _stamp_reconciled(run.run_id, now)
                        continue
                    outcome = recover_stalled_run(run, base_url=base_url, headers=headers, now=now)
                    if outcome == "recovery_requested":
                        summary["recovery_requested"] += 1
                    elif outcome == "failed_stalled":
                        summary["failed_stalled"] += 1
                    else:
                        summary["remote_active"] += 1
                    _stamp_reconciled(run.run_id, now)
                    continue
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


REDRIVE_AFTER = {
    "website_run_changed": "2.1.0",
    "website_configuration_changed": "2.1.0",
    "article_system_setup_existing_unverified": "2.1.0",
}


def _version_tuple(value):
    try:
        return tuple(int(part) for part in str(value).split("-", 1)[0].split("."))
    except ValueError:
        return ()


def fixed_failure_can_redrive(code, version):
    """Only a named fixed failure and a confirmed sufficiently new worker qualify."""
    minimum = {**REDRIVE_AFTER, **getattr(settings, "CONTENT_FACTORY_REDRIVE_AFTER", {})}.get(code)
    return bool(minimum and _version_tuple(version) and _version_tuple(version) >= _version_tuple(minimum))


def worker_recovery_supported(*, base_url, headers):
    """Enable the recovery/failure ladder only after confirmed endpoint support."""
    try:
        response = requests.get(f"{base_url}/health", headers=headers, timeout=(3, 10))
        body = response.json() if response.status_code == 200 else {}
        runtime = body.get("runtime") if isinstance(body, dict) else {}
        version = runtime.get("version") if isinstance(runtime, dict) else None
        return bool(_version_tuple(version) >= (2, 1, 0))
    except (requests.RequestException, ValueError, AttributeError):
        return False


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        stamp = parse_datetime(value)
        return timezone.make_aware(stamp) if stamp and timezone.is_naive(stamp) else stamp
    except (TypeError, ValueError, OverflowError):
        return None


def remote_heartbeat_stale(payload, *, now):
    """Use execution heartbeat/progress, never the timestamp of the GET itself."""
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    values = [payload.get(key) or result.get(key) for key in ("activity_heartbeat_at", "last_progress_at", "heartbeat_at")]
    stamps = [stamp for value in values if (stamp := _timestamp(value))]
    if stamps:
        return max(stamps) < now - timedelta(minutes=10)
    # A fresh lease is explicitly reported as active even by workers that do
    # not expose the timestamp yet. Missing heartbeat otherwise needs recovery.
    return payload.get("run_activity_state") not in {"active", "recovering"}


def recovery_was_accepted(payload):
    """A successful HTTP response is insufficient proof of requeued execution."""
    return isinstance(payload, dict) and any(payload.get(key) is True for key in (
        "recovery_requested", "stale_recovery_requeued", "orphaned_recovery_requeued"))


def _claim_run_recovery(run, now):
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().filter(run_id=run.run_id, status__in=ACTIVE_RUN_STATUSES).first()
        if not current or (current.result or {}).get("reconciliation_recovery"):
            return False
        result = {**(current.result or {}), "reconciliation_recovery": {"requested_at": now.isoformat(), "accepted": False}}
        ContentFactoryRun.objects.filter(pk=current.pk, status__in=ACTIVE_RUN_STATUSES).update(result=result, reconciled_at=now)
        return True


def _record_recovery_result(run_id, *, accepted, now):
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().filter(run_id=run_id, status__in=ACTIVE_RUN_STATUSES).first()
        if current:
            result = dict(current.result or {})
            result["reconciliation_recovery"] = {**result.get("reconciliation_recovery", {}),
                "accepted": accepted, "checked_at": now.isoformat()}
            ContentFactoryRun.objects.filter(pk=current.pk).update(result=result)


def recover_stalled_run(run, *, base_url, headers, now):
    """Ask the worker once; after five minutes without progress, fence and refund."""
    previous = (run.result or {}).get("reconciliation_recovery") or {}
    requested_at = _timestamp(previous.get("requested_at"))
    if previous:
        if requested_at and now - requested_at < timedelta(minutes=5):
            return "waiting_for_recovery"
        try:
            requests.post(f"{base_url}/api/runs/{run.run_id}/cancel", headers=headers,
                json={"reason": "worker_heartbeat_lost"}, timeout=(3, 15))
        except requests.RequestException:
            pass
        return "failed_stalled" if _finalize_run_failure(run.run_id,
            error="The article worker stopped reporting progress and recovery did not finish. Any charge is being refunded.",
            outcome="worker_recovery_exhausted", now=now) else "callback_won"
    if not _claim_run_recovery(run, now):
        return "waiting_for_recovery"
    accepted = False
    try:
        response = requests.post(f"{base_url}/api/runs/{run.run_id}/recover", headers=headers,
            json={"reason": "worker_heartbeat_lost"}, timeout=(3, 30))
        accepted = response.status_code in {200, 202} and recovery_was_accepted(response.json())
    except (requests.RequestException, ValueError):
        pass
    _record_recovery_result(run.run_id, accepted=accepted, now=now)
    return "recovery_requested" if accepted else "recovery_unconfirmed"


def _refund_failed_run(run, *, reason):
    from roo.models import Ledger
    from integrations.services.article_generation import refund_content_factory_request_for_user
    request = run.run_request or {}
    key = request.get("client_request_id")
    ledger_id = request.get("roo_points_ledger_id")
    ledger = Ledger.objects.filter(pk=ledger_id, source="CONTENT_FACTORY", kind="SPEND").select_related("user").first() if str(ledger_id or "").isdigit() else None
    if ledger is None and key:
        ledger = Ledger.objects.filter(idempotency_key=f"content_factory:charge:{key}", source="CONTENT_FACTORY", kind="SPEND").select_related("user").first()
    if ledger and ledger.user:
        request = {**request, "client_request_id": ledger.idempotency_key.removeprefix("content_factory:charge:")}
        refund_content_factory_request_for_user(user=ledger.user, actor_id=ledger.created_by_slack_id or "",
            article_request=request, resolved_domain=run.domain, reason=reason)
    elif request.get("roo_points_cost"):
        raise ValueError("The recorded spend could not be resolved for refund.")
    # Refund I/O may overlap a worker receipt. Merge the fresh result under a
    # brief lock rather than replacing it with our pre-refund snapshot.
    with transaction.atomic():
        current = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first()
        if current:
            current.result = {**(current.result or {}), "reconciliation_refund_pending": False}
            current.save(update_fields=["result"])


def _settle_sweep_refunds(*, limit):
    for run in ContentFactoryRun.objects.filter(status__in=["failed", "cancelled"], result__reconciliation_refund_pending=True)[:limit]:
        try:
            _refund_failed_run(run, reason="run_reconciliation_failed")
        except Exception:
            logger.warning("content_factory_reconciliation_refund_pending run_id=%s", run.run_id)


def _reconcile_orphan_operations(*, limit, now):
    from .website_models import WebsiteConnectionOperation
    from .dispatch_models import ContentFactoryDispatchOutbox
    count = 0
    pending_refunds = WebsiteConnectionOperation.objects.filter(action__in=["workflow", "restart"], state="failed",
        receipt__refund_pending=True).select_related("connection__organization")[:limit]
    for op in pending_refunds:
        try:
            _refund_orphan_operation(op)
        except Exception:
            logger.warning("content_factory_orphan_refund_pending operation_id=%s", op.pk)
    operations = WebsiteConnectionOperation.objects.filter(action__in=["workflow", "restart"], state__in=["pending", "running"],
        created_at__lt=now - timedelta(minutes=10)).filter(Q(payload__run_id__isnull=True) | Q(payload__run_id="")).select_related("connection__organization")[:limit]
    for op in operations:
        key = op.payload.get("client_request_id")
        if key and ContentFactoryDispatchOutbox.objects.filter(client_request_id=key,
                state__in=["pending", "delivering", "refund_pending", "awaiting_resolution"]).exists():
            continue
        run = ContentFactoryRun.objects.filter(organization_id=op.connection.organization_id, run_request__client_request_id=key).first() if key else None
        if run:
            continue
        outcome, remote_payload = _lookup_orphan_child(op)
        if outcome == "dispatched":
            try:
                _adopt_orphan_child(op, remote_payload)
            except Exception:
                logger.warning("content_factory_orphan_adoption_pending operation_id=%s", op.pk)
            continue
        if outcome not in {"absent", "rejected"}:
            WebsiteConnectionOperation.objects.filter(pk=op.pk, state=op.state, updated_at=op.updated_at).update(
                receipt={**op.receipt, "status": "dispatch_reconciliation_pending", "remote_outcome_unknown": True}, updated_at=now)
            continue
        changed = WebsiteConnectionOperation.objects.filter(pk=op.pk, state=op.state, updated_at=op.updated_at).update(
            state="failed", receipt={**op.receipt, "status": "failed", "reason_code": "dispatch_child_missing", "refund_pending": True}, updated_at=now)
        if changed:
            count += 1
            try:
                _refund_orphan_operation(op)
            except Exception:
                logger.warning("content_factory_orphan_refund_pending operation_id=%s", op.pk)
    return {"failed_orphan_receipts": count}


def _lookup_orphan_child(op):
    """A missing local child does not prove that a timed-out POST was rejected."""
    from .vibe_marketing_views import _content_factory_remote_config, _lookup_content_factory_dispatch_by_key
    key = op.payload.get("client_request_id")
    if not key:
        return "absent", {}
    remote = _content_factory_remote_config()
    if not remote["enabled"]:
        return "unknown", {}
    return _lookup_content_factory_dispatch_by_key(remote, key)


def restart_receipt_resolution(outcome):
    """Release only the restart's provisional reuse; it created no new debit."""
    return "completed" if outcome == "dispatched" else "failed" if outcome in {"absent", "rejected"} else "pending"


def _reconcile_restart_receipts(*, limit, now):
    """Close original-run receipts even when the web request died before its final write."""
    from .vibe_marketing_views import _content_factory_remote_config, _lookup_content_factory_dispatch_by_key, _create_local_run
    from .website_models import WebsiteConnectionOperation
    settled = 0
    candidates = ContentFactoryRun.objects.filter(result__restart_receipt__state="pending",
        updated_at__lt=now - timedelta(minutes=10))[:limit]
    for original in candidates:
        receipt = (original.result or {}).get("restart_receipt") or {}
        key = str(receipt.get("client_request_id") or "")
        if not key:
            outcome, body = "absent", {}
        else:
            child = ContentFactoryRun.objects.filter(organization_id=original.organization_id,
                domain=original.domain, run_request__client_request_id=key).exclude(run_id=key).first()
            if child:
                outcome, body = "dispatched", {"run_id": child.run_id}
            else:
                remote = _content_factory_remote_config()
                if not remote["enabled"]:
                    continue
                try:
                    outcome, body = _lookup_content_factory_dispatch_by_key(remote, key)
                except Exception:
                    continue
        state = restart_receipt_resolution(outcome)
        if state == "pending":
            continue
        child_id = str(body.get("run_id") or "")
        if state == "completed" and child_id:
            operation = WebsiteConnectionOperation.objects.select_related("connection__organization").filter(
                connection__organization_id=original.organization_id, action="workflow", payload__client_request_id=key).first()
            try:
                if operation:
                    child = _adopt_orphan_child(operation, body)
                else:
                    dispatch_payload = receipt.get("dispatch_payload") or original.run_request or {}
                    child = _create_local_run(workflow="article_generation", domain=original.domain, github_repo=original.github_repo,
                        payload={**dispatch_payload, "client_request_id": key, "original_billing_source_run_id": original.run_id,
                            "restart_source_run_id": original.run_id}, remote_data={**body, "dispatch_recovered_by_key": True})
                child_id = child.run_id
            except Exception:
                continue
        with transaction.atomic():
            current = ContentFactoryRun.objects.select_for_update().filter(pk=original.pk).first()
            saved = (current.result or {}).get("restart_receipt") if current else None
            if not current or not saved or saved.get("client_request_id") != key or saved.get("state") != "pending":
                continue
            receipt = {**saved, "state": state, "reused_authorization": "accepted" if state == "completed" else "released",
                "finished_at": now.isoformat(), "reconciled": True, "charged": False}
            if child_id:
                receipt["child_run_id"] = child_id
            if state == "failed":
                receipt["error_code"] = "dispatch_rejected" if outcome == "rejected" else "dispatch_child_missing"
            current.result = {**current.result, "restart_receipt": receipt}
            if state == "completed":
                count = saved.get("user_action_count")
                existing_count = current.result.get("user_action_count")
                existing_count = existing_count if type(existing_count) is int and existing_count >= 0 else 0
                if type(count) is not int or count < 0:
                    count = existing_count + 1
                current.result.update(restart_child_run_id=child_id, user_action_count=max(existing_count, count))
                child = ContentFactoryRun.objects.select_for_update().filter(run_id=child_id, organization_id=current.organization_id).first()
                if child:
                    child_count = (child.result or {}).get("user_action_count")
                    child_count = child_count if type(child_count) is int and child_count >= 0 else 0
                    child.result = {**(child.result or {}), "user_action_count": max(child_count, count)}
                    child.run_request = {**{key: value for key, value in (current.run_request or {}).items() if key.startswith("roo_points_")},
                        **(child.run_request or {}), "original_billing_source_run_id": current.run_id,
                        "restart_source_run_id": current.run_id}
                    child.save(update_fields=["result", "run_request"])
            current.save(update_fields=["result"])
            settled += 1
    return settled


def _adopt_orphan_child(op, remote_payload):
    from .vibe_marketing_views import _create_local_run
    from .website_connections import contract_for
    from .website_operations import bind_operation_run
    payload = {**contract_for(op.connection), "connection_generation": op.generation,
        "repository_id": op.payload.get("repository_id") or op.connection.repository_id,
        "github_installation_id": op.payload.get("installation_id") or op.connection.installation_id,
        "domain": op.connection.organization.domain, "github_repo": op.connection.github_repo,
        "client_request_id": op.payload["client_request_id"], "operation_id": str(op.pk),
        "operation_attempt": op.payload.get("attempt", 1), "deletion_epoch": op.payload.get("deletion_epoch", 0)}
    run = _create_local_run(workflow=op.payload.get("workflow") or "article_generation",
        domain=op.connection.organization.domain, github_repo=op.connection.github_repo,
        payload=payload, remote_data={**remote_payload, "dispatch_recovered_by_key": True})
    bind_operation_run(op, run)
    return run


def _refund_orphan_operation(op):
    """Retry a failed receipt's idempotent refund until its saved spend settles."""
    from roo.models import Ledger
    from integrations.services.article_generation import refund_content_factory_request_for_user
    from .website_models import WebsiteConnectionOperation
    key = op.payload.get("client_request_id")
    ledger = Ledger.objects.filter(idempotency_key=f"content_factory:charge:{key}", kind="SPEND",
        source="CONTENT_FACTORY").select_related("user").first() if key else None
    if ledger and ledger.user:
        refund_content_factory_request_for_user(user=ledger.user, actor_id=ledger.created_by_slack_id or "",
            article_request={"client_request_id": key}, resolved_domain=op.connection.organization.domain,
            reason="Article dispatch did not create a run.")
    with transaction.atomic():
        current = WebsiteConnectionOperation.objects.select_for_update().filter(pk=op.pk, state="failed").first()
        if current:
            current.receipt = {**current.receipt, "refund_pending": False, "refund_settled": bool(ledger)}
            current.save(update_fields=["receipt", "updated_at"])


def _redrive_fixed_runs(*, limit, now):
    base_url = _remote_base_url()
    if not base_url:
        return 0
    candidates = list(ContentFactoryRun.objects.filter(status="blocked", updated_at__lt=now - timedelta(minutes=10),
        workflow__in=DURABLY_READABLE_WORKFLOWS).filter(Q(result__sweep_redrive_attempted__isnull=True) | Q(result__sweep_redrive_attempted=False))[:limit])
    if not candidates:
        return 0
    try:
        response = requests.get(f"{base_url}/health", headers=_remote_headers(), timeout=(3, 10))
        if response.status_code != 200:
            return 0
        version = (response.json().get("runtime") or {}).get("version")
    except (requests.RequestException, ValueError):
        return 0
    count = 0
    for run in candidates:
        result = run.result or {}
        code = result.get("error_code") or (result.get("authority_denied") or {}).get("code")
        if not fixed_failure_can_redrive(code, version):
            continue
        try:
            from .website_connections import authority_guard, scoped_run_contract
            from .website_operations import advance_workflow_attempt
            with authority_guard(scoped_run_contract(run), action="read"):
                advance_workflow_attempt(run)
                ContentFactoryRun.objects.filter(pk=run.pk, status="blocked").update(
                    result={**result, "sweep_redrive_attempted": True, "sweep_redrive_version": version})
            response = requests.post(f"{base_url}/api/runs/{run.run_id}/resume", headers=_remote_headers(), timeout=(3, 30))
            if response.status_code in {200, 202}:
                count += 1
        except Exception:
            logger.warning("content_factory_sweep_redrive_unavailable run_id=%s", run.run_id)
    return count


def report_repeated_refusals(*, limit, now):
    """Persist an actionable alert once per incident; delivery is an operator concern."""
    count = 0
    for run in ContentFactoryRun.objects.filter(updated_at__gte=now - timedelta(minutes=10))[:max(limit, 100)]:
        result = run.result or {}
        events = result.get("authority_refusals") or []
        recent = [value for value in events if isinstance(value, dict) and (stamp := _timestamp(value.get("at")))
            and stamp >= now - timedelta(minutes=10)]
        if len(recent) > 3 and not result.get("refusal_alert_reported"):
            ContentFactoryRun.objects.filter(pk=run.pk, updated_at=run.updated_at).update(
                result={**result, "refusal_alert_reported": now.isoformat()})
            logger.error("content_factory_repeated_refusals run_id=%s count=%s", run.run_id, len(recent))
            count += 1
    _deliver_pending_refusal_alerts(limit=limit, now=now)
    return count


def _deliver_pending_refusal_alerts(*, limit, now):
    """Send configured ops alerts outside locks, retaining bounded delivery receipts."""
    from uuid import NAMESPACE_URL, uuid5
    import re
    channel = str(getattr(settings, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID", "") or "").strip()
    if not re.fullmatch(r"[CG][A-Z0-9]{8,}", channel):
        return 0
    from integrations.services.slack import SlackService
    candidates = ContentFactoryRun.objects.filter(updated_at__gte=now - timedelta(days=1),
        result__refusal_alert_reported__isnull=False)[:limit]
    delivered = 0
    for candidate in candidates:
        with transaction.atomic():
            run = ContentFactoryRun.objects.select_for_update().get(pk=candidate.pk)
            receipt = (run.result or {}).get("refusal_alert_delivery") or {}
            lease = _timestamp(receipt.get("next_attempt_at"))
            if receipt.get("status") == "sent" or receipt.get("attempts", 0) >= 3 or lease and lease > now:
                continue
            receipt = {**receipt, "status": "delivering", "attempts": receipt.get("attempts", 0) + 1,
                "next_attempt_at": (now + timedelta(minutes=5)).isoformat(),
                "client_msg_id": receipt.get("client_msg_id") or str(uuid5(NAMESPACE_URL,
                    f"mlai-content-refusal:{run.run_id}:{run.result['refusal_alert_reported']}"))}
            run.result = {**run.result, "refusal_alert_delivery": receipt}
            run.save(update_fields=["result"])
        # No founder email, article text, provider body or credential is included.
        codes = sorted({str(item.get("code", "unknown")) for item in run.result.get("authority_refusals", [])
            if isinstance(item, dict) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", str(item.get("code", "")))})
        safe_id = re.sub(r"[^A-Za-z0-9_:.-]", "_", str(run.run_id))[:100]
        safe_workflow = re.sub(r"[^a-z0-9_]", "_", str(run.workflow))[:50]
        text = f"Content Factory run {safe_id} exceeded 3 authority refusals in 10 minutes. Workflow: {safe_workflow}. Codes: {', '.join(codes[:8])}."
        try:
            response = SlackService.get_client().chat_postMessage(channel=channel, text=text,
                client_msg_id=receipt["client_msg_id"], unfurl_links=False, unfurl_media=False)
            message_id = str(response.get("ts") or "")
            if not response.get("ok") or not message_id:
                raise ValueError("Ops alert delivery was not acknowledged.")
            receipt.update(status="sent", message_id=message_id, channel_id=channel)
            delivered += 1
        except Exception:
            receipt["status"] = "needs_attention" if receipt["attempts"] >= 3 else "pending"
            logger.warning("content_factory_ops_alert_delivery_pending run_id=%s", run.run_id)
        with transaction.atomic():
            current = ContentFactoryRun.objects.select_for_update().filter(pk=run.pk).first()
            saved = (current.result or {}).get("refusal_alert_delivery") if current else None
            if current and saved and saved.get("client_msg_id") == receipt["client_msg_id"] and saved.get("attempts") == receipt["attempts"]:
                current.result = {**current.result, "refusal_alert_delivery": receipt}
                current.save(update_fields=["result"])
    return delivered
