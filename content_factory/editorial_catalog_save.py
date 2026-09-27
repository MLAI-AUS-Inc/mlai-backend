"""Make an owner's Save immediately usable while retaining catalogue receipts."""
from copy import deepcopy

from .editorial_catalog import (
    CATALOG_KEY, FIELDS, CatalogConflict, approve_catalog, catalog_payload, review_payload,
)


def activate_saved_entries(original, updated, selections, *, actor_id, saved_at):
    """Activate explicit saved entries and preserve unchanged linked offers.

    Called inside the existing owner-locked transaction, after draft validation
    and suggestion provenance. No intermediate draft is persisted. Service
    writes do not call this function, and unrelated drafts are never activated.
    """
    before, after = catalog_payload(original), review_payload(updated)
    if not isinstance(selections, list) or not selections:
        raise ValueError("Choose a customer profile or next step to save")
    indexed = {
        kind: {item["id"]: item for item in after[field]}
        for kind, field in FIELDS.items()
    }
    selected = set()
    for selection in selections:
        if not isinstance(selection, dict) or set(selection) != {"kind", "id", "version"}:
            raise ValueError("Saved entries require kind, id and version")
        kind, key, version = selection["kind"], selection["id"], selection["version"]
        if not isinstance(kind, str) or kind not in FIELDS or not isinstance(key, str):
            raise ValueError("Unknown customer profile or next step")
        item = indexed[kind].get(key)
        if item is None or type(version) is not int or item["version"] != version:
            raise CatalogConflict("This entry changed. Reload before saving")
        if item["status"] == "retired" or (kind, key) in selected:
            raise ValueError("Choose unique active entries to save")
        selected.add((kind, key))

    # Editing an ICP need not force a second Save on its unchanged CTAs. Rebind
    # only previously active offers whose business definition was not edited.
    audiences = {key for kind, key in selected if kind == "audience"}
    metadata = {"version", "status", "approved_by", "approved_at"}
    def definition(item):
        return {k: v for k, v in item.items() if k not in metadata}

    for old in before["cta_options"]:
        item = indexed["offer"].get(old["id"])
        if (old["status"] == "approved" and audiences.intersection(old["audience_ids"])
                and item is not None and item["status"] == "draft"
                and definition(item) == definition(old)):
            selected.add(("offer", item["id"]))

    result = approve_catalog(updated, {
        "expected_editorial_catalog_version": after["editorial_catalog_version"],
        "entries": [entry for entry in after["review_entries"]
                    if (entry["kind"], entry["id"]) in selected],
    }, actor_id=actor_id, approved_at=saved_at)
    # A single owner Save is one persisted catalogue revision, even though the
    # pure edit/receipt helpers each independently advance their input revision.
    result = deepcopy(result)
    changed = any(result[CATALOG_KEY][field] != before[field] for field in FIELDS.values())
    result[CATALOG_KEY]["version"] = before["editorial_catalog_version"] + int(changed)
    return result
