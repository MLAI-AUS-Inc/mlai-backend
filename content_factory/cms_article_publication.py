"""One native post create, durable uncertain-outcome recovery and public proof."""

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from django.db import transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from . import cms_article_approval as approvals
from . import cms_artifact_review as reviews
from . import cms_staged_review as stages
from . import cms_post_contract as journal
from . import cms_live_evidence as public
from .channel_artifacts import enabled
from .channel_artifact_contract import canonical_bytes, digest
from .cms_api_transport import CmsApiError, request_json
from .cms_connection_authority import current_credential
from .cms_publication_policy import native_post_fields
from .website_contract import WebsiteAuthorityError
from .cms_goal_effects import require_work, CmsGoalWorkDenied

ACTION = "cms-publish"
LEASE_SECONDS = 180
FIELDS = {"schema_version", "binding", "credential_operation_id", "approval_operation_id", "staged_revision",
    "billing_digest", "intent", "post_state", "public_observation", "failures"}


def _deny():
    return WebsiteAuthorityError("website_connection_changed", "The original CMS publication, approval or payment changed.")


def _intent(identifier, source, value, policy, receipt):
    return journal.make_intent(binding=source.admitted.scope.binding, operation_id=str(identifier), source_run_id=source.run.run_id,
        run_generation=value["source_generation"], artifact_sha256=value["revision"],
        editorial_sha256=digest(value["original_editorial_acceptance"]), approval_sha256=receipt["digest"],
        title=value["article"]["title"], slug_seed=value["article"]["slug"][:150].rstrip("-"), html=value["article"]["html"],
        status="published", native_fields=native_post_fields(policy["policy"]))


def _current(operation, *, lease_id=None, fresh_media=False):
    from .website_models import WebsiteConnectionOperation
    try:
        payload = operation.payload if operation is not None else None
        if (not enabled() or operation is None or operation.action != ACTION or operation.state not in {"pending", "running", "completed"} or
                not isinstance(payload, dict) or set(payload) - {"lease_id"} != FIELDS or
                type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
                type(payload["failures"]) is not int or not 0 <= payload["failures"] <= 5 or
                operation.state == "completed" and "lease_id" in payload or
                lease_id is not None and (operation.state != "running" or payload.get("lease_id") != lease_id)):
            raise _deny()
        approval = WebsiteConnectionOperation.objects.filter(pk=payload["approval_operation_id"], action=approvals.ACTION).first()
        source, value, policy, receipt = approvals.current_approval(approval, fresh_media=fresh_media)
        stage = WebsiteConnectionOperation.objects.filter(pk=approval.payload["stage_operation_id"], action=stages.ACTION).first()
        _source, _value, media = stages.current_stage(stage, fresh_media=fresh_media, allow_media_recheck=not fresh_media)
        expected = _intent(operation.pk, source, value, policy, receipt)
        journal.validate_post_state(payload["post_state"], expected)
        if (canonical_bytes(payload["intent"]) != canonical_bytes(expected) or payload["binding"] != source.admitted.scope.binding or
                operation.generation != payload["binding"]["generation"] or str(operation.connection_id) != payload["binding"]["connection_id"] or
                payload["credential_operation_id"] != source.admitted.operation.payload["credential_operation_id"] or
                payload["staged_revision"] != value["revision"] or payload["billing_digest"] != source.billing["digest"] or
                payload["public_observation"] is not None and operation.state != "completed"):
            raise _deny()
        return SimpleNamespace(source=source, value=value, policy=policy, approval=approval, media=media,
            intent=expected, post_state=payload["post_state"])
    except (WebsiteAuthorityError, CmsApiError):
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def _progress(operation):
    payload = operation.payload if isinstance(operation.payload, dict) else {}
    state = payload.get("post_state") if isinstance(payload.get("post_state"), dict) else {}
    issued = type(state.get("create_count")) is not int or state["create_count"] != 0
    confirmed = state.get("phase") == "provider_confirmed" and type(state.get("create_count")) is int and state["create_count"] == 1
    return {"status": "provider_confirmed" if confirmed else "publication_pending", "channel": "cms",
        "provider_modified": True if confirmed else None if issued else False,
        "outcome_unknown": bool(issued and not confirmed), "fallback_safe": not issued,
        "approval_recorded": True, "publishing_verified": False}


def _receipt(operation, observation):
    return {**_progress(operation), "status": "article_live", "publishing_verified": True,
        "publicUrl": observation["public_url"], "sourceRunId": observation["source_run_id"],
        "reviewRevision": operation.payload["staged_revision"], "publicProofDigest": observation["digest"]}


def start_publication(approval):
    """Reserve from private exact approval only; never charge or call a provider."""
    if not enabled():
        raise _deny()
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    source, _value, _policy, _receipt_value = approvals.current_approval(approval, fresh_media=False)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.run.organization_id)
        approval = WebsiteConnectionOperation.objects.select_for_update().filter(pk=approval.pk, action=approvals.ACTION).first()
        source, value, policy, receipt = approvals.current_approval(approval, fresh_media=False)
        identifier = uuid4()
        intent = _intent(identifier, source, value, policy, receipt)
        operation, _created = WebsiteConnectionOperation.objects.get_or_create(idempotency_key="cms-publish:" + str(approval.pk), defaults={
            "id": identifier, "action": ACTION, "connection": source.admitted.scope.connection,
            "generation": source.admitted.scope.binding["generation"], "state": "pending", "next_attempt_at": timezone.now(),
            "payload": {"schema_version": 1, "binding": source.admitted.scope.binding,
                "credential_operation_id": source.admitted.operation.payload["credential_operation_id"],
                "approval_operation_id": str(approval.pk), "staged_revision": value["revision"], "billing_digest": source.billing["digest"],
                "intent": intent, "post_state": journal.new_post_state(intent), "public_observation": None, "failures": 0}})
        _current(operation)
        if operation.state == "completed":
            current_publication(operation, fresh=False)
        elif not operation.receipt:
            operation.receipt = _progress(operation)
            operation.save(update_fields=["receipt", "updated_at"])
        return operation


def _fresh(operation, lease):
    scope = _current(operation, lease_id=lease)
    observed = reviews.fetch_handoff(scope.source.run, expected_revision=scope.source.handoff["review_revision"])
    operation.refresh_from_db()
    scope = _current(operation, lease_id=lease)
    if canonical_bytes(observed) != canonical_bytes(scope.source.handoff):
        raise _deny()
    return scope


def _request(operation, lease, effect, *, handoff_checked=False):
    scope = _current(operation, lease_id=lease) if handoff_checked else _fresh(operation, lease)
    if not approvals._ready(scope.media, now=timezone.now()):
        return None, True
    binding, credential = current_credential(scope.source.admitted.scope.context, scope.source.admitted.scope.config,
        scope.source.admitted.scope.connection, user=scope.source.admitted.scope.user)
    if binding != scope.intent["binding"]:
        raise _deny()
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    if effect.method == "POST":
        require_work(scope.source.run.run_request)
    response, _pagination = request_json(binding, credential, method=effect.method, resource=effect.resource,
        identifier=effect.identifier, query=effect.query, payload=effect.payload)
    return response, False


def current_publication(operation, *, now=None, fresh=True):
    """Resolve the private live result against current original approval and proofs."""
    scope = _current(operation, fresh_media=fresh)
    if operation.state != "completed":
        raise _deny()
    if fresh:
        from .cms_unpublish_authority import requested
        if requested(operation): raise _deny()
    try:
        observation = operation.payload["public_observation"]
        # Historical status may retain a once-verified result. Milestone consumers
        # must use the default fresh observation and never trust its checksum alone.
        checked = now if fresh else datetime.fromisoformat(observation["checked_at"])
        public.validate_public_observation(observation, intent=scope.intent, state=scope.post_state, now=checked)
        if canonical_bytes(operation.receipt) != canonical_bytes(_receipt(operation, observation)):
            raise _deny()
        return scope.source, deepcopy(scope.intent), deepcopy(scope.post_state), deepcopy(observation)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def request_public_recheck(operation, *, now=None):
    """Refresh the same published post on demand, with no new create allowance."""
    from organizations.models import Organization
    from .website_models import WebsiteConnectionOperation
    now = now or timezone.now()
    source = _current(operation).source
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=source.run.organization_id)
        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=operation.pk, action=ACTION).first()
        scope = _current(saved)
        from .cms_unpublish_authority import requested
        if requested(saved): return saved
        if saved.state in {"pending", "running"}:
            return saved
        # Validate the exact saved receipt at its original observation time
        # before clearing it. An invalid historical receipt is not repairable
        # merely by recomputing its checksum or fetching a different URL.
        current_publication(saved, fresh=False)
        try:
            public.validate_public_observation(saved.payload["public_observation"], intent=scope.intent, state=scope.post_state, now=now)
            return saved
        except CmsApiError:
            pass
        saved.payload = {**deepcopy(saved.payload), "public_observation": None, "failures": 0}
        saved.state, saved.next_attempt_at = "pending", now
        saved.receipt = _progress(saved)
        saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
        return saved


def advance_publication(identifier, *, now=None):
    """Advance one durable phase; uncertain creates reconcile only by GET."""
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
        scope = _current(operation, lease_id=lease)
        waiting, failure, observation, updated = not approvals._ready(scope.media, now=now), None, None, None
        phase = scope.post_state["phase"]
        try:
            if waiting:
                pass
            elif phase == "reconcile_exhausted":
                raise CmsApiError("post_recovery_unavailable")
            elif phase == "provider_confirmed":
                response, waiting = _request(operation, lease, journal.confirmation_effect(scope.intent, scope.post_state))
                if not waiting:
                    journal.observe_confirmation(scope.intent, scope.post_state, response)
                    scope = _fresh(operation, lease)
                    waiting = not approvals._ready(scope.media, now=timezone.now())
                    if not waiting:
                        from .website_connections import require_unlocked_remote_call
                        require_unlocked_remote_call()
                        html = public.fetch_public_article(scope.intent, scope.post_state)
                        observation = public.observe_public_article(scope.intent, scope.post_state, html)
            else:
                state = deepcopy(scope.post_state)
                if phase == "create_ready":
                    # Resolve read-only service prerequisites before committing
                    # the sole provider allowance. A transient CF read must not
                    # consume a post allowance that was never attempted.
                    scope = _fresh(operation, lease)
                    waiting = not approvals._ready(scope.media, now=timezone.now())
                    if waiting:
                        effect = None
                    else:
                        current_credential(scope.source.admitted.scope.context, scope.source.admitted.scope.config,
                            scope.source.admitted.scope.connection, user=scope.source.admitted.scope.user)
                if phase == "create_ready" and not waiting:
                    with transaction.atomic():
                        Organization.objects.select_for_update().get(pk=scope.source.run.organization_id)
                        saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
                        current = _current(saved, lease_id=lease)
                        require_work(current.source.run.run_request, now=now)
                        if saved.payload["post_state"] != state:
                            raise _deny()
                        state, effect = journal.issue_create(scope.intent, state)
                        saved.payload = {**deepcopy(saved.payload), "post_state": state}
                        saved.receipt = _progress(saved)
                        saved.save(update_fields=["payload", "receipt", "updated_at"])
                    operation.refresh_from_db()
                elif phase != "create_ready":
                    effect = journal.lookup_effect(scope.intent, state)
                if not waiting:
                    response, waiting = _request(operation, lease, effect, handoff_checked=phase == "create_ready")
                if not waiting:
                    updated = journal.observe_posts(scope.intent, state, response, lookup=phase != "create_ready")
        except reviews.ArtifactReadError as exc:
            failure = "transient" if exc.category == "unavailable" else "artifact_changed"
        except CmsApiError as exc:
            failure = exc.category
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=scope.source.run.organization_id)
            saved = WebsiteConnectionOperation.objects.select_for_update().filter(pk=identifier, action=ACTION).first()
            if saved is None or saved.state != "running" or saved.payload.get("lease_id") != lease:
                return saved
            scope = _current(saved, lease_id=lease)
            saved.payload = deepcopy(saved.payload)
            if updated is not None:
                saved.payload["post_state"] = updated
            if observation is not None:
                waiting = not approvals._ready(scope.media, now=timezone.now())
            if waiting:
                saved.state, saved.next_attempt_at = "pending", now + timedelta(seconds=15)
                saved.receipt = {**_progress(saved), "status": "publication_waiting_media"}
            elif failure is not None:
                from .website_failure_contract import classify_failure
                failures = saved.payload["failures"] + 1
                saved.payload["failures"] = failures
                unknown = saved.payload["post_state"]["outcome_unknown"]
                # Even a malformed create reply may follow a successful write.
                # Recover that same journal by reads, never a second POST.
                recover = unknown and phase == "create_ready" and failure != "credentials_required"
                retry = (failure in {"transient", "origin_unavailable", "public_article_unavailable"} or recover) and failures < 5
                classified = classify_failure("CONNECTION_SERVICE_TRANSIENT" if retry or failure in {"transient", "origin_unavailable", "public_article_unavailable"}
                    else "missing_or_expired_credentials" if failure == "credentials_required" else "PUBLISHING_ADAPTER_REQUIRED", "cms_publication")
                saved.state = "pending" if retry else "needs_user" if failure == "credentials_required" else "blocked"
                saved.next_attempt_at = now + timedelta(seconds=15 * 2 ** (failures - 1)) if retry else None
                saved.receipt = {**_progress(saved), "status": "publication_pending" if retry else "publication_blocked",
                    "failure": classified, "failure_class": classified["failure_class"]}
            elif saved.payload["post_state"]["phase"] == "reconcile_exhausted":
                from .website_failure_contract import classify_failure
                classified = classify_failure("PUBLISHING_ADAPTER_REQUIRED", "cms_publication")
                saved.state, saved.next_attempt_at = "blocked", None
                saved.receipt = {**_progress(saved), "status": "publication_outcome_unknown", "failure": classified, "failure_class": classified["failure_class"]}
            elif observation is not None:
                public.validate_public_observation(observation, intent=scope.intent, state=scope.post_state)
                saved.payload["public_observation"] = observation
                saved.state, saved.next_attempt_at = "completed", None
                saved.receipt = _receipt(saved, observation)
            else:
                saved.payload["failures"] = 0
                saved.state, saved.next_attempt_at = "pending", now + timedelta(seconds=15) if saved.payload["post_state"]["outcome_unknown"] else now
                saved.receipt = _progress(saved)
            saved.payload.pop("lease_id", None)
            saved.save(update_fields=["payload", "receipt", "state", "next_attempt_at", "updated_at"])
            return saved
    except CmsGoalWorkDenied:
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"state": candidate.state, "updated_at": candidate.updated_at}
        saved = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).first()
        if saved is not None:
            uncertain = saved.payload["post_state"]["outcome_unknown"]
            WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).update(
                state="pending" if uncertain else "blocked", next_attempt_at=now + timedelta(seconds=15) if uncertain else None,
                receipt={**_progress(saved), "status": "goal_write_reconciliation" if uncertain else "goal_work_stopped"}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()
    except (WebsiteAuthorityError, CmsApiError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        owned = {"state": "running", "payload__lease_id": lease} if claimed else {"state": candidate.state, "updated_at": candidate.updated_at}
        saved = WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).first()
        if saved is not None:
            # Retain every issued allowance/ID even if a refund or disconnect
            # wins after HTTP. Never turn uncertain side effects into absence.
            WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION, **owned).update(state="cancelled", next_attempt_at=None,
                receipt={**_progress(saved), "status": "publication_source_changed", "publishing_verified": False}, updated_at=timezone.now())
        return WebsiteConnectionOperation.objects.filter(pk=identifier, action=ACTION).first()


def ensure_publications(*, limit=20, connection_id=None):
    """Resume each exact private approval automatically, with no new paid run."""
    if not enabled():
        return 0
    from .website_models import WebsiteConnectionOperation
    existing = WebsiteConnectionOperation.objects.filter(action=ACTION, payload__staged_revision=OuterRef("payload__staged_revision"))
    parents = WebsiteConnectionOperation.objects.filter(action=approvals.ACTION, state="completed", connection__state="connected")
    if connection_id:
        parents = parents.filter(connection_id=connection_id)
    parents = parents.annotate(_publication_exists=Exists(existing)).filter(_publication_exists=False)
    count = 0
    for parent in parents.order_by("created_at")[:limit]:
        try:
            start_publication(parent)
            count += 1
        except (WebsiteAuthorityError, CmsApiError):
            continue
    return count
