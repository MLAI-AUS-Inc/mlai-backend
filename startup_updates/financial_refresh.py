"""Durable, founder-scoped chart refresh candidates, independent of generation."""
import copy
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from django.db import close_old_connections, transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import NotFound, ValidationError

from founder_tools.models import VibeRaisingCompany
from integrations.models import ExternalServiceConnection
from startup_updates.evidence_contract import content_hash, reporting_period
from startup_updates.financial_history import build_snapshot, financial_display, history_payload
from startup_updates.financial_refresh_sources import (
    apply_stage, build_candidate_evidence, connection_identity, failure_details,
    fetch_stage, local_time, month_slots, next_stage,
)
from startup_updates.models import MonthlyEvidenceSnapshot, MonthlyUpdateDraft, StartupProfile, StartupDataDeletionRequest
from startup_updates.revisions import RevisionConflict
from workflow_runs.models import ContentFactoryRun

WORKFLOW = "pulse_financial_refresh"
CANDIDATE_TTL = timedelta(hours=24)
LEASE_SECONDS = 180
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pulse-financials")
_SLOTS = threading.BoundedSemaphore(8)
logger = logging.getLogger(__name__)


def _uuid(value, field):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError({field: "Use a valid request identifier."}) from exc


def _positive(value, field):
    if value is not None and (type(value) is not int or value <= 0):
        raise ValidationError({field: "Use a positive integer."})
    return value


def request_scope(company, user, data, profile, *, now=None):
    """Validate immutable request identity before any provider access."""
    now = now or timezone.now()
    raw_month = data.get("targetMonth")
    try:
        month = date.fromisoformat(raw_month)
        if month.day != 1 or month.isoformat() != raw_month:
            raise ValueError()
        period = reporting_period(month, profile.reporting_timezone, as_of=now)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"targetMonth": "Choose this month or an earlier reporting month."}) from exc
    sources = data.get("inputSources")
    if not isinstance(sources, list) or any(type(source) is not str for source in sources) or not sources or set(sources) - {"stripe", "xero"}:
        raise ValidationError({"inputSources": "Choose Stripe and/or Xero financial sources."})
    update_id = _positive(data.get("updateId"), "updateId")
    expected = _positive(data.get("expectedRevision"), "expectedRevision")
    creation_key = _uuid(data.get("creationKey"), "creationKey") if not update_id else None
    if not update_id and expected is not None:
        raise ValidationError({"expectedRevision": "Choose the saved update before refreshing its revision."})
    cutoff = datetime.fromisoformat(period["cutoff"]).astimezone(ZoneInfo(profile.reporting_timezone))
    coverage_end = (cutoff - timedelta(microseconds=1)).date()
    return {"requestId": _uuid(data.get("requestId"), "requestId"), "companyId": str(company.pk),
        "organizationId": company.organization_id, "userId": user.pk,
        "targetMonth": month.isoformat(), "updateId": update_id, "creationKey": creation_key,
        "expectedRevision": expected, "inputSources": sorted(set(sources)),
        "period": period, "coverageEnd": coverage_end.isoformat(),
        "timezone": profile.reporting_timezone, "currency": profile.default_currency,
        "configVersion": profile.reporting_config_version}


def _identity(request):
    return {key: request.get(key) for key in ("requestId", "companyId", "organizationId", "userId",
        "targetMonth", "updateId", "creationKey", "expectedRevision", "inputSources", "timezone", "currency", "configVersion")}


def _equivalent_scope(left, right):
    """Ignore click IDs and wall-clock cutoffs when coalescing in-flight reads."""
    return ({key: value for key, value in _identity(left).items() if key != "requestId"}
        == {key: value for key, value in _identity(right).items() if key != "requestId"}
        and left.get("connections") == right.get("connections"))


def _check_base(request):
    if request["updateId"]:
        draft = get_object_or_404(MonthlyUpdateDraft, pk=request["updateId"], organization_id=request["organizationId"])
        if draft.month.isoformat() != request["targetMonth"] or draft.current_revision_id != request["expectedRevision"]:
            raise RevisionConflict("This update changed. Save your writing, then refresh its financials again.")


def _connections(request, *, lock=False):
    """Recheck ownership and exact source account on every read and commit."""
    if not VibeRaisingCompany.objects.filter(pk=request["companyId"], organization_id=request["organizationId"],
            profile__user_id=request["userId"]).exists():
        raise NotFound("This startup is no longer available.")
    if StartupDataDeletionRequest.objects.filter(organization_id=request["organizationId"],
            provider="startup", delete_derived_data=True).filter(
                Q(status="deleting") | Q(started_at__gte=parse_datetime(request["period"]["as_of"]))
            ).exists():
        raise RevisionConflict("Startup data was deleted after this refresh began. Start a new refresh.")
    rows = ExternalServiceConnection.objects
    if lock:
        rows = rows.select_for_update()
    result = {}
    for provider, pin in request["connections"].items():
        if pin is None:
            continue
        connection = rows.filter(pk=pin["id"], organization_id=request["organizationId"],
            user_id=request["userId"], provider=provider).exclude(status="disconnected").first()
        if connection is None or not connection.access_token or connection_identity(connection) != pin:
            raise RevisionConflict("A financial connection changed. Refresh financials again after reconnecting it.")
        result[provider] = connection
    return result


def start_refresh(*, company, user, data):
    """Create one persisted scoped job; do not create or edit a Pulse revision."""
    from integrations.services.source_accounts import report_connection
    if company.organization_id is None:
        raise ValidationError("Add a startup domain and connect Xero before refreshing financials.")
    profile, _ = StartupProfile.objects.get_or_create(organization_id=company.organization_id)
    request = request_scope(company, user, data, profile)
    identity = content_hash({"company": str(company.pk), "user": user.pk, "request": request["requestId"]})
    run_id = f"pulse-financial-{identity}"
    with transaction.atomic():
        # The company row serializes admission without modifying prior jobs.
        get_object_or_404(VibeRaisingCompany.objects.select_for_update(), pk=company.pk,
            profile__user=user, organization_id=company.organization_id)
        existing = ContentFactoryRun.objects.filter(run_id=run_id, workflow=WORKFLOW).first()
        if existing:
            if _identity(existing.run_request) != _identity(request):
                raise RevisionConflict("This refresh request already belongs to different inputs. Start a new refresh.")
            run = existing
        else:
            _check_base(request)
            request["connections"] = {}
            sources = {}
            for provider in request["inputSources"]:
                queryset = ExternalServiceConnection.objects.filter(organization_id=company.organization_id,
                    user=user, provider=provider).exclude(status="disconnected")
                connection = report_connection(queryset, user=user, organization=company.organization, provider=provider)
                pin = connection_identity(connection) if connection and connection.access_token else None
                request["connections"][provider] = pin
                sources[provider] = {"provider": provider, "status": "pending" if pin else "reconnect_required"}
                if pin is None:
                    sources[provider]["detail"] = f"Reconnect {provider.title()} to refresh this source."
            recent = list(ContentFactoryRun.objects.select_for_update().filter(workflow=WORKFLOW,
                organization_id=company.organization_id, run_request__companyId=str(company.pk),
                run_request__userId=user.pk, created_at__gte=timezone.now() - CANDIDATE_TTL).order_by("-created_at")[:300])
            alias = next((item for item in recent if request["requestId"] in item.result.get("requestAliases", [])), None)
            if alias is not None and not _equivalent_scope(alias.run_request, request):
                raise RevisionConflict("This refresh request already belongs to different inputs. Start a new refresh.")
            run = alias or next((item for item in recent if item.status in {"queued", "running"}
                and _equivalent_scope(item.run_request, request)), None)
            if run is not None:
                aliases = run.result.setdefault("requestAliases", [])
                if request["requestId"] not in aliases:
                    aliases.append(request["requestId"])
                    run.save(update_fields=["result", "updated_at"])
            else:
                run = ContentFactoryRun.objects.create(run_id=run_id, workflow=WORKFLOW,
                    organization_id=company.organization_id, domain=company.organization.domain,
                    status="queued", run_request=request, current_step="financial_refresh",
                    result={"sources": sources, "months": [month.isoformat() for month in month_slots(date.fromisoformat(request["targetMonth"]))]})
        transaction.on_commit(lambda: submit_refresh(run.run_id), robust=True)
    return serialize_refresh(run)


def serialize_refresh(run):
    """Return only owner-preview DTOs; raw reports remain private server evidence."""
    result = run.result or {}
    candidate = result.get("candidate")
    state = ("partial" if any(source["status"] != "ready" for source in result.get("sources", {}).values()) else "ready") if candidate else run.status
    value = {"refreshId": run.run_id, "status": state if state in {"queued", "running", "ready", "partial", "failed"} else "failed",
        "targetMonth": run.run_request.get("targetMonth"), "sources": list(result.get("sources", {}).values()),
        "candidate": copy.deepcopy(candidate)}
    if result.get("detail"):
        value["detail"] = result["detail"]
    if run.status in {"queued", "running"}:
        retry_at = parse_datetime(result.get("retryAt") or "")
        value["retryAfterSeconds"] = max(2, int((retry_at - timezone.now()).total_seconds())) if retry_at else 2
    return value


def refresh_status(*, company, user, refresh_id):
    """Read one owned job and wake durable queued work after process restarts."""
    run = get_object_or_404(ContentFactoryRun, run_id=refresh_id, workflow=WORKFLOW,
        organization_id=company.organization_id, run_request__companyId=str(company.pk), run_request__userId=user.pk)
    _connections(run.run_request)
    if run.status in {"queued", "running"}:
        submit_refresh(run.run_id)
    return serialize_refresh(run)


def submit_refresh(refresh_id):
    """Bound local work; persisted stages and fenced leases allow poll recovery."""
    if not _SLOTS.acquire(blocking=False):
        return False
    def work():
        close_old_connections()
        try:
            # Enough for the six reports plus bounded invoice pages. A later
            # poll resumes a long collection without repeating completed reads.
            for _ in range(64):
                if not advance_refresh(refresh_id):
                    break
        except Exception as exc:
            # Leave the saved lease for safe polling recovery. Provider bodies
            # and source report contents must never enter application logs.
            logger.warning("Pulse financial refresh interrupted (%s)", type(exc).__name__)
        finally:
            close_old_connections()
            _SLOTS.release()
    try:
        _POOL.submit(work)
    except RuntimeError:
        _SLOTS.release()
        return False
    return True


def _finish(run, progress):
    request = run.run_request
    # With Xero selected the chart is the accounting chart, so it must refresh.
    # Without Xero, Stripe alone supplies it.
    primary = progress["sources"].get("xero") or progress["sources"].get("stripe")
    if primary["status"] != "ready":
        run.status = "failed"
        progress["detail"] = (primary["detail"] if primary.get("reason") == "rate_limited" else
            "The financial chart could not refresh. Your saved chart and writing are unchanged.")
        return
    captured = timezone.now()
    payload = build_candidate_evidence(request, progress)
    payload["financial_refresh"]["capturedAt"] = captured.isoformat()
    chart = build_snapshot(payload, date.fromisoformat(request["targetMonth"]), captured_at=captured.isoformat())
    snapshot, _ = MonthlyEvidenceSnapshot.objects.get_or_create(organization_id=request["organizationId"],
        content_hash=content_hash(payload), defaults={"month": request["targetMonth"], "payload": payload})
    progress["snapshotId"], progress["snapshotHash"] = snapshot.pk, snapshot.content_hash
    progress["chart"] = chart
    display = financial_display(request["organizationId"])
    progress["candidate"] = {"id": run.run_id, "expiresAt": (captured + CANDIDATE_TTL).isoformat(),
        "ownerPreview": history_payload({"enabled": True, "shareAmounts": True, "snapshot": chart, "display": display}),
        "audiencePreview": history_payload({"enabled": True, "shareAmounts": False, "snapshot": chart, "display": display})}
    run.status = "completed"


def advance_refresh(refresh_id):
    """Claim one read, perform I/O without row locks, and commit only its fence."""
    now, token = timezone.now(), uuid4().hex
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=refresh_id, workflow=WORKFLOW).first()
        if run is None or run.status not in {"queued", "running"}:
            return False
        progress = copy.deepcopy(run.result)
        if run.created_at + CANDIDATE_TTL <= now:
            run.status, progress["detail"] = "failed", "This refresh expired. Start a new financial refresh."
            run.result = progress
            run.save(update_fields=["status", "result", "updated_at"])
            return False
        for key in ("leaseUntil", "retryAt"):
            until = parse_datetime(progress.get(key) or "")
            if until and until > now:
                return False
        try:
            connections = _connections(run.run_request)
        except (NotFound, RevisionConflict):
            run.status, progress["detail"] = "failed", "A financial connection changed. Reconnect it and refresh again."
            run.result = progress
            run.save(update_fields=["status", "result", "updated_at"])
            return False
        stage = next_stage(progress, run.run_request["inputSources"])
        if progress["sources"].get("xero", {}).get("status") in {"failed", "reconnect_required"}:
            # The accounting chart needs Xero, so no chart can be saved from this
            # refresh. Say why on Stripe rather than leaving a bare "Waiting"
            # (the status stays one that every client version understands).
            stripe = progress["sources"].get("stripe")
            if stripe and stripe.get("status") not in {"ready", "failed", "reconnect_required"}:
                progress["sources"]["stripe"] = {"provider": "stripe", "status": "pending",
                    "detail": "Stripe refreshes together with Xero. Try again once Xero is available."}
            stage = None
        if stage is None:
            try:
                _finish(run, progress)
            except ValueError:
                run.status, progress["detail"] = "failed", "No compatible verified figures were returned. Your saved chart is unchanged."
            run.result = progress
            run.save(update_fields=["status", "result", "updated_at"])
            return False
        provider, step = stage
        progress["sources"][provider] = {"provider": provider, "status": "refreshing"}
        progress.update(leaseToken=token, leaseUntil=(now + timedelta(seconds=LEASE_SECONDS)).isoformat())
        run.status, run.result = "running", progress
        run.save(update_fields=["status", "result", "updated_at"])
        request = copy.deepcopy(run.run_request)
    error, receipt = None, None
    try:
        receipt = fetch_stage(provider, step, connections[provider], request, progress)
    except Exception as exc:
        error = exc
    with transaction.atomic():
        run = ContentFactoryRun.objects.select_for_update().filter(run_id=refresh_id, workflow=WORKFLOW).first()
        if run is None or run.result.get("leaseToken") != token or run.status != "running":
            return False
        progress = copy.deepcopy(run.result)
        progress.pop("leaseToken", None)
        progress.pop("leaseUntil", None)
        try:
            _connections(request, lock=True)
        except (NotFound, RevisionConflict):
            run.status, progress["detail"] = "failed", "A financial connection changed. Reconnect it and refresh again."
        else:
            if error is None:
                # Retries are per read: a recovered read must not leave a stale
                # deadline (which stopped the worker after every later stage) or
                # spend the next read's attempts.
                progress.pop("attempts", None)
                progress.pop("retryAt", None)
                progress = apply_stage(progress, provider, step, receipt)
            else:
                failure, retryable, delay = failure_details(error, provider, tz_name=request.get("timezone"))
                attempts = int(progress.get("attempts", 0)) + 1
                if retryable and attempts < 3:
                    retry_at = timezone.now() + timedelta(seconds=delay)
                    progress.update(attempts=attempts, retryAt=retry_at.isoformat())
                    progress["sources"][provider]["detail"] = (
                        f"Waiting to retry {provider.title()} at {local_time(retry_at, request.get('timezone'))}. "
                        "Your writing is safe.")
                else:
                    progress["sources"][provider] = failure
                    progress.pop("attempts", None)
                    progress.pop("retryAt", None)
            run.status = "queued"
        run.result = progress
        run.save(update_fields=["status", "result", "updated_at"])
    return run.status == "queued" and not progress.get("retryAt")


def candidate_for_save(candidate_id, *, company, user, draft, expected_revision, input_sources=None):
    """Resolve the exact preview within the normal save transaction, never refetch."""
    run = get_object_or_404(ContentFactoryRun.objects.select_for_update(), run_id=candidate_id, workflow=WORKFLOW,
        organization_id=draft.organization_id, run_request__companyId=str(company.pk), run_request__userId=user.pk)
    request, result = run.run_request, run.result
    candidate = result.get("candidate") or {}
    expiry = parse_datetime(candidate.get("expiresAt") or "")
    if run.status != "completed" or not expiry or expiry <= timezone.now():
        raise RevisionConflict("This financial preview expired or is incomplete. Refresh financials again.")
    expected_id = request.get("updateId")
    same_draft = expected_id == draft.pk if expected_id else str(draft.creation_key or "") == request.get("creationKey")
    if not same_draft or request["targetMonth"] != draft.month.isoformat() or request["expectedRevision"] != expected_revision:
        raise RevisionConflict("This financial preview belongs to an earlier update or revision. Refresh financials again.")
    if input_sources is not None and sorted(set(input_sources) & {"xero", "stripe"}) != request["inputSources"]:
        raise RevisionConflict("Financial source selection changed. Refresh financials again.")
    profile = StartupProfile.objects.get(organization_id=draft.organization_id)
    if (profile.reporting_timezone, profile.default_currency, profile.reporting_config_version) != (
            request["timezone"], request["currency"], request["configVersion"]):
        raise RevisionConflict("Reporting settings changed. Refresh financials again.")
    _connections(request, lock=True)
    snapshot = get_object_or_404(MonthlyEvidenceSnapshot, pk=result.get("snapshotId"),
        organization_id=draft.organization_id, month=draft.month, content_hash=result.get("snapshotHash"))
    if content_hash(snapshot.payload) != snapshot.content_hash:
        raise RevisionConflict("This financial preview could not be verified. Refresh financials again.")
    return snapshot
