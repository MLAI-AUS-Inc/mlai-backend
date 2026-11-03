"""Durable HTTP command receipts; a reply never becomes execution authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import secrets

from django.contrib.auth import get_user_model
from rest_framework.response import Response

from .first_article_commands import identity

KEY = "founder_run_commands_v1"
MAX_COMMANDS = 1024
_active = ContextVar("founder_run_command", default=None)


class CommandRefused(ValueError):
    def __init__(self, code="operation_conflict", status=409):
        self.code, self.status = code, status
        super().__init__(code)


def applies(run, action, header):
    """Leave unkeyed controls, legacy runs and other actions on their old path."""
    from .run_reconciliation import authoritative_run
    from .run_state import ARTICLE_WORKFLOWS
    return header is not None and action in {"approve", "deny", "cancel"} and run.workflow in ARTICLE_WORKFLOWS and authoritative_run(run)


def _response(exc):
    detail = ("Your original command is still being confirmed. Refresh this article."
        if exc.code == "command_pending" else "Review the current article and startup permission before continuing.")
    return Response({"detail": detail, "code": exc.code}, status=exc.status)


def _authorized(context, user, action, roster):
    from .run_revision_child_admission import _principal, _require_rights
    current = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    try:
        principal = _principal(current)
        _require_rights(current, context.organization, roster, publish=action == "approve")
    except ValueError as exc:
        raise CommandRefused("permission_changed", 403) from exc
    return principal


def _source(context, run_pk, binding=None):
    from workflow_runs.models import ContentFactoryRun
    from .website_cancellation_retirement import run_binding
    run = ContentFactoryRun.objects.select_for_update().filter(pk=run_pk, organization=context.organization).first()
    current = run_binding(run) if run else None
    if not current or binding is not None and current != binding:
        raise CommandRefused()
    return run, current


def _save(config, strategy):
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


def _claim(context, user, run, action, data, header):
    from founder_tools import startup_team
    from .content_first_article import FirstArticleRefusal
    try:
        digest, body_hash = identity(header, data)
    except FirstArticleRefusal as exc:
        raise CommandRefused("invalid_key", 400) from exc
    if not isinstance(data, dict):
        raise CommandRefused("invalid_command", 400)
    group = bool(data.get("cancel_group") or data.get("cancelGroup"))
    if group and action != "cancel":
        raise CommandRefused("invalid_command", 400)
    with startup_team._locked(context.organization) as (config, roster):
        principal = _authorized(context, user, action, roster)
        current_run, binding = _source(context, run.pk)
        if current_run.run_id != run.run_id:
            raise CommandRefused()
        expected = {"schema_version": 1, "company_id": str(context.company.pk), "principal": principal,
            "run_pk": run.pk, "run_id": run.run_id, "binding": binding, "action": action, "body_sha256": body_hash}
        strategy = deepcopy(config.pillar_strategy or {})
        commands = strategy.get(KEY, {})
        if not isinstance(commands, dict):
            raise CommandRefused()
        receipt = commands.get(digest)
        if receipt is not None:
            if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in expected.items()):
                raise CommandRefused()
            if group:
                for target in receipt.get("targets", []):
                    _source(context, target["run_pk"], target["binding"])
            if receipt.get("state") == "accepted":
                return None, current_run, deepcopy(receipt.get("outcome", {}))
            # There is deliberately no clock expiry on an unknown external effect.
            if receipt.get("state") != "ready":
                raise CommandRefused("command_pending", 503)
        elif len(commands) >= MAX_COMMANDS:
            raise CommandRefused("command_capacity", 409)
        revision = data.get("reviewedRevision")
        if action == "approve" and (not isinstance(revision, str) or not revision):
            raise CommandRefused("revision_required", 400)
        if revision is not None and revision != (current_run.result.get("articleExport") or {}).get("revision"):
            raise CommandRefused("revision_changed", 409)
        targets = None
        if group:
            from . import vibe_marketing_views as views
            from .website_cancellation_retirement import run_binding
            targets = receipt.get("targets") if receipt else None
            if targets is None:
                targets = [{"run_pk": member.pk, "run_id": member.run_id, "binding": run_binding(member),
                    "pull_requests": list(views._draft_pull_request_urls(member))}
                    for member in views._article_draft_group_members(current_run, context.organization)]
            if (not targets or len(targets) > 200 or any(not target["binding"] for target in targets)
                    or not any(target["run_pk"] == run.pk for target in targets)):
                raise CommandRefused()
        receipt = {**expected, "state": "claimed", "token": secrets.token_hex(16),
            **({"targets": targets} if group else {})}
        strategy[KEY] = {**commands, digest: receipt}
        _save(config, strategy)
        return {"context": context, "user": user, "digest": digest, "receipt": receipt,
            "data": deepcopy(data), "transport_started": False, "acknowledged": False,
            "submitted": set(), "acknowledged_targets": set()}, current_run, {}


@contextmanager
def _locked(claim, *, authorize=True):
    from founder_tools import startup_team
    context, original = claim["context"], claim["receipt"]
    with startup_team._locked(context.organization) as (config, roster):
        if authorize and _authorized(context, claim["user"], original["action"], roster) != original["principal"]:
            raise CommandRefused("permission_changed", 403)
        strategy = deepcopy(config.pillar_strategy or {})
        receipt = strategy.get(KEY, {}).get(claim["digest"])
        if not isinstance(receipt, dict) or receipt.get("token") != original["token"]:
            raise CommandRefused()
        run, _ = _source(context, original["run_pk"], original["binding"])
        yield config, strategy, receipt, run


def before_transport(*, run_id, action, payload):
    """Fence the exact outgoing ticket and persist uncertainty before the POST."""
    claim = _active.get()
    if claim is None:
        return
    from .run_ownership import require_event_identity, require_event_execution
    with _locked(claim) as (config, strategy, receipt, run):
        group = "targets" in receipt
        if (receipt["state"] != ("dispatching" if group else "claimed") or action != receipt["action"]
                or not group and run_id != receipt["run_id"] or run_id in claim["submitted"]):
            raise CommandRefused()
        if group:
            run = _group_target(claim, receipt, run_id)
        try:
            from .run_reconciliation import authoritative_run
            if authoritative_run(run):
                require_event_identity(run, payload)
                require_event_execution(run, payload, current=True)
        except ValueError as exc:
            raise CommandRefused() from exc
        receipt["state"] = "dispatching"
        _save(config, strategy)
        claim["transport_started"] = True
        claim["submitted"].add(run_id)


def _group_target(claim, receipt, run_id):
    target = next((item for item in receipt["targets"] if item["run_id"] == run_id), None)
    if target is None:
        raise CommandRefused()
    return _source(claim["context"], target["run_pk"], target["binding"])[0]


def group_members():
    """Use only the group captured by the original command, never newer siblings."""
    claim = _active.get()
    if claim is None or "targets" not in claim["receipt"]:
        return None
    with _locked(claim) as (_config, _strategy, receipt, _run):
        return [_group_target(claim, receipt, target["run_id"]) for target in receipt["targets"]]


def begin_group():
    """Persist uncertainty before cleanup, hiding, or any group cancellation."""
    claim = _active.get()
    if claim is None or "targets" not in claim["receipt"]:
        return
    with _locked(claim) as (config, strategy, receipt, _run):
        if receipt["state"] != "claimed":
            raise CommandRefused()
        receipt["state"] = "dispatching"
        _save(config, strategy)
        claim["transport_started"] = True


def before_group_cleanup(run_id, url):
    """A group command cannot close a newly discovered or unrelated PR."""
    claim = _active.get()
    if claim is None or "targets" not in claim["receipt"]:
        return
    with _locked(claim) as (_config, _strategy, receipt, _run):
        _group_target(claim, receipt, run_id)
        target = next(item for item in receipt["targets"] if item["run_id"] == run_id)
        if receipt["state"] != "dispatching" or url not in target["pull_requests"]:
            raise CommandRefused()


@contextmanager
def group_projection(run_id):
    """Recheck current rights before a group writes local cancellation metadata."""
    from django.db import transaction
    claim = _active.get()
    if claim is None or "targets" not in claim["receipt"]:
        with transaction.atomic():
            yield
        return
    with _locked(claim) as (_config, _strategy, receipt, _run):
        _group_target(claim, receipt, run_id)
        yield


def validate_prior_approval(run):
    """An existing consent cannot hide a different keyed revision or waiver."""
    claim = _active.get()
    if claim is None or claim["receipt"]["action"] != "approve":
        return
    from .article_review_views import check_approval_comments
    if claim["data"].get("reviewedRevision") != (run.result.get("articleExport") or {}).get("revision"):
        raise CommandRefused("revision_changed", 409)
    if check_approval_comments(run, claim["data"]) is not None:
        raise CommandRefused("unresolved_comments", 409)


def observe_acknowledgement(*, run_id, action, data):
    """Remember only successful command ACKs, never financial or lifecycle proof."""
    claim = _active.get()
    if claim is None:
        return
    original = claim["receipt"]
    if (not isinstance(data, dict) or run_id not in claim["submitted"] or action != original["action"]):
        return
    if (data.get("run_id") != run_id or data.get("error") or data.get("accepted") is False
            or data.get("allowed") is False or data.get("action", action) != action):
        return
    try:
        if int(data.get("content_factory_status_code") or 0) >= 400:
            return
    except (ValueError, TypeError, OverflowError):
        return
    from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, require_event_identity, require_event_execution
    if WIRE_REQUEST_KEY in data or WIRE_EXECUTION_KEY in data:
        with _locked(claim) as (_config, _strategy, _receipt, run):
            if "targets" in original:
                run = _group_target(claim, original, run_id)
            try:
                require_event_identity(run, data)
                require_event_execution(run, data, current=True)
            except ValueError:
                return
    if action == "approve":
        claim["acknowledged"] = data.get("action") == action and data.get("accepted") is True
    else:
        # The existing deny protocol returns run_id/status; cancellation may
        # additionally carry closure proof, which only its settlement owner uses.
        claim["acknowledged"] = data.get("status") == {"deny": "denied", "cancel": "cancelled"}[action]
    if claim["acknowledged"]:
        claim["acknowledged_targets"].add(run_id)


def _finish(claim, response):
    accepted = 200 <= response.status_code < 300
    group = "targets" in claim["receipt"]
    if accepted and claim["transport_started"] and (
            claim["submitted"] != claim["acknowledged_targets"] if group else not claim["acknowledged"]):
        raise CommandRefused("command_pending", 503)
    with _locked(claim) as (config, strategy, receipt, _run):
        if accepted:
            receipt["state"] = "accepted"
            if group:
                allowed = {target["run_id"] for target in receipt["targets"]}
                outcome = {key: response.data.get(key, []) for key in ("cancelledRunIds", "protectedRunIds")}
                if any(not isinstance(value, list) or any(item not in allowed for item in value) for value in outcome.values()):
                    raise CommandRefused()
                receipt["outcome"] = outcome
        elif not claim["transport_started"]:
            receipt["state"] = "ready"
        _save(config, strategy)


def _release_unstarted(claim):
    """A local refusal may retry its identical body; an external effect may not."""
    if claim["transport_started"]:
        return
    from founder_tools import startup_team
    with startup_team._locked(claim["context"].organization) as (config, _roster):
        strategy = deepcopy(config.pillar_strategy or {})
        receipt = strategy.get(KEY, {}).get(claim["digest"])
        if (isinstance(receipt, dict) and receipt.get("token") == claim["receipt"]["token"]
                and receipt.get("state") == "claimed"):
            receipt["state"] = "ready"
            _save(config, strategy)


def execute(*, context, user, run, action, data, header, invoke):
    """Replay the original authorized Run without another external submission."""
    from . import vibe_marketing_views as views
    claim = None
    try:
        claim, current_run, outcome = _claim(context, user, run, action, data, header)
        if claim is None:
            return Response({**views._serialize_run(current_run, context=context), **outcome}, status=202)
        token = _active.set(claim)
        try:
            response = invoke(current_run)
            _finish(claim, response)
            return response
        finally:
            _active.reset(token)
            _release_unstarted(claim)
    except CommandRefused as exc:
        return _response(exc)
