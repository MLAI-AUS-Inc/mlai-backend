"""Durable consent to exact native CMS bytes, with fresh private evidence."""

from copy import deepcopy
from datetime import datetime, timedelta
import re
from uuid import UUID, uuid4

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from . import cms_artifact_review as reviews
from . import cms_staged_review as stages
from . import cms_publication_policy as policies
from .channel_artifacts import enabled
from .channel_artifact_contract import canonical_bytes, digest
from .cms_api_transport import CmsApiError
from .cms_owned_media import completed_media, request_public_recheck
from .cms_owner_connection import _owner
from .website_contract import WebsiteAuthorityError, connection_contract

ACTION = "cms-approval"
LEASE_SECONDS = 90
IDENTITY = {"schema_version", "binding", "credential_operation_id", "stage_operation_id", "staged_revision",
    "policy_operation_id", "policy_digest", "basis", "owner_user_id", "billing_digest", "native_fields_sha256",
    "source_handoff_digest", "requested_at"}


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The exact CMS article, owner consent or payment changed.")


def _time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise _deny()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0) or parsed > timezone.now() + timedelta(seconds=30):
        raise _deny()
    return value


def _identity(stage, source, value, policy, *, basis, requested_at):
    if (basis not in {"owner_final_review", "owner_setup_auto"} or
            policy["policy"]["review_policy"] != ("auto" if basis == "owner_setup_auto" else "review")):
        raise _deny()
    return {"schema_version": 1, "binding": source.admitted.scope.binding,
        "credential_operation_id": source.admitted.operation.payload["credential_operation_id"],
        "stage_operation_id": str(stage.pk), "staged_revision": value["revision"],
        "policy_operation_id": policy["operation_id"], "policy_digest": policy["policy"]["digest"], "basis": basis,
        "owner_user_id": str(source.admitted.scope.user.pk), "billing_digest": source.billing["digest"],
        "native_fields_sha256": digest(policies.native_post_fields(policy["policy"])),
        "source_handoff_digest": source.handoff["digest"], "requested_at": _time(requested_at)}


def _current(operation, *, lease_id=None, fresh_media=False):
    from .website_models import WebsiteConnectionOperation
    try:
        payload = operation.payload if operation is not None else None
        if (not enabled() or operation is None or operation.action != ACTION or operation.state not in {"pending", "running", "completed"} or
                not isinstance(payload, dict) or set(payload) - {"lease_id"} != IDENTITY | {"failures"} or
                type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
                type(payload["failures"]) is not int or not 0 <= payload["failures"] <= 5 or
                operation.state == "completed" and "lease_id" in payload or
                lease_id is not None and (operation.state != "running" or payload.get("lease_id") != lease_id)):
            raise _deny()
        stage = WebsiteConnectionOperation.objects.filter(pk=payload["stage_operation_id"], action=stages.ACTION).first()
        source, value, media = stages.current_stage(stage, fresh_media=fresh_media, allow_media_recheck=not fresh_media)
        policy = policies.for_article(source)
        expected = _identity(stage, source, value, policy, basis=payload["basis"], requested_at=payload["requested_at"])
        if (canonical_bytes({key: payload[key] for key in IDENTITY}) != canonical_bytes(expected) or
                str(operation.connection_id) != expected["binding"]["connection_id"] or operation.generation != expected["binding"]["generation"]):
            raise _deny()
        return stage, source, value, media, policy
    except (WebsiteAuthorityError, CmsApiError):
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def _receipt(operation, value, *, approved_at):
    payload = operation.payload
    result = {"schema_version": 1, "kind": "cms_publication_approval", "approval_operation_id": str(operation.pk),
        "intent_sha256": digest({key: payload[key] for key in IDENTITY}), "staged_revision": value["revision"],
        "html_sha256": value["article"]["profile"]["html_sha256"], "title_sha256": value["article"]["profile"]["title_sha256"],
        "preview_sha256": value["preview_sha256"], "envelope_digest": value["envelope_digest"],
        "source_handoff_digest": value["source_handoff_digest"], "billing_digest": value["billing_digest"],
        "policy_operation_id": payload["policy_operation_id"], "policy_digest": payload["policy_digest"],
        "native_fields_sha256": payload["native_fields_sha256"], "basis": payload["basis"], "approved_at": _time(approved_at),
        "approval_recorded": True, "provider_modified": False, "publishing_verified": False}
    return {**result, "digest": digest(result)}


def current_approval(operation, *, fresh_media=True):
    """Reload private consent, original spend, policy and current exact article.

    A copied receipt or an owner/worker JSON flag is never publication authority.
    Historical reads are available for status only; publication uses the default.
    """
    stage, source, value, media, policy = _current(operation, fresh_media=fresh_media)
    try:
        if (operation.state != "completed" or not isinstance(operation.receipt, dict) or
                canonical_bytes(operation.receipt) != canonical_bytes(_receipt(operation, value, approved_at=operation.receipt["approved_at"]))):
            raise _deny()
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
    return source, value, policy, deepcopy(operation.receipt)


def _start(stage, *, basis, expected_revision):
    if not enabled():
        raise _deny()
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    source, _value, _media = stages.current_stage(stage, fresh_media=False, allow_media_recheck=True)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.run.organization_id)
        stage = WebsiteConnectionOperation.objects.select_for_update().filter(pk=stage.pk, action=stages.ACTION).first()
        source, value, _media = stages.current_stage(stage, fresh_media=False, allow_media_recheck=True)
        if not isinstance(expected_revision, str) or expected_revision != value["revision"]:
            raise _deny()
        policy = policies.for_article(source)
        identity = _identity(stage, source, value, policy, basis=basis, requested_at=timezone.now().isoformat())
        operation, _created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key="cms-approval:" + value["revision"] + ":" + policy["policy"]["digest"], defaults={
                "action": ACTION, "connection": source.admitted.scope.connection, "generation": source.admitted.scope.binding["generation"],
                "state": "pending", "next_attempt_at": timezone.now(), "payload": {**identity, "failures": 0},
                "receipt": {"status": "approval_pending", "approval_recorded": False, "provider_modified": False}})
        _current(operation)
        if operation.state == "completed":
            current_approval(operation, fresh_media=False)
        return operation


def owner_stage(context, config, *, user, data):
    """Scope the selected company before looking up any private article preview."""
    if not enabled():
        raise _deny()
    for item in (context.organization, context.profile, context.company, user, config): item.refresh_from_db()
    _owner(context, config, user)
    binding = policies.validate_current(context, config, config.website_connection, user=user)
    expected = connection_contract(data)
    if (expected.get("website_connection_id") != binding["connection_id"] or expected.get("connection_generation") != binding["generation"] or
            expected.get("repository_id") is not None or expected.get("connection_target_id")):
        raise _deny()
    try:
        identifier = data.get("stage_id")
        selected = {}
        if identifier is not None:
            if not isinstance(identifier, str) or str(UUID(identifier)) != identifier:
                raise _deny()
            selected["pk"] = identifier
        else:
            run_id = data.get("source_run_id")
            if not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id) is None:
                raise _deny()
            selected["payload__staged__source_run_id"] = run_id
        from .website_models import WebsiteConnectionOperation
        rows = WebsiteConnectionOperation.objects.filter(action=stages.ACTION,
            connection_id=binding["connection_id"], generation=binding["generation"], **selected)
        stage = rows.order_by("-created_at").first() if identifier is None else rows.first()
        source, value, _media = stages.current_stage(stage, fresh_media=False, allow_media_recheck=True)
        if source.admitted.scope.binding != binding:
            raise _deny()
        policies.for_article(source)
        return stage, value
    except (ValueError, TypeError, KeyError, AttributeError):
        raise _deny() from None


def projection(stage, value, operation=None):
    """Return inert final preview and bounded status, never private provider data."""
    return {"stageId": str(stage.pk), "revision": value["revision"], "title": value["article"]["title"],
        "previewHtml": value["preview_html"], "status": operation.state if operation else "review_required",
        "approvalRecorded": bool(operation and operation.state == "completed")}


def review(context, config, *, user, data):
    """Read the selected original run's private final preview and consent status."""
    stage, value = owner_stage(context, config, user=user, data=data)
    from .website_models import WebsiteConnectionOperation
    operation = WebsiteConnectionOperation.objects.filter(action=ACTION, connection_id=stage.connection_id,
        generation=stage.generation, payload__stage_operation_id=str(stage.pk)).first()
    if operation is not None and operation.state == "completed":
        current_approval(operation, fresh_media=False)
    return projection(stage, value, operation)


def approve(context, config, *, user, data):
    """Accept one explicit owner choice for precisely the displayed final revision."""
    if data.get("publication_confirmed") is not True:
        raise WebsiteAuthorityError("website_prepare_blocked", "Confirm publication of this exact article.", status=422)
    if not isinstance(data.get("stage_id"), str):
        raise _deny()
    stage, value = owner_stage(context, config, user=user, data=data)
    operation = _start(stage, basis="owner_final_review", expected_revision=data.get("expected_revision"))
    return projection(stage, value, operation)


def _ready(media, *, now):
    try:
        completed_media(media, now=now)
        return True
    except CmsApiError:
        request_public_recheck(media, now=now)
        return False
    except WebsiteAuthorityError:
        # Only an already validated historical recheck can wait here.
        completed_media(media, fresh=False, allow_rechecking=True)
        return False


def advance_approval(identifier, *, now=None):
    """Resume choice once proofs refresh; no model, billing or provider writes."""
    if not enabled():
        return None
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None or candidate.state not in {"pending", "running"}:
        return candidate
    now, lease, claimed = now or timezone.now(), str(uuid4()), False
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if operation is None or operation.state not in {"pending", "running"} or operation.next_attempt_at and operation.next_attempt_at > now:
                return operation
            _current(operation)
            operation.payload = {**deepcopy(operation.payload), "lease_id": lease}
            operation.state, operation.next_attempt_at = "running", now + timedelta(seconds=LEASE_SECONDS)
            operation.attempts += 1
            operation.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
            claimed = True
        operation.refresh_from_db()
        _stage, source, _value, media, _policy = _current(operation, lease_id=lease)
        waiting = not _ready(media, now=now)
        observed, failure = None, None
        if not waiting:
            try:
                observed = reviews.fetch_handoff(source.run, expected_revision=source.handoff["review_revision"])
            except reviews.ArtifactReadError as exc:
                failure = exc.category
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=source.run.organization_id)
            saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if saved is None or saved.state != "running" or saved.payload.get("lease_id") != lease:
                return saved
            _stage, source, value, media, _policy = _current(saved, lease_id=lease)
            if observed is not None and canonical_bytes(observed) != canonical_bytes(source.handoff):
                raise _deny()
            waiting = waiting or not _ready(media, now=timezone.now())
            saved.payload = deepcopy(saved.payload)
            if waiting:
                saved.state, saved.next_attempt_at = "pending", now + timedelta(seconds=15)
                saved.receipt = {"status": "approval_waiting_media", "approval_recorded": False, "provider_modified": False}
            elif failure is not None:
                from .website_failure_contract import classify_failure
                failures = saved.payload["failures"] + 1
                saved.payload["failures"] = failures
                retry = failure == "unavailable" and failures < 5
                classified = classify_failure("CONNECTION_SERVICE_TRANSIENT" if failure == "unavailable" else "PORTABLE_PACKAGE_DRIFT", "cms_approval")
                saved.state, saved.next_attempt_at = ("pending", now + timedelta(seconds=15 * 2 ** (failures - 1))) if retry else ("blocked", None)
                saved.receipt = {"status": "approval_pending" if retry else "approval_blocked", "failure": classified,
                    "failure_class": classified["failure_class"], "approval_recorded": False, "provider_modified": False}
            else:
                saved.state, saved.next_attempt_at = "completed", None
                saved.receipt = _receipt(saved, value, approved_at=timezone.now().isoformat())
            saved.payload.pop("lease_id", None)
            saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            return saved
    except (WebsiteAuthorityError, CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"state": candidate.state, "updated_at": candidate.updated_at}
        WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).update(state="cancelled", next_attempt_at=None,
            receipt={"status": "approval_source_changed", "approval_recorded": False, "provider_modified": False}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()


def ensure_approvals(*, limit=20, connection_id=None):
    """Only original private setup consent can automatically approve a stage."""
    if not enabled():
        return 0
    from .website_models import WebsiteConnectionOperation
    existing = WebsiteConnectionOperation.objects.filter(action=ACTION, payload__staged_revision=OuterRef("payload__staged__revision"))
    parents = WebsiteConnectionOperation.objects.filter(action=stages.ACTION, state="completed", connection__state="connected", payload__review_policy="auto")
    if connection_id:
        parents = parents.filter(connection_id=connection_id)
    parents = parents.annotate(_approval_exists=Exists(existing)).filter(_approval_exists=False)
    count = 0
    for stage in parents.order_by("created_at")[:limit]:
        try:
            source, value, _media = stages.current_stage(stage, fresh_media=False, allow_media_recheck=True)
            if policies.for_article(source)["policy"]["review_policy"] != "auto":
                continue
            _start(stage, basis="owner_setup_auto", expected_revision=value["revision"])
            count += 1
        except (WebsiteAuthorityError, CmsApiError):
            continue
    return count
