"""Site capability record: which setup tier a website reached, and why (plan C10, 5.8).

One record per connection generation, computed from stored facts only (the
"proofs refresh themselves" rule): the latest inventory scan, the current
publish target, the latest setup run's ladder receipt, the latest preparation
and the deployment checks. Nothing here calls GitHub, the founder's site or a
model, so staff can read it at any time.

The record is the contract ``contracts/site-capability-record-v1.schema.json``
(``tests_website_capability_record_unit`` validates the projection against it,
and MLAI Chat generates its staff types from the same file).

For the staff list (one query for the page, never one per startup) the last
observed tier is kept beside the connection's other capability facts, under
``WebsiteConnection.capabilities["capabilityRecord"]``: generation, tier, rank,
whether the tier was proven, and since when. No schema change. The refresh
sweep writes it only when it changes, and a site that drops below a tier it
had proven in the same generation raises one staff alert (``our_team_item``,
kind ``site_tier_dropped``).
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from .website_discovery import TIER_LABELS, TIER_RANKS, discovery_snapshot, mapping

logger = logging.getLogger(__name__)

VERSION = 1
SNAPSHOT_KEY = "capabilityRecord"
LADDER_TIERS = ("native", "agentic", "static_dropin", "content_files")
TIERS = (*LADDER_TIERS, "cannot_yet", "not_checked")
STATES = ("ready", "in_setup", "your_turn", "fixing", "cannot_yet", "not_started")
TIER_SOURCES = ("proven", "predicted", "none")
DEPLOY_LINK_STATES = ("proven", "unproven", "ask_raised")
CONTENT_KINDS = ("cms", "repository", "unknown", "not_checked")
CMS_KINDS = ("wordpress", "drupal", "ghost", "headless_cms")
CANT_BE_DONE_REASONS = ("cms_content", "no_code_export", "not_on_github", "server_rendered_stack", "not_fixed_in_time",
                        "stack_not_supported_yet", "private_build_inputs")
HOST_CONFIG_KEYS = ("buildCommand", "baseDirectory", "publishDirectory", "productionBranch")
PROVEN_ADAPTER_MODES = {"deterministic": "native", "model_synthesized": "agentic"}
SWEEP_LIMIT = 500


def _text(value, limit=200):
    return value[:limit] if isinstance(value, str) and value else None


def _latest_inventory(connection):
    from django.db.models import Q
    rows = connection.scan_snapshots.filter(generation=connection.generation).exclude(detector_version="github_head").filter(
        Q(evidence__repository_inventory__discovery_complete=True) | Q(evidence__repository_discovery__discovery_complete=True)
        | Q(evidence__scan_summary__repository_inventory__discovery_complete=True)
        | Q(evidence__scan_summary__repository_discovery__discovery_complete=True) | Q(evidence__scan_complete=True))
    snapshot = rows.order_by("-created_at", "-pk").first()
    if snapshot is None:
        return {}
    evidence = mapping(snapshot.evidence)
    discovery = discovery_snapshot({**mapping(evidence.get("scan_summary")), **evidence})
    discovery["sourceSha"] = snapshot.source_sha
    return discovery


def _latest_prepare(connection):
    return connection.operations.filter(generation=connection.generation, action="prepare").order_by("-created_at", "-pk").first()


def _latest_setup_result(connection):
    from workflow_runs.models import ContentFactoryRun
    run = (ContentFactoryRun.objects.filter(organization_id=connection.organization_id, workflow="article_system_setup",
            run_request__website_connection_id=str(connection.pk), run_request__connection_generation=connection.generation)
           .exclude(status__in=["cancelled", "denied"]).order_by("-updated_at", "-pk").only("result").first())
    return mapping(getattr(run, "result", None))


def _ladder(result):
    for source in (result, mapping(result.get("article_system_setup"))):
        receipt = mapping(source.get("setup_ladder"))
        if receipt.get("schema_version") == 1 and receipt.get("tier") in LADDER_TIERS:
            return receipt
    return {}


def _adapter_mode(result, contract):
    for source in (result, mapping(result.get("article_system_setup")), mapping(result.get("setup_plan")),
                   mapping(result.get("scaffold_plan")), contract):
        mode = source.get("adapter_mode")
        if mode in PROVEN_ADAPTER_MODES:
            return mode
    return None


def _state(op):
    if op is None:
        return "not_started", None
    receipt = mapping(op.receipt)
    if receipt.get("status") == "cant_be_done_yet":
        reason = (mapping(receipt.get("cant_be_done_yet")).get("reason")
                  or mapping(receipt.get("staff_fixing")).get("cant_be_done_reason"))
        return "cannot_yet", reason if reason in CANT_BE_DONE_REASONS else None
    if receipt.get("status") == "staff_fixing":
        return "fixing", None
    if op.state == "completed":
        return "ready", None
    if op.state == "needs_user":
        return "your_turn", None
    if op.state in {"pending", "running"}:
        return "in_setup", None
    if op.state == "cancelled":
        return "not_started", None
    # Any other stop (needs_attention, stopped, failed) is ours to fix (B10).
    return "fixing", None


def _tier(*, state, cant_reason, discovery, ladder, adapter_mode, verified):
    content = mapping(discovery.get("contentSource"))
    if state == "cannot_yet" or content.get("kind") == "cms":
        reason = cant_reason or ("cms_content" if content.get("kind") == "cms" else "")
        return {"id": "cannot_yet", "rank": None, "label": TIER_LABELS["cannot_yet"] if reason == "cms_content"
                else "Can't be done yet.", "reason": reason, "source": "proven" if state == "cannot_yet" else "predicted"}
    proven = state == "ready" and verified
    tier = ladder.get("tier") or PROVEN_ADAPTER_MODES.get(adapter_mode)
    if tier in LADDER_TIERS:
        return {"id": tier, "rank": TIER_RANKS[tier], "label": TIER_LABELS[tier], "reason": "",
                "source": "proven" if proven else "predicted"}
    predicted = mapping(discovery.get("setupTier")).get("id")
    if predicted in LADDER_TIERS:
        return {"id": predicted, "rank": TIER_RANKS[predicted], "label": TIER_LABELS[predicted], "reason": "",
                "source": "predicted"}
    return {"id": "not_checked", "rank": None, "label": TIER_LABELS["not_checked"], "reason": "", "source": "none"}


def _tiers_left(ladder, tier):
    rungs = [rung for rung in ladder.get("rungs") or [] if isinstance(rung, dict)]
    left = [{"tier": rung["tier"], "reason": _text(rung.get("reason"), 120) or "left"}
            for rung in rungs if rung.get("tier") in LADDER_TIERS and rung.get("outcome") == "left"]
    if not left and tier == "agentic":
        left = [{"tier": "native", "reason": "no_native_adapter"}]
    return left[:4]


def _host_config(connection):
    raw = mapping(mapping(connection.capabilities).get("hostConfig"))
    return {key: _text(raw.get(key), 300) for key in HOST_CONFIG_KEYS}


def _toolchain(discovery):
    fingerprint = mapping(discovery.get("siteFingerprint"))
    fields = mapping(fingerprint.get("fields"))
    def field(name):
        value = mapping(fields.get(name)).get("value")
        return _text(value, 64)
    return {"framework": _text(discovery.get("framework"), 64) or field("framework"),
            "runtime": field("runtime"), "packageManager": field("package_manager")}


def _host(connection, discovery):
    capabilities = mapping(connection.capabilities)
    host = _text(capabilities.get("hosting_provider"), 64) or _text(mapping(capabilities.get("deploymentFingerprint")).get("provider"), 64)
    if host:
        return host
    fingerprint = discovery.get("siteFingerprint")
    if isinstance(fingerprint, dict):
        try:
            from .website_site_fingerprint import hosting_hint
            return _text(hosting_hint(fingerprint), 64)
        except (KeyError, TypeError, AttributeError):
            return None
    return None


def _deploy_link(connection, op):
    action = mapping(mapping(getattr(op, "receipt", None)).get("user_action"))
    if action.get("id") == "deploy_site":
        return {"state": "ask_raised", "checkedAt": None}
    if connection.verified_sha:
        deployment = connection.operations.filter(generation=connection.generation, action="deployment-verify", state="completed",
            payload__source_sha=connection.verified_sha).order_by("-created_at", "-pk").first()
        if deployment is not None:
            return {"state": "proven", "checkedAt": _text(mapping(deployment.receipt).get("checked_at"), 64)
                    or deployment.updated_at.isoformat()}
    return {"state": "unproven", "checkedAt": None}


def build(connection, *, now=None):
    """The capability record of ``connection``'s current generation (contract v1)."""
    now = now or timezone.now()
    discovery = _latest_inventory(connection)
    op = _latest_prepare(connection)
    state, cant_reason = _state(op)
    target = (connection.targets.filter(generation=connection.generation, source_sha=connection.verified_sha)
              .order_by("-updated_at", "-pk").first() if connection.verified_sha else None)
    contract = mapping(getattr(target, "contract", None))
    verification = mapping(contract.get("verification"))
    verified = bool(target and verification.get("status") in {"passed", "verified", "preview_verified"})
    result = _latest_setup_result(connection)
    ladder = _ladder(result)
    tier = _tier(state=state, cant_reason=cant_reason, discovery=discovery, ladder=ladder,
                 adapter_mode=_adapter_mode(result, contract), verified=verified)
    content = mapping(discovery.get("contentSource"))
    recipe = mapping(contract.get("article_recipe"))
    previous = mapping(mapping(connection.capabilities).get(SNAPSHOT_KEY))
    previous_tier = previous.get("tier") if previous.get("generation") == connection.generation else None
    return {
        "version": VERSION,
        "connectionId": str(connection.pk), "connectionGeneration": int(connection.generation),
        "githubRepo": connection.github_repo or "", "branch": connection.branch or "", "appRoot": connection.app_root or "",
        "state": state, "tier": tier, "tiersLeft": _tiers_left(ladder, tier["id"]),
        "recipeVersion": _text(contract.get("recipe_version"), 40) or _text(recipe.get("schema"), 40)
            or (str(recipe["version"])[:40] if isinstance(recipe.get("version"), (int, str)) and recipe.get("version") != "" else None),
        "toolchain": _toolchain(discovery), "host": _host(connection, discovery), "hostConfig": _host_config(connection),
        "deployLink": _deploy_link(connection, op),
        "contentSource": {"kind": content.get("kind") if content.get("kind") in CONTENT_KINDS else "not_checked",
                          "cms": content.get("cms") if content.get("cms") in CMS_KINDS else None},
        "acceptance": {"buildVerified": verified, "adapter": _text(getattr(target, "adapter", None), 100),
                       "verifiedAt": connection.last_verified_at.isoformat() if connection.last_verified_at else None},
        "lastVerifiedSha": _text(connection.verified_sha, 64),
        "previousTier": previous_tier if previous_tier in TIERS else None,
        "lastProvenTier": _last_proven(previous, connection.generation),
        "tierDropped": _dropped(previous, tier, generation=connection.generation),
        "observedAt": now.isoformat(),
    }


def _rank(tier_id):
    # cannot_yet sits below every tier; not_checked is no observation.
    return TIER_RANKS.get(tier_id, 99 if tier_id == "cannot_yet" else None)


def _last_proven(previous, generation):
    """The last tier this generation proved (kept across later unproven observations)."""
    if previous.get("generation") != generation:
        return None
    proven = previous.get("proven")
    return proven if proven in TIERS else None


def _dropped(previous, tier, *, generation):
    before = _rank(_last_proven(previous, generation))
    after = _rank(tier["id"])
    if before is None or after is None:
        return False
    # Only a tier we proved, or a closed "can't be done yet", counts as a drop.
    return after > before and (tier["source"] == "proven" or (tier["id"] == "cannot_yet" and tier["source"] != "none"))


def snapshot(record):
    """The compact value kept on the connection for the staff list."""
    tier = record["tier"]
    proven = tier["id"] if tier["source"] == "proven" else record.get("lastProvenTier")
    return {"version": VERSION, "generation": record["connectionGeneration"], "tier": tier["id"], "rank": tier["rank"],
            "source": tier["source"], "state": record["state"], "proven": proven}


def staff_tier(connection):
    """The last observed tier of ``connection``'s current generation, for staff rows; no query."""
    if connection is None:
        return None
    value = mapping(mapping(connection.capabilities).get(SNAPSHOT_KEY))
    if value.get("version") != VERSION or value.get("generation") != connection.generation or value.get("tier") not in TIERS:
        return None
    return value["tier"]


def refresh(connection_id, *, now=None):
    """Recompute and store one connection's snapshot; alert once when a proven tier drops."""
    from .website_models import WebsiteConnection
    now = now or timezone.now()
    with transaction.atomic():
        connection = WebsiteConnection.objects.select_for_update().select_related("organization").filter(pk=connection_id).first()
        if connection is None or connection.state != "connected":
            return None
        record = build(connection, now=now)
        stored = mapping(mapping(connection.capabilities).get(SNAPSHOT_KEY))
        value = snapshot(record)
        changed = {key: stored.get(key) for key in value} != value
        if changed:
            since = stored.get("since") if stored.get("tier") == value["tier"] and stored.get("generation") == value["generation"] else now.isoformat()
            capabilities = {**mapping(connection.capabilities), SNAPSHOT_KEY: {**value, "since": since}}
            # Only this key changes; updated_at is left alone so no lease or
            # freshness check reads a staff bookkeeping write as founder activity.
            WebsiteConnection.objects.filter(pk=connection.pk).update(capabilities=capabilities)
            connection.capabilities = capabilities
    if record["tierDropped"]:
        _alert_drop(connection, record, now=now)
    return record


def _alert_drop(connection, record, *, now):
    from .founder_journey_alerts import queue_founder_journey_alert
    try:
        queue_founder_journey_alert(organization=connection.organization, alert_reason="our_team_item",
            slot=f"site-tier-dropped-{connection.pk}-{connection.generation}-{record['lastProvenTier']}-{record['tier']['id']}",
            details={"kind": "site_tier_dropped", "from_tier": record["lastProvenTier"] or "unknown",
                     "to_tier": record["tier"]["id"], "state": record["state"],
                     "connection_generation": record["connectionGeneration"]})
    except Exception:  # noqa: BLE001 - the stored record stands; the next change alerts again
        logger.error("site_tier_drop_alert_failed connection=%s", connection.pk, exc_info=True)


def run_capability_record_sweep(now=None, limit=None) -> dict:
    """Refresh the stored tier of connected websites, most recently active first. No flag: read-only facts."""
    from .website_models import WebsiteConnection
    now = now or timezone.now()
    ids = list(WebsiteConnection.objects.filter(state="connected").order_by("-updated_at", "-pk")
               .values_list("pk", flat=True)[:limit or SWEEP_LIMIT])
    counts = {"checked": 0, "dropped": 0, "failed": 0}
    for pk in ids:
        try:
            record = refresh(pk, now=now)
        except Exception:  # noqa: BLE001 - one bad row must not hide every other site's tier
            counts["failed"] += 1
            logger.error("capability_record_sweep_row_failed connection=%s", pk, exc_info=True)
            continue
        if record is not None:
            counts["checked"] += 1
            counts["dropped"] += int(record["tierDropped"])
    return counts


def staff_tier_counts(connections) -> dict:
    """Sites by tier for the staff view, from stored snapshots only."""
    counts = {tier: 0 for tier in TIERS}
    counts["unknown"] = 0
    for connection in connections:
        tier = staff_tier(connection)
        counts[tier or "unknown"] += 1
    return counts
