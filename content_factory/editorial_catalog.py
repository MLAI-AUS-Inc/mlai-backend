"""Content-bound editorial policy in the existing strategy JSON.

Ordinary service writes edit drafts. Only an authenticated owner approval path
may issue receipts. Legacy approval strings are not silently trusted. Helpers
are pure; persistence callers must hold the owning organisation's row lock.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from .editorial_contract import (
    ArticleEditorialBrief, AudienceOption, normalize_cta_options, resolve_editorial_brief,
)

CATALOG_KEY = "editorial_catalog"
FIELDS = {"audience": "audience_options", "offer": "cta_options"}
EDIT_FIELDS = {*FIELDS.values(), "expected_editorial_catalog_version"}


class CatalogConflict(ValueError):
    """The editor no longer holds the version/content being changed."""


def content_hash(item):
    content = {key: value for key, value in item.items() if key not in {"status", "approved_by", "approved_at"}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _records(field, value):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list; omit it to retain existing entries")
    for item in value:
        if not isinstance(item, dict) or type(item.get("version", 1)) is not int:
            raise ValueError("Catalog entries require an integer version")
    models = ([AudienceOption.model_validate(item) for item in value]
              if field == "audience_options" else normalize_cta_options(value))
    records = [item.model_dump(mode="json") for item in models]
    if len({item["id"] for item in records}) != len(records):
        raise ValueError("Catalog entry ids must be unique")
    return records


def _receipt_valid(item, receipt):
    return (item["status"] == "approved" and isinstance(receipt, dict)
            and receipt.get("content_sha256") == content_hash(item)
            and bool(receipt.get("approved_by")) and bool(receipt.get("approved_at"))
            and item["approved_by"] == receipt["approved_by"]
            and item["approved_at"] == receipt["approved_at"])


def _dependencies(offer, audiences):
    ids = offer["audience_ids"]
    if not ids or any(key not in audiences or audiences[key]["status"] != "approved" for key in ids):
        raise ValueError("Approved offers require approved, known audiences")
    if (not offer["countries"] or len(set(ids)) != len(ids)
            or len(set(offer["countries"])) != len(offer["countries"])
            or any(not re.fullmatch(r"[A-Z]{2}", country) for country in offer["countries"])):
        raise ValueError("Approved offers require unique audiences and explicit two-letter markets")
    return {key: content_hash(audiences[key]) for key in ids}


def catalog_payload(strategy):
    if strategy is not None and not isinstance(strategy, dict):
        raise ValueError("Invalid stored editorial strategy")
    catalog = (strategy or {}).get(CATALOG_KEY, {})
    if not isinstance(catalog, dict):
        raise ValueError("Invalid stored editorial catalog")
    revision = catalog.get("version", 0)
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid stored editorial catalog version")
    receipts = catalog.get("approval_receipts", {})
    if not isinstance(receipts, dict) or any(not isinstance(receipts.get(kind, {}), dict) for kind in FIELDS):
        raise ValueError("Invalid stored editorial approval receipts")
    result = {field: _records(field, catalog.get(field, [])) for field in FIELDS.values()}
    for kind, field in FIELDS.items():
        for item in result[field]:
            receipt = receipts.get(kind, {}).get(item["id"])
            valid = _receipt_valid(item, receipt)
            if valid and kind == "offer":
                try:
                    dependencies = _dependencies(item, {a["id"]: a for a in result["audience_options"]})
                    valid = receipt.get("audiences") == dependencies
                except ValueError:
                    valid = False
            if not valid:
                if item["status"] == "approved":
                    item["status"] = "draft"
                item["approved_by"] = item["approved_at"] = None
    return {**result, "editorial_catalog_version": revision}


def review_payload(strategy):
    result = catalog_payload(strategy)
    result["review_entries"] = [
        {"kind": kind, "id": item["id"], "version": item["version"], "content_sha256": content_hash(item)}
        for kind, field in FIELDS.items() for item in result[field]
    ]
    return result


def article_brief_for_catalog(strategy, request_payload):
    """Resolve an explicit request against current receipts, without defaults.

    Only a genuinely catalog-free request with no brief keeps the legacy path.
    An empty configured envelope, a missing selected entry or an explicit null
    brief must not silently downgrade a catalog-backed article to that path.
    """
    current = catalog_payload(strategy)
    if not isinstance(request_payload, dict):
        raise ValueError("Article request must be a JSON object")
    keys = [key for key in ("editorial_brief", "editorialBrief") if key in request_payload]
    if not keys:
        if CATALOG_KEY in (strategy or {}):
            raise ValueError("Configured catalog requires an explicit approved editorial brief")
        return None
    if len(keys) == 2 and request_payload[keys[0]] != request_payload[keys[1]]:
        raise ValueError("Conflicting editorial_brief and editorialBrief values")
    brief = ArticleEditorialBrief.model_validate(request_payload[keys[0]])
    resolve_editorial_brief(
        brief, [AudienceOption.model_validate(item) for item in current["audience_options"]],
        normalize_cta_options(current["cta_options"]),
    )
    return brief.model_dump(mode="json")


def merge_strategy(existing, incoming):
    """Generated scans cannot erase or replace policy or approval history."""
    result = deepcopy(incoming or {})
    result.pop(CATALOG_KEY, None)
    if CATALOG_KEY in (existing or {}):
        result[CATALOG_KEY] = deepcopy(existing[CATALOG_KEY])
    return result


def _expected(current, payload):
    expected = payload.get("expected_editorial_catalog_version")
    if type(expected) is not int or expected < 0:
        raise ValueError("expected_editorial_catalog_version is required and must be a nonnegative integer")
    if expected != current["editorial_catalog_version"]:
        raise CatalogConflict("Editorial catalog has changed; reload before saving or approving")


def _save(strategy, current, records, receipts, history):
    result = deepcopy(strategy or {})
    changed = any(records[field] != current[field] for field in FIELDS.values())
    result[CATALOG_KEY] = {**records, "schema_version": 2,
                          "version": current["editorial_catalog_version"] + int(changed),
                          "approval_receipts": receipts, "approval_history": history}
    return result


def _retained_receipts(strategy, current):
    receipts = (strategy or {}).get(CATALOG_KEY, {}).get("approval_receipts", {})
    return {kind: {item["id"]: deepcopy(receipts[kind][item["id"]])
                   for item in current[field] if item["status"] == "approved"}
            for kind, field in FIELDS.items()}


def update_catalog(strategy, payload):
    """Save drafts/retirements, never approve from caller-supplied provenance."""
    if set(payload) - EDIT_FIELDS:
        raise ValueError("Unknown editorial catalog edit fields")
    current = catalog_payload(strategy)
    _expected(current, payload)
    if not any(field in payload for field in FIELDS.values()):
        raise ValueError("Supply audience_options or cta_options")
    records = {field: _records(field, payload.get(field, current[field])) for field in FIELDS.values()}
    receipts = _retained_receipts(strategy, current)
    for kind, field in FIELDS.items():
        old = {item["id"]: item for item in current[field]}
        if set(old) - {item["id"] for item in records[field]}:
            raise ValueError("Retire catalog entries instead of deleting their version history")
        for item in records[field]:
            previous = old.get(item["id"])
            if previous == item:
                continue
            if previous and item["version"] <= previous["version"]:
                raise CatalogConflict("Changed catalog entries require a higher version")
            if item["status"] == "approved" or item["approved_by"] is not None or item["approved_at"] is not None:
                raise ValueError("Save changed entries as draft or retired with cleared approval fields; use owner approval after review")
            receipts[kind].pop(item["id"], None)
    audiences = {item["id"]: item for item in records["audience_options"]}
    for item in records["cta_options"]:
        if item["status"] == "approved":
            try:
                dependencies = _dependencies(item, audiences)
                if receipts["offer"][item["id"]].get("audiences") != dependencies:
                    raise ValueError("Changed dependency")
            except ValueError as exc:
                raise ValueError("Audience changes require dependent offers to be saved as draft or retired at higher versions") from exc
    history = deepcopy((strategy or {}).get(CATALOG_KEY, {}).get("approval_history", []))
    return _save(strategy, current, records, receipts, history)


def approve_catalog(strategy, payload, *, actor_id, approved_at):
    """Issue receipts after owner authorisation; identity/time are server arguments."""
    if set(payload) != {"expected_editorial_catalog_version", "entries"}:
        raise ValueError("Approval requires only expected_editorial_catalog_version and entries")
    if (not isinstance(actor_id, str) or not actor_id.strip()
            or not isinstance(approved_at, datetime) or approved_at.utcoffset() is None):
        raise ValueError("Approval requires a server actor and timezone-aware timestamp")
    current = catalog_payload(strategy)
    _expected(current, payload)
    selected = payload["entries"]
    if not isinstance(selected, list) or not selected:
        raise ValueError("Select at least one reviewed catalog entry")
    records = {field: deepcopy(current[field]) for field in FIELDS.values()}
    indexed = {kind: {item["id"]: item for item in records[field]} for kind, field in FIELDS.items()}
    seen = set()
    for selection in selected:
        if not isinstance(selection, dict) or set(selection) != {"kind", "id", "version", "content_sha256"}:
            raise ValueError("Each approval must identify kind, id, version and content_sha256")
        kind, key = selection["kind"], selection["id"]
        if not isinstance(kind, str) or not isinstance(key, str) or kind not in FIELDS:
            raise ValueError("Unknown catalog entry kind or id")
        item = indexed[kind].get(key)
        if item is None or type(selection["version"]) is not int or (item["version"], content_hash(item)) != (selection["version"], selection["content_sha256"]):
            raise CatalogConflict("Reviewed catalog entry has changed; reload and review it again")
        if (kind, key) in seen or item["status"] == "retired":
            raise ValueError("Select unique draft entries; retired entries must be revised first")
        seen.add((kind, key))
    receipts = _retained_receipts(strategy, current)
    history = deepcopy((strategy or {}).get(CATALOG_KEY, {}).get("approval_history", []))
    timestamp = approved_at.astimezone(timezone.utc).isoformat()
    # Audience approval precedes dependent offers, regardless of selection order.
    for kind in FIELDS:
        for selected_kind, key in sorted(seen):
            if selected_kind != kind:
                continue
            item = indexed[kind][key]
            if item["status"] == "approved":
                continue
            receipt = {"content_sha256": content_hash(item), "approved_by": actor_id.strip(), "approved_at": timestamp}
            if kind == "offer":
                receipt["audiences"] = _dependencies(item, indexed["audience"])
            item.update(status="approved", approved_by=receipt["approved_by"], approved_at=timestamp)
            receipts[kind][key] = receipt
            history.append({"kind": kind, "id": key, "version": item["version"],
                            **deepcopy(receipt), "entry": deepcopy(item)})
    return _save(strategy, current, records, receipts, history)


def discovery_audience_context(strategy, payload):
    """Optional approved reader context for discovery, without choosing an offer."""
    identity = payload.get("preferredAudienceId") or payload.get("preferred_audience_id")
    if not identity:
        return None
    current = catalog_payload(strategy)
    version = payload.get("expectedEditorialCatalogVersion", payload.get("expected_editorial_catalog_version"))
    if type(version) is not int or version != current["editorial_catalog_version"]:
        raise CatalogConflict("Customer profiles changed. Reload before researching ideas.")
    audience = next((a for a in current["audience_options"] if a["id"] == identity and a["status"] == "approved"), None)
    if audience is None:
        raise ValueError("Choose an approved customer profile for research")
    return {k:v for k,v in audience.items() if k not in {"approved_by", "approved_at", "status"}}
