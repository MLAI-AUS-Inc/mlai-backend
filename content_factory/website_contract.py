"""Pure validation for website consent fences and reversible write evidence."""

import hashlib
import json
import re
from pathlib import PurePosixPath
from uuid import UUID


CONNECTION_FIELDS = ("website_connection_id", "connection_generation", "connection_target_id", "repository_id")
REPO_ACTIONS = {"portable", "read", "scan", "config_write", "preview", "setup", "publish", "merge", "cleanup", "custom_contract"}
WRITE_ACTIONS = {"setup", "publish", "merge", "cleanup"}
CAPABILITY_KEYS = ("inventoryReady", "templatesValid", "generationReady", "publishingReady", "previewSupported")
SHA_PATTERN = re.compile(r"^[a-fA-F0-9]{40}(?:[a-fA-F0-9]{24})?$")
TEMPLATE_WRAPPERS = re.compile(r"(?mi)^#{1,6}\s+(?:EXISTING ARTIFACT|BASE TEMPLATE|CODEBASE CONTEXT|ARTIFACT TO ADAPT|REPOSITORY CONTEXT|GENERATION INSTRUCTIONS|UPDATE INSTRUCTIONS)\s*$")
SECRET_KEYS = {"github_token", "github_access_token", "github_refresh_token", "access_token", "refresh_token", "authorization", "api_key", "private_key", "password"}


class WebsiteAuthorityError(ValueError):
    """A stable denial with an explicit retry policy for transient verification."""

    def __init__(self, code, message, *, status=409, field_errors=None, retryable=False):
        self.code, self.status = code, status
        self.field_errors = field_errors or []
        self.retryable = retryable
        super().__init__(message)

    def as_dict(self):
        return {"allowed": False, "error": self.code, "code": self.code, "detail": str(self), "retryable": self.retryable, "fieldErrors": self.field_errors}


def positive_integer(value, *, name, optional=False):
    """Reject booleans and fractional numbers in identifiers and generations."""
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool) or not str(value or "").isdigit() or int(value) < 1:
        raise WebsiteAuthorityError("invalid_connection_contract", f"{name} must be a positive integer.")
    return int(value)


def connection_contract(payload):
    """Read the explicit consent tuple; incomplete tuples are never legacy."""
    payload = payload if isinstance(payload, dict) else dict(payload or {})
    identifier = payload.get("website_connection_id") or payload.get("connection_id") or payload.get("connectionId")
    generation = payload.get("connection_generation", payload.get("connectionGeneration"))
    if not identifier and generation is None:
        if any(payload.get(key) not in (None, "") for key in ("connection_target_id", "connectionTargetId", "repository_id", "repositoryId")):
            raise WebsiteAuthorityError("invalid_connection_contract", "The complete reviewed website connection identity is required.")
        return {}
    try:
        identifier = str(UUID(str(identifier)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise WebsiteAuthorityError("invalid_connection_contract", "A valid website connection ID is required.") from exc
    result = {
        "website_connection_id": identifier,
        "connection_generation": positive_integer(generation, name="connection_generation"),
    }
    target = payload.get("connection_target_id") or payload.get("connectionTargetId")
    if target:
        result["connection_target_id"] = str(target)
    repo = positive_integer(payload.get("repository_id", payload.get("repositoryId")), name="repository_id", optional=True)
    if repo:
        result["repository_id"] = repo
    return result


def validate_authority(connection, payload, *, action="read", domain="", github_repo=""):
    """Check one persisted connection without granting authority from the payload."""
    if action not in REPO_ACTIONS:
        raise WebsiteAuthorityError("invalid_connection_action", "Unknown website operation.", status=400)
    contract = connection_contract(payload)
    if not contract:
        raise WebsiteAuthorityError("website_connection_required", "Reload the web app or update MLAI, then reconnect this website to continue.")
    if str(connection.id) != contract["website_connection_id"] or connection.generation != contract["connection_generation"]:
        raise WebsiteAuthorityError("website_connection_changed", "The website connection changed. Refresh before continuing.")
    if action != "portable" and connection.state not in {"connected", "paused"}:
        raise WebsiteAuthorityError("website_disconnected", "This website is disconnected. Reconnect it before continuing.")
    if action in WRITE_ACTIONS and connection.state != "connected":
        raise WebsiteAuthorityError("website_publishing_paused", "Publishing is paused for this website.")
    if action in {"publish", "merge"} and not (connection.capabilities or {}).get("publishingReady"):
        raise WebsiteAuthorityError("website_publish_verification_required", "Verify a publishing adapter for this repository before publishing.")
    if domain and domain.casefold() != str(connection.organization.domain).casefold():
        raise WebsiteAuthorityError("website_scope_mismatch", "Website connection does not belong to this company.")
    if github_repo and github_repo.casefold() != connection.github_repo.casefold():
        raise WebsiteAuthorityError("website_repository_changed", "The repository does not match this website connection.")
    if contract.get("repository_id") and connection.repository_id != contract["repository_id"]:
        raise WebsiteAuthorityError("website_repository_changed", "The repository identity changed.")
    return contract


def safe_repository_path(value, *, allow_empty=False):
    """Validate portable relative paths before storage or worker execution."""
    path = str(value or "").strip()
    if allow_empty and path in {"", "."}:
        return ""
    parts = PurePosixPath(path).parts
    if (not path or path == "." or path.startswith("/") or "\\" in path or "\x00" in path
            or any(part in {"..", ".git"} for part in parts)
            or re.match(r"^[A-Za-z]:", path)):
        raise WebsiteAuthorityError("invalid_repository_path", "Use a relative path inside the selected application root.", status=400)
    return str(PurePosixPath(path))


def sanitized_evidence(value):
    """Do not persist credential values in source/run/mutation evidence."""
    if isinstance(value, dict):
        return {
            str(key): sanitized_evidence(item) for key, item in value.items()
            if str(key).casefold() not in SECRET_KEYS
        }
    if isinstance(value, list):
        return [sanitized_evidence(item) for item in value]
    return value


def evidence_digest(value):
    """Stable identity for a validated evidence envelope."""
    return hashlib.sha256(json.dumps(sanitized_evidence(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def template_validation(body):
    """Validate saved seeds before optional generation, preserving explicit drafts."""
    if not isinstance(body, str) or not body.strip():
        return {"valid": False, "code": "template_empty", "message": "A template body is required."}
    if TEMPLATE_WRAPPERS.search(body):
        return {"valid": False, "code": "legacy_template_envelope", "message": "Saved template contains a historical generation envelope."}
    return {"valid": True, "code": "", "message": ""}


def cleanup_plan(files, current_hashes, *, retained_paths=()):
    """Plan deletion only for unchanged exclusively-owned files; never guess edits."""
    retained = set(retained_paths)
    removable_kinds = {"setup_scaffolding", "integration_config", "setup_route"}
    result = {"deletions": [], "conflicts": [], "retained": []}
    for entry in files:
        if not isinstance(entry, dict):
            raise WebsiteAuthorityError("invalid_mutation_ledger", "Mutation files must be objects.")
        path = safe_repository_path(entry.get("path"))
        if path in retained or entry.get("retained_dependencies") or entry.get("kind") not in removable_kinds:
            result["retained"].append(path)
        elif current_hashes.get(path) is None:
            result["retained"].append(path)
        elif entry.get("ownership") != "created" or not entry.get("after_sha256"):
            result["conflicts"].append({"path": path, "reason": "shared_or_unproven_ownership"})
        elif current_hashes[path] != entry["after_sha256"]:
            result["conflicts"].append({"path": path, "reason": "modified_after_generation"})
        else:
            result["deletions"].append(path)
    if any(entry.get("kind") in {"published_article", "user_content"} or entry.get("retained_dependencies") for entry in files) or retained:
        result["retained"].extend(result["deletions"])
        result["deletions"] = []
    return result
