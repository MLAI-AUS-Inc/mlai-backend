"""Preview-only grants for a saved revision, independent of the parent's terminal execution."""

from contextlib import contextmanager
import json
from datetime import timedelta
from uuid import uuid4
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac, constant_time_compare
from workflow_runs.models import ContentFactoryRun
from .article_preview_recovery_state import effective, instant, mapping, terminal
from .models import OrganizationContentConfig
from .website_connections import authority_guard, contract_for
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_operations import deletion_epoch
from .website_publishing_inputs import candidate_for_target, TARGET_OBSERVATIONS
from .website_repository_identity import has_pausing_blocker

ACTION = "saved-preview-recovery"
LEASE = timedelta(minutes=20)
SOURCE_FAILURES = {
    "website_source_changed",
    "website_configuration_changed",
    "source_repin_refused",
    "source_changed_mid_run",
}
SHA = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"[a-f0-9]{64}")


class Refused(ValueError):
    def __init__(self, reason="destination_changed", status=409):
        self.reason, self.status = reason, status
        super().__init__(reason)


def enabled():
    return getattr(settings, "ARTICLES_SAVED_PREVIEW_RECOVERY_ENABLED", False) is True


def enabled_for(run):
    domains = getattr(settings, "ARTICLES_SAVED_PREVIEW_RECOVERY_DOMAINS", "mlai.au")
    return (
        enabled()
        and isinstance(domains, str)
        and run.domain.casefold().rstrip(".")
        in {domain.strip().casefold().rstrip(".") for domain in domains.split(",") if domain.strip()}
    )


def exact_wire(received, stored):
    """Never normalize away unknown, credential-shaped or differently typed fields."""
    return (
        isinstance(received, dict)
        and received.keys() == stored.keys()
        and json.dumps(received, sort_keys=True, separators=(",", ":"))
        == json.dumps(stored, sort_keys=True, separators=(",", ":"))
    )


def current(run, *, lock=False):
    rows = WebsiteConnectionOperation.objects.filter(
        action=ACTION,
        idempotency_key__startswith=f"{ACTION}:{run.pk}:",
        connection__organization_id=run.organization_id,
        payload__source_run_id=run.run_id,
    )
    return (rows.select_for_update() if lock else rows).order_by("-created_at", "-pk").first()


def _safe_parent(run):
    from .run_ownership import user_stop_intent
    from .run_cancellation import OWNER_CANCELLATION_REASONS
    from types import SimpleNamespace
    from .vibe_marketing_views import (
        ARTICLE_WORKFLOWS,
        _run_has_external_publish_evidence,
        _latest_review_ready_component_revision,
    )

    result = mapping(run.result)
    if (
        run.workflow not in ARTICLE_WORKFLOWS
        or run.status in {"cancelled", "deleted", "superseded"}
        or user_stop_intent(run)
        or run.approval_state in {"approved", "denied"}
        or result.get("article_review_approval")
        or result.get("cancellation_reason") in OWNER_CANCELLATION_REASONS
        or result.get("cancelled_by") == "owner"
        or result.get("draft_deleted_at")
        or result.get("cancel_requested_at")
        or result.get("user_cancelled")
        or _run_has_external_publish_evidence(run)
    ):
        raise Refused("article_cancelled")
    if (
        _latest_review_ready_component_revision(run, SimpleNamespace(organization=run.organization))
        is not None
    ):
        raise Refused("revision_changed")
    if run.status == "denied":
        receipts = (
            result,
            mapping(result.get("result")),
            mapping(result.get("failure")),
            mapping(result.get("approval_blocker")),
        )
        if not any(
            row.get(key) in SOURCE_FAILURES
            for row in receipts
            for key in ("reason", "code", "error", "error_code", "blocking_code", "blocking_reason")
            if isinstance(row.get(key), str)
        ):
            raise Refused("article_cancelled")


def _principal(run, user):
    from core.actor_ids import actor_ids_for_user
    from founder_tools import startup_team
    from .run_ownership import owner_receipt

    if not user or not user.is_active or not user.is_authenticated:
        raise Refused("access_changed")
    request = mapping(run.run_request)
    owner = owner_receipt(run)
    if owner:
        from .run_execution_control import require_current_principal

        principal = owner["identity"]["principal"]
        if principal.get("kind") != "user" or str(principal.get("user_id")) != str(user.pk):
            raise Refused("access_changed")
        try:
            require_current_principal(run)
        except ValueError as exc:
            raise Refused("access_changed") from exc
    elif request.get("roo_points_requested_by_user_id") is not None:
        if str(request["roo_points_requested_by_user_id"]) != str(user.pk):
            raise Refused("access_changed")
    elif not run.slack_user_id or run.slack_user_id not in actor_ids_for_user(user):
        raise Refused("access_changed")
    with startup_team._locked(run.organization) as (_, roster):
        if roster and roster.get("frozen"):
            raise Refused("access_changed")
        if startup_team._owner(run.organization) != user.pk and (
            not startup_team.enabled()
            or "write_articles" not in startup_team._rights(user, run.organization, roster)
        ):
            raise Refused("access_changed")


def retained_private_draft(run, user):
    """Private download uses current founder ownership and no website credential."""
    if not enabled() or run.status not in {"denied", "failed", "blocked"}:
        return False
    try:
        _safe_parent(run)
        _principal(run, user)
    except (Refused, ValueError):
        return False
    return True


def _recipe(target, execution):
    """Prefer the sealed producer schema, with exact build and target equality for older receipts."""
    try:
        sealed = candidate_for_target(target)
    except ValueError as exc:
        raise Refused() from exc
    if sealed:
        return sealed["digest"]
    if not isinstance(execution, dict) or not execution:
        raise Refused()
    # The mutable observations do not define the destination. Preserve every
    # actual adapter input, including nested contract fields and the build recipe.
    stable = {
        k: v
        for k, v in target.items()
        if k not in TARGET_OBSERVATIONS or k in {"contract", "contract_digest"}
    }
    if not stable.get("delivery_adapter") or not stable.get("route_path") or not stable.get("route_template"):
        raise Refused()
    return evidence_digest({"target": stable, "repo_execution_contract": execution})


def _historical_recipe(site, run, key, source):
    if not isinstance(source, str) or not SHA.fullmatch(source):
        raise Refused()
    recipes = set()
    # Evidence has to predate the article's admission: a later scan of an old
    # commit cannot attest to the original founder's selected configuration.
    rows = site.scan_snapshots.filter(
        generation=site.generation,
        source_sha=source,
        created_at__lte=run.created_at,
        evidence__has_key="publish_targets",
    ).order_by("-created_at")[:17]
    if len(rows) > 16:
        raise Refused()
    for row in rows:
        targets = row.evidence.get("publish_targets")
        if not isinstance(targets, list):
            raise Refused()
        selected = [r for r in targets if isinstance(r, dict) and r.get("target_id") == key]
        if len(selected) != 1:
            raise Refused()
        recipes.add(_recipe(selected[0], row.evidence.get("repo_execution_contract")))
    if len(recipes) != 1:
        raise Refused()
    return recipes.pop()


def scoped(run, user, *, confirmed=False):
    if not enabled_for(run):
        raise Refused("recovery_disabled")
    _safe_parent(run)
    _principal(run, user)
    req = mapping(run.run_request)
    cfg = (
        OrganizationContentConfig.objects.select_related("website_connection")
        .filter(organization_id=run.organization_id)
        .first()
    )
    site = cfg.website_connection if cfg else None
    if not site or site.state != "connected" or has_pausing_blocker(site):
        raise Refused("access_changed")
    if site.authorized_by_id != user.pk:
        raise Refused("access_changed")
    key = cfg.default_publish_target_id
    if (
        not key
        or run.domain.casefold() != run.organization.domain.casefold()
        or site.organization_id != run.organization_id
        or site.github_repo.casefold() != run.github_repo.casefold()
        or (cfg.github_repo or "").casefold() != site.github_repo.casefold()
        or req.get("website_connection_id") != str(site.pk)
        or req.get("connection_generation") != site.generation
        or req.get("repository_id") != site.repository_id
        or not confirmed
        and req.get("connection_target_id") != key
        or req.get("branch") != site.branch
        or req.get("app_root") != site.app_root
        or not site.installation_id
        or str(req.get("github_installation_id") or "") != site.installation_id
        or type(req.get("configuration_revision")) is not int
        or not 0 < req["configuration_revision"] <= site.configuration_version
        or type(req.get("deletion_epoch")) is not int
        or req["deletion_epoch"] != deletion_epoch(site)
    ):
        raise Refused()
    target = site.targets.filter(
        generation=site.generation, target_key=key, source_sha=site.verified_sha, verified_at__isnull=False
    ).first()
    if (
        not target
        or not SHA.fullmatch(site.verified_sha)
        or not target.capabilities.get("previewSupported")
        and not target.capabilities.get("publishingReady")
    ):
        raise Refused()
    contract = target.contract
    key_digest = _recipe(contract, cfg.repo_execution_contract)
    if not confirmed and _historical_recipe(site, run, key, req.get("expected_source_sha")) != key_digest:
        raise Refused()
    directory = contract.get("route_path")
    if (
        not isinstance(directory, str)
        or not re.fullmatch(r"/(?:[\w.-]+/)*[\w.-]*/?", directory)
        or any(part in (".", "..") for part in directory.split("/"))
    ):
        raise Refused()
    return site, {
        "source_sha": site.verified_sha,
        "website_connection_id": str(site.pk),
        "connection_generation": site.generation,
        "repository_id": site.repository_id,
        "github_repo": site.github_repo,
        "branch": site.branch,
        "target_key": key,
        "configuration_version": site.configuration_version,
        "app_root": site.app_root,
        "contract_digest": key_digest,
        "deletion_epoch": deletion_epoch(site),
        "installation_id": site.installation_id,
        "directory": directory,
    }


def destination_offer(run, user, revision, scope):
    # Scope includes the reviewed app root, selected recipe, configuration,
    # installation, generation, deletion watermark and independently verified SHA.
    token = salted_hmac(
        "saved-preview-destination-v1",
        evidence_digest([run.organization_id, run.run_id, user.pk, revision, scope]),
        algorithm="sha256",
    ).hexdigest()
    return {
        "offerId": token,
        "repository": scope["github_repo"],
        "branch": scope["branch"],
        "directory": scope["directory"],
    }


MISSING_DEPLOYMENT = {
    "preview_expired",
    "deployment_expired",
    "route_missing",
    "deployment_missing",
    "provider_placeholder",
}


def missing_deployment(run, snapshot):
    raw = mapping(snapshot.get("previewRecovery"))
    return (
        raw.get("runId") == run.run_id
        and raw.get("revision") == snapshot.get("revision")
        and raw.get("state") in ("unavailable", "settled")
        and isinstance(raw.get("reason"), str)
        and raw["reason"] in MISSING_DEPLOYMENT
    )


def reserve(run, user, snapshot, *, automatic=False, offer_id=None, replaces=None):
    revision = snapshot.get("revision")
    if not enabled_for(run) or snapshot.get("previewRecoverySupported") is not True:
        raise Refused("recovery_disabled")
    if (
        not isinstance(revision, str)
        or not 0 < len(revision) <= 200
        or snapshot.get("approved") is True
        or snapshot.get("runId") not in (None, run.run_id)
    ):
        raise Refused("revision_changed")
    if (
        automatic
        and not terminal(run)
        and mapping(snapshot.get("previewRecovery")).get("state") not in {"unavailable", "settled"}
    ):
        return None
    if not replaces and not missing_deployment(run, snapshot):
        raise Refused("pending_validation")
    confirmed = offer_id is not None
    site, scope = scoped(run, user, confirmed=confirmed)
    if confirmed and (
        not isinstance(offer_id, str)
        or not DIGEST.fullmatch(offer_id)
        or not constant_time_compare(offer_id, destination_offer(run, user, revision, scope)["offerId"])
    ):
        raise Refused()
    with transaction.atomic():
        WebsiteConnection.objects.select_for_update().get(pk=site.pk)
        locked = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        if scoped(locked, user, confirmed=confirmed)[1] != scope:
            raise Refused()
        previous = current(locked, lock=True)
        if replaces and previous and previous.pk != replaces.pk:
            return previous
        if replaces and (
            not previous
            or previous.receipt.get("status") != "expired"
            or site.operations.filter(
                action=ACTION,
                payload__source_run_id=run.run_id,
                payload__wire__revision=revision,
                created_at__gte=timezone.now() - timedelta(days=1),
            ).count()
            >= 3
        ):
            raise Refused("recovery_exhausted")
        key = evidence_digest(
            [
                run.organization_id,
                run.run_id,
                revision,
                str(site.pk),
                site.generation,
                offer_id,
                str(replaces.pk) if replaces else None,
            ]
        )
        identifier = uuid4()
        now = timezone.now()
        generation = previous.payload["wire"]["generation"] + 1 if previous else 1
        wire = {
            **scope,
            "revision": revision,
            "operation_id": str(identifier),
            "attempt": 1,
            "generation": generation,
            "expires_at": (now + LEASE).isoformat(),
        }
        op, created = WebsiteConnectionOperation.objects.get_or_create(
            idempotency_key=f"{ACTION}:{run.pk}:{key}",
            defaults={
                "pk": identifier,
                "connection": site,
                "generation": site.generation,
                "action": ACTION,
                "state": "pending",
                "payload": {
                    "source_run_id": run.run_id,
                    "user_id": user.pk,
                    "destination_offer": offer_id,
                    "wire": wire,
                },
                "receipt": {"status": "queued"},
                "next_attempt_at": now,
            },
        )
        if created:
            if previous and previous.state == "pending":
                settle(previous, "revision_changed")
            from .article_preview_recovery_worker import advance
            from .website_wakeups import submit_operation

            transaction.on_commit(lambda: submit_operation(advance, op.pk), robust=True)
        return op


def settle(op, reason):
    op.receipt = {"status": "settled", "reason": reason}
    op.state, op.next_attempt_at = "completed", None
    op.save(update_fields=["state", "receipt", "next_attempt_at", "updated_at"])


@contextmanager
def authority(run_id, wire):
    if not enabled() or not isinstance(wire, dict):
        raise Refused()
    try:
        op = WebsiteConnectionOperation.objects.select_related("connection").get(
            pk=wire.get("operation_id"), action=ACTION, payload__source_run_id=run_id
        )
    except (WebsiteConnectionOperation.DoesNotExist, ValueError, TypeError, ValidationError):
        raise Refused() from None
    if not exact_wire(wire, op.payload["wire"]):
        raise Refused()
    user = get_user_model().objects.filter(pk=op.payload["user_id"], is_active=True).first()
    run = ContentFactoryRun.objects.select_related("organization").get(
        run_id=run_id, organization_id=op.connection.organization_id
    )
    confirmed = bool(op.payload.get("destination_offer"))
    site, scope = scoped(run, user, confirmed=confirmed)
    if any(wire[k] != v for k, v in scope.items()):
        raise Refused()
    binding = {
        **contract_for(site),
        "connection_target_id": wire["target_key"],
        "expected_source_sha": wire["source_sha"],
        "configuration_revision": wire["configuration_version"],
    }
    with authority_guard(binding, action="preview", require_current_head=True):
        run = ContentFactoryRun.objects.select_for_update().select_related("organization").get(pk=run.pk)
        locked = current(run, lock=True)
        deadline = instant(wire.get("expires_at"))
        if (
            not enabled()
            or not locked
            or locked.pk != op.pk
            or locked.state != "pending"
            or not deadline
            or deadline <= timezone.now()
            or scoped(run, user, confirmed=confirmed)[1] != scope
        ):
            raise Refused()
        yield locked


def observe_expiry(op, snapshot):
    from .article_preview_recovery_worker import _matching_proof

    wire = op.payload["wire"]
    if op.receipt.get("status") == "ready":
        proof = _matching_proof(op.payload["source_run_id"], wire, snapshot)
        previous = instant(
            mapping(mapping(op.receipt.get("proof")).get("previewRecovery")).get("verifiedUntil")
        )
        if proof and previous and instant(proof["previewRecovery"]["verifiedUntil"]) > previous:
            receipt = {**op.receipt, "proof": proof}
            if WebsiteConnectionOperation.objects.filter(pk=op.pk, receipt=op.receipt).update(
                receipt=receipt
            ):
                op.receipt = receipt
    raw = mapping(snapshot.get("previewRecovery"))
    wire = op.payload["wire"]
    if (
        op.receipt.get("status") == "ready"
        and raw.get("state") in {"unavailable", "settled"}
        and raw.get("reason") in MISSING_DEPLOYMENT
        and raw.get("runId") == op.payload["source_run_id"]
        and raw.get("operationId") == str(op.pk)
        and raw.get("sourceSha") == wire["source_sha"]
        and raw.get("revision") == wire["revision"]
        and raw.get("attempt") == wire["attempt"]
        and raw.get("generation") == wire["generation"]
    ):
        receipt = {**op.receipt, "status": "expired", "reason": raw["reason"]}
        if WebsiteConnectionOperation.objects.filter(pk=op.pk, receipt__status="ready").update(
            receipt=receipt
        ):
            op.receipt = receipt


def presentation(run, snapshot, user, *, auto=False, force=False, latest=False, offer_id=None):
    revision = snapshot.get("revision")
    op = current(run) if run.organization_id else None
    refusal, offer = None, None
    if latest or snapshot.get("approved") is True:
        refusal = "article_cancelled"
    elif not enabled_for(run) or not run.github_repo:
        refusal = "recovery_disabled"
    elif not isinstance(revision, str) or not revision:
        refusal = "revision_changed"
    else:
        try:
            # Current consent must still match even after a valid ready callback.
            if offer_id is not None:
                op = reserve(run, user, snapshot, offer_id=offer_id)
            elif op and op.payload["wire"]["revision"] == revision:
                _, scope = scoped(run, user, confirmed=bool(op.payload.get("destination_offer")))
                if any(op.payload["wire"].get(k) != v for k, v in scope.items()):
                    raise Refused()
                if auto or force:
                    observe_expiry(op, snapshot)
                if (auto or force) and op.receipt.get("status") == "expired":
                    op = reserve(
                        run, user, snapshot, offer_id=op.payload.get("destination_offer"), replaces=op
                    )
            elif force or auto and missing_deployment(run, snapshot):
                op = reserve(run, user, snapshot, automatic=not force)
            elif missing_deployment(run, snapshot):
                scoped(run, user)
            elif mapping(snapshot.get("previewRecovery")).get("state") not in (
                "ready",
                "building",
                "validation_pending",
            ):
                refusal = "pending_validation"
            elif terminal(run):
                _safe_parent(run)
            elif not run.github_repo:
                refusal = "recovery_disabled"
        except (Refused, WebsiteAuthorityError) as exc:
            refusal = getattr(exc, "reason", "access_changed")
            if force:
                raise Refused(refusal) from exc
            if refusal == "destination_changed":
                try:
                    if snapshot.get("previewRecoverySupported") is True:
                        _, selected = scoped(run, user, confirmed=True)
                        offer = destination_offer(run, user, revision, selected)
                except (Refused, WebsiteAuthorityError):
                    pass
    if refusal and force:
        raise Refused(refusal)
    view = effective(run, snapshot, operation=op, refusal=refusal)
    if (
        not op
        and not refusal
        and view["state"] == "draft_ready"
        and snapshot.get("previewRecoverySupported") is True
        and missing_deployment(run, snapshot)
    ):
        view["actions"].append("rebuild_preview")
    if offer:
        view["destinationOffer"] = offer
        view["actions"] = ["review_draft", "confirm_destination"]
    if (
        not terminal(run)
        and user
        and not latest
        and view["draftRevision"]
        and refusal not in {"article_cancelled", "access_changed", "destination_changed"}
    ):
        from .founder_article_review_contract import _current_access

        if _current_access(run, user=user, organization=run.organization, review=snapshot)["canEdit"]:
            view["actions"].append("edit_draft")
    if view["state"] == "site_ready":
        from .vibe_marketing_views import _backend_live_preview_proxy_prefix

        view["previewUrl"] = _backend_live_preview_proxy_prefix(run.run_id) + "/"
    return view


def observed_edit(run, expected=None):
    """Invalidate a prior frame before an independently authorized edit is dispatched."""
    with transaction.atomic():
        op = current(run, lock=True)
        if (
            op
            and op.receipt.get("status") in {"queued", "building", "ready"}
            and (not expected or expected == op.payload["wire"]["revision"])
        ):
            settle(op, "revision_changed")


def readable(run, *, operation_id=None, revision=None):
    """Use only the latest, current, independently read ready artifact."""
    if not enabled() or not getattr(run, "organization_id", None):
        return None
    op = current(run)
    if not op or op.receipt.get("status") != "ready":
        return None
    wire = op.payload["wire"]
    if operation_id and operation_id != str(op.pk) or revision and revision != wire["revision"]:
        return None
    proof = mapping(op.receipt.get("proof"))
    user = get_user_model().objects.filter(pk=op.payload["user_id"], is_active=True).first()
    try:
        _, scope = scoped(run, user, confirmed=bool(op.payload.get("destination_offer")))
        if (
            any(wire[k] != v for k, v in scope.items())
            or effective(run, proof, operation=op)["state"] != "site_ready"
        ):
            return None
    except (Refused, WebsiteAuthorityError):
        return None
    return op


def applies(run, snapshot):
    """Leave existing healthy and portable review surfaces intact during a mixed-version rollout."""
    from .portable_drafts import original_portable_run

    if not getattr(run, "github_repo", "") or original_portable_run(run):
        return False
    if terminal(run):
        return True
    raw = mapping(snapshot.get("previewRecovery"))
    if (
        raw.get("state") == "unavailable"
        and raw.get("reason") == "preview_unavailable"
        and snapshot.get("previewedRevision") == snapshot.get("revision")
        and snapshot.get("previewPending") is False
    ):
        return False
    if (
        raw.get("runId") == run.run_id
        and raw.get("revision") == snapshot.get("revision")
        and raw.get("state") in ("ready", "building", "unavailable", "settled", "validation_pending")
    ):
        return True
    return enabled() and current(run) is not None


def remember_review(run, snapshot, started):
    """Persist bounded read evidence independently of terminal status or current website-write authority."""
    if (
        not enabled_for(run)
        or not isinstance(snapshot.get("revision"), str)
        or not 0 < len(snapshot["revision"]) <= 200
    ):
        return
    identifier = mapping(run.run_request).get("website_connection_id")
    if not identifier:
        return
    try:
        site = WebsiteConnection.objects.filter(pk=identifier, organization_id=run.organization_id).first()
    except (ValidationError, ValueError):
        return
    if not site:
        return
    raw = mapping(snapshot.get("previewRecovery"))
    public = {
        k: v
        for k, v in raw.items()
        if k
        in {
            "state",
            "reason",
            "runId",
            "revision",
            "operationId",
            "sourceSha",
            "leaseExpiresAt",
            "attempt",
            "generation",
            "verifiedUntil",
        }
        and (type(v) is int or isinstance(v, str) and len(v) <= 200)
    }
    saved = {
        "revision": snapshot["revision"],
        "approved": snapshot.get("approved") is True,
        "previewRecoverySupported": snapshot.get("previewRecoverySupported") is True,
        "previewRecovery": public,
    }
    diagnostics = {
        key: raw[key]
        for key in ("initialSourceCode", "configurationRefusalReason")
        if isinstance(raw.get(key), str) and raw[key] in SOURCE_FAILURES
    }
    if diagnostics:
        saved["sourceDiagnostics"] = diagnostics
    key = f"preview-review-observation:{run.pk}"
    with transaction.atomic():
        record, created = WebsiteConnectionOperation.objects.select_for_update().get_or_create(
            idempotency_key=key,
            defaults={
                "connection": site,
                "generation": site.generation,
                "action": "preview-review-observation",
                "state": "completed",
                "receipt": {"started": started.isoformat(), "snapshot": saved},
            },
        )
        if (
            not created
            and record.receipt.get("snapshot") != saved
            and (instant(record.receipt.get("started")) or started) <= started
        ):
            record.receipt = {"started": started.isoformat(), "snapshot": saved}
            record.save(update_fields=["receipt", "updated_at"])


def run_view(run, *, viewer=None):
    """Pure mirror for legacy terminal receipts; only a real recovery row can supersede them."""
    if not getattr(run, "organization_id", None):
        return None
    raw = mapping(mapping(run.result).get("previewRecovery"))
    if (not terminal(run) and not raw and not enabled()) or not getattr(run, "github_repo", ""):
        return None
    op = current(run) if enabled() else None
    if op:
        proof = mapping(op.receipt.get("proof"))
        snapshot = {"revision": op.payload["wire"]["revision"], **proof}
        observer = viewer or get_user_model().objects.filter(pk=op.payload["user_id"], is_active=True).first()
        view = presentation(run, snapshot, observer)
        if viewer is None:
            view.pop("destinationOffer", None)
            view["actions"] = [
                action
                for action in view["actions"]
                if action not in {"edit_draft", "confirm_destination", "rebuild_preview"}
            ]
        return view
    if enabled() and getattr(run, "pk", None):
        record = WebsiteConnectionOperation.objects.filter(
            idempotency_key=f"preview-review-observation:{run.pk}",
            connection__organization_id=run.organization_id,
        ).first()
        if record:
            snapshot = mapping(record.receipt.get("snapshot"))
            if viewer:
                return presentation(run, snapshot, viewer)
            return effective(run, snapshot, refusal="recovery_disabled")
    if terminal(run) or raw:
        return effective(
            run, {"revision": raw.get("revision"), "previewRecovery": raw}, refusal="recovery_disabled"
        )
    return None
