"""Bound CF task evidence to backend-owned original run locators over HTTPS."""
from datetime import timedelta
import json
import re
import time
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.utils import timezone
import requests

from . import website_goal as goals, website_rollout_inspection as rollout

MAX_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_SECONDS = 10
BASIS = "original_goal_cf_task_executions_v1"


def _lease(value):
    return (value is None or isinstance(value, dict) and set(value) == {"lease_id", "owner_digest"}
        and isinstance(value.get("lease_id"), str) and re.fullmatch(r"[a-f0-9]{32}", value["lease_id"]) is not None
        and isinstance(value.get("owner_digest"), str) and goals.DIGEST.fullmatch(value["owner_digest"]) is not None)


def _handoff(previous, current):
    """An acquired foreign guard fence closes an interrupted same-generation task."""
    proof, before, after = current["handoff_proof"], previous["lease_proof"], current["lease_proof"]
    if (not isinstance(proof, dict) or set(proof) != {"previous_lease_id", "previous_owner_digest",
            "previous_heartbeat_at", "previous_released", "previous_outcome", "ttl_seconds", "observed_at"}
            or before is None or after is None or not _lease(before) or not _lease(after)
            or proof["previous_lease_id"] != before["lease_id"] or proof["previous_owner_digest"] != before["owner_digest"]
            or before["lease_id"] == after["lease_id"] or before["owner_digest"] == after["owner_digest"]
            or any(current[key] != previous[key] for key in ("generation", "request_scope_digest", "goal_lineage_digest"))
            or type(proof["previous_released"]) is not bool
            or proof["previous_outcome"] not in {None, "released", "retry_scheduled", "worker_shutdown", "poisoned", "task_raised"}
            or type(proof["ttl_seconds"]) not in {int, float} or not 0 < proof["ttl_seconds"] <= 604800):
        return False
    at, beat = goals.stamp(proof["observed_at"]), goals.stamp(proof["previous_heartbeat_at"])
    if (at is None or beat is None or at < beat or at < goals.stamp(previous["observed_at"])
            or at > goals.stamp(current["observed_at"])):
        return False
    return ((at - beat).total_seconds() >= proof["ttl_seconds"]
        or proof["previous_released"] and proof["previous_outcome"] == "worker_shutdown")


def _epochs(value, locator, *, goal_id):
    """Validate retained attempt tuples against the exact current owned tuple."""
    from .website_zoo_scope import SCOPE_FIELDS, OPTIONAL_FIELDS, uuid_id
    epochs = value.get("scope_epochs")
    if not isinstance(epochs, list) or not 1 <= len(epochs) <= 32:
        return None
    by_digest, previous_attempt, stable, stable_lineage = {}, 0, None, None
    for epoch in epochs:
        if (not isinstance(epoch, dict) or set(epoch) != {"request_scope_digest", "goal_lineage_digest",
                "scope_fields", "lineage_fields", "scope", "lineage"}):
            return None
        scope, lineage = epoch["scope"], epoch["lineage"]
        if (not isinstance(scope, dict) or not set(SCOPE_FIELDS) <= set(scope)
                or set(scope) - set(SCOPE_FIELDS) - set(OPTIONAL_FIELDS)
                or epoch["scope_fields"] != sorted(scope) or epoch["scope_fields"] != locator["scope_fields"]
                or epoch["request_scope_digest"] != goals.digest(scope)
                or not isinstance(lineage, dict) or not lineage
                or set(lineage) - {"first_article_goal_id", "first_article_goal_operation_id", "strategy_goal_id", "goal_budget"}
                or epoch["lineage_fields"] != sorted(lineage)
                or epoch["goal_lineage_digest"] != goals.digest(lineage)
                or any(value != goal_id for key, value in lineage.items() if key != "goal_budget")
                or "goal_budget" in lineage and (not isinstance(lineage["goal_budget"], dict)
                    or set(lineage["goal_budget"]) != {"goal_id", "scope_digest"}
                    or lineage["goal_budget"]["goal_id"] != goal_id
                    or not isinstance(lineage["goal_budget"]["scope_digest"], str)
                    or not goals.DIGEST.fullmatch(lineage["goal_budget"]["scope_digest"]))
                or any(not uuid_id(scope[key]) for key in ("website_connection_id", "operation_id"))
                or any(type(scope[key]) is not int or scope[key] < 1 for key in ("connection_generation", "repository_id", "operation_attempt"))
                or type(scope["deletion_epoch"]) is not int or scope["deletion_epoch"] < 0
                or any(not isinstance(scope[key], str) or not 1 <= len(scope[key]) <= 255
                    for key in ("domain", "github_repo", "client_request_id"))
                or "expected_source_sha" in scope and (not isinstance(scope["expected_source_sha"], str)
                    or not goals.SHA.fullmatch(scope["expected_source_sha"]))
                or "configuration_revision" in scope and (type(scope["configuration_revision"]) is not int or scope["configuration_revision"] < 1)
                or any(key in scope and (not isinstance(scope[key], str) or len(scope[key]) > 1000)
                    for key in ("branch", "app_root", "connection_target_id"))):
            return None
        current = {key: item for key, item in scope.items() if key != "operation_attempt"}
        if (scope["operation_attempt"] <= previous_attempt
                or stable is not None and current != stable
                or stable_lineage is not None and lineage != stable_lineage
                or epoch["request_scope_digest"] in by_digest):
            return None
        stable, stable_lineage, previous_attempt = current, lineage, scope["operation_attempt"]
        by_digest[epoch["request_scope_digest"]] = epoch
    latest = epochs[-1]
    if any(latest[key] != locator[key] for key in ("request_scope_digest", "goal_lineage_digest", "scope_fields", "lineage_fields")):
        return None
    return by_digest


def project(value, locator, *, goal_id, now):
    """Accept only exact saved scope/lineage and ordered task entry/outcome pairs."""
    header = {"schema_version", "basis", "run_id", "goal_id", "scope_fields", "request_scope_digest",
        "lineage_fields", "goal_lineage_digest", "overflow", "coverage", "records", "gaps", "coverage_scope", "scope_epochs"}
    fields = {"sequence", "execution_id", "event", "outcome", "task", "instance_id", "observed_at", "revision",
        "configuration", "flags", "run_predicates", "generation", "state_version", "request_scope_digest", "goal_lineage_digest",
        "lease_proof", "handoff_proof"}
    try:
        if (not isinstance(value, dict) or set(value) != header
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or value["basis"] != "cf_admitted_task_execution_v1" or value["goal_id"] != goal_id
                or value["coverage_scope"] != "admitted_task_body_journal_only"
                or value["coverage"] not in {"complete", "partial"} or type(value["overflow"]) is not bool
                or any(value[key] != locator[key] for key in ("run_id", "scope_fields", "request_scope_digest", "lineage_fields", "goal_lineage_digest"))
                or not isinstance(value["records"], list) or not 1 <= len(value["records"]) <= 256):
            return None
        epochs = _epochs(value, locator, goal_id=goal_id)
        if epochs is None:
            return None
        opened, seen, rows = {}, set(), []
        for number, row in enumerate(value["records"], 1):
            at = goals.stamp(row.get("observed_at")) if isinstance(row, dict) else None
            if (not isinstance(row, dict) or set(row) != fields or type(row["sequence"]) is not int or row["sequence"] != number
                    or row["event"] not in {"entry", "outcome"} or at is None or at > now + timedelta(seconds=30)
                    or any(not isinstance(row[key], str) or not re.fullmatch(r"[a-f0-9]{32}", row[key]) for key in ("execution_id", "instance_id"))
                    or not isinstance(row["task"], str) or not re.fullmatch(r"[A-Za-z0-9_.:]{1,160}", row["task"])
                    or row["revision"] is not None and (not isinstance(row["revision"], str) or not goals.SHA.fullmatch(row["revision"]))
                    or row["configuration"] not in {"observed", "unavailable"}
                    or not isinstance(row["flags"], dict) or set(row["flags"]) != set(rollout.CF_FLAGS)
                    or any(flag is not None and type(flag) is not bool for flag in row["flags"].values())
                    or row["configuration"] == "observed" and any(type(flag) is not bool for flag in row["flags"].values())
                    or row["configuration"] == "unavailable" and any(flag is not None for flag in row["flags"].values())
                    or not isinstance(row["run_predicates"], dict) or set(row["run_predicates"]) != set(rollout.SCOPED_PREDICATES)
                    or any(flag is not None and type(flag) is not bool for flag in row["run_predicates"].values())
                    or not _lease(row["lease_proof"])
                    or row["outcome"] != "lease_superseded" and row["handoff_proof"] is not None
                    or row["request_scope_digest"] not in epochs
                    or row["goal_lineage_digest"] != epochs[row["request_scope_digest"]]["goal_lineage_digest"]
                    or any(type(row[key]) is not int or row[key] < 0 for key in ("generation", "state_version"))
                    or any(type(locator["execution"].get(key)) is not int for key in ("generation", "state_version"))
                    or row["generation"] > locator["execution"]["generation"]
                    or row["generation"] == locator["execution"]["generation"]
                        and row["state_version"] > locator["execution"]["state_version"]):
                return None
            identifier = row["execution_id"]
            if row["event"] == "entry":
                if identifier in seen or row["outcome"] is not None:
                    return None
                opened[identifier] = row; seen.add(identifier)
            else:
                if (identifier not in opened or row["outcome"] not in {"returned", "raised", "generation_superseded", "lease_superseded"}
                        or row["outcome"] == "generation_superseded" and row["generation"] <= opened[identifier]["generation"]
                        or row["outcome"] == "lease_superseded" and not _handoff(opened[identifier], row)
                        or row["outcome"] not in {"generation_superseded", "lease_superseded"}
                            and any(row[key] != opened[identifier][key] for key in (
                                "generation", "request_scope_digest", "goal_lineage_digest", "lease_proof"))):
                    return None
                del opened[identifier]
            rows.append({key: row[key] for key in fields})
        gaps = (["observation_capacity"] if value["overflow"] else []) + (["task_outcome_unobserved"] if opened else [])
        if value["gaps"] != gaps or value["coverage"] != ("partial" if gaps else "complete"):
            return None
        safe_epochs = [{**{key: epoch[key] for key in ("request_scope_digest", "goal_lineage_digest", "scope_fields", "lineage_fields")},
            "operation_attempt": epoch["scope"]["operation_attempt"]} for epoch in epochs.values()]
        return {key: rows if key == "records" else safe_epochs if key == "scope_epochs" else value[key] for key in header}
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None


def fetch(original):
    """Bound all reads by one deadline and byte budget; never hydrate or mutate."""
    runs, gaps, used = [], [], 0
    result = {"schema_version": 1, "basis": BASIS, "coverage": "partial", "runs": runs, "gaps": gaps}
    if not original.get("runs_complete") or not original.get("runs"):
        gaps.append("original_run_scope_incomplete")
        return result
    try:
        base, key = getattr(settings, "CONTENT_FACTORY_URL", ""), getattr(settings, "CONTENT_FACTORY_API_KEY", None)
        parsed = urlsplit(base)
        if (not isinstance(key, str) or not key or parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment or len(base) > 2048):
            raise ValueError
        started = time.monotonic()
        with requests.Session() as client:
            client.trust_env = False
            for locator in original["runs"]:
                remaining = MAX_SECONDS - (time.monotonic() - started)
                if remaining <= 0 or used >= MAX_TOTAL_BYTES:
                    gaps.append("cf_execution_collection_bounds")
                    break
                url = base.rstrip("/") + "/api/internal/articles/execution-observations?" + urlencode({"run_id": locator["run_id"]})
                try:
                    raw = bytearray()
                    with client.get(url, headers={"X-API-Key": key, "Accept": "application/json"}, stream=True,
                            allow_redirects=False, timeout=(min(3, remaining), min(8, remaining))) as response:
                        if response.status_code != 200 or response.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
                            raise ValueError
                        for chunk in response.iter_content(chunk_size=8192):
                            used += len(chunk)
                            if time.monotonic() - started > MAX_SECONDS or len(raw) + len(chunk) > MAX_BYTES or used > MAX_TOTAL_BYTES:
                                raise ValueError
                            raw.extend(chunk)
                    observed = project(json.loads(raw), locator, goal_id=original["original_goal_id"], now=timezone.now())
                    if observed is None:
                        raise ValueError
                    runs.append(observed)
                    if observed["coverage"] != "complete":
                        gaps.append("cf_task_execution_partial")
                except Exception:
                    gaps.append("cf_task_execution_unavailable")
    except Exception:
        gaps.append("cf_authenticated_transport_unavailable")
    if len(runs) != len(original["runs"]):
        gaps.append("cf_run_execution_coverage_incomplete")
    result["gaps"] = sorted(set(gaps))
    result["coverage"] = "partial" if gaps else "complete"
    return result
