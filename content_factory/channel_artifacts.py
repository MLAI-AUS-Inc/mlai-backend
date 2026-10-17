"""Backend-owned artifact reservations; worker envelopes never grant authority."""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.conf import settings
from django.db import transaction

from .channel_artifact_contract import ArtifactContractError, digest, make_envelope, validate_envelope
from .cms_connection_authority import validate_current
from .cms_operation_scope import current_scope
from .website_contract import WebsiteAuthorityError, CONNECTION_FIELDS
from .website_publishing_channels import saved_record, scope_for

ACTION = "cms-artifact"
ENVELOPE = "publishing_channel_artifact"
_IDENTITY = ("domain", "topic", "target_keyword", "custom_title", "topic_candidate_id", "topic_selection", "source_run_id",
    "author_id", "default_author_name", "default_author_profile", "editorial_brief", "editorial_admission",
    "analytics_article_id", "analytics_config", "request_source", ENVELOPE)
_GIT = (*CONNECTION_FIELDS, "github_token", "expected_source_sha", "source_sha", "repo_head_sha", "app_root", "branch")


def enabled():
    """Require all existing CMS gates plus an independent artifact rollout."""
    from .cms_connection_authority import enabled as cms_enabled
    return cms_enabled() and getattr(settings, "ARTICLES_CHANNEL_ARTIFACTS_ENABLED", False)


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The original article owner, CMS, policy or paid reservation changed.")


def _policy(context, config):
    record = saved_record(config.article_system)
    if (record is None or record["scope"] != scope_for(context, config) or
            not record["selection"] or record["selection"]["channel"] != "cms"):
        raise _deny()
    # CMS auto-publication consent is not implemented yet. Existing Git auto
    # approval, worker booleans and a saved auto_publish preference cannot
    # silently become CMS publication approval.
    return {"schema_version": 1, "channel_choice": record, "review_policy": "review",
        "auto_publish_preference": config.auto_publish is True}


@dataclass(frozen=True)
class GenerationScope:
    """A private controller argument, not accepted from owner/worker JSON."""

    binding: dict = field(repr=False)
    credential_operation_id: str = field(repr=False)
    policy: dict = field(repr=False)


def prepare_generation(context, config, *, user):
    """Check original current consent before entering the canonical billing path."""
    if not enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS article artifacts are not available.")
    binding = validate_current(context, config, config.website_connection, user=user)
    reference = (config.website_connection.capabilities or {}).get("_cms_credential_operation_id")
    try:
        if not isinstance(reference, str) or str(UUID(reference)) != reference:
            raise ValueError("Invalid private reference")
    except (ValueError, TypeError, AttributeError):
        raise _deny() from None
    return GenerationScope(deepcopy(binding), reference, _policy(context, config))


def _request_identity(payload):
    return digest({key: payload.get(key) for key in _IDENTITY})


def _artifact_request(payload, envelope):
    if (not isinstance(payload, dict) or validate_envelope(payload.get(ENVELOPE)) != envelope or
            payload.get("client_request_id") != envelope["billing_request_id"] or
            payload.get("domain", "").lower().removeprefix("www.") != envelope["public_origin"].split("/")[2].removeprefix("www.") or
            payload.get("delivery_mode") != "content_only" or payload.get("delivery_mode_confirmed") is not True or
            payload.get("github_repo") or any(payload.get(key) not in (None, "") for key in _GIT)):
        raise _deny()


def reserve_generation(scope, *, context, config, user, payload):
    """Enlist private CMS intent in the same transaction as the spend and outbox.

    The caller must roll back this transaction on an admission/charge failure.
    No provider credential or transferable publishing approval enters the worker
    request. Reusing a billing key requires the exact original artifact intent.
    """
    if not enabled() or not isinstance(scope, GenerationScope) or not transaction.get_connection().in_atomic_block:
        raise _deny()
    from organizations.models import Organization
    from .website_models import WebsiteConnection, WebsiteConnectionOperation
    Organization.objects.select_for_update().get(pk=context.organization.pk)
    WebsiteConnection.objects.select_for_update().get(pk=scope.binding["connection_id"])
    for value in (context.organization, context.company, context.profile, user, config):
        value.refresh_from_db()
    fresh = prepare_generation(context, config, user=user)
    if fresh != scope:
        raise _deny()
    from .cms_connection_authority import current_credential
    current_credential(context, config, config.website_connection, user=user)
    key = f"cms-artifact:{scope.binding['connection_id']}:{payload.get('client_request_id', '')}"
    if len(key) > 255:
        key = "cms-artifact:" + digest(key)
    existing = WebsiteConnectionOperation.objects.filter(idempotency_key=key).first()
    operation_id = str(existing.pk) if existing else str(uuid4())
    binding = scope.binding
    envelope = make_envelope(channel="cms", platform=binding["platform"], artifact_operation_id=operation_id,
        company_id=binding["company_id"], organization_id=binding["organization_id"], owner_user_id=binding["owner_user_id"],
        connection_id=binding["connection_id"], connection_generation=binding["generation"],
        public_origin=binding["public_origin"], scope_sha256=digest(binding), billing_request_id=payload["client_request_id"],
        review_policy="review", policy_sha256=digest(scope.policy))
    if ENVELOPE in payload and payload[ENVELOPE] != envelope:
        raise _deny()
    payload[ENVELOPE] = envelope
    _artifact_request(payload, envelope)
    private = {"schema_version": 1, "binding": deepcopy(binding), "credential_operation_id": scope.credential_operation_id,
        "envelope": envelope, "policy": deepcopy(scope.policy), "request_sha256": _request_identity(payload)}
    operation, created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key=key, defaults={
        "pk": operation_id, "connection": config.website_connection, "generation": binding["generation"],
        "action": ACTION, "state": "pending", "payload": private, "receipt": {}})
    if (operation.action != ACTION or operation.state not in {"pending", "running"} or
            operation.payload != private or operation.generation != binding["generation"] or
            str(operation.connection_id) != binding["connection_id"]):
        raise _deny()
    return operation


def authorize_payload(payload, *, require_outbox=True):
    """Recheck private original identity and current consent, without CMS HTTP."""
    if not enabled():
        raise WebsiteAuthorityError("publishing_adapter_required", "CMS article artifacts are disabled.")
    try:
        envelope = validate_envelope(payload.get(ENVELOPE))
        if envelope["channel"] != "cms":
            raise _deny()
        from .website_models import WebsiteConnectionOperation
        operation = WebsiteConnectionOperation.objects.filter(pk=envelope["artifact_operation_id"], action=ACTION).first()
        if operation is None:
            raise _deny()
        private = operation.payload if isinstance(operation.payload, dict) else {}
        if (set(private) != {"schema_version", "binding", "credential_operation_id", "envelope", "policy", "request_sha256"} or
                type(private["schema_version"]) is not int or private["schema_version"] != 1 or
                private["envelope"] != envelope or digest(private["binding"]) != envelope["scope_sha256"] or
                digest(private["policy"]) != envelope["policy_sha256"]):
            raise _deny()
        _artifact_request(payload, envelope)
        if private["request_sha256"] != _request_identity(payload):
            raise _deny()
        scope = current_scope(operation)
        if scope.binding != private["binding"] or _policy(scope.context, scope.config) != private["policy"]:
            raise _deny()
        from .cms_connection_authority import current_credential
        current_credential(scope.context, scope.config, scope.connection, user=scope.user)
        if require_outbox:
            from .dispatch_models import ContentFactoryDispatchOutbox
            row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=envelope["billing_request_id"],
                organization_id=envelope["organization_id"]).first()
            saved = row.payload.get("request") if row and isinstance(row.payload, dict) else None
            if (row is None or row.state in {"failed", "cancelled", "refund_pending"} or
                    not isinstance(saved, dict) or saved.get(ENVELOPE) != envelope or
                    _request_identity(saved) != private["request_sha256"] or
                    any(saved.get(key) != payload.get(key) for key in set(saved) | set(payload) if key.startswith("roo_points_")) or
                    saved.get("roo_points_cost", 0) and not saved.get("roo_points_ledger_id")):
                raise _deny()
        return SimpleNamespace(operation=operation, envelope=envelope, scope=scope)
    except WebsiteAuthorityError:
        raise
    except (ArtifactContractError, ValueError, TypeError, KeyError, AttributeError):
        raise _deny() from None


def admission_receipt(query):
    """Resolve the original outbox request; incoming values can only select it."""
    from .dispatch_models import ContentFactoryDispatchOutbox
    row = ContentFactoryDispatchOutbox.objects.filter(client_request_id=query.get("billing_request_id")).first()
    payload = row.payload.get("request") if row and isinstance(row.payload, dict) else None
    if not isinstance(payload, dict):
        raise _deny()
    authorized = authorize_payload(payload)
    envelope = authorized.envelope
    if (query.get("artifact_operation_id") != envelope["artifact_operation_id"] or
            query.get("envelope_digest") != envelope["digest"] or query.get("domain") != payload.get("domain")):
        raise _deny()
    from workflow_runs.models import ContentFactoryRun
    runs = ContentFactoryRun.objects.filter(organization_id=envelope["organization_id"],
        run_request__client_request_id=envelope["billing_request_id"])
    if runs.filter(status__in=["cancelled", "denied"]).exists():
        raise _deny()
    return {"allowed": True, "permission_mode": "none", "capabilities": {}, ENVELOPE: deepcopy(envelope)}


def artifact_run_update_allowed(run, payload, *, event_type=""):
    """Accept artifacts only from original durable consent, never approval flags."""
    if run is None or getattr(run, "status", "") in {"cancelled", "denied"} or not isinstance(payload, dict):
        return False
    original = getattr(run, "run_request", None)
    if not isinstance(original, dict) or ENVELOPE not in original:
        return False
    try:
        authorized = authorize_payload(original)
        if str(getattr(run, "organization_id", "")) != authorized.envelope["organization_id"]:
            return False
        incoming = payload.get("run_request")
        if incoming is not None and (not isinstance(incoming, dict) or ENVELOPE in incoming and incoming[ENVELOPE] != authorized.envelope):
            return False
        if isinstance(incoming, dict) and any(incoming[key] != value for key, value in original.items() if key in incoming):
            return False
        cleaned = deepcopy(payload)
        if isinstance(cleaned.get("run_request"), dict):
            cleaned["run_request"].pop(ENVELOPE, None)
        forbidden = {ENVELOPE, "approved", "auto_approved", "auto_publish", "cms_publish_approval", "channel_approval",
            "sealed_credential", "application_password", "integration_key", "secret", "_cms_binding"}
        def safe(value):
            if isinstance(value, dict):
                return all(not (re.sub(r"(?<!^)(?=[A-Z])", "_", str(key)).lower() in forbidden and item not in (None, "", False, [], {})) and safe(item) for key, item in value.items())
            if isinstance(value, list):
                return all(safe(item) for item in value)
            return True
        if not safe(cleaned):
            return False
        # Reuse the mature artifact-only recursive checks without treating this
        # CMS run as an unbound portable run anywhere in the application.
        from .portable_drafts import portable_run_update_allowed
        surrogate = SimpleNamespace(run_id=run.run_id, domain=run.domain, workflow=run.workflow, github_repo="",
            run_request={key: value for key, value in original.items() if key != ENVELOPE})
        return portable_run_update_allowed(surrogate, cleaned, event_type=event_type)
    except (WebsiteAuthorityError, ArtifactContractError, ValueError, TypeError, AttributeError):
        return False


def artifact_run_control_allowed(run, action, payload):
    """Editorial recovery reuses paid intent; CMS approval has its own boundary."""
    return action in {"resume", "preview"} and artifact_run_update_allowed(run, payload)


def callback_candidate(run_id, payload):
    """Find backend intent before taking the organization then run locks."""
    from workflow_runs.models import ContentFactoryRun
    original = ContentFactoryRun.objects.filter(run_id=run_id).first() if run_id else None
    if original is not None and ENVELOPE in (original.run_request or {}):
        return original
    request = payload.get("run_request")
    if isinstance(request, dict) and ENVELOPE in request:
        key = request.get("client_request_id")
        candidate = ContentFactoryRun.objects.filter(run_id=key).first() if key else None
        if candidate is None or ENVELOPE not in (candidate.run_request or {}):
            raise _deny()
        return candidate
    return None


@contextmanager
def artifact_write_guard(payload):
    """Hold the original organization consent fence through local persistence."""
    from organizations.models import Organization
    try:
        envelope = validate_envelope(payload.get(ENVELOPE))
    except (ArtifactContractError, ValueError, TypeError, AttributeError):
        raise _deny() from None
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=envelope["organization_id"])
        yield authorize_payload(payload)
