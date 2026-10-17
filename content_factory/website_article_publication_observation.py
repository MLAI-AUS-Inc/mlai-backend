"""Read old article effects through current authority, without repository writes.

An observation is not a source fence, approval, durable content seal or proof
that unrecorded effects cannot exist. Later effect stages must recheck it.
"""
from copy import deepcopy
from datetime import timedelta
import json
import time
from urllib.parse import quote
from uuid import UUID, uuid4

from . import website_article_refresh as refresh, website_article_refresh_intent as intents, website_goal as goals
from .article_source_refresh_contract import digest, validate_inspection
from .bounded_service_read import read_limited, ServiceUnavailable
from .website_contract import WebsiteAuthorityError, evidence_digest

KEY = "article_source_refresh_publication_observation"
LEASE = timedelta(seconds=120)
MAX_MUTATIONS = 32


def _deny():
    raise WebsiteAuthorityError("website_operation_changed", "Original article publication evidence changed.")


def _transient():
    raise WebsiteAuthorityError("website_prepare_dependency_transient", "Waiting to verify the original article publication.", retryable=True)


def _mutation_rows(connection, run_ids):
    return list(connection.repository_mutations.filter(generation=connection.generation,
        run_id__in=run_ids).order_by("operation_id")[:MAX_MUTATIONS + 1])


def inventory(connection, request, inspection):
    """Union private worker references and backend receipts, retaining ambiguity."""
    inspection = validate_inspection(inspection, request)
    root = request["intent"]["binding"]["paid_root"]["run_id"]
    runs = {root, *(child["run_id"] for child in inspection["publication_children"])}
    rows = _mutation_rows(connection, runs)
    if len(rows) > MAX_MUTATIONS:
        _deny()
    result, seen = {}, set()
    prefix = str(connection.pk) + ":"
    for row in rows:
        if (str(row.connection_id) != str(connection.pk) or row.generation != connection.generation
                or row.run_id not in runs or not isinstance(row.operation_id, str) or not row.operation_id.startswith(prefix)
                or not goals.DIGEST.fullmatch(row.operation_id[len(prefix):]) or row.operation_id in seen
                or row.status not in {"proposed", "applied"} or not isinstance(row.files, list)
                or row.patch_digest != evidence_digest(row.files)):
            _deny()
        seen.add(row.operation_id)
        item = {"mutation_id": row.operation_id[len(prefix):], "run_id": row.run_id,
            "base_sha": row.base_sha, "head_sha": row.head_sha, "branch": row.branch,
            "pr_url": row.pr_url, "status": row.status, "files_sha256": digest(row.files)}
        # The shared closed validator also validates DB-owned rows, including
        # historical main-branch effects; such effects must not disappear.
        candidate = {**inspection, "publications": [item]}
        candidate["digest"] = digest({key: value for key, value in candidate.items() if key != "digest"})
        validate_inspection(candidate, request)
        result[item["mutation_id"]] = {"backend": item, "worker": None}
    for item in inspection["publications"]:
        if item["run_id"] not in runs:
            _deny()
        result.setdefault(item["mutation_id"], {"backend": None, "worker": None})["worker"] = deepcopy(item)
    if len(result) > MAX_MUTATIONS:
        _deny()
    pulls, branches = set(), {connection.branch}
    for pair in result.values():
        for item in pair.values():
            if item is None:
                continue
            branches.add(item["branch"])
            if item["pr_url"]:
                pulls.add(int(item["pr_url"].rsplit("/", 1)[1]))
    pulls.update(row["number"] for row in inspection["publication_refs"])
    if len(pulls) > 16 or len(branches) > MAX_MUTATIONS + 1:
        _deny()
    return {"schema_version": 1, "mutations": {key: result[key] for key in sorted(result)},
        "pulls": sorted(pulls), "branches": sorted(branches), "references": inspection["publication_refs"]}


def _provider_json(path, *, headers, deadline):
    from .website_connections import require_unlocked_remote_call
    require_unlocked_remote_call()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _transient()
    response = None
    try:
        response = read_limited("GET", "https://api.github.com" + path, headers=headers,
            maximum=65536, deadline=min(12, remaining))
        if (response.status_code == 429 or response.status_code >= 500
                or response.status_code == 403 and (response.headers.get("retry-after")
                    or response.headers.get("x-ratelimit-remaining") == "0")):
            _transient()
        if response.status_code != 200:
            # A missing/inaccessible PR or branch is not proof of no effect.
            _deny()
        value = json.loads(response.content)
        if not isinstance(value, dict):
            _deny()
        return value
    except WebsiteAuthorityError:
        raise
    except ServiceUnavailable:
        _transient()
    except (ValueError, TypeError, RecursionError):
        _deny()
    finally:
        if response is not None:
            response.close()


def _repository(value, connection):
    return (isinstance(value, dict) and type(value.get("id")) is int and value["id"] == connection.repository_id
        and isinstance(value.get("full_name"), str) and value["full_name"].casefold() == connection.github_repo.casefold())


def read_provider(connection, request, inspection, recorded, *, now):
    """Read fresh repository, branches and every known PR with one read token."""
    from .website_connections import require_unlocked_remote_call
    from integrations.services.github_app import create_installation_access_token, GitHubAppTokenError
    from integrations import http_client
    from .website_github_failure import github_failure
    require_unlocked_remote_call()
    deadline, credential, headers = time.monotonic() + 60, None, {}
    try:
        credential = create_installation_access_token(installation_id=connection.installation_id,
            repository=connection.github_repo, repository_id=connection.repository_id, permission_mode="read", use_cache=False)
        headers = {"Authorization": f"Bearer {credential.token}", "Accept": "application/vnd.github+json"}
        repo = "/repos/" + connection.github_repo
        if not _repository(_provider_json(repo, headers=headers, deadline=deadline), connection):
            _deny()
        branches = {}
        for branch in recorded["branches"]:
            value = _provider_json(repo + "/branches/" + quote(branch, safe=""), headers=headers, deadline=deadline)
            sha = goals.mapping(value.get("commit")).get("sha")
            if value.get("name") != branch or not isinstance(sha, str) or not goals.SHA.fullmatch(sha):
                _deny()
            branches[branch] = sha
        if branches[connection.branch] != request["intent"]["binding"]["source_sha"]:
            _transient()
        pulls = []
        for number in recorded["pulls"]:
            value = _provider_json(repo + f"/pulls/{number}", headers=headers, deadline=deadline)
            head, base = goals.mapping(value.get("head")), goals.mapping(value.get("base"))
            if (type(value.get("number")) is not int or value["number"] != number
                    or value.get("html_url", "").casefold() != f"https://github.com/{connection.github_repo}/pull/{number}".casefold()
                    or not all(_repository(side.get("repo"), connection) for side in (head, base))
                    or base.get("ref") != connection.branch or not isinstance(head.get("ref"), str)
                    or not 0 < len(head["ref"]) <= 255 or any(ord(char) < 33 for char in head["ref"])
                    or not isinstance(head.get("sha"), str) or not goals.SHA.fullmatch(head["sha"])
                    or value.get("state") not in {"open", "closed"} or type(value.get("merged")) is not bool
                    or value.get("merged") and (value["state"] != "closed" or not isinstance(value.get("merge_commit_sha"), str)
                        or not goals.SHA.fullmatch(value["merge_commit_sha"]))
                    or value.get("auto_merge") is not None and not isinstance(value["auto_merge"], dict)):
                _deny()
            if head["ref"] not in branches:
                branch = _provider_json(repo + "/branches/" + quote(head["ref"], safe=""), headers=headers, deadline=deadline)
                sha = goals.mapping(branch.get("commit")).get("sha")
                if branch.get("name") != head["ref"] or not isinstance(sha, str) or not goals.SHA.fullmatch(sha):
                    _deny()
                branches[head["ref"]] = sha
            matching = [pair["backend"] for pair in recorded["mutations"].values()
                if pair["backend"] is not None and pair["backend"]["status"] == "applied"
                and pair["backend"]["branch"] == head["ref"] and pair["backend"]["head_sha"] == head["sha"]
                and pair["backend"]["pr_url"].casefold() == f"https://github.com/{connection.github_repo}/pull/{number}".casefold()]
            pulls.append({"number": number, "head_sha": head["sha"], "head_branch": head["ref"],
                "state": value["state"], "merged": value["merged"],
                "merge_sha": value.get("merge_commit_sha") if value["merged"] else None,
                "auto_merge_pending": value.get("auto_merge") is not None,
                "matching_backend_mutations": sorted(row["mutation_id"] for row in matching),
                "branch_head_matches": branches[head["ref"]] == head["sha"]})
        value = {"schema_version": 1, "kind": "article_source_publication_observation", "request_digest": request["digest"],
            "inspection_digest": inspection["digest"], "inventory_digest": digest(recorded),
            "repository_id": connection.repository_id, "source_sha": branches[connection.branch], "branches": branches,
            "pulls": pulls, "recorded_mutations": len(recorded["mutations"]),
            "unpaired_mutations": sorted(key for key, pair in recorded["mutations"].items()
                if pair["backend"] != pair["worker"]),
            "unattributed_pulls": [row["number"] for row in pulls if not row["matching_backend_mutations"]],
            "observed_at": now.isoformat(), "effect_authority": False, "absence_proved": False}
        value["digest"] = digest(value)
        return value
    except WebsiteAuthorityError:
        raise
    except (GitHubAppTokenError, http_client.RequestException) as exc:
        raise github_failure(exc, legacy_code="github_source_unavailable",
            legacy_message="GitHub could not verify the original article publication.", legacy_retryable=True, source=True) from None
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        _deny()
    finally:
        if credential is not None:
            try:
                http_client.delete("https://api.github.com/installation/token", headers=headers, timeout=(3, 8))
            except http_client.RequestException:
                pass


def advance(parent, context, config, *, user, now, request, inspection):
    """Journal one bounded provider observation and wait for owned source fencing."""
    from .website_connections import authority_guard, contract_for
    from .website_first_article import _save_fields, _wait
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        operation = refresh._inspection_operation(request["operation_id"], lock=True)
        refresh._validate_operation(operation, parent, request)
        current = intents._current_binding(parent, context, config, user=user, connection=connection)
        if not intents.binding_matches(request["intent"]["binding"], current):
            _deny()
        recorded = inventory(connection, request, inspection)
        saved = parent.receipt.get(KEY)
        if saved is not None:
            if (not isinstance(saved, dict) or saved.get("digest") != digest({key: value for key, value in saved.items() if key != "digest"})
                    or saved.get("effect_authority") is not False or saved.get("absence_proved") is not False):
                _deny()
            checked = goals.stamp(saved.get("observed_at"))
            if (saved.get("request_digest") == request["digest"] and saved.get("inspection_digest") == inspection["digest"]
                    and saved.get("inventory_digest") == digest(recorded) and checked is not None
                    and now - timedelta(minutes=5) <= checked <= now + timedelta(minutes=5)):
                _wait(parent, now, "Securing the original publication before rebuilding on the changed source.")
                return True
        lease = goals.mapping(operation.receipt).get("publication_observation_lease")
        if lease is not None:
            try:
                if (not isinstance(lease, dict) or set(lease) != {"nonce", "request_digest", "inspection_digest", "expires_at"}
                        or str(UUID(lease["nonce"])) != lease["nonce"] or goals.stamp(lease["expires_at"]) is None
                        or lease["request_digest"] != request["digest"]):
                    _deny()
            except (ValueError, TypeError, KeyError):
                _deny()
            if goals.stamp(lease["expires_at"]) > now:
                _wait(parent, now, "Waiting to verify the original article publication.")
                parent.next_attempt_at = max(parent.next_attempt_at, goals.stamp(lease["expires_at"]))
                return True
        _save_fields(parent, {})
        lease = {"nonce": str(uuid4()), "request_digest": request["digest"], "inspection_digest": inspection["digest"],
            "expires_at": (now + LEASE).isoformat()}
        operation.receipt = {**goals.mapping(operation.receipt), "publication_observation_lease": lease}
        operation.save(update_fields=["receipt", "updated_at"])
    observed = read_provider(connection, request, inspection, recorded, now=now)
    with authority_guard(contract_for(parent.connection), action="read") as connection:
        operation = refresh._inspection_operation(request["operation_id"], lock=True)
        refresh._validate_operation(operation, parent, request)
        current = intents._current_binding(parent, context, config, user=user, connection=connection)
        if (not intents.binding_matches(request["intent"]["binding"], current)
                or goals.mapping(operation.receipt).get("publication_observation_lease") != lease
                or parent.receipt.get("article_source_refresh_inspection") != inspection
                or inventory(connection, request, inspection) != recorded):
            _deny()
        _save_fields(parent, {KEY: observed, "article_source_refresh_stage": "publication_observed"})
        operation.receipt = {key: value for key, value in operation.receipt.items() if key != "publication_observation_lease"}
        operation.save(update_fields=["receipt", "updated_at"])
    _wait(parent, now, "Securing the original publication before rebuilding on the changed source.")
    return True
