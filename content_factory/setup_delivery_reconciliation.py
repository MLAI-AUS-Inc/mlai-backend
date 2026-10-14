"""Bounded read-only recovery of enrolled setup projections in the scheduler."""

from copy import deepcopy
from datetime import timedelta
import json
import logging
from uuid import uuid4

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from workflow_runs.models import ContentFactoryRun
from .setup_delivery_contract import MAX_BYTES, DeliveryContractError
from .setup_delivery_receiver import (_locked_context, _metadata, _save_metadata,
    receive_setup_delivery, DeliveryScopeError, pull_pin_matches, _original_scope_matches)
from .website_contract import WebsiteAuthorityError

logger = logging.getLogger(__name__)
WAITING_STATUSES = {"awaiting_approval", "awaiting_confirmation", "awaiting_delivery_mode"}


def enrolled_run(run):
    """Enrollment comes from the saved backend dispatch, never a GET response."""
    return ((run.run_request or {}).get("setup_delivery_v1") == {"schema_version": 1}
            or _metadata(run).get("enrolled") is True)


def _claim_pull(candidate, now):
    original = candidate.run_request or {}
    from .setup_delivery_enrollment import SCOPE_FIELDS
    scope = {key: original[key] for key in SCOPE_FIELDS if key in original}
    envelope = {"scope": scope, "run_id": candidate.run_id, "workflow": candidate.workflow}
    with transaction.atomic():
        _, _, run = _locked_context(envelope)
        if run is None or not enrolled_run(run) or run.status in {"cancelled", "denied"}:
            return None
        metadata = _metadata(run)
        pull = metadata.get("pull", {})
        expires = parse_datetime(pull["expires_at"]) if isinstance(pull.get("expires_at"), str) else None
        if expires and expires > now:
            return None
        if run.status in WAITING_STATUSES and metadata.get("waiting_acknowledged") is True:
            return None
        nonce = uuid4().hex
        metadata["pull"] = {"nonce": nonce, "expires_at": (now + timedelta(seconds=120)).isoformat()}
        pin = {"nonce": nonce, "run_request": deepcopy(run.run_request), "status": run.status,
            "latest": deepcopy(metadata.get("latest")), "generation": (run.result or {}).get("generation"),
            "state_version": (run.result or {}).get("state_version")}
        _save_metadata(run, metadata)
        ContentFactoryRun.objects.filter(run_id=run.run_id).update(reconciled_at=now)
        return pin


def _release_pull(run_id, pin, *, outcome, now):
    candidate = ContentFactoryRun.objects.filter(run_id=run_id).first()
    if candidate is None:
        return
    from .setup_delivery_enrollment import SCOPE_FIELDS
    original = candidate.run_request or {}
    envelope = {"scope": {key: original[key] for key in SCOPE_FIELDS if key in original},
                "run_id": run_id, "workflow": candidate.workflow}
    try:
        with transaction.atomic():
            _, _, run = _locked_context(envelope)
            if run is None or _metadata(run).get("pull", {}).get("nonce") != pin["nonce"]:
                return
            metadata = _metadata(run)
            metadata.pop("pull", None)
            metadata["last_probe"] = {"at": now.isoformat(), "outcome": outcome}
            _save_metadata(run, metadata)
    except (WebsiteAuthorityError, DeliveryScopeError):
        # Revoke/delete wins. Do not recreate a run or rebind a cleanup fence.
        return


def _read_bounded_snapshot(base_url, headers, run_id):
    """One authenticated configured-origin GET, outside every database lock."""
    from .setup_delivery_contract import DeliveryContractError
    import re
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", run_id):
        raise DeliveryContractError("delivery_identity_mismatch", ["/run_id"])
    response = requests.get(f"{base_url.rstrip('/')}/api/runs/{run_id}",
        params={"view": "delivery_snapshot"}, headers=headers, timeout=(3.05, 10),
        allow_redirects=False, stream=True)
    try:
        if response.status_code != 200:
            return None, "unavailable"
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise DeliveryContractError("delivery_payload_too_large", ["/"])
        try:
            return json.loads(raw), "available"
        except (ValueError, UnicodeError, RecursionError):
            raise DeliveryContractError("delivery_schema_invalid", ["/"]) from None
    finally:
        response.close()


def reconcile_setup_delivery_run(candidate, *, base_url, headers, now=None):
    """Recover one projection; unavailable state cannot fail, refund or restart."""
    now = now or timezone.now()
    try:
        pin = _claim_pull(candidate, now)
    except (WebsiteAuthorityError, DeliveryScopeError, KeyError):
        return "fenced"
    if pin is None:
        return "lease_conflict"
    outcome = "unavailable"
    try:
        envelope, _ = _read_bounded_snapshot(base_url, headers, candidate.run_id)
        if envelope is None:
            return outcome
        if not getattr(settings, "CONTENT_FACTORY_SETUP_DELIVERY_V1_APPLY_ENABLED", False):
            from .setup_delivery_contract import validate_envelope
            validated = validate_envelope(envelope, expected_kind="run_snapshot", path_run_id=candidate.run_id)
            with transaction.atomic():
                _, _, current = _locked_context(validated)
                if not pull_pin_matches(current, pin) or not _original_scope_matches(current, validated["scope"]):
                    outcome = "deferred"
                else:
                    outcome = "shadow_validated"
            return outcome
        response = receive_setup_delivery(envelope, expected_kind="run_snapshot",
            path_run_id=candidate.run_id, pull_pin=pin)
        if response.status_code == 200 and response.data.get("disposition") == "applied":
            outcome = "repaired"
        elif response.status_code == 200:
            outcome = "no_change"
        elif response.status_code == 422:
            outcome = "contract_rejected"
        else:
            outcome = "fenced" if response.data.get("acknowledged") else "deferred"
        return outcome
    except DeliveryContractError as exc:
        outcome = "contract_rejected"
        logger.warning("setup_delivery_pull_rejected code=%s fields=%s", exc.code, exc.paths)
        return outcome
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # Safe metric only: no raw response, URL, credential or exception body.
        logger.warning("setup_delivery_pull_unavailable run_id=%s", candidate.run_id)
        return outcome
    finally:
        _release_pull(candidate.run_id, pin, outcome=outcome, now=now)


def run_setup_delivery_reconciliation_sweep(*, limit=10, now=None):
    """Extend the existing scheduler with bounded, independently flagged pulls."""
    summary = {"delivery_checked": 0, "delivery_reconciler_repairs_total": 0,
               "delivery_unavailable": 0, "delivery_no_change": 0, "delivery_contract_rejected": 0,
               "delivery_shadow_validated": 0}
    if not getattr(settings, "CONTENT_FACTORY_SETUP_DELIVERY_RECONCILIATION_ENABLED", False):
        return summary
    from .reconciliation import _remote_base_url, _remote_headers
    base_url, headers = _remote_base_url(), _remote_headers()
    if not base_url or not headers.get("X-API-KEY"):
        return summary
    now = now or timezone.now()
    limit = min(limit if type(limit) is int and limit > 0 else 10, 10)
    candidates = list(ContentFactoryRun.objects.filter(
        workflow__in=["repo_scan", "article_system_setup"],
        run_request__setup_delivery_v1__schema_version=1,
        updated_at__lt=now - timedelta(minutes=10),
    ).exclude(status__in=["cancelled", "denied"])
        .filter(Q(reconciled_at__isnull=True) | Q(reconciled_at__lt=now - timedelta(minutes=2)))
        .filter(~Q(status__in=WAITING_STATUSES)
            | Q(result__delivery_v1__waiting_acknowledged__isnull=True)
            | ~Q(result__delivery_v1__waiting_acknowledged=True))
        .order_by("reconciled_at", "updated_at")[:limit])
    for run in candidates:
        summary["delivery_checked"] += 1
        outcome = reconcile_setup_delivery_run(run, base_url=base_url, headers=headers, now=now)
        if outcome == "repaired":
            summary["delivery_reconciler_repairs_total"] += 1
            logger.warning("delivery_reconciler_repair run_id=%s", run.run_id)
        elif outcome in {"unavailable", "deferred", "fenced", "lease_conflict"}:
            summary["delivery_unavailable"] += 1
        elif outcome == "contract_rejected":
            summary["delivery_contract_rejected"] += 1
        elif outcome == "shadow_validated":
            summary["delivery_shadow_validated"] += 1
        else:
            summary["delivery_no_change"] += 1
    return summary
