"""Exact commands for local library writes and their bounded external preparation."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import secrets
from functools import wraps

from django.contrib.auth import get_user_model
from django.db import transaction
from rest_framework.response import Response

from .first_article_commands import identity
from .founder_run_commands import CommandRefused, _authorized, _response

KEY = "founder_library_commands_v1"
_active = ContextVar("founder_library_command", default=None)
AUTHOR_FIELDS = {"companyId", "authors", "authorsData", "defaultAuthorId", "default_author_id", "operationId"}


def settings_candidate(request):
    """Do not change general settings validation or first-domain creation."""
    header = getattr(request, "headers", {}).get("Idempotency-Key")
    if header is None:
        return False
    if {"authors", "authorsData"} & request.data.keys() and set(request.data) <= AUTHOR_FIELDS:
        return True
    from .content_first_article import FirstArticleRefusal
    try:
        digest, _ = identity(header, {})
    except FirstArticleRefusal:
        return False
    from . import vibe_marketing_views as views
    from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
    from .models import OrganizationContentConfig
    company_id = views._company_id_from_request(request)
    if not company_id:
        company_id = VibeRaisingProfile.objects.filter(user=request.user).values_list("active_company_id", flat=True).first()
    if company_id:
        from uuid import UUID
        try:
            company_id = UUID(str(company_id))
        except ValueError:
            return False
    organization_id = VibeRaisingCompany.objects.filter(pk=company_id, profile__user=request.user).values_list("organization_id", flat=True).first()
    strategy = OrganizationContentConfig.objects.filter(organization_id=organization_id).values_list("pillar_strategy", flat=True).first() or {}
    saved = strategy.get(KEY, {})
    return not isinstance(saved, dict) or digest in saved


def applies(request, context, *, authors=False):
    """Only explicit keyed new-journey calls enter the durable boundary."""
    from .founder_journey_cohort import is_new_journey
    from .models import OrganizationContentConfig
    header = getattr(request, "headers", {}).get("Idempotency-Key")
    if _active.get() is not None or header is None:
        return False
    from .content_first_article import FirstArticleRefusal
    try:
        digest, _ = identity(header, {})
    except FirstArticleRefusal:
        digest = None
    strategy = OrganizationContentConfig.objects.filter(organization=context.organization).values_list("pillar_strategy", flat=True).first() or {}
    saved = strategy.get(KEY, {})
    retained = not isinstance(saved, dict) or digest is not None and digest in saved
    return retained or (is_new_journey(context.organization)
        and (not authors or bool({"authors", "authorsData"} & request.data.keys())
            and set(request.data) <= AUTHOR_FIELDS))


def _principal(context, user, action, roster):
    principal = _authorized(context, user, "cancel", roster)
    if action == "authors":
        from .content_first_article import require_owner, FirstArticleRefusal
        try:
            require_owner(get_user_model().objects.get(pk=user.pk), context.organization)
        except FirstArticleRefusal as exc:
            raise CommandRefused("permission_changed", 403) from exc
    return principal


def _save(config, digest, receipt):
    config.refresh_from_db()
    strategy = deepcopy(config.pillar_strategy or {})
    strategy[KEY] = {**strategy.get(KEY, {}), digest: receipt}
    config.pillar_strategy = strategy
    config.save(update_fields=["pillar_strategy", "updated_at"])


def _expected(request, context, action, resource, principal):
    from .content_first_article import FirstArticleRefusal
    try:
        digest, body = identity(request.headers["Idempotency-Key"], request.data)
    except FirstArticleRefusal as exc:
        raise CommandRefused("invalid_key", 400) from exc
    return digest, {"schema_version": 1, "company_id": str(context.company.pk), "principal": principal,
        "action": action, "resource": str(resource), "body_sha256": body}


def _existing(config, digest, expected):
    commands = (config.pillar_strategy or {}).get(KEY, {})
    if not isinstance(commands, dict):
        raise CommandRefused()
    receipt = commands.get(digest)
    if receipt is not None:
        if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
            raise CommandRefused()
    elif len(commands) >= 1024:
        raise CommandRefused("command_capacity")
    return deepcopy(receipt)


def _project(context, request, receipt):
    from . import vibe_marketing_views as views
    if receipt["action"] == "authors":
        return Response(views._serialize_bootstrap(context, request=request), status=200)
    outcome = receipt["outcome"]
    if receipt["action"] == "discard":
        return Response(deepcopy(outcome), status=200)
    from .models import TopicFeedback
    from .topic_feedback import serialize_topic_feedback
    feedback = TopicFeedback.objects.filter(pk=outcome["id"], organization=context.organization).first()
    if feedback is None:
        return Response({"detail": "Topic feedback not found."}, status=404)
    return Response({**serialize_topic_feedback(feedback), **{k: v for k, v in outcome.items() if k != "id"}},
        status=receipt["status"])


def local(*, request, context, action, resource, invoke):
    """Commit local SQL and its minimal receipt in one startup transaction."""
    from founder_tools import startup_team
    try:
        with startup_team._locked(context.organization) as (config, roster):
            principal = _principal(context, request.user, action, roster)
            digest, expected = _expected(request, context, action, resource, principal)
            receipt = _existing(config, digest, expected)
            if receipt is not None and receipt.get("state") != "accepted":
                raise CommandRefused("command_pending", 503)
            if receipt is None:
                token = _active.set({"local": True})
                try:
                    result = invoke()
                finally:
                    _active.reset(token)
                if not 200 <= result.status_code < 300:
                    transaction.set_rollback(True)
                    return result
                outcome = {k: result.data[k] for k in ("id", "created", "restored") if k in result.data}
                receipt = {**expected, "state": "accepted", "status": result.status_code, "outcome": outcome}
                _save(config, digest, receipt)
        return _project(context, request, receipt)
    except CommandRefused as exc:
        return _response(exc)


def _article_binding(article):
    return {k: str(getattr(article, k)) for k in ("pk", "organization_id", "slug", "title", "primary_keyword", "source_run_id")}


def _discard_subject(claim, receipt):
    from .models import WrittenArticle
    article = WrittenArticle.objects.select_for_update().filter(pk=receipt["resource"],
        organization=claim["context"].organization).first()
    if article is None or _article_binding(article) != receipt["article"]:
        raise CommandRefused()
    return article


def _claim(request, context, action, resource):
    from founder_tools import startup_team
    from . import vibe_marketing_views as views
    from .website_cancellation_retirement import run_binding
    with startup_team._locked(context.organization) as (config, roster):
        principal = _principal(context, request.user, action, roster)
        digest, expected = _expected(request, context, action, resource, principal)
        receipt = _existing(config, digest, expected)
        if receipt and receipt.get("state") == "accepted":
            return None, receipt
        from .founder_journey_cohort import is_new_journey
        if not is_new_journey(context.organization):
            raise CommandRefused("journey_changed")
        if receipt and receipt.get("state") != "ready":
            raise CommandRefused("command_pending", 503)
        prepared = {}
        if action == "discard":
            article = views.WrittenArticle.objects.select_for_update().filter(pk=resource, organization=context.organization).first()
            if article is None:
                raise CommandRefused("article_not_found", 404)
            if receipt:
                prepared = {"article": receipt["article"], "targets": receipt["targets"]}
                if prepared["article"] != _article_binding(article):
                    raise CommandRefused()
            else:
                members = views._written_article_lineage_members(article, context.organization)
                prepared = {"article": _article_binding(article), "targets": [
                    {"run_pk": r.pk, "run_id": r.run_id, "binding": run_binding(r)} for r in members]}
            if len(prepared["targets"]) > 200 or any(not row["binding"] for row in prepared["targets"]):
                raise CommandRefused()
        receipt = {**expected, **prepared, "state": "claimed", "token": secrets.token_hex(16)}
        _save(config, digest, receipt)
        return {"context": context, "user": request.user, "digest": digest, "receipt": receipt,
            "submitted": set(), "acknowledged": set()}, receipt


@contextmanager
def _locked(claim):
    from founder_tools import startup_team
    with startup_team._locked(claim["context"].organization) as (config, roster):
        original = claim["receipt"]
        if _principal(claim["context"], claim["user"], original["action"], roster) != original["principal"]:
            raise CommandRefused("permission_changed", 403)
        receipt = deepcopy((config.pillar_strategy or {}).get(KEY, {}).get(claim["digest"]))
        if not isinstance(receipt, dict) or receipt.get("token") != original["token"]:
            raise CommandRefused()
        yield config, receipt


def _target(claim, receipt, run_id):
    from .founder_run_commands import _source
    target = next((row for row in receipt["targets"] if row["run_id"] == run_id), None)
    if target is None:
        raise CommandRefused()
    return _source(claim["context"], target["run_pk"], target["binding"])[0]


def discard_members():
    claim = _active.get()
    if not claim or claim.get("local") or claim["receipt"]["action"] != "discard":
        return None
    with _locked(claim) as (_config, receipt):
        _discard_subject(claim, receipt)
        return [_target(claim, receipt, r["run_id"]) for r in receipt["targets"]]


def before_transport(*, run_id, action, payload):
    """Retain uncertainty before each bound discard cancellation."""
    claim = _active.get()
    if not claim or claim.get("local") or claim["receipt"]["action"] != "discard":
        return
    from .run_reconciliation import authoritative_run
    from .run_ownership import require_event_identity, require_event_execution
    with _locked(claim) as (config, receipt):
        _discard_subject(claim, receipt)
        run = _target(claim, receipt, run_id)
        if action != "cancel" or receipt["state"] not in {"claimed", "dispatching"} or run_id in claim["submitted"]:
            raise CommandRefused()
        if authoritative_run(run):
            try:
                require_event_identity(run, payload)
                require_event_execution(run, payload, current=True)
            except ValueError as exc:
                raise CommandRefused() from exc
        receipt["state"] = "dispatching"
        _save(config, claim["digest"], receipt)
        claim["submitted"].add(run_id)


def discard_cancel_response(run_id, data):
    claim = _active.get()
    if not claim or claim.get("local") or claim["receipt"]["action"] != "discard":
        return
    with _locked(claim) as (_config, receipt):
        run = _target(claim, receipt, run_id)
        from .run_ownership import WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, require_event_identity, require_event_execution
        try:
            rejected = int(data.get("content_factory_status_code") or 0) == 409
            accepted = (data.get("run_id") == run_id and data.get("status") == "cancelled"
                and not data.get("error") and data.get("accepted") is not False and data.get("allowed") is not False)
            if not (rejected or accepted) or run_id not in claim["submitted"]:
                raise ValueError("Unknown cancellation")
            if WIRE_REQUEST_KEY in data or WIRE_EXECUTION_KEY in data:
                require_event_identity(run, data)
                require_event_execution(run, data, current=True)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CommandRefused("command_pending", 503) from exc
        claim["acknowledged"].add(run_id)


@contextmanager
def local_commit(action):
    """Include the accepted command in the same transaction as the local effect."""
    claim = _active.get()
    if not claim or claim.get("local") or claim["receipt"]["action"] != action:
        with transaction.atomic():
            yield
        return
    with _locked(claim) as (config, receipt):
        if action == "discard":
            article = _discard_subject(claim, receipt)
            if article.publish_status in {"pr_open", "merged", "live"} or claim["submitted"] != claim["acknowledged"]:
                raise CommandRefused()
            for target in receipt["targets"]:
                _target(claim, receipt, target["run_id"])
        claim["commit"] = (config, receipt)
        try:
            yield
        finally:
            claim.pop("commit", None)


def accept_local(action, outcome=None):
    claim = _active.get()
    if not claim or claim.get("local") or claim["receipt"]["action"] != action:
        return
    config, receipt = claim["commit"]
    receipt["state"] = "accepted"
    if outcome is not None:
        receipt["outcome"] = deepcopy(outcome)
    _save(config, claim["digest"], receipt)


def atomic_author_settings(function):
    """Save the author command receipt atomically with existing profile writes."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        claim = _active.get()
        if not claim or claim.get("local") or claim["receipt"]["action"] != "authors":
            return function(*args, **kwargs)
        with local_commit("authors"):
            result = function(*args, **kwargs)
            if not isinstance(result, Response):
                accept_local("authors")
            return result
    return wrapped


def before_author_photos():
    claim = _active.get()
    if not claim or claim.get("local") or claim["receipt"]["action"] != "authors":
        return
    with _locked(claim) as (config, receipt):
        receipt["state"] = "dispatching"
        _save(config, claim["digest"], receipt)


def external(*, request, context, action, resource, invoke):
    """Prepare once, atomically accept local changes, then project current state."""
    from founder_tools import startup_team
    claim = None
    try:
        claim, receipt = _claim(request, context, action, resource)
        if claim is None:
            return _project(context, request, receipt)
        token = _active.set(claim)
        try:
            result = invoke()
            if 200 <= result.status_code < 300:
                with _locked(claim) as (_config, current):
                    if current["state"] != "accepted":
                        raise CommandRefused("command_pending", 503)
            return result
        finally:
            _active.reset(token)
            # No started external operation is released by elapsed time/error.
            with startup_team._locked(context.organization) as (config, _roster):
                current = deepcopy((config.pillar_strategy or {}).get(KEY, {}).get(claim["digest"]))
                if current and current.get("token") == receipt["token"] and current.get("state") == "claimed":
                    current["state"] = "ready"
                    _save(config, claim["digest"], current)
    except CommandRefused as exc:
        return _response(exc)
