"""Private paid read of known publication effects after causal source stopping.

Only captured backend references are observed. Unknown worker references,
in-flight effects, complete inventory and physical quiet remain unproved.
No result changes a child, approval, source binding or execution permission.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import re
import time
from types import SimpleNamespace
from uuid import UUID, uuid4

from django.db import transaction
from django.utils import timezone

from . import website_goal as goals, website_article_source_history as history
from . import website_article_credentials as credentials
from .website_contract import WebsiteAuthorityError, evidence_digest

ACTION = "article-source-publication-read"
KEY = "article_stopped_publication_observation"
LEASE_KEY = "publication_read_lease"
LEASE = timedelta(seconds=120)
FRESH = timedelta(minutes=5)
MAX_ATTEMPTS = 32
MAX_MUTATIONS = 32


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "The stopped article publication read changed.")


def _mutation_rows(connection, run_ids):
    return list(connection.repository_mutations.filter(generation=connection.generation,
        run_id__in=run_ids).order_by("operation_id")[:MAX_MUTATIONS + 1])


def _number(url, repository):
    if not isinstance(url, str):
        _deny()
    match = re.fullmatch(re.escape(f"https://github.com/{repository}/pull/") + r"([1-9][0-9]{0,9})", url, re.IGNORECASE)
    if match is None:
        _deny()
    return int(match[1])


def inventory(connection, stop):
    """Read bounded original ledgers/results without inventing worker coverage."""
    from . import website_article_source_fence as fences
    binding = stop["source_fence"]["binding"]
    run_ids = {node["run_id"] for node in binding["runs"]}
    rows = _mutation_rows(connection, run_ids)
    if len(rows) > MAX_MUTATIONS:
        _deny()
    mutations, references = {}, set()
    for row in rows:
        prefix = str(connection.pk) + ":"
        identifier = row.operation_id.removeprefix(prefix) if isinstance(row.operation_id, str) else ""
        if (str(row.connection_id) != str(connection.pk) or row.generation != connection.generation
                or row.run_id not in run_ids or not row.operation_id.startswith(prefix)
                or not goals.DIGEST.fullmatch(identifier) or identifier in mutations
                or row.status not in {"proposed", "applied"} or not isinstance(row.files, list)
                or row.patch_digest != evidence_digest(row.files) or row.base_sha != binding["source_sha"]
                or not isinstance(row.head_sha, str) or row.head_sha and not goals.SHA.fullmatch(row.head_sha)
                or row.status == "applied" and not row.head_sha
                or not isinstance(row.branch, str) or not 0 < len(row.branch) <= 255
                or any(ord(char) < 33 for char in row.branch)):
            _deny()
        number = _number(row.pr_url, connection.github_repo) if row.pr_url else None
        mutations[identifier] = {"run_id": row.run_id, "status": row.status, "branch": row.branch,
            "base_sha": row.base_sha, "head_sha": row.head_sha, "pr_number": number,
            "files_sha256": goals.digest(row.files)}
        if number is not None:
            references.add(number)
    # Mirrored result references can reveal a PR without a mutation receipt.
    # They never establish ownership of its head or complete effect coverage.
    for node in binding["runs"]:
        run = fences._run(connection, node["run_id"])
        operation = fences._operation(connection, node["operation_id"])
        if (run is None or goals.digest(run.run_request) != node["run_request_digest"]
                or operation is None or operation.payload.get("attempt") != node["operation_attempt"]
                or goals.mapping(operation.receipt).get(fences.KEY) != stop["source_fence_id"]):
            _deny()
        result = goals.mapping(run.result)
        for field in ("pr_url", "prUrl", "publish_pr_url", "publishPrUrl"):
            if result.get(field):
                references.add(_number(result[field], connection.github_repo))
        intent = result.get("publish_merge_intent")
        if intent is not None:
            intent = goals.mapping(intent)
            number = intent.get("pr_number")
            if (type(number) is not int or not 0 < number < 10**10 or intent.get("run_id") != run.run_id
                    or intent.get("website_connection_id") != str(connection.pk)
                    or intent.get("connection_generation") != connection.generation
                    or str(intent.get("repository_id")) != str(connection.repository_id)
                    or intent.get("source_sha") != binding["source_sha"]
                    or intent.get("base_branch") != connection.branch):
                _deny()
            references.add(number)
    if len(references) > 16:
        _deny()
    return {"mutations": {key: mutations[key] for key in sorted(mutations)}, "pulls": sorted(references)}


def _snapshot(parent, config, connection):
    stop, receipt, scope, sha = history._fresh(parent, config)
    return {"stop_request_digest": stop["digest"], "source_fence_id": stop["source_fence_id"],
        "goal_operation_id": str(parent.pk), "read_scope": scope, "current_source_sha": sha,
        "stop_receipt_digest": receipt["digest"], "inventory": inventory(connection, stop),
        "provider": {"repository": connection.github_repo, "repository_id": connection.repository_id,
            "installation_id": str(connection.installation_id), "branch": connection.branch}}


def _row(connection, identifier, *, lock=False, scope=None):
    from .website_models import WebsiteConnectionOperation
    query = WebsiteConnectionOperation.objects.select_for_update() if lock else WebsiteConnectionOperation.objects
    return query.filter(pk=identifier, connection_id=scope["connection_id"] if scope else connection.pk,
        generation=scope["connection_generation"] if scope else connection.generation, action=ACTION).first()


def validate_snapshot(snapshot):
    """Validate the closed read snapshot without granting any operation purpose."""
    from .website_article_source_fence import _scope
    fields = {"stop_request_digest", "source_fence_id", "goal_operation_id", "read_scope", "current_source_sha",
        "stop_receipt_digest", "inventory", "provider"}
    try:
        valid = (isinstance(snapshot, dict) and set(snapshot) == fields and _scope(snapshot["read_scope"])
            and all(str(UUID(snapshot[key])) == snapshot[key] for key in ("source_fence_id", "goal_operation_id"))
            and all(isinstance(snapshot[key], str) and goals.DIGEST.fullmatch(snapshot[key])
                for key in ("stop_request_digest", "stop_receipt_digest"))
            and isinstance(snapshot["current_source_sha"], str) and goals.SHA.fullmatch(snapshot["current_source_sha"])
            and isinstance(snapshot["inventory"], dict) and set(snapshot["inventory"]) == {"pulls", "mutations"}
            and isinstance(snapshot["inventory"]["mutations"], dict) and len(snapshot["inventory"]["mutations"]) <= MAX_MUTATIONS
            and isinstance(snapshot["inventory"]["pulls"], list) and len(snapshot["inventory"]["pulls"]) <= 16
            and all(type(number) is int and 0 < number < 10**10 for number in snapshot["inventory"]["pulls"])
            and snapshot["inventory"]["pulls"] == sorted(set(snapshot["inventory"]["pulls"]))
            and isinstance(snapshot["provider"], dict)
            and set(snapshot["provider"]) == {"repository", "repository_id", "installation_id", "branch"}
            and type(snapshot["provider"]["repository_id"]) is int and snapshot["provider"]["repository_id"] > 0
            and str(snapshot["provider"]["repository_id"]) == snapshot["read_scope"]["repository_id"]
            and isinstance(snapshot["provider"]["repository"], str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", snapshot["provider"]["repository"])
            and isinstance(snapshot["provider"]["installation_id"], str)
            and re.fullmatch(r"[1-9][0-9]{0,19}", snapshot["provider"]["installation_id"])
            and snapshot["provider"]["branch"] == snapshot["read_scope"]["branch"])
    except (ValueError, TypeError, KeyError, AttributeError):
        valid = False
    if not valid:
        _deny()
def _valid_row(row, snapshot):
    validate_snapshot(snapshot)
    if (row is None or row.action != ACTION or row.payload != snapshot or row.state not in {"running", "completed"}
            or str(row.connection_id) != snapshot["read_scope"]["connection_id"]
            or row.generation != snapshot["read_scope"]["connection_generation"]
            or not isinstance(row.receipt, dict) or set(row.receipt) != {"schema_version", "credential", "observation"}
            or type(row.receipt.get("schema_version")) is not int or row.receipt["schema_version"] != 1
            or not (row.receipt.get("credential") is None and not snapshot["inventory"]["pulls"]
                or credentials.valid_receipt(row.receipt.get("credential")))):
        _deny()


@contextmanager
def _credential_record(connection, identifier, snapshot):
    """Settlement of an existing local mint survives owner/flag/cache revocation."""
    from .website_models import WebsiteConnection
    with transaction.atomic():
        if WebsiteConnection.objects.select_for_update().filter(pk=snapshot["read_scope"]["connection_id"]).first() is None:
            _deny()
        row = _row(connection, identifier, lock=True, scope=snapshot["read_scope"])
        _valid_row(row, snapshot)
        yield row
        row.save(update_fields=["receipt", "updated_at"])


def _settle(connection, identifier, snapshot, **changes):
    with _credential_record(connection, identifier, snapshot) as row:
        previous = row.receipt["credential"]
        if previous["revocation_state"] == "confirmed":
            changes.pop("revocation_state", None)
        row.receipt = {**row.receipt, "credential": {**previous, **changes}}
        if not credentials.valid_receipt(row.receipt["credential"]):
            _deny()


def _check(parent, config, snapshot, lease):
    from . import website_article_source_stop as stops
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        if _snapshot(parent, config, connection) != snapshot:
            _deny()
        stop = stops._operation(parent.receipt[stops.KEY], lock=True)
        if stop is None or stop.receipt.get(LEASE_KEY) != lease:
            _deny()
        row = _row(connection, lease["operation_id"], lock=True)
        _valid_row(row, snapshot)
        if row.state != "running":
            _deny()
        _save_fields(parent, {})  # original prepare claim, not just the read lease
        return row


def _read(connection, recorded, credential, recheck):
    from .website_article_publication_observation import _provider_json, _repository
    deadline = time.monotonic() + 60
    headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
    repo = "/repos/" + connection.github_repo
    recheck()
    if not _repository(_provider_json(repo, headers=headers, deadline=deadline), connection):
        _deny()
    pulls = []
    for number in recorded["pulls"]:
        recheck()
        value = _provider_json(repo + f"/pulls/{number}", headers=headers, deadline=deadline)
        recheck()
        head, base = goals.mapping(value.get("head")), goals.mapping(value.get("base"))
        merged, state, merge_sha = value.get("merged"), value.get("state"), value.get("merge_commit_sha")
        if (type(value.get("number")) is not int or value["number"] != number
                or value.get("html_url", "").casefold() != f"https://github.com/{connection.github_repo}/pull/{number}".casefold()
                or not all(_repository(side.get("repo"), connection) for side in (head, base))
                or base.get("ref") != connection.branch or not isinstance(head.get("ref"), str)
                or not 0 < len(head["ref"]) <= 255 or any(ord(char) < 33 for char in head["ref"])
                or not isinstance(head.get("sha"), str) or not goals.SHA.fullmatch(head["sha"])
                or state not in {"open", "closed"} or type(merged) is not bool
                or merged and (state != "closed" or not isinstance(merge_sha, str) or not goals.SHA.fullmatch(merge_sha))
                or value.get("auto_merge") is not None and not isinstance(value["auto_merge"], dict)):
            _deny()
        matching = sorted(key for key, row in recorded["mutations"].items()
            if row["status"] == "applied" and row["pr_number"] == number
            and row["branch"] == head["ref"] and row["head_sha"] == head["sha"])
        pulls.append({"number": number, "head_sha": head["sha"], "head_branch": head["ref"],
            "state": state, "merged": merged, "merge_sha": merge_sha if merged else None,
            "auto_merge_pending": value.get("auto_merge") is not None, "matching_backend_mutations": matching})
    return pulls


def _observe(parent, config, snapshot, lease):
    """Mint once after durable reservation, keep the bearer local, retire finally."""
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from integrations import http_client
    from .website_github_failure import github_failure
    from .website_connections import require_unlocked_remote_call
    connection, credential = parent.connection, None
    provider = deepcopy(snapshot["provider"])
    identifier = lease["operation_id"]
    if not snapshot["inventory"]["pulls"]:
        _check(parent, config, snapshot, lease)
        return []  # No known reference needs HTTP; this never proves absence.
    try:
        _check(parent, config, snapshot, lease)
        require_unlocked_remote_call()
        credential = create_installation_access_token(installation_id=provider["installation_id"],
            repository=provider["repository"], repository_id=provider["repository_id"], permission_mode="read", use_cache=False)
        expires = getattr(credential, "expires_at", None)
        if not isinstance(expires, datetime) or timezone.is_naive(expires) or expires <= timezone.now():
            _settle(connection, identifier, snapshot, mint_state="uncertain")
            _deny()
        _settle(connection, identifier, snapshot, mint_state="issued", expires_at=expires.isoformat())
        _check(parent, config, snapshot, lease)
        _settle(connection, identifier, snapshot, delivery_possible=True)
        target = SimpleNamespace(github_repo=provider["repository"], repository_id=provider["repository_id"], branch=provider["branch"])
        return _read(target, snapshot["inventory"], credential, lambda: _check(parent, config, snapshot, lease))
    except (GitHubAppTokenError, http_client.RequestException) as exc:
        raise github_failure(exc, legacy_code="github_source_unavailable",
            legacy_message="GitHub could not verify the stopped article publication. Retry shortly.", legacy_retryable=True, source=True) from None
    finally:
        if credential is None:
            _settle(connection, identifier, snapshot, mint_state="uncertain")
        else:
            confirmed = False
            response = None
            try:
                require_unlocked_remote_call()
                response = http_client.delete("https://api.github.com/installation/token",
                    headers={"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}, timeout=(3, 8))
                confirmed = response.status_code == 204
            except http_client.RequestException:
                pass
            finally:
                if response is not None:
                    response.close()
                _settle(connection, identifier, snapshot, revocation_state="confirmed" if confirmed else "uncertain", revocation_attempts=1)


def _valid_observation(row, snapshot, saved):
    _valid_row(row, snapshot)
    fields = {"schema_version", "kind", "purpose_digest", "operation_id", "source_fence_id", "scope", "pulls",
        "observed_at", "quiet", "inventory_complete", "effect_authority", "approval_carried", "digest"}
    if (not isinstance(saved, dict) or set(saved) != fields or type(saved.get("schema_version")) is not int
            or saved["schema_version"] != 1 or saved["kind"] != "article_stopped_publication_observation"
            or saved["digest"] != goals.digest({k: v for k, v in saved.items() if k != "digest"})
            or saved["purpose_digest"] != goals.digest(snapshot) or saved["operation_id"] != str(row.pk)
            or saved["source_fence_id"] != snapshot["source_fence_id"] or saved["scope"] != "captured_backend_references"
            or any(saved[key] is not False for key in ("quiet", "inventory_complete", "effect_authority", "approval_carried"))
            or not goals.stamp(saved["observed_at"]) or not isinstance(saved["pulls"], list)
            or len(saved["pulls"]) != len(snapshot["inventory"]["pulls"])
            or [value.get("number") for value in saved["pulls"] if isinstance(value, dict)] != snapshot["inventory"]["pulls"]
            or row.state != "completed" or row.receipt["observation"] != saved):
        _deny()
    fields = {"number", "head_sha", "head_branch", "state", "merged", "merge_sha", "auto_merge_pending", "matching_backend_mutations"}
    for pull in saved["pulls"]:
        if (not isinstance(pull, dict) or set(pull) != fields or type(pull["number"]) is not int
                or not isinstance(pull["head_sha"], str) or not goals.SHA.fullmatch(pull["head_sha"])
                or not isinstance(pull["head_branch"], str) or not 0 < len(pull["head_branch"]) <= 255
                or any(ord(char) < 33 for char in pull["head_branch"])
                or pull["state"] not in {"open", "closed"} or type(pull["merged"]) is not bool
                or type(pull["auto_merge_pending"]) is not bool
                or pull["merged"] and (pull["state"] != "closed" or not isinstance(pull["merge_sha"], str)
                    or not goals.SHA.fullmatch(pull["merge_sha"]))
                or not pull["merged"] and pull["merge_sha"] is not None):
            _deny()
        matching = sorted(key for key, mutation in snapshot["inventory"]["mutations"].items()
            if mutation["status"] == "applied" and mutation["pr_number"] == pull["number"]
            and mutation["branch"] == pull["head_branch"] and mutation["head_sha"] == pull["head_sha"])
        if pull["matching_backend_mutations"] != matching:
            _deny()


def advance(parent, config, *, now):
    """Observe known original PRs under a distinct leased, paid current purpose."""
    from . import website_article_source_stop as stops
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields
    from .website_models import WebsiteConnectionOperation
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        snapshot = _snapshot(parent, config, connection)
        stop = stops._operation(parent.receipt[stops.KEY], lock=True)
        saved = stop.receipt.get(KEY)
        if saved is not None:
            if (not isinstance(saved, dict) or saved.get("digest") != goals.digest({k: v for k, v in saved.items() if k != "digest"})
                    or saved.get("quiet") is not False or saved.get("inventory_complete") is not False
                    or saved.get("effect_authority") is not False or saved.get("approval_carried") is not False):
                _deny()
            stamp = goals.stamp(saved.get("observed_at"))
            if saved.get("purpose_digest") == goals.digest(snapshot) and stamp is not None and now - FRESH <= stamp <= now:
                _valid_observation(_row(connection, saved.get("operation_id"), lock=True), snapshot, saved)
                _save_fields(parent, {KEY: deepcopy(saved)})
                return saved
        lease = stop.receipt.get(LEASE_KEY)
        if lease is not None:
            try:
                valid = (isinstance(lease, dict) and set(lease) == {"operation_id", "purpose_digest", "expires_at"}
                    and str(UUID(lease["operation_id"])) == lease["operation_id"]
                    and goals.stamp(lease["expires_at"]) is not None
                    and isinstance(lease["purpose_digest"], str) and goals.DIGEST.fullmatch(lease["purpose_digest"]))
            except (ValueError, TypeError, KeyError, AttributeError):
                valid = False
            if not valid:
                _deny()
            prior = _row(connection, lease["operation_id"], lock=True)
            if (prior is None or not isinstance(prior.payload, dict) or goals.digest(prior.payload) != lease["purpose_digest"]
                    or prior.payload.get("source_fence_id") != snapshot["source_fence_id"]
                    or prior.payload.get("goal_operation_id") != str(parent.pk)):
                _deny()
            _valid_row(prior, prior.payload)
            if goals.stamp(lease["expires_at"]) > now:
                return None
        attempts = list(WebsiteConnectionOperation.objects.filter(connection_id=connection.pk, action=ACTION,
            payload__source_fence_id=snapshot["source_fence_id"]).order_by("pk")[:MAX_ATTEMPTS])
        if len(attempts) >= MAX_ATTEMPTS:
            _deny()
        _save_fields(parent, {})
        identifier = uuid4()
        WebsiteConnectionOperation.objects.create(pk=identifier, connection=connection, generation=connection.generation,
            action=ACTION, state="running", idempotency_key=f"{connection.pk}:article-publication-read:{identifier}",
            payload=deepcopy(snapshot), receipt={"schema_version": 1, "observation": None, "credential": {
                "schema_version": 1, "mint_state": "pending", "expires_at": None, "delivery_possible": False,
                "revocation_state": "not_attempted", "revocation_attempts": 0} if snapshot["inventory"]["pulls"] else None})
        lease = {"operation_id": str(identifier), "purpose_digest": goals.digest(snapshot), "expires_at": (now + LEASE).isoformat()}
        stop.receipt = {**stop.receipt, LEASE_KEY: lease}
        stop.save(update_fields=["receipt", "updated_at"])
    pulls = _observe(parent, config, snapshot, lease)
    with authority_guard(contract_for(parent.connection), action="read"):
        row = _check(parent, config, snapshot, lease)
        observed = {"schema_version": 1, "kind": "article_stopped_publication_observation", "purpose_digest": goals.digest(snapshot),
            "operation_id": lease["operation_id"], "source_fence_id": snapshot["source_fence_id"],
            "scope": "captured_backend_references", "pulls": pulls, "observed_at": now.isoformat(),
            "quiet": False, "inventory_complete": False, "effect_authority": False, "approval_carried": False}
        observed["digest"] = goals.digest(observed)
        row.state = "completed"
        row.receipt = {**row.receipt, "observation": observed}
        row.save(update_fields=["state", "receipt", "updated_at"])
        stop = stops._operation(parent.receipt[stops.KEY], lock=True)
        stop.receipt = {**{key: value for key, value in stop.receipt.items() if key != LEASE_KEY}, KEY: observed}
        stop.save(update_fields=["receipt", "updated_at"])
        _save_fields(parent, {KEY: deepcopy(observed)})
        return observed
