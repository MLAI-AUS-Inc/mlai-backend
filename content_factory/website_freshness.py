"""Keep an unchanged website's article verification fresh without asking its owner.

Integration proof expires after ``VERIFICATION_MAX_AGE`` even when nothing about
the site changed. TalaThrive (25 Oct 2026) sat at "Check your website
integration again" for a day with its branch still at the commit verified on
17 Oct; the only way back was a person asking us to re-read the same CI check.

This renews that proof by itself, for the one case that needs no decision: the
selected branch is still at the verified commit, and the target was verified by
the repository's own native CI check (schema 2, ``authenticated_ci``). It reads
that check again, rechecks the branch head, and re-observes the live route,
through the same functions the source re-verification path uses. The scheduler
renews proof two days before it expires; opening the Articles page renews it
first if the scheduler has not. A changed source, sealed or hosted proof, a
custom contract and any provider refusal stay with the existing owner flows.
"""
from datetime import timedelta
import logging

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .activation import mapping
from .website_contract import WebsiteAuthorityError

logger = logging.getLogger(__name__)

# Renew well inside the seven-day proof window so an idle site never shows it expiring.
REFRESH_AFTER = timedelta(days=5)
PAGE_RETRY_SECONDS = 10 * 60
SWEEP_RETRY_SECONDS = 3 * 60 * 60
SWEEP_EVERY_SECONDS = 30 * 60
SWEEP_LIMIT = 10
# Any of these means the source or setup itself needs work, not just a fresh read.
SOURCE_BLOCKERS = frozenset({"repository_source_changed", "APPLICATION_ROOT_VERIFICATION_REQUIRED",
    "SETUP_BRANCH_VERIFICATION_REQUIRED"})


def enabled():
    return getattr(settings, "WEBSITE_VERIFICATION_AUTO_REFRESH_ENABLED", True) is True


def _stamp(value):
    stamp = parse_datetime(value) if isinstance(value, str) else value
    return stamp if stamp is not None and not timezone.is_naive(stamp) else None


def renewable_proof(config, connection):
    """The verified target and its saved native CI receipt when they can be renewed unattended, else None."""
    if connection is None or connection.state != "connected" or connection.app_root:
        return None
    if str(getattr(config, "github_repo", "") or "").casefold() != connection.github_repo.casefold():
        return None
    if any(isinstance(item, dict) and item.get("code") in SOURCE_BLOCKERS for item in connection.blockers or []):
        return None
    if not mapping(connection.capabilities).get("publishingReady") or not connection.verified_sha:
        return None
    key = str(getattr(config, "default_publish_target_id", "") or "")
    target = connection.targets.filter(generation=connection.generation, target_key=key).first() if key else None
    if (target is None or not target.verified_at or target.adapter == "custom_contract_v1"
            or target.source_sha != connection.verified_sha or not mapping(target.capabilities).get("publishingReady")):
        return None
    proof = mapping(mapping(target.contract).get("verification"))
    if proof.get("status") not in ("passed", "verified") or proof.get("schema_version") == 3:
        return None
    from .website_sealed_workflow import required
    if required(connection, target):
        return None
    from .website_verification import ci_attestation_for
    try:
        receipt = ci_attestation_for(connection, proof)
    except (WebsiteAuthorityError, TypeError, ValueError):
        return None
    if receipt.get("attestation_origin") != "authenticated_ci":
        return None
    return target, receipt


def _deployment_receipt(connection, target):
    operation = connection.operations.filter(generation=connection.generation, action="deployment-verify", state="completed",
        payload__source_sha=connection.verified_sha, payload__target_id=target.target_key).order_by("-created_at").first()
    return mapping(operation.receipt) if operation else {}


def due(connection, target, receipt, *, now=None):
    """Which proof is old enough to renew: (ci, deployment).

    The live check is renewed only when one already passed for this source; a
    first deployment check belongs to setup.
    """
    now = now or timezone.now()
    threshold = now - REFRESH_AFTER
    ci_due = any(stamp is None or stamp < threshold
        for stamp in (connection.last_verified_at, target.verified_at, _stamp(receipt.get("checked_at"))))
    deployment = _deployment_receipt(connection, target)
    deployment_stamp = _stamp(deployment.get("checked_at"))
    deployment_due = deployment.get("status") == "passed" and (deployment_stamp is None or deployment_stamp < threshold)
    return ci_due, deployment_due


def refresh(config, *, now=None, trigger="sweep"):
    """Renew due proof for an unchanged source and report what happened.

    Outcomes: ``fresh`` (nothing due), ``refreshed``, ``needs_owner`` (a provider
    refused, so an owner flow decides), ``unavailable`` (try again later) and
    ``not_renewable`` (this site's proof is not the unattended kind).
    """
    now = now or timezone.now()
    connection = config.website_connection
    renewable = renewable_proof(config, connection)
    if renewable is None:
        return {"outcome": "not_renewable"}
    target, saved = renewable
    ci_due, deployment_due = due(connection, target, saved, now=now)
    if not (ci_due or deployment_due):
        return {"outcome": "fresh"}
    from .website_verification import (current_ci_attestation, discover_source_attestation, record_ci_attestation,
        verify_live_deployment)
    renewed = []
    try:
        receipt = None if ci_due else current_ci_attestation(connection, target)
        if receipt is None:
            # Reads the native check at the verified commit; recording it rechecks
            # that the branch is still there before renewing the proof.
            proof = discover_source_attestation(connection, target, connection.verified_sha)
            receipt = record_ci_attestation(proof).receipt
            renewed.append("ci")
        if deployment_due:
            verify_live_deployment(config, data=receipt)
            renewed.append("deployment")
    except WebsiteAuthorityError as exc:
        outcome = {"outcome": "unavailable" if exc.retryable else "needs_owner", "reason": exc.code, "renewed": renewed}
    except Exception:
        logger.exception("website_freshness_failed connection=%s trigger=%s", connection.pk, trigger)
        outcome = {"outcome": "unavailable", "reason": "website_freshness_unavailable", "renewed": renewed}
    else:
        outcome = {"outcome": "refreshed", "renewed": renewed}
    logger.info("website_freshness connection=%s trigger=%s outcome=%s reason=%s renewed=%s", connection.pk, trigger,
        outcome["outcome"], outcome.get("reason", ""), ",".join(renewed))
    return outcome


def refresh_for_page(organization, *, now=None):
    """Opening Articles renews due proof first, so the page shows the result instead of the expiry."""
    if not enabled():
        return None
    from .models import OrganizationContentConfig
    config = OrganizationContentConfig.objects.select_related("website_connection").filter(organization=organization).first()
    connection = getattr(config, "website_connection", None)
    renewable = renewable_proof(config, connection) if config else None
    if renewable is None or not any(due(connection, *renewable, now=now)):
        return None
    if not cache.add(f"website-freshness:page:{connection.pk}:{connection.generation}", "1", PAGE_RETRY_SECONDS):
        return None
    return refresh(config, now=now, trigger="page")


def run_verification_freshness_sweep(*, now=None, limit=SWEEP_LIMIT):
    """Renew proof for connected sites before it expires. Self-throttled for the minute scheduler."""
    if not enabled():
        return {"status": "disabled"}
    if not cache.add("website-freshness:sweep", "1", SWEEP_EVERY_SECONDS):
        return {"status": "skipped"}
    now = now or timezone.now()
    from .models import OrganizationContentConfig
    counts = {"examined": 0, "refreshed": 0, "needs_owner": 0, "unavailable": 0}
    configs = OrganizationContentConfig.objects.select_related("website_connection").filter(
        website_connection__state="connected", website_connection__last_verified_at__isnull=False,
    ).order_by("website_connection__last_verified_at")
    for config in configs[:200]:
        if counts["examined"] >= limit:
            break
        connection = config.website_connection
        renewable = renewable_proof(config, connection)
        if renewable is None or not any(due(connection, *renewable, now=now)):
            continue
        # A site that could not be renewed waits before the sweep tries it again.
        if not cache.add(f"website-freshness:sweep:{connection.pk}:{connection.generation}", "1", SWEEP_RETRY_SECONDS):
            continue
        counts["examined"] += 1
        outcome = refresh(config, now=now, trigger="sweep")["outcome"]
        if outcome in counts:
            counts[outcome] += 1
    return {"status": "completed", **counts}
