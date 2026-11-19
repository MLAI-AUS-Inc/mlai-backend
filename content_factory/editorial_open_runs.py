"""Open articles written for a customer profile or next step the founder just changed (plan B9).

A save is never blocked. Its response lists the articles still being written
for an earlier version of a changed entry, so the client can say "one article
is being written for this profile and will finish with the earlier wording".
A no-offer article keeps going on the version it was admitted with (A8); an
article with an offer stops at its next check and asks the founder.
"""
from __future__ import annotations

import logging

from django.conf import settings

from .editorial_catalog import FIELDS, catalog_payload
from .run_state import ARTICLE_WORKFLOWS
from .run_status_contract import ACTIVE, AWAITING

logger = logging.getLogger(__name__)

MAX_OPEN_RUNS = 20
BRIEF_KEYS = ("editorial_brief", "editorialBrief")


def _versions(strategy):
    try:
        current = catalog_payload(strategy)
    except ValueError:
        return {}
    return {(kind, item["id"]): item for kind, field in FIELDS.items() for item in current[field]}


def changed_entries(before, after):
    """``{(kind, id): entry}`` for entries whose version or approval state changed in this save."""
    old, new = _versions(before), _versions(after)
    return {key: entry for key, entry in new.items()
            if key not in old or old[key]["version"] != entry["version"] or old[key]["status"] != entry["status"]}


def _brief(run):
    request = run.run_request if isinstance(run.run_request, dict) else {}
    value = next((request[key] for key in BRIEF_KEYS if isinstance(request.get(key), dict)), None)
    return value or {}


def _title(run):
    request = run.run_request if isinstance(run.run_request, dict) else {}
    for key in ("topic", "selected_title", "custom_title", "target_keyword"):
        text = request.get(key)
        if isinstance(text, str) and text.strip():
            return " ".join(text.split())[:160]
    return ""


def open_runs_for_changed_entries(organization, before, after) -> list[dict]:
    """Open article runs whose brief names an earlier version of an entry changed by this save.

    Read-only and best effort: any failure returns an empty list and is logged.
    """
    try:
        changed = changed_entries(before, after)
        if not changed:
            return []
        from workflow_runs.models import ContentFactoryRun
        runs = (ContentFactoryRun.objects.filter(organization=organization, workflow__in=ARTICLE_WORKFLOWS,
                                                 status__in=sorted(ACTIVE | AWAITING))
                .only("run_id", "status", "run_request", "workflow").order_by("-created_at")[:200])
        result = []
        for run in runs:
            brief = _brief(run)
            for kind, id_key, version_key in (("audience", "audience_id", "audience_version"),
                                              ("offer", "offer_id", "offer_version")):
                entry = changed.get((kind, brief.get(id_key)))
                written_for = brief.get(version_key)
                if entry is None or type(written_for) is not int or written_for >= entry["version"]:
                    continue
                offer_brief = brief.get("conversion_intent") == "offer"
                # Mirrors the worker's rule: only a no-offer article whose
                # profile is still approved and still allows no offer continues.
                continues = (not offer_brief and entry["status"] == "approved"
                             and (kind != "audience" or bool(entry.get("allow_no_offer")))
                             and getattr(settings, "CONTENT_FACTORY_EDITORIAL_SUPERSEDED_OK", True))
                result.append({
                    "run_id": run.run_id, "title": _title(run), "kind": kind, "entry_id": entry["id"],
                    "written_for_version": written_for, "current_version": entry["version"],
                    # A no-offer article finishes with the earlier wording; an
                    # offer article asks the founder at its next check.
                    "outcome": "finishes_with_earlier_wording" if continues else "asks_founder",
                })
                break
            if len(result) >= MAX_OPEN_RUNS:
                break
        return result
    except Exception:  # noqa: BLE001 - advisory; a save is never blocked or failed by this read
        logger.exception("editorial_open_runs_unavailable org_id=%s", getattr(organization, "pk", None))
        return []
