"""Durable owned CMS media staging; no post creation or publication approval."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from . import cms_artifact_review as reviews
from . import cms_media_contract as journal
from .channel_artifacts import enabled
from .channel_artifact_contract import digest
from .cms_article_source import current_article
from .cms_api_transport import CmsApiError, request_json
from .cms_connection_authority import current_credential
from .cms_media_evidence import fetch_source_image, observe_owned_image
from .cms_media_transport import upload_image
from .website_contract import WebsiteAuthorityError

ACTION = "cms-media"
LEASE_SECONDS = 180
FIELDS = {"schema_version", "binding", "credential_operation_id", "source_review_operation_id", "handoff_digest",
    "billing_digest", "intents", "states", "failures"}


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The original CMS media, article or payment changed.")


def _current(operation, *, lease_id=None, allow_completed=False):
    from .website_models import WebsiteConnectionOperation
    payload = operation.payload
    if (not enabled() or operation is None or operation.action != ACTION or
            operation.state not in ({"pending", "running", "completed"} if allow_completed else {"pending", "running"}) or
            not isinstance(payload, dict) or set(payload) - {"lease_id"} != FIELDS or
            operation.state == "completed" and "lease_id" in payload or
            type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
            type(payload["failures"]) is not int or not 0 <= payload["failures"] <= 5 or
            lease_id is not None and (operation.state != "running" or payload.get("lease_id") != lease_id)):
        raise _deny()
    parent = WebsiteConnectionOperation.objects.filter(pk=payload["source_review_operation_id"], action=reviews.ACTION).first()
    source = current_article(parent)
    if (payload["binding"] != source.admitted.scope.binding or payload["credential_operation_id"] != source.admitted.operation.payload["credential_operation_id"] or
            str(operation.connection_id) != payload["binding"]["connection_id"] or operation.generation != payload["binding"]["generation"] or
            payload["handoff_digest"] != source.handoff["digest"] or payload["billing_digest"] != source.billing["digest"] or
            not isinstance(payload["intents"], list) or not isinstance(payload["states"], list) or
            len(payload["intents"]) != len(source.handoff["images"]) or len(payload["states"]) != len(payload["intents"])):
        raise _deny()
    for intent, state, asset in zip(payload["intents"], payload["states"], source.handoff["images"]):
        journal.validate_state(state, intent)
        if (intent["binding"] != payload["binding"] or intent["review_operation_id"] != str(parent.pk) or
                intent["handoff_digest"] != payload["handoff_digest"] or intent["asset"] != asset or
                intent["source_run_id"] != source.run.run_id or intent["source_generation"] != source.handoff["run_generation"] or
                intent["title"] != source.handoff["article"]["title"]):
            raise _deny()
    return source


def completed_media(operation, *, now=None, fresh=True):
    """Require current private ownership and fresh public bytes before final review."""
    source = _current(operation, allow_completed=True)
    if operation.state != "completed" or operation.receipt != _progress(operation):
        raise _deny()
    for intent, state in zip(operation.payload["intents"], operation.payload["states"]):
        if state["phase"] != "public_verified":
            raise _deny()
        journal.validate_state(state, intent, now=(now or timezone.now()) if fresh else None)
    return source


def request_public_recheck(operation, *, now=None):
    """Refresh existing public URLs on demand, preserving every upload allowance.

    Final review/publishing can request this when observations expired. It does
    no HTTP, starts no upload, and is not a periodic polling loop or approval.
    """
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    source = completed_media(operation, fresh=False)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.run.organization_id)
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation.pk, action=ACTION).first()
        completed_media(saved, fresh=False)
        try:
            completed_media(saved, now=now)
            return saved
        except CmsApiError:
            pass
        saved.payload = deepcopy(saved.payload)
        saved.payload["states"] = [{**state, "phase": "provider_ack", "evidence": None}
            for state in saved.payload["states"]]
        saved.payload["failures"] = 0
        saved.state, saved.next_attempt_at = "pending", now
        saved.receipt = _progress(saved)
        saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
        return saved


def start_media(review_operation):
    """Reserve all image slots together from a current private reviewed artifact."""
    if not enabled():
        raise _deny()
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    source = current_article(review_operation)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.run.organization_id)
        review_operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=review_operation.pk, action=reviews.ACTION).first()
        source = current_article(review_operation)
        intents = [journal.make_intent(binding=source.admitted.scope.binding, review_operation_id=str(review_operation.pk),
            handoff_digest=source.handoff["digest"], source_run_id=source.run.run_id, source_generation=source.handoff["run_generation"],
            asset=asset, upload_id=str(uuid4()), title=source.handoff["article"]["title"]) for asset in source.handoff["images"]]
        operation, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key="cms-media:" + source.handoff["digest"], defaults={"action": ACTION,
                "connection": source.admitted.scope.connection, "generation": source.admitted.scope.binding["generation"],
                "state": "pending", "next_attempt_at": timezone.now(), "payload": {"schema_version": 1,
                    "binding": source.admitted.scope.binding,
                    "credential_operation_id": source.admitted.operation.payload["credential_operation_id"],
                    "source_review_operation_id": str(review_operation.pk), "handoff_digest": source.handoff["digest"],
                    "billing_digest": source.billing["digest"], "intents": intents,
                    "states": [journal.new_state(intent) for intent in intents], "failures": 0},
                "receipt": {"status": "media_pending", "count": len(intents), "verified": 0,
                    "provider_modified": False, "approval_recorded": False}})
        if operation.state in {"pending", "running"}:
            _current(operation)
        elif (operation.action != ACTION or operation.payload.get("handoff_digest") != source.handoff["digest"] or
                operation.payload.get("billing_digest") != source.billing["digest"]):
            raise _deny()
        return operation


def _fresh(operation, lease):
    """Observe current CF revision outside locks, then recheck private scope."""
    source = _current(operation, lease_id=lease)
    observed = reviews.fetch_handoff(source.run, expected_revision=source.handoff["review_revision"])
    operation.refresh_from_db()
    current = _current(operation, lease_id=lease)
    if observed != current.handoff:
        raise _deny()
    return current


def _progress(operation):
    states = operation.payload["states"]
    issued = any(state["create_count"] for state in states)
    confirmed_write = any(state["create_count"] and state["acknowledgement"] is not None for state in states)
    modified = True if confirmed_write else None if issued else False
    verified = sum(state["phase"] == "public_verified" for state in states)
    return {"status": "media_verified" if verified == len(states) else "media_pending", "count": len(states),
        "verified": verified, "provider_modified": modified, "approval_recorded": False}


def advance_media(identifier, *, now=None):
    """Advance one image effect with a durable lease and at most one upload POST."""
    if not enabled():
        return None
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    from .website_connections import require_unlocked_remote_call
    candidate = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    if candidate is None or candidate.state not in {"pending", "running"}:
        return candidate
    now, lease, claimed = now or timezone.now(), str(uuid4()), False
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=candidate.connection.organization_id)
            operation = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if (operation is None or operation.state not in {"pending", "running"} or
                    operation.next_attempt_at and operation.next_attempt_at > now):
                return operation
            _current(operation)
            operation.payload = {**deepcopy(operation.payload), "lease_id": lease}
            operation.state, operation.next_attempt_at = "running", now + timedelta(seconds=LEASE_SECONDS)
            operation.attempts += 1
            operation.save(update_fields=["payload", "state", "next_attempt_at", "attempts", "updated_at"])
            claimed = True
        operation.refresh_from_db()
        source = _current(operation, lease_id=lease)
        index = next((i for i, state in enumerate(operation.payload["states"]) if state["phase"] != "public_verified"), None)
        updated, failure = None, None
        if index is not None:
            intent, state = deepcopy(operation.payload["intents"][index]), deepcopy(operation.payload["states"][index])
            phase = state["phase"]
            try:
                if source.admitted.scope.connection.capabilities.get("_cms_identity", {}).get("can_upload_media") is not True:
                    raise CmsApiError("media_permission_unavailable")
                if phase in {"reconcile_exhausted", "recovery_unsupported"}:
                    raise CmsApiError("media_recovery_unavailable")
                if phase == "upload_issued" and intent["binding"]["platform"] == "ghost":
                    updated = journal.ghost_unknown(intent, state)
                elif phase == "reserved" and intent["binding"]["platform"] == "ghost":
                    updated = journal.ghost_ready(intent, state)
                elif phase == "provider_ack":
                    source = _fresh(operation, lease)
                    require_unlocked_remote_call()
                    observed = observe_owned_image(intent["binding"], handoff_digest=intent["handoff_digest"], asset=intent["asset"],
                        acknowledgement=journal.acknowledgement(state["acknowledgement"], intent))
                    updated = journal.record_public(intent, state, observed)
                else:
                    # Verify original raster before any provider resource lookup
                    # or upload. No unverified manifest bytes reach the CMS.
                    image = fetch_source_image(intent["asset"])
                    source = _fresh(operation, lease)
                    if phase == "create_ready":
                        # Commit the sole POST allowance before leaving for HTTP.
                        with transaction.atomic():
                            Organization.objects.select_for_update().get(pk=source.run.organization_id)
                            saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
                            _current(saved, lease_id=lease)
                            if saved.payload["states"][index] != state:
                                raise _deny()
                            saved.payload = deepcopy(saved.payload)
                            state = journal.issue_upload(intent, state)
                            saved.payload["states"][index] = state
                            saved.save(update_fields=["payload", "updated_at"])
                        operation.refresh_from_db()
                        source = _fresh(operation, lease)
                    binding, credential = current_credential(source.admitted.scope.context, source.admitted.scope.config,
                        source.admitted.scope.connection, user=source.admitted.scope.user)
                    if binding != intent["binding"]:
                        raise _deny()
                    require_unlocked_remote_call()
                    if phase == "create_ready":
                        ack = upload_image(binding, credential, upload_id=intent["upload_id"], image=image,
                            title=intent["title"], alt_text=intent["asset"]["alt"])
                        updated = journal.record_ack(intent, state, ack)
                    else:
                        rows, _pagination = request_json(binding, credential, **journal.lookup_options(intent, state))
                        updated = journal.observe_lookup(intent, state, rows, image=image)
            except reviews.ArtifactReadError as exc:
                failure = "transient" if exc.category == "unavailable" else "artifact_changed"
            except CmsApiError as exc:
                failure = exc.category
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=source.run.organization_id)
            saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if saved is None or saved.state != "running" or saved.payload.get("lease_id") != lease:
                return saved
            _current(saved, lease_id=lease)
            saved.payload = deepcopy(saved.payload)
            if updated is not None:
                saved.payload["states"][index] = updated
            if failure is not None:
                from .website_failure_contract import classify_failure
                failures = saved.payload["failures"] + 1
                saved.payload["failures"] = failures
                retry = failure in {"transient", "origin_unavailable"} and failures < 5
                classified = (classify_failure("SOURCE_CAPTURE_UNAVAILABLE", "cms_media") if failure in {"transient", "origin_unavailable"}
                    else classify_failure("missing_or_expired_credentials", "cms_media") if failure == "credentials_required"
                    else classify_failure("PUBLISHING_ADAPTER_REQUIRED", "cms_media"))
                saved.state = "pending" if retry else "needs_user" if failure == "credentials_required" else "blocked"
                saved.next_attempt_at = now + timedelta(seconds=15 * 2 ** (failures - 1)) if retry else None
                saved.receipt = {**_progress(saved), "status": "media_pending" if retry else "media_blocked",
                    "failure": classified, "failure_class": classified["failure_class"]}
            else:
                saved.payload["failures"] = 0
                saved.receipt = _progress(saved)
                complete = saved.receipt["verified"] == saved.receipt["count"]
                exhausted = any(state["phase"] in {"reconcile_exhausted", "recovery_unsupported"} for state in saved.payload["states"])
                saved.state, saved.next_attempt_at = ("completed", None) if complete else ("pending", now)
                if exhausted:
                    from .website_failure_contract import classify_failure
                    classified = classify_failure("PUBLISHING_ADAPTER_REQUIRED", "cms_media")
                    saved.state, saved.next_attempt_at = "blocked", None
                    saved.receipt = {**saved.receipt, "status": "media_recovery_unavailable", "failure": classified,
                        "failure_class": classified["failure_class"]}
            saved.payload.pop("lease_id", None)
            saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            return saved
    except (WebsiteAuthorityError, CmsApiError, ValueError, TypeError, AttributeError, KeyError):
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"state": candidate.state, "updated_at": candidate.updated_at}
        WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).update(state="cancelled", next_attempt_at=None,
            receipt={"status": "media_source_changed", "provider_modified": None, "approval_recorded": False}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()


def ensure_media(*, limit=20, connection_id=None):
    """Reserve every current handoff's complete media set without new billing."""
    if not enabled():
        return 0
    from .website_models import WebsiteConnectionOperation
    existing = WebsiteConnectionOperation.objects.filter(action=ACTION,
        payload__handoff_digest=OuterRef("payload__handoff__digest"))
    parents = WebsiteConnectionOperation.objects.filter(action=reviews.ACTION, state="completed", connection__state="connected")
    if connection_id:
        parents = parents.filter(connection_id=connection_id)
    parents = parents.annotate(_media_exists=Exists(existing)).filter(_media_exists=False)
    count = 0
    for parent in parents.order_by("created_at")[:limit]:
        try:
            start_media(parent)
            count += 1
        except (WebsiteAuthorityError, CmsApiError):
            continue
    return count
