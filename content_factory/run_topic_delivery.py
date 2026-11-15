"""Verify retained research choices independently of callback lifecycle hints."""
from copy import deepcopy
import json
import math
from time import monotonic
from urllib.parse import quote
from uuid import uuid4

import requests
from django.db import connection, transaction
from django.utils import timezone

from .run_artifact_observations import ArtifactObservationInvalid, _digest, _hash, _timestamp
from .run_ownership import (TOPIC_DELIVERY_RESULT_KEY, WIRE_EXECUTION_KEY, WIRE_REQUEST_KEY,
    current_execution_ticket, owner_receipt, require_event_execution, require_event_identity, wire_identity)

TOPIC_KIND = "topic_selection_v1"
ISLAND_KIND = "island_proposals_v1"
MAX_BYTES = 2 * 1024 * 1024
MAX_PROOFS = 256


def topic_kind(run):
    """Select only current, actually admitted research workflows."""
    if owner_receipt(run) is None:
        return None
    if run.workflow == "auto_discovery":
        return TOPIC_KIND
    if run.workflow == "island_refresh" and isinstance((run.run_request or {}).get("island_research_brief"), dict):
        return ISLAND_KIND
    return None


def _bounded(value, depth=0):
    if depth > 14:
        return False
    if value is None or type(value) is bool:
        return True
    if type(value) in {int, float}:
        return abs(value) <= 2**53 and math.isfinite(value)
    if type(value) is str:
        return len(value) <= 20000
    if isinstance(value, list):
        return len(value) <= 4096 and all(_bounded(item, depth + 1) for item in value)
    if isinstance(value, dict):
        from .website_contract import SECRET_KEYS
        return len(value) <= 128 and all(type(key) is str and 0 < len(key) <= 128
            and key not in SECRET_KEYS and not key.startswith(("backend_run_", "run_owner_"))
            and _bounded(item, depth + 1) for key, item in value.items())
    return False


def valid_topic_payload(kind, payload):
    """Retain the existing choice shape with finite data and exact canonical hash."""
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1 or not _bounded(payload)):
        return False
    if kind == TOPIC_KIND:
        if set(payload) != {"schema_version", "selection", "selection_sha256"}:
            return False
        selection = payload.get("selection")
        options = selection.get("options") if isinstance(selection, dict) else None
        if (not isinstance(options, list) or not 1 <= len(options) <= 8
                or any(not isinstance(row, dict) or type(row.get("keyword")) is not str
                    or not 0 < len(row["keyword"].strip()) <= 200 for row in options)
                or len({row["keyword"].strip().casefold() for row in options}) != len(options)
                or selection.get("selected_keyword") not in (None, "", *(row["keyword"] for row in options))):
            return False
        return _hash(payload.get("selection_sha256")) and payload["selection_sha256"] == _digest(selection)
    if kind == ISLAND_KIND:
        if set(payload) != {"schema_version", "suggested_islands", "proposals_sha256"}:
            return False
        proposals = payload.get("suggested_islands")
        if not isinstance(proposals, list) or not 1 <= len(proposals) <= 100:
            return False
        for row in proposals:
            if (not isinstance(row, dict) or type(row.get("id")) is not str or not 0 < len(row["id"]) <= 200
                    or type(row.get("pillar_keyword")) is not str or not 0 < len(row["pillar_keyword"].strip()) <= 200
                    or not isinstance(row.get("keywords"), list) or not 1 <= len(row["keywords"]) <= 1000
                    or not isinstance(row.get("centroid_embedding"), list) or not row["centroid_embedding"]
                    or any(type(n) not in {int, float} or not math.isfinite(n) for n in row["centroid_embedding"])
                    or any(not isinstance(keyword, dict) or type(keyword.get("keyword")) is not str
                        or not 0 < len(keyword["keyword"].strip()) <= 200
                        or type(keyword.get("volume")) not in {int, float} or not math.isfinite(keyword["volume"]) or keyword["volume"] <= 0
                        or type(keyword.get("difficulty")) not in {int, float} or not 0 <= keyword["difficulty"] <= 100
                        for keyword in row["keywords"])):
                return False
        return (_hash(payload.get("proposals_sha256")) and payload["proposals_sha256"] == _digest(proposals)
            and len({row["id"] for row in proposals}) == len(proposals))
    return False


def _state(run):
    saved = (run.result or {}).get(TOPIC_DELIVERY_RESULT_KEY)
    if TOPIC_DELIVERY_RESULT_KEY not in (run.result or {}):
        return {"schema_version": 1, "admission_digest": _digest(wire_identity(owner_receipt(run))), "proofs": {}}
    if (not isinstance(saved, dict) or type(saved.get("schema_version")) is not int or saved["schema_version"] != 1
            or saved.get("admission_digest") != _digest(wire_identity(owner_receipt(run)))
            or not isinstance(saved.get("proofs"), dict) or len(saved["proofs"]) > MAX_PROOFS):
        raise ArtifactObservationInvalid("The retained topic benefit history is unavailable.")
    return deepcopy(saved)


def verified_topic_delivery(run, *, evidence_id=None, current=False):
    """Read a validated historical benefit, optionally on the current ticket."""
    if topic_kind(run) is None:
        return None
    try:
        for proof in _state(run)["proofs"].values():
            if (not isinstance(proof, dict) or proof.get("kind") not in {TOPIC_KIND, ISLAND_KIND}
                    or proof.get("run_id") != run.run_id or proof.get("delivered") is not True
                    or type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
                    or not all(_hash(proof.get(key)) for key in ("evidence_sha256", "source_sha256", "payload_sha256"))
                    or not _timestamp(proof.get("verified_at"))
                    or proof.get("proof_sha256") != _digest({key: value for key, value in proof.items() if key != "proof_sha256"})
                    or not valid_topic_payload(proof["kind"], proof.get("payload"))
                    or proof["payload_sha256"] != _digest(proof["payload"])
                    or proof["source_sha256"] != _digest(proof["payload"])):
                continue
            if evidence_id is not None and proof["evidence_sha256"] != evidence_id:
                continue
            require_event_identity(run, proof)
            require_event_execution(run, proof, current=current)
            return deepcopy(proof)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return None
    return None


def has_verified_topic_delivery(run):
    """A verified historical choice remains earned across later failed tickets."""
    return verified_topic_delivery(run) is not None


def _fetch_topic(run):
    from .vibe_marketing_views import _content_factory_remote_config, _content_factory_headers
    from .website_connections import require_unlocked_remote_call
    if connection.in_atomic_block:
        raise ArtifactObservationInvalid("Topic reads require an unlocked boundary.")
    require_unlocked_remote_call()
    config = _content_factory_remote_config()
    if not config.get("enabled"):
        raise ArtifactObservationInvalid("The topic source is unavailable.")
    deadline = monotonic() + 15
    reply = requests.get(config["base_url"].rstrip("/") + "/api/runs/" + quote(run.run_id, safe="") + "/topic-selection",
        headers=_content_factory_headers(), timeout=(3, 10), stream=True, allow_redirects=False)
    try:
        if reply.status_code != 200:
            raise ArtifactObservationInvalid("The canonical research choices are unavailable.")
        data = bytearray()
        for chunk in reply.iter_content(chunk_size=1):
            if monotonic() > deadline or len(data) + len(chunk) > MAX_BYTES:
                raise ArtifactObservationInvalid("The canonical research choice read exceeded its bound.")
            data.extend(chunk)
        try:
            value = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ArtifactObservationInvalid("The canonical research choices are invalid.") from exc
        kind = topic_kind(run)
        if (not isinstance(value, dict) or set(value) != {"schema_version", "run_id", WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, kind}
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1 or value.get("run_id") != run.run_id
                or not valid_topic_payload(kind, value.get(kind))):
            raise ArtifactObservationInvalid("The canonical research choices are invalid.")
        require_event_identity(run, value)
        require_event_execution(run, value, current=True)
        return value
    finally:
        reply.close()


def _notify(run_id, evidence_id, nonce):
    """Use only the saved original automation destination, once per benefit.

    A process/broker/provider ambiguity retains its claim. It cannot be turned
    into an automatic second send; canonical choices are already durable.
    """
    from workflow_runs.models import ContentFactoryRun
    from integrations.services.notification_adapters import resolve_automation_run, send_topic_selection
    run = ContentFactoryRun.objects.select_related("organization").get(run_id=run_id)
    from .research_workspace import accepts_research_write, research_config
    if not accepts_research_write(research_config(run.organization), run.run_id):
        return
    state = _state(run)
    notice = state.get("notification", {})
    if notice.get("nonce") != nonce or notice.get("state") != "claimed":
        return
    context = (run.run_request or {}).get("notification_context")
    automation = resolve_automation_run(context)
    if (automation is None or automation.automation.organization_id != run.organization_id
            or str(automation.automation.user_id) != owner_receipt(run)["identity"]["principal"].get("user_id")
            or automation.content_factory_run_id not in {None, "", run.run_id, owner_receipt(run)["identity"]["dispatch_key"]}):
        return
    proof = verified_topic_delivery(run, evidence_id=evidence_id, current=True)
    if proof is None or proof["kind"] != TOPIC_KIND:
        return
    from .run_execution_control import require_current_principal
    require_current_principal(run)
    data = {"event": "topic_selection", "event_type": "topic_selection", "run_id": run.run_id,
        "job_id": run.run_id, "domain": run.domain, "notification_context": deepcopy(context),
        "selection": deepcopy(proof["payload"]["selection"])}
    outcome = "uncertain"
    try:
        deliveries = send_topic_selection(data)
        if deliveries and all(delivery.status in {"sent", "opted_out"} for delivery in deliveries):
            outcome = "completed"
    finally:
        with transaction.atomic():
            current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
            fresh = _state(current)
            if fresh.get("notification", {}).get("nonce") == nonce:
                fresh["notification"]["state"] = outcome
                ContentFactoryRun.objects.filter(pk=current.pk).update(result={**current.result, TOPIC_DELIVERY_RESULT_KEY: fresh})


def project_verified_topics(run_id):
    """Project actual retained choices without worker status, approval or money."""
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .run_artifact_observations import fetch_evidence
    from .run_execution_control import require_current_principal
    from .run_reconciliation import authoritative_run
    from .run_dispatch_settlement import _original_request_intact
    from .run_backend_facts import update_or_create_job_facts
    if connection.in_atomic_block:
        raise ArtifactObservationInvalid("Topic verification requires an unlocked boundary.")
    run = ContentFactoryRun.objects.select_related("organization").get(run_id=run_id)
    kind = topic_kind(run)
    if not authoritative_run(run) or kind is None or not _original_request_intact(run):
        raise ArtifactObservationInvalid("The research admission is unavailable.")
    require_current_principal(run)
    ticket = current_execution_ticket(run)
    existing = verified_topic_delivery(run, current=True)
    if existing is not None:
        if not (run.run_request or {}).get("notification_context") or kind == ISLAND_KIND:
            from .founder_transitions import notify_verified_research_ready
            notify_verified_research_ready(run, evidence_id=existing["evidence_sha256"])
        return {"status": "observation_only", "run_id": run.run_id, "verified": [kind]}
    evidence = fetch_evidence(run)
    record = next((row for row in evidence.records if row["kind"] == kind), None)
    if record is None:
        return {"status": "observation_only", "run_id": run.run_id, "verified": []}
    snapshot = _fetch_topic(run)
    if (record[WIRE_EXECUTION_KEY] != ticket or snapshot[kind] != record["payload"]
            or record["source_sha256"] != _digest(snapshot[kind])):
        raise ArtifactObservationInvalid("The canonical research choices differ from their retained evidence.")
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=run.organization_id)
        current = ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        require_current_principal(current)
        if (not _original_request_intact(current) or current_execution_ticket(current) != ticket
                or current.organization_id != run.organization_id or current.run_id != run.run_id):
            raise ArtifactObservationInvalid("The research execution changed during verification.")
        require_event_identity(current, record)
        require_event_execution(current, record, current=True)
        state = _state(current)
        identifier = record["evidence_sha256"]
        if identifier not in state["proofs"]:
            if len(state["proofs"]) >= MAX_PROOFS:
                raise ArtifactObservationInvalid("The research benefit history is full.")
            proof = {**deepcopy(record), "verified_at": timezone.now().isoformat(), "delivered": True}
            proof["proof_sha256"] = _digest(proof)
            state["proofs"][identifier] = proof
        payload = snapshot[kind]
        if kind == TOPIC_KIND:
            selection = deepcopy(payload["selection"])
            update_or_create_job_facts(job_id=current.run_id, defaults={"selection_data": selection,
                "status": "awaiting_confirmation",
                "selected_keyword": str(selection.get("selected_keyword") or selection["options"][0]["keyword"]),
                "selection_reason": str(selection.get("selection_reason") or "")})
        else:
            result = {**current.result, "island_research": True, "suggested_islands": deepcopy(payload["suggested_islands"])}
            ContentFactoryRun.objects.filter(pk=current.pk).update(result=result)
            update_or_create_job_facts(job_id=current.run_id, defaults={"selection_data": {
                "island_research": True, "suggested_islands": deepcopy(payload["suggested_islands"])}})
        current.refresh_from_db()
        if kind == TOPIC_KIND and "notification" not in state and (current.run_request or {}).get("notification_context"):
            nonce = uuid4().hex
            state["notification"] = {"state": "claimed", "nonce": nonce, "evidence_id": identifier}
            transaction.on_commit(lambda: _notify(current.run_id, identifier, nonce), robust=True)
        ContentFactoryRun.objects.filter(pk=current.pk).update(result={**current.result, TOPIC_DELIVERY_RESULT_KEY: state})
        if not (current.run_request or {}).get("notification_context") or kind == ISLAND_KIND:
            from .founder_transitions import notify_verified_research_ready
            current.result = {**current.result, TOPIC_DELIVERY_RESULT_KEY: state}
            notify_verified_research_ready(current, evidence_id=identifier)
    return {"status": "observation_only", "run_id": run.run_id, "verified": [kind]}
