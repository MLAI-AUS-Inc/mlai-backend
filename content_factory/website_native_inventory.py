"""Bounded display evidence; an observed count never establishes readiness."""
from copy import deepcopy
import hashlib
import json
from pathlib import PurePosixPath
import re
from urllib.parse import unquote, urlsplit

from django.conf import settings

from .website_failure_redaction import redact_secret_text
from .website_goal import DIGEST, SHA, mapping

FIELDS = {"schema_version", "kind", "run_id", "source_scope", "origin", "listing_route", "inventory_kind",
    "existing_article_count", "native_inventory_digest", "archive_manifest_digest", "digest"}
SCOPE_FIELDS = {"repository_id", "website_connection_id", "connection_generation", "branch", "app_root", "source_sha"}


def validate_summary(value, *, connection, run_id):
    """Bind the original observation to the already scoped carrier setup run."""
    try:
        if (not isinstance(value, dict) or set(value) != FIELDS or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1 or value.get("kind") != "native_article_inventory_summary"
                or not isinstance(run_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", run_id) is None
                or value["run_id"] != run_id or type(value["existing_article_count"]) is not int
                or not 0 <= value["existing_article_count"] <= 10000
                or value["inventory_kind"] not in {"static_html", "literal_collection"}
                or any(not isinstance(value[key], str) or not DIGEST.fullmatch(value[key])
                    for key in ("native_inventory_digest", "archive_manifest_digest", "digest"))):
            return None
        scope = value["source_scope"]
        if (not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS
                or type(scope["repository_id"]) is not int or type(scope["connection_generation"]) is not int
                or scope["repository_id"] != connection.repository_id or scope["connection_generation"] != connection.generation
                or scope["website_connection_id"] != str(connection.pk) or scope["branch"] != connection.branch
                or scope["app_root"] != connection.app_root or not isinstance(scope["source_sha"], str) or not SHA.fullmatch(scope["source_sha"])):
            return None
        origin, route = value["origin"], value["listing_route"]
        if not isinstance(origin, str) or not 0 < len(origin) <= 2048 or not isinstance(route, str) or not 0 < len(route) <= 4096:
            return None
        parsed = urlsplit(origin)
        site = str(getattr(connection, "site_url", "") or connection.organization.domain).strip()
        expected = urlsplit(site if "://" in site else "https://" + site)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment or parsed.hostname.lower() != str(expected.hostname or "").lower()
                or parsed.port not in ({expected.port} if expected.port else {None, 443 if parsed.scheme == "https" else 80})
                or not route.startswith("/") or route.startswith("//") or "\\" in route or "?" in route or "#" in route
                or re.search(r"%2f|%5c", route, re.I) or any(ord(char) < 32 for char in unquote(route))
                or str(PurePosixPath(route)) != route or any(part in {".", ".."} for part in unquote(route).split("/"))):
            return None
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > 16384 or any(ord(char) < 32 for item in (origin, route, scope["branch"], scope["app_root"]) for char in item):
            return None
        if redact_secret_text(raw) != raw:
            return None
        payload = {key: item for key, item in value.items() if key != "digest"}
        if hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest() != value["digest"]:
            return None
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return None


def for_operation(op):
    """Read only the canonical original-run scope; never choose an arbitrary latest run."""
    if not getattr(settings, "ARTICLES_EXISTING_INVENTORY_DISPLAY_ENABLED", False) or op.action != "prepare":
        return None
    from .website_goal_evidence import _child
    child = _child(op, op.connection)
    if child is None:
        return None
    result = mapping(child.result)
    copies = [mapping(result.get("article_system_setup_cache")),
        mapping(mapping(result.get("article_system_setup")).get("article_system_setup_cache"))]
    summaries = [cache["native_inventory_summary"] for cache in copies if "native_inventory_summary" in cache]
    if not summaries or any(item != summaries[0] for item in summaries):
        return None
    checked = validate_summary(summaries[0], connection=op.connection, run_id=child.run_id)
    if checked is None:
        return None
    return {"count": checked["existing_article_count"], "listingRoute": checked["listing_route"],
        "sourceSha": checked["source_scope"]["source_sha"], "runId": child.run_id,
        "inventoryDigest": checked["native_inventory_digest"], "evidenceDigest": checked["digest"], "kind": "observed_original_inventory"}


def add_display(status, op):
    """Add one explanatory observation, preserving every goal state and action."""
    summary = for_operation(op)
    if summary is None:
        return status
    result = {**status, "existingArticles": summary}
    count = summary["count"]
    message = f"We found {count} existing article{'s' if count != 1 else ''}."
    result["evidence"] = [*status.get("evidence", []), {"id": "native_capture", "result": "passed", "at": None, "message": message}]
    return result
