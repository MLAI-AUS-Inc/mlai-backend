"""Closed, content-bound intent recorded before an article's Git push."""
from hashlib import sha256
import re

from .website_failure_contract import canonical_bytes

FIELDS = {"version", "purpose", "run_id", "goal_id", "scope", "generation", "mutation_id",
    "base_sha", "head_sha", "tree_sha", "branch_name", "push_repository", "files_digest", "file_count", "intent_id"}
SCOPE = {"website_connection_id", "connection_generation", "connection_target_id", "repository_id",
    "github_repo", "expected_source_sha", "operation_id", "operation_attempt", "deletion_epoch",
    "client_request_id", "branch", "app_root"}
SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
HASH = re.compile(r"[a-f0-9]{64}\Z")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


def digest(value):
    return sha256(canonical_bytes(value)).hexdigest()


def validate(value):
    """Accept bounded identifiers and hashes, never content or credentials."""
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError("Invalid article publication intent")
    scope = value["scope"]
    if (type(value["version"]) is not int or value["version"] != 1
            or value["purpose"] != "article_publication"
            or not isinstance(value["run_id"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value["run_id"])
            or value["goal_id"] is not None and (not isinstance(value["goal_id"], str) or len(value["goal_id"]) > 200)
            or type(value["generation"]) is not int or value["generation"] < 0
            or not isinstance(scope, dict) or set(scope) - SCOPE
            or any(type(v) not in {str, int, bool, type(None)} or isinstance(v, str) and (len(v) > 2048 or any(ord(c) < 32 for c in v)) for v in scope.values())):
        raise ValueError("Invalid publication scope")
    for key in ("website_connection_id", "github_repo", "operation_id", "client_request_id"):
        if not isinstance(scope.get(key), str) or not scope[key]:
            raise ValueError("Incomplete publication scope")
    for key in ("connection_generation", "repository_id", "operation_attempt", "deletion_epoch"):
        if type(scope.get(key)) is not int or scope[key] < (0 if key == "deletion_epoch" else 1):
            raise ValueError("Invalid publication execution identity")
    if not SHA.fullmatch(str(scope.get("expected_source_sha") or "")) or value["base_sha"] != scope["expected_source_sha"]:
        raise ValueError("Publication source mismatch")
    if any(not isinstance(value[key], str) or not SHA.fullmatch(value[key]) for key in ("base_sha", "head_sha", "tree_sha")):
        raise ValueError("Invalid publication commit identity")
    if value["head_sha"] == value["base_sha"]:
        raise ValueError("Publication must declare an actual content commit")
    if any(not isinstance(value[key], str) or not HASH.fullmatch(value[key]) for key in ("mutation_id", "files_digest", "intent_id")):
        raise ValueError("Invalid publication digest")
    branch, repo = value["branch_name"], value["push_repository"]
    if (not isinstance(branch, str) or not 1 <= len(branch) <= 255
            or any(ord(c) < 33 for c in branch) or any(x in branch for x in ("..", "@{", "\\", ":", "?", "*", "["))
            or branch.startswith(("/", ".")) or branch.endswith(("/", ".", ".lock"))
            or not isinstance(repo, dict) or set(repo) != {"id", "full_name"}
            or type(repo["id"]) is not int or repo["id"] < 1
            or not isinstance(repo["full_name"], str) or not REPO.fullmatch(repo["full_name"])
            or type(value["file_count"]) is not int or not 1 <= value["file_count"] <= 1000):
        raise ValueError("Invalid publication destination")
    if digest({key: item for key, item in value.items() if key != "intent_id"}) != value["intent_id"]:
        raise ValueError("Publication intent digest mismatch")
    return value
