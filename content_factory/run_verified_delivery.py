"""Verify typed retained artifacts without accepting worker lifecycle snapshots."""
from copy import deepcopy
import hashlib
import json
from time import monotonic
from urllib.parse import quote

from django.db import connection, transaction
from django.utils import timezone

from .run_artifact_observations import ArtifactObservationInvalid, _digest, _hash, _timestamp, fetch_evidence
from .run_ownership import (VERIFIED_DELIVERY_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, owner_receipt, require_event_execution, require_event_identity, wire_identity)

MAX_REVIEW_BYTES = 4_000_000
MAX_PROOFS = 1024


def verified_delivery_receipt(run):
    """Previously verified value survives later failed or superseded tickets."""
    owner = owner_receipt(run)
    result = getattr(run, "result", None)
    state = result.get(VERIFIED_DELIVERY_RESULT_KEY) if isinstance(result, dict) else None
    if (owner is None or not isinstance(state, dict) or type(state.get("schema_version")) is not int
            or state["schema_version"] != 1 or state.get("admission_digest") != _digest(wire_identity(owner))
            or not isinstance(state.get("proofs"), dict) or len(state["proofs"]) > MAX_PROOFS):
        return None
    for proof in state["proofs"].values():
        try:
            if (not isinstance(proof, dict) or proof.get("delivered") is not True
                    or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
                    or proof.get("run_id") != run.run_id or proof.get("kind") not in {"article_body_v1", "publish_pr_v1"}
                    or not all(_hash(proof.get(field)) for field in ("evidence_sha256", "source_sha256", "payload_sha256", "proof_sha256"))
                    or not _timestamp(proof.get("observed_at")) or not _timestamp(proof.get("verified_at"))
                    or proof["proof_sha256"] != _digest({key: value for key, value in proof.items() if key != "proof_sha256"})):
                continue
            require_event_identity(run, proof)
            require_event_execution(run, proof)
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            continue
        return deepcopy(proof)
    return None


def has_verified_delivery(run):
    return verified_delivery_receipt(run) is not None


def verified_draft_ready(run):
    """A public ready receipt needs its independently verified body and bytes."""
    if not has_verified_delivery(run):
        return False
    result = getattr(run, "result", None) or {}
    ready = result.get("draft_ready_v1")
    proofs = result[VERIFIED_DELIVERY_RESULT_KEY]["proofs"]
    try:
        return isinstance(ready, dict) and any(isinstance(proof, dict) and proof.get("kind") == "draft_ready_v1"
            and proof.get("payload_sha256") == _digest(ready)
            and proof.get("proof_sha256") == _digest({key: value for key, value in proof.items() if key != "proof_sha256"})
            for proof in proofs.values())
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return False


def _fetch_review(run):
    """Independent authenticated current canonical read, with bounded transport."""
    import requests
    from .vibe_marketing_views import _content_factory_headers, _content_factory_remote_config
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    config = _content_factory_remote_config()
    if not config.get("enabled"):
        raise ArtifactObservationInvalid("The canonical article is unavailable.")
    deadline = monotonic() + 15
    response = requests.get(config["base_url"].rstrip("/") + "/api/runs/" + quote(run.run_id, safe="") + "/article-review",
        headers=_content_factory_headers(), timeout=(3, 10), stream=True, allow_redirects=False)
    try:
        if response.status_code != 200:
            raise ArtifactObservationInvalid("The canonical article is unavailable.")
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=1):
            if len(raw) + len(chunk) > MAX_REVIEW_BYTES or monotonic() > deadline:
                raise ArtifactObservationInvalid("The canonical article exceeds its read bound.")
            raw.extend(chunk)
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ArtifactObservationInvalid("The canonical article is unavailable.")
        return value
    finally:
        response.close()


def _current_context(run):
    from .run_execution_control import current_execution_request, require_current_principal
    from .website_connections import authority_guard
    require_current_principal(run)
    request = current_execution_request(run)
    if not run.github_repo:
        if (request.get("delivery_mode") != "content_only" or request.get("delivery_mode_confirmed") is not True
                or any(value is not None for field, value in current_execution_ticket(run)["scope"].items()
                       if field != "source_run_id")):
            raise ArtifactObservationInvalid("The portable delivery consent is unavailable.")
        return request, None, None
    with authority_guard(request, action="read", require_current_head=True) as website:
        if website.organization_id != run.organization_id or website.github_repo != run.github_repo:
            raise ArtifactObservationInvalid("The article website changed.")
        snapshot = (website.generation, website.configuration_version, website.state, website.installation_id,
                    website.authorized_by_id, website.repository_id, website.verified_sha)
    return request, website, snapshot


def _body(run, records):
    from .article_export import article_export
    body = records.get("article_body_v1")
    ready = records.get("draft_ready_v1")
    if body is None and ready is None:
        return None
    if body is None:
        raise ArtifactObservationInvalid("Draft readiness needs an independently readable article.")
    review = _fetch_review(run)
    exported = article_export(review, run_id=run.run_id, allow_preview_pending=True)
    normalized = exported.get("markdown", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    payload = body["payload"]
    if (exported.get("status") != "ready" or exported.get("revision") != payload["revision"]
            or normalized != payload["body"] or hashlib.sha256(normalized.encode()).hexdigest() != payload["article_sha256"]
            or ready is not None and (ready["payload"]["revision"] != payload["revision"]
                or ready["source_sha256"] != _digest(ready["payload"]))):
        raise ArtifactObservationInvalid("The retained article differs from the current canonical body.")
    return exported


def _pull(run, record, website, body):
    if record is None:
        return
    from integrations.services.github_connections import read_bound_repository
    from .publish_merge_worker import _owned
    payload = record["payload"]
    if website is None or not _owned(payload, website):
        raise ArtifactObservationInvalid("The publication destination changed.")
    response = read_bound_repository(website,
        f"https://api.github.com/repos/{website.github_repo}/pulls/{payload['number']}")
    response.raise_for_status()
    pull = response.json()
    if (not isinstance(pull, dict) or not _owned(payload, website, pull)
            or (pull.get("head") or {}).get("sha") != payload["head_sha"]
            or pull.get("html_url") != payload["pr_url"] or pull.get("state") != "open"):
        raise ArtifactObservationInvalid("The actual pull request differs from its retained receipt.")
    if payload["kind"] == "article":
        from django.contrib.auth import get_user_model
        from .article_publish_approval import article_publish_approval_receipt, article_publish_approval_receipt_matches
        from .publish_reviewed_draft import may_publish
        digest = payload["article_sha256"]
        approved = (run.result or {}).get("article_approval_v1", {})
        approver = get_user_model().objects.filter(pk=approved.get("approved_by_user_id"), is_active=True).first() if isinstance(approved, dict) else None
        named = ((run.run_request or {}).get("approved_body_sha256") == digest
                 and isinstance(approved, dict) and approved.get("approved_body_sha256") == digest
                 and approver is not None and may_publish(approver, run.organization)
                 and (run.run_request or {}).get("approval", {}).get("by_user_id") == approver.pk)
        consent = article_publish_approval_receipt(run)
        actor = consent.get("actor_id", "")
        from integrations.services.github_installations import resolve_user_for_actor_id
        hosted_approver = resolve_user_for_actor_id(actor)
        if hosted_approver is None and str(actor).isdigit():
            hosted_approver = get_user_model().objects.filter(pk=int(actor), is_active=True).first()
        hosted = (article_publish_approval_receipt_matches(run)
                  and hosted_approver is not None and may_publish(hosted_approver, run.organization)
                  and payload.get("review_quality_inputs_sha256") == (run.result or {}).get("article_preview_quality", {}).get("inputs_sha256"))
        if (not named and not hosted or body is None
                or hashlib.sha256(body["markdown"].replace("\r\n", "\n").replace("\r", "\n").strip().encode()).hexdigest() != digest):
            raise ArtifactObservationInvalid("The publication lacks exact reviewed-body consent.")


def project_verified_delivery(run_id):
    """Fetch actual evidence and independent body/provider proof before SQL locks."""
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .run_reconciliation import authoritative_run
    from .run_execution_control import current_execution_request, require_current_principal
    from .website_contract import validate_authority
    from .website_operations import validate_operation
    if connection.in_atomic_block:
        raise ArtifactObservationInvalid("Delivery verification requires an unlocked boundary.")
    run = ContentFactoryRun.objects.select_related("organization").get(run_id=run_id)
    if not authoritative_run(run):
        raise ArtifactObservationInvalid("The delivery admission is unavailable.")
    request, website, website_snapshot = _current_context(run)
    observed = fetch_evidence(run)
    records = {record["kind"]: record for record in observed.records}
    current = current_execution_ticket(run)
    # A current read never blesses an old source or operation scope. Previously
    # verified historical benefit is retained separately by the receipt reader.
    if any(record[WIRE_EXECUTION_KEY]["scope"] != current["scope"] for record in records.values()):
        raise ArtifactObservationInvalid("The retained delivery belongs to a previous source scope.")
    body = _body(run, records)
    _pull(run, records.get("publish_pr_v1"), website, body)
    if not records:
        return {"status": "observation_only", "run_id": run.run_id, "verified": []}
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=run.organization_id)
        fresh = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        require_current_principal(fresh)
        if (not authoritative_run(fresh) or current_execution_ticket(fresh) != current
                or current_execution_request(fresh) != request or fresh.run_id != run.run_id):
            raise ArtifactObservationInvalid("The delivery execution changed during verification.")
        # Validate the retained creation tickets again against the locked owner.
        for record in records.values():
            require_event_identity(fresh, record)
            require_event_execution(fresh, record)
        if website is not None:
            from .website_models import WebsiteConnection
            locked_website = WebsiteConnection.objects.select_for_update().get(pk=website.pk)
            if website_snapshot != (locked_website.generation, locked_website.configuration_version, locked_website.state,
                locked_website.installation_id, locked_website.authorized_by_id, locked_website.repository_id, locked_website.verified_sha):
                raise ArtifactObservationInvalid("The website changed during delivery verification.")
            validate_authority(locked_website, request, action="read", domain=fresh.domain, github_repo=fresh.github_repo)
            validate_operation(locked_website, request)
        state = deepcopy((fresh.result or {}).get(VERIFIED_DELIVERY_RESULT_KEY,
            {"schema_version": 1, "admission_digest": _digest(wire_identity(owner_receipt(fresh))), "proofs": {}}))
        if (not isinstance(state, dict) or state.get("schema_version") != 1
                or not isinstance(state.get("proofs"), dict) or len(state["proofs"]) > MAX_PROOFS):
            raise ArtifactObservationInvalid("The verified delivery history is unavailable.")
        result = deepcopy(fresh.result or {})
        for kind, record in records.items():
            identifier = record["evidence_sha256"]
            if identifier not in state["proofs"]:
                if len(state["proofs"]) >= MAX_PROOFS:
                    raise ArtifactObservationInvalid("The verified delivery history is full.")
                proof = {"schema_version": 1, "run_id": fresh.run_id,
                    WIRE_REQUEST_KEY: deepcopy(record[WIRE_REQUEST_KEY]), WIRE_EXECUTION_KEY: deepcopy(record[WIRE_EXECUTION_KEY]),
                    "kind": kind, "evidence_sha256": identifier, "source_sha256": record["source_sha256"],
                    "payload_sha256": record["payload_sha256"], "observed_at": record["observed_at"],
                    "verified_at": timezone.now().isoformat(), "delivered": kind == "article_body_v1"
                        or kind == "publish_pr_v1" and record["payload"]["kind"] == "article"}
                proof["proof_sha256"] = _digest(proof)
                state["proofs"][identifier] = proof
            if kind in {"draft_ready_v1", "publish_pr_v1"}:
                result[kind] = deepcopy(record["payload"])
        if body is not None:
            result["articleExport"] = body
        result[VERIFIED_DELIVERY_RESULT_KEY] = state
        ContentFactoryRun.objects.filter(pk=fresh.pk).update(result=result)
        fresh.result = result
        if has_verified_delivery(fresh):
            from .first_article_entitlement import consume_entitlement
            consume_entitlement(organization=fresh.organization_id, run=fresh)
        if "draft_ready_v1" in records:
            from .founder_funnel import timestamp
            from .founder_funnel_events import after_commit, emit_for_organization
            at = timestamp(records["draft_ready_v1"]["payload"]["at"])
            after_commit(lambda: emit_for_organization("draft_ready", fresh.organization_id, at=at,
                run_id=fresh.run_id, source="verified.delivery_artifact"), using="default")
    return {"status": "observation_only", "run_id": run.run_id, "verified": sorted(records)}
