"""Withdraw one original PR's auto-merge without reviving its stopped workflow."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import json
import re
import time
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone
from . import website_goal as goals, website_article_stopped_publications as reads
from . import website_article_credentials as credentials
from .website_contract import WebsiteAuthorityError
from .bounded_service_read import read_limited, ServiceUnavailable

ACTION = "article-source-merge-stop"
KEY = "article_stopped_auto_merge"
LEASE_KEY = "merge_stop_lease"
LEASE = timedelta(seconds=180)
MAX_ATTEMPTS = 32


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "Article recovery changed before the queued merge could be stopped.")


def _row(scope, identifier, *, lock=False):
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects.select_for_update() if lock else WebsiteConnectionOperation.objects
    return query.filter(pk=identifier, connection_id=scope["connection_id"],
        generation=scope["connection_generation"], action=ACTION).first()


def _valid(row, payload):
    if not isinstance(payload, dict) or set(payload) != {"snapshot", "target"}:
        _deny()
    snapshot, target = payload["snapshot"], payload["target"]
    reads.validate_snapshot(snapshot)
    if (not isinstance(target, dict) or set(target) != {"number", "head_sha", "head_branch", "mutations"}
            or type(target["number"]) is not int or target["number"] not in snapshot["inventory"]["pulls"]
            or not isinstance(target["head_sha"], str) or not goals.SHA.fullmatch(target["head_sha"])
            or not isinstance(target["head_branch"], str) or not 0 < len(target["head_branch"]) <= 255
            or any(ord(c) < 33 for c in target["head_branch"])):
        _deny()
    matching = sorted(key for key, mutation in snapshot["inventory"]["mutations"].items()
        if mutation["status"] == "applied" and mutation["pr_number"] == target["number"]
        and mutation["branch"] == target["head_branch"] and mutation["head_sha"] == target["head_sha"])
    if not matching or target["mutations"] != matching:
        _deny()
    scope = snapshot["read_scope"]
    if (row is None or row.action != ACTION or row.payload != payload or row.state not in {"running", "completed"}
            or str(row.connection_id) != scope["connection_id"] or row.generation != scope["connection_generation"]
            or not isinstance(row.receipt, dict) or set(row.receipt) != {"schema_version", "credential", "mutation", "result"}
            or type(row.receipt["schema_version"]) is not int or row.receipt["schema_version"] != 1
            or not credentials.valid_receipt(row.receipt["credential"])):
        _deny()
    mutation = row.receipt["mutation"]
    if (not isinstance(mutation, dict) or set(mutation) != {"state", "node_id", "request_id"}
            or not isinstance(mutation["state"], str) or mutation["state"] not in {"not_attempted", "possible", "response_received"}
            or mutation["state"] == "not_attempted" and (mutation["node_id"] is not None or mutation["request_id"] is not None)
            or mutation["state"] != "not_attempted" and (not _node(mutation["node_id"]) or mutation["request_id"] != str(row.pk))):
        _deny()


    saved = row.receipt["result"]
    if row.state == "completed":
        fields = {"schema_version", "operation_id", "purpose_digest", "source_fence_id", "result", "observed_at",
            "quiet", "inventory_complete", "effect_authority", "approval_carried", "digest"}
        if (not isinstance(saved, dict) or set(saved) != fields or type(saved["schema_version"]) is not int or saved["schema_version"] != 1
                or saved["operation_id"] != str(row.pk) or saved["purpose_digest"] != goals.digest(payload)
                or saved["source_fence_id"] != snapshot["source_fence_id"] or not goals.stamp(saved["observed_at"])
                or saved["digest"] != goals.digest({key: value for key, value in saved.items() if key != "digest"})
                or any(saved[key] is not False for key in ("quiet", "inventory_complete", "effect_authority", "approval_carried"))): _deny()
        result = saved["result"]
        if (not isinstance(result, dict) or set(result) != {"number", "node_id", "head_sha", "head_branch", "state", "merged", "merge_sha", "auto_merge_pending", "outcome"}
                or result["number"] != target["number"] or type(result["number"]) is not int or not _node(result["node_id"])
                or not isinstance(result["head_sha"], str) or not goals.SHA.fullmatch(result["head_sha"])
                or not isinstance(result["head_branch"], str) or not 0 < len(result["head_branch"]) <= 255
                or any(ord(c) < 33 for c in result["head_branch"]) or not isinstance(result["state"], str) or result["state"] not in {"open", "closed"}
                or type(result["merged"]) is not bool or type(result["auto_merge_pending"]) is not bool
                or not isinstance(result["outcome"], str) or result["outcome"] not in {"not_pending", "closed", "merged", "changed"}
                or result["merged"] and (result["state"] != "closed" or not isinstance(result["merge_sha"], str) or not goals.SHA.fullmatch(result["merge_sha"]))
                or not result["merged"] and result["merge_sha"] is not None): _deny()
        expected = ("changed" if result["head_sha"] != target["head_sha"] or result["head_branch"] != target["head_branch"] else
            "merged" if result["merged"] else "closed" if result["state"] == "closed" else "pending" if result["auto_merge_pending"] else "not_pending")
        if result["outcome"] != expected: _deny()
    elif saved is not None: _deny()


def _node(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_+/=\-]{1,255}", value) is not None


@contextmanager
def _record(identifier, payload):
    """Settle the recorded attempt after owner/flag revocation without authority."""
    from .website_models import WebsiteConnection
    scope = payload["snapshot"]["read_scope"]
    with transaction.atomic():
        if WebsiteConnection.objects.select_for_update().filter(pk=scope["connection_id"]).first() is None:
            _deny()
        row = _row(scope, identifier, lock=True)
        _valid(row, payload)
        yield row
        row.save(update_fields=["receipt", "updated_at"])


def _credential(identifier, payload, **changes):
    with _record(identifier, payload) as row:
        prior = row.receipt["credential"]
        if prior["revocation_state"] == "confirmed": changes.pop("revocation_state", None)
        row.receipt = {**row.receipt, "credential": {**prior, **changes}}
        if not credentials.valid_receipt(row.receipt["credential"]): _deny()


def _valid_lease(lease):
    try:
        stamp = goals.stamp(lease.get("expires_at")) if isinstance(lease, dict) else None
        return (isinstance(lease, dict) and set(lease) == {"operation_id", "nonce", "purpose_digest", "expires_at"}
            and stamp is not None and not timezone.is_naive(stamp)
            and all(isinstance(lease[key], str) and str(UUID(lease[key])) == lease[key] for key in ("operation_id", "nonce"))
            and isinstance(lease["purpose_digest"], str) and goals.DIGEST.fullmatch(lease["purpose_digest"]) is not None)
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _check(parent, config, payload, lease):
    from . import website_article_source_stop as stops
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    snapshot = payload["snapshot"]
    if not _valid_lease(lease) or lease["purpose_digest"] != goals.digest(payload): _deny()
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        if reads._snapshot(parent, config, connection) != snapshot or goals.stamp(lease["expires_at"]) <= timezone.now(): _deny()
        stop = stops._operation(parent.receipt[stops.KEY], lock=True)
        if stop is None or stop.receipt.get(LEASE_KEY) != lease: _deny()
        row = _row(snapshot["read_scope"], lease["operation_id"], lock=True)
        _valid(row, payload)
        if row.state != "running": _deny()
        _save_fields(parent, {})
        return row


def _pull(connection, target, headers, check, deadline):
    from .website_article_publication_observation import _provider_json, _repository
    check()
    value = _provider_json(f"/repos/{connection.github_repo}/pulls/{target['number']}", headers=headers, deadline=deadline)
    check()
    head, base = goals.mapping(value.get("head")), goals.mapping(value.get("base"))
    merged, state, merge_sha = value.get("merged"), value.get("state"), value.get("merge_commit_sha")
    if (type(value.get("number")) is not int or value["number"] != target["number"] or not _node(value.get("node_id"))
            or not isinstance(value.get("html_url"), str) or value["html_url"].casefold() != f"https://github.com/{connection.github_repo}/pull/{target['number']}".casefold()
            or not all(_repository(side.get("repo"), connection) for side in (head, base)) or base.get("ref") != connection.branch
            or not isinstance(head.get("sha"), str) or not goals.SHA.fullmatch(head["sha"])
            or not isinstance(head.get("ref"), str) or not 0 < len(head["ref"]) <= 255 or any(ord(c) < 33 for c in head["ref"])
            or type(merged) is not bool or not isinstance(state, str) or state not in {"open", "closed"}
            or merged and (state != "closed" or not isinstance(merge_sha, str) or not goals.SHA.fullmatch(merge_sha))
            or value.get("auto_merge") is not None and not isinstance(value["auto_merge"], dict)):
        _deny()
    outcome = ("changed" if head["sha"] != target["head_sha"] or head["ref"] != target["head_branch"] else
        "merged" if merged else "closed" if state == "closed" else "pending" if value.get("auto_merge") is not None else "not_pending")
    return {"number": target["number"], "node_id": value["node_id"], "head_sha": head["sha"], "head_branch": head["ref"],
        "state": state, "merged": merged, "merge_sha": merge_sha if merged else None,
        "auto_merge_pending": value.get("auto_merge") is not None, "outcome": outcome}


def _disable(identifier, payload, lease, parent, config, credential):
    from .website_article_publication_observation import _provider_json, _repository, _transient
    from .website_connections import require_unlocked_remote_call
    provider = payload["snapshot"]["provider"]
    from types import SimpleNamespace
    target_connection = SimpleNamespace(github_repo=provider["repository"], repository_id=provider["repository_id"], branch=provider["branch"])
    check = lambda: _check(parent, config, payload, lease)
    headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
    deadline = time.monotonic() + 60
    check()
    if not _repository(_provider_json(f"/repos/{provider['repository']}", headers=headers, deadline=deadline), target_connection): _deny()
    before = _pull(target_connection, payload["target"], headers, check, deadline)
    if before["outcome"] != "pending": return before
    check()
    with _record(identifier, payload) as row:
        row.receipt = {**row.receipt, "mutation": {"state": "possible", "node_id": before["node_id"], "request_id": identifier}}
    check()
    response = None
    try:
        require_unlocked_remote_call()
        remaining = deadline - time.monotonic()
        if remaining <= 0: _transient()
        response = read_limited("POST", "https://api.github.com/graphql", headers=headers, maximum=65536, deadline=min(12, remaining), body={
            "query": "mutation($id: ID!, $nonce: String!) { disablePullRequestAutoMerge(input: {pullRequestId: $id, clientMutationId: $nonce}) { clientMutationId pullRequest { id } } }",
            "variables": {"id": before["node_id"], "nonce": identifier}})
        if response.status_code != 200: _transient()
        data = json.loads(response.content)
        result = goals.mapping(goals.mapping(data.get("data")).get("disablePullRequestAutoMerge"))
        if data.get("errors") or result.get("clientMutationId") != identifier or goals.mapping(result.get("pullRequest")).get("id") != before["node_id"]: _transient()
        with _record(identifier, payload) as row:
            row.receipt = {**row.receipt, "mutation": {**row.receipt["mutation"], "state": "response_received"}}
    except (ServiceUnavailable, ValueError, TypeError, AttributeError, RecursionError):
        _transient()
    finally:
        if response is not None: response.close()
    check()
    after = _pull(target_connection, payload["target"], headers, check, deadline)
    if after["node_id"] != before["node_id"]: _deny()
    if after["outcome"] == "pending": _transient()
    return after


def _execute(parent, config, payload, lease):
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from integrations import http_client
    from .website_connections import require_unlocked_remote_call
    from .website_github_failure import github_failure
    identifier, credential = lease["operation_id"], None
    provider = deepcopy(payload["snapshot"]["provider"])
    try:
        _check(parent, config, payload, lease)
        require_unlocked_remote_call()
        credential = create_installation_access_token(installation_id=provider["installation_id"], repository=provider["repository"],
            repository_id=provider["repository_id"], permission_mode="write", permission_profile="pull_request_control", use_cache=False,
            before_issue=lambda: _check(parent, config, payload, lease))
        expires = getattr(credential, "expires_at", None)
        if not isinstance(expires, datetime) or timezone.is_naive(expires) or expires <= timezone.now():
            _credential(identifier, payload, mint_state="uncertain")
            _deny()
        _credential(identifier, payload, mint_state="issued", expires_at=expires.isoformat())
        _check(parent, config, payload, lease)
        _credential(identifier, payload, delivery_possible=True)
        return _disable(identifier, payload, lease, parent, config, credential)
    except (GitHubAppTokenError, http_client.RequestException) as exc:
        raise github_failure(exc, legacy_code="github_source_unavailable", legacy_message="GitHub could not stop the old article's auto-merge. Retry shortly.", legacy_retryable=True, source=True) from None
    finally:
        if credential is None:
            _credential(identifier, payload, mint_state="uncertain")
        else:
            response, confirmed = None, False
            try:
                require_unlocked_remote_call()
                response = http_client.delete("https://api.github.com/installation/token", headers={"Authorization": f"Bearer {credential.token}"}, timeout=(3, 8))
                confirmed = response.status_code == 204
            except http_client.RequestException: pass
            finally:
                try:
                    if response is not None: response.close()
                finally:
                    _credential(identifier, payload, revocation_state="confirmed" if confirmed else "uncertain", revocation_attempts=1)


def advance(parent, config, observation, *, now):
    """Stop at most one owned queued merge per pass; results grant no new effects."""
    from . import website_article_source_stop as stops
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    from .website_models import WebsiteConnectionOperation
    if observation is None: return None
    if not isinstance(observation, dict): _deny()
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        snapshot = reads._snapshot(parent, config, connection)
        reads._valid_observation(reads._row(connection, observation.get("operation_id"), lock=True), snapshot, observation)
        stamp = goals.stamp(observation["observed_at"])
        if stamp is None or not now - reads.FRESH <= stamp <= now: _deny()
        stop = stops._operation(parent.receipt[stops.KEY], lock=True)
        if stop is None: _deny()
        lease = stop.receipt.get(LEASE_KEY)
        if lease is not None:
            if not _valid_lease(lease): _deny()
            prior = _row(snapshot["read_scope"], lease["operation_id"], lock=True)
            if prior is None or goals.digest(prior.payload) != lease["purpose_digest"]: _deny()
            _valid(prior, prior.payload)
            if goals.stamp(lease["expires_at"]) > now: return None
        attempts = list(WebsiteConnectionOperation.objects.filter(connection_id=connection.pk, generation=connection.generation, action=ACTION,
            payload__snapshot__source_fence_id=snapshot["source_fence_id"]).order_by("pk")[:MAX_ATTEMPTS])
        for prior in attempts: _valid(prior, prior.payload)
        completed = {prior.payload["target"]["number"] for prior in attempts if prior.state == "completed"}
        unfinished = {prior.payload["target"]["number"] for prior in attempts if prior.state == "running"}
        candidates = [value for value in observation["pulls"] if value["matching_backend_mutations"]
            and value["number"] not in completed and (value["number"] in unfinished or value["auto_merge_pending"]
                and value["state"] == "open" and not value["merged"])]
        if not candidates: return None
        if len(attempts) >= MAX_ATTEMPTS: _deny()
        selected = candidates[0]
        payload = {"snapshot": deepcopy(snapshot), "target": {"number": selected["number"], "head_sha": selected["head_sha"],
            "head_branch": selected["head_branch"], "mutations": selected["matching_backend_mutations"]}}
        identifier = str(uuid4())
        _save_fields(parent, {})
        row = WebsiteConnectionOperation.objects.create(pk=identifier, connection=connection, generation=connection.generation,
            action=ACTION, state="running", idempotency_key=f"{connection.pk}:article-merge-stop:{identifier}", payload=deepcopy(payload), receipt={
                "schema_version": 1, "credential": {"schema_version": 1, "mint_state": "pending", "expires_at": None, "delivery_possible": False,
                    "revocation_state": "not_attempted", "revocation_attempts": 0},
                "mutation": {"state": "not_attempted", "node_id": None, "request_id": None}, "result": None})
        _valid(row, payload)
        lease = {"operation_id": identifier, "nonce": str(uuid4()), "purpose_digest": goals.digest(payload), "expires_at": (now + LEASE).isoformat()}
        stop.receipt = {**stop.receipt, LEASE_KEY: lease}
        stop.save(update_fields=["receipt", "updated_at"])
    result = _execute(parent, config, payload, lease)
    with authority_guard(contract_for(parent.connection), action="read"):
        row = _check(parent, config, payload, lease)
        receipt = {"schema_version": 1, "operation_id": identifier, "purpose_digest": goals.digest(payload), "source_fence_id": snapshot["source_fence_id"],
            "result": result, "observed_at": now.isoformat(), "quiet": False, "inventory_complete": False, "effect_authority": False, "approval_carried": False}
        receipt["digest"] = goals.digest(receipt)
        row.state = "completed"; row.receipt = {**row.receipt, "result": receipt}; row.save(update_fields=["state", "receipt", "updated_at"])
        stop = stops._operation(parent.receipt[stops.KEY], lock=True)
        # Keep the original read operation immutable, but invalidate its latest
        # cache pointer: its pending state preceded this stop or fresh GET.
        stop.receipt = {**{key: value for key, value in stop.receipt.items() if key not in {LEASE_KEY, reads.KEY}}, KEY: receipt}
        stop.save(update_fields=["receipt", "updated_at"])
        _save_fields(parent, {KEY: deepcopy(receipt)}, remove=(reads.KEY,))
        return receipt
