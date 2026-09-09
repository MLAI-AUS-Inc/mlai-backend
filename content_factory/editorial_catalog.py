"""Versioned editorial policy stored with the existing content strategy JSON.

The reserved catalog is independent of generated pillars. Service responses
expose its typed audience_options/cta_options as top-level contract fields.
"""
from copy import deepcopy

from .editorial_contract import AudienceOption, normalize_cta_options

CATALOG_KEY = "editorial_catalog"


def catalog_payload(strategy):
    catalog = (strategy or {}).get(CATALOG_KEY, {})
    return {"audience_options": deepcopy(catalog.get("audience_options", [])), "cta_options": deepcopy(catalog.get("cta_options", [])), "editorial_catalog_version": int(catalog.get("version", 0))}


def merge_strategy(existing, incoming):
    """A topic rescan cannot erase or replace an approved editorial catalog."""
    result = deepcopy(incoming or {})
    result.pop(CATALOG_KEY, None)
    if CATALOG_KEY in (existing or {}):
        result[CATALOG_KEY] = deepcopy(existing[CATALOG_KEY])
    return result


def update_catalog(strategy, payload):
    current = catalog_payload(strategy)
    if payload.get("expected_editorial_catalog_version", current["editorial_catalog_version"]) != current["editorial_catalog_version"]:
        raise ValueError("Editorial catalog has changed; reload before saving")
    audiences = [AudienceOption.model_validate(item) for item in payload.get("audience_options", current["audience_options"])]
    offers = normalize_cta_options(payload.get("cta_options", current["cta_options"]))
    audience_ids = {item.id for item in audiences}
    if len(audience_ids) != len(audiences):
        raise ValueError("Audience ids must be unique")
    for field, items in (("audience_options", audiences), ("cta_options", offers)):
        old = {item["id"]: item for item in current[field]}
        for item in items:
            if item.status == "approved" and (not item.approved_by or not item.approved_at):
                raise ValueError("Approved catalog entries require explicit approval provenance")
            previous = old.get(item.id)
            if previous and previous != item.model_dump(mode="json") and item.version <= previous["version"]:
                raise ValueError("Changed catalog entries require a higher version and renewed approval")
    for offer in offers:
        if offer.status == "approved" and (not offer.audience_ids or not set(offer.audience_ids) <= audience_ids or not offer.countries):
            raise ValueError("Approved offers require known audiences and explicit markets")
    updated = {"audience_options": [a.model_dump(mode="json") for a in audiences], "cta_options": [o.model_dump(mode="json") for o in offers]}
    changed = any(current[key] != value for key, value in updated.items())
    result = deepcopy(strategy or {})
    result[CATALOG_KEY] = {**updated, "schema_version": 1, "version": current["editorial_catalog_version"] + int(changed)}
    return result
