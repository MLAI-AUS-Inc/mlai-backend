"""Exact HTTP identities for direct canonical article-review commands.

Only command acceptance is recorded here. CF events retain ownership of run
lifecycle, delivery, execution budgets and settlement. An uncertain external
effect is never inferred absent from a clock or a newer review snapshot.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
import re
import secrets

from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from rest_framework.response import Response

KEY = "founder_review_commands_v1"
MAX_COMMANDS = 1024
ACTIONS = frozenset({"editText", "editTextBatch", "regenerateImage", "chooseImage",
    "refresh", "undo", "discardImage", "allowAI"})
_active = ContextVar("founder_review_http_command", default=None)


class ReviewCommandRefusal(ValueError):
    """A command cannot acquire or reuse the saved review authority."""

    def __init__(self, reason="operation_conflict", status=409):
        self.reason, self.status = reason, status
        super().__init__("Your original article request is still being confirmed."
            if reason == "in_progress" else "Review the current article before trying again.")


def _digest(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False, cls=DjangoJSONEncoder)
    except (TypeError, ValueError):
        raise ReviewCommandRefusal()
    return hashlib.sha256(raw.encode()).hexdigest()


def _identity(context, user, run, data, header, *, action=None):
    operation = data.get("operationId")
    action = action or data.get("action")
    if (type(header) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", header)
            or operation != header or action not in {*ACTIONS, "comment"}):
        raise ReviewCommandRefusal()
    return hashlib.sha256(header.encode()).hexdigest(), {
        "company_id": str(context.company.pk), "user_id": user.pk,
        "run_id": run.run_id, "action": action, "body_sha256": _digest(data),
    }


def _binding(run):
    from .run_ownership import owner_receipt, current_execution_ticket, wire_identity
    receipt = owner_receipt(run)
    if receipt is not None:
        return _digest({"admission": wire_identity(receipt), "execution": current_execution_ticket(run)})
    return _digest({"run_id": run.run_id, "organization_id": str(run.organization_id),
        "workflow": run.workflow, "domain": run.domain, "request": run.run_request})


def _authorize(context, user, run):
    """Recheck a fresh current user and company under the organization lock."""
    from django.contrib.auth import get_user_model
    from founder_tools.models import VibeRaisingCompany
    from .run_revision_child_admission import require_revision_requester
    from .run_ownership import user_stop_intent
    from workflow_runs.models import ContentFactoryRun
    fresh_user = get_user_model().objects.filter(pk=getattr(user, "pk", None), is_active=True).first()
    fresh_run = ContentFactoryRun.objects.filter(pk=run.pk, organization_id=context.organization.pk).first()
    if (fresh_user is None or fresh_run is None or fresh_run.run_id != run.run_id
            or fresh_run.domain != context.organization.domain
            or not VibeRaisingCompany.objects.filter(pk=context.company.pk,
                organization_id=context.organization.pk).exists()):
        raise ReviewCommandRefusal("permission_denied", 403)
    try:
        require_revision_requester(fresh_run, fresh_user)
        if user_stop_intent(fresh_run):
            raise ReviewCommandRefusal()
    except ReviewCommandRefusal:
        raise
    except (ValueError, PermissionError):
        raise ReviewCommandRefusal("permission_denied", 403)
    return fresh_user, fresh_run


def _valid(record, identity):
    return (isinstance(record, dict) and set(record) == {"schema_version", "identity", "binding",
        "state", "lease_token", "lease_until", "response_status", "response_sha256"}
        and type(record["schema_version"]) is int and record["schema_version"] == 1
        and record["identity"] == identity and isinstance(record["identity"], dict)
        and type(record["identity"].get("user_id")) is int
        and isinstance(record["binding"], str) and re.fullmatch("[a-f0-9]{64}", record["binding"])
        and type(record["state"]) is str and record["state"] in {"reserved", "dispatching", "accepted", "rejected", "unknown"}
        and isinstance(record["lease_token"], str) and re.fullmatch("[a-f0-9]{32}", record["lease_token"])
        and isinstance(record["lease_until"], str)
        and (record["response_status"] is None or type(record["response_status"]) is int)
        and (record["response_sha256"] is None or isinstance(record["response_sha256"], str)
            and re.fullmatch("[a-f0-9]{64}", record["response_sha256"])))


def _strategy(config):
    value = config.pillar_strategy
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ReviewCommandRefusal()
    return deepcopy(value)


def _claim(context, user, run, identity, slot, *, allow_new):
    from founder_tools import startup_team
    with startup_team._locked(context.organization) as (config, _):
        _, current = _authorize(context, user, run)
        strategy = _strategy(config)
        records = strategy.get(KEY, {})
        if not isinstance(records, dict):
            raise ReviewCommandRefusal()
        record = records.get(slot)
        binding = _binding(current)
        if record is not None:
            if not _valid(record, identity) or record["binding"] != binding:
                raise ReviewCommandRefusal()
            if record["state"] == "accepted":
                if record["response_status"] not in {200, 201, 202, 204} or record["response_sha256"] is None:
                    raise ReviewCommandRefusal()
                return None, record["response_status"]
            if record["state"] == "rejected":
                raise ReviewCommandRefusal()
            # Dispatching and unknown commands never become safe to resend by age.
            if record["state"] != "reserved":
                raise ReviewCommandRefusal("in_progress")
            try:
                live = timezone.datetime.fromisoformat(record["lease_until"]) > timezone.now()
            except (TypeError, ValueError):
                raise ReviewCommandRefusal()
            if live or not allow_new:
                raise ReviewCommandRefusal("in_progress")
        else:
            if not allow_new or len(records) >= MAX_COMMANDS:
                raise ReviewCommandRefusal()
            record = {"schema_version": 1, "identity": identity, "binding": binding,
                "state": "reserved", "response_status": None, "response_sha256": None}
        record = {**record, "lease_token": secrets.token_hex(16),
            "lease_until": (timezone.now() + timedelta(seconds=120)).isoformat()}
        strategy[KEY] = {**records, slot: record}
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])
        return deepcopy(record), None


def _dispatch(context, user, run, slot, original):
    from founder_tools import startup_team
    with startup_team._locked(context.organization) as (config, _):
        _, current = _authorize(context, user, run)
        strategy = _strategy(config)
        records = strategy.get(KEY)
        record = records.get(slot) if isinstance(records, dict) else None
        if record != original or _binding(current) != original["binding"]:
            raise ReviewCommandRefusal()
        record["state"] = "dispatching"
        strategy[KEY] = records
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])


def require_current_effect(run):
    """Fence the original command again immediately before external review IO."""
    active = _active.get()
    if active is None:
        return
    context, user, saved_run, _, original = active
    if saved_run.pk != run.pk:
        raise ReviewCommandRefusal()
    from founder_tools import startup_team
    with startup_team._locked(context.organization):
        _, current = _authorize(context, user, run)
        if _binding(current) != original["binding"]:
            raise ReviewCommandRefusal()


def refresh_resume_required(run):
    """Keep keyed preview refresh from implicitly granting another execution."""
    if _active.get() is None:
        return None
    require_current_effect(run)
    return Response({"code": "operation_conflict", "reason": "operation_conflict",
        "detail": "Resume the original execution before refreshing this preview."}, status=409)


@contextmanager
def image_debit_guard(run):
    """Keep current review permission locked through an image debit."""
    active = _active.get()
    if active is None:
        yield
        return
    from founder_tools import startup_team
    with startup_team._locked(active[0].organization):
        require_current_effect(run)
        yield


def _finish(context, slot, original, response):
    from founder_tools import startup_team
    status = getattr(response, "status_code", None)
    accepted = type(status) is int and 200 <= status < 300
    rejected = type(status) is int and 400 <= status < 500 and status not in {402, 429}
    digest = _digest(response.data) if accepted else None
    with startup_team._locked(context.organization) as (config, _):
        strategy = _strategy(config)
        records = strategy.get(KEY)
        record = records.get(slot) if isinstance(records, dict) else None
        if (not _valid(record, original["identity"]) or record["state"] != "dispatching"
                or record.get("lease_token") != original["lease_token"] or record.get("binding") != original["binding"]):
            raise ReviewCommandRefusal()
        # A local insufficient-wallet refusal made no image allocation or HTTP call.
        state = "accepted" if accepted else "rejected" if rejected else "reserved" if status == 402 else "unknown"
        records[slot] = {**record, "state": state, "lease_until": timezone.now().isoformat(),
            "response_status": status if type(status) is int else None, "response_sha256": digest}
        strategy[KEY] = records
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])
    if state == "unknown":
        from .founder_journey_alerts import queue_founder_journey_alert
        queue_founder_journey_alert(organization=context.organization, alert_reason="our_team_item",
            company_id=context.company.pk, run=_run_for_alert(context, original), slot="review-" + slot,
            details={"command": original["identity"]["action"], "outcome": "unknown"})


def _run_for_alert(context, original):
    from workflow_runs.models import ContentFactoryRun
    return ContentFactoryRun.objects.filter(organization_id=context.organization.pk,
        run_id=original["identity"]["run_id"]).first()


def _retains_identity(context, data, header):
    """A malformed retry cannot escape an already sealed command's boundary."""
    candidates = [header]
    if isinstance(data, dict):
        candidates.extend((data.get("operationId"), data.get("operation_id")))
    candidates = [value for value in candidates if type(value) is str]
    if not candidates:
        return False
    from .models import OrganizationContentConfig
    strategy = OrganizationContentConfig.objects.filter(organization=context.organization).values_list("pillar_strategy", flat=True).first()
    records = strategy.get(KEY) if isinstance(strategy, dict) else None
    return isinstance(records, dict) and any(hashlib.sha256(value.encode()).hexdigest() in records for value in candidates)


def _conflict_response():
    return Response({"code": "operation_conflict", "reason": "operation_conflict",
        "detail": "Review the original command before trying again."}, status=409)


def execute(*, request, context, run, invoke, readback):
    """Replay only durable acceptance; uncertain operations cannot repeat effects."""
    data = request.data
    header = getattr(request, "headers", {}).get("Idempotency-Key")
    if not isinstance(data, dict) or data.get("action") not in ACTIONS or header is None:
        return _conflict_response() if _retains_identity(context, data, header) else invoke()
    from .founder_journey_cohort import is_new_journey
    from .models import OrganizationContentConfig
    allow_new = is_new_journey(context.organization)
    if not allow_new:
        strategy = OrganizationContentConfig.objects.filter(organization=context.organization).values_list("pillar_strategy", flat=True).first()
        if not isinstance(strategy, dict) or KEY not in strategy:
            return invoke()
    claim = None
    slot = None
    try:
        slot, identity = _identity(context, request.user, run, data, header)
        claim, replay_status = _claim(context, request.user, run, identity, slot, allow_new=allow_new)
        if replay_status is not None:
            # Current authorized data is a projection; the saved command proves acceptance.
            response = readback()
            if response.status_code < 300:
                response.status_code = replay_status
            return response
        _dispatch(context, request.user, run, slot, claim)
        token = _active.set((context, request.user, run, slot, claim))
        response = None
        try:
            response = invoke()
            return response
        finally:
            _active.reset(token)
            _finish(context, slot, claim, response)
    except ReviewCommandRefusal as exc:
        return Response({"code": "operation_conflict", "reason": exc.reason, "detail": str(exc)}, status=exc.status)


def execute_comment(*, request, context, run, invoke):
    """Atomically seal a v2 comment's exact body and original local row.

    The first revision read happens outside SQL locks. Acceptance and the local
    comment commit together; a replay can never recreate a deleted comment.
    """
    data = request.data
    header = getattr(request, "headers", {}).get("Idempotency-Key")
    if not isinstance(data, dict) or header is None or "expectedRevision" not in data:
        return _conflict_response() if _retains_identity(context, data, header) else invoke()
    from founder_tools import startup_team
    from .founder_journey_cohort import is_new_journey
    from .models import OrganizationContentConfig, VibeMarketingComponentComment
    from . import vibe_marketing_views as views
    from uuid import NAMESPACE_URL, uuid5
    allow_new = is_new_journey(context.organization)
    strategy = OrganizationContentConfig.objects.filter(organization=context.organization).values_list("pillar_strategy", flat=True).first()
    if not allow_new and (not isinstance(strategy, dict) or KEY not in strategy):
        return invoke()
    try:
        slot, identity = _identity(context, request.user, run, data, header, action="comment")
        expected = data.get("expectedRevision")
        if type(expected) is not str or not 0 < len(expected) <= 200:
            raise ReviewCommandRefusal()
        comment_id = uuid5(NAMESPACE_URL, f"review-comment:{run.run_id}:{request.user.pk}:{header}")

        def retained(record, binding):
            if (not _valid(record, identity) or record["binding"] != binding
                    or record["state"] != "accepted" or record["response_status"] != 201
                    or record["response_sha256"] is None):
                raise ReviewCommandRefusal()
            comment = VibeMarketingComponentComment.objects.filter(
                pk=comment_id, run=run, actor_id=request.user.pk).first()
            if comment is None:
                return Response({"detail": "The original comment is no longer available."}, status=404)
            return Response(views._serialize_component_comment(comment), status=200)

        with startup_team._locked(context.organization) as (config, _):
            _, current = _authorize(context, request.user, run)
            binding = _binding(current)
            records = _strategy(config).get(KEY, {})
            if not isinstance(records, dict):
                raise ReviewCommandRefusal()
            if slot in records:
                return retained(records[slot], binding)
            if not allow_new or len(records) >= MAX_COMMANDS:
                raise ReviewCommandRefusal()
        from .article_review_views import remote_review
        snapshot = remote_review(current)
        if isinstance(snapshot, Response):
            return snapshot
        if not isinstance(snapshot, dict) or snapshot.get("revision") != expected:
            return Response({"code": "revision_changed", "detail": "Review the current draft before commenting."}, status=409)
        with startup_team._locked(context.organization) as (config, _):
            _, current = _authorize(context, request.user, run)
            if _binding(current) != binding:
                raise ReviewCommandRefusal()
            strategy = _strategy(config)
            records = strategy.get(KEY, {})
            if not isinstance(records, dict):
                raise ReviewCommandRefusal()
            if slot in records:
                return retained(records[slot], binding)
            if not is_new_journey(context.organization) or len(records) >= MAX_COMMANDS:
                raise ReviewCommandRefusal()
            if (views._latest_review_ready_component_revision(current, context) is not None
                    or (current.result or {}).get("article_review_approval")
                    or views._run_has_external_publish_evidence(current)
                    or snapshot.get("approved") is True or snapshot.get("readOnly") is True):
                raise ReviewCommandRefusal()
            # Never adopt an unsealed historical row, even with a matching UUID.
            if VibeMarketingComponentComment.objects.filter(pk=comment_id).exists():
                raise ReviewCommandRefusal()
            response = invoke()
            if response.status_code != 201:
                return response
            records[slot] = {"schema_version": 1, "identity": identity, "binding": binding,
                "state": "accepted", "lease_token": secrets.token_hex(16),
                "lease_until": timezone.now().isoformat(), "response_status": 201,
                "response_sha256": _digest(response.data)}
            strategy[KEY] = records
            config.pillar_strategy = strategy
            config.save(update_fields=["pillar_strategy", "updated_at"])
            return response
    except ReviewCommandRefusal as exc:
        return Response({"code": "operation_conflict", "reason": exc.reason, "detail": str(exc)}, status=exc.status)
