"""Live-URL verification for new-journey websites (P1.13, contract IF-P1-16).

A new-journey startup's articles setup is proven by its public website, not by
a CI workflow file in its repository: after the setup pull request merges and
the founder's host deploys it, MLAI fetches the blog page (and one article,
when one exists) through the SSRF-pinned fetcher and requires the reviewed
``mlai-artifact-digest`` marker. No workflow file is written and no Workflows,
Checks or Statuses permission is asked for, so new GitHub App installations
need contents and pull requests only. The App's own permissions never change.

Everything here applies only while ``ARTICLES_LIVE_URL_VERIFICATION_ENABLED``
is on and the organisation is new-journey (``founder_journey_cohort``). Legacy
organisations keep exact-commit CI proof byte for byte.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings
from django.utils import timezone

from .activation import mapping
from .website_connections import authority_guard, contract_for, require_unlocked_remote_call
from .website_contract import SHA_PATTERN, WebsiteAuthorityError, evidence_digest
from .website_models import WebsiteConnectionOperation

logger = logging.getLogger(__name__)

ORIGIN = "live_url"
METHOD = "live_url"
_DIGEST = re.compile(r"[a-f0-9]{64}")


def flag_enabled() -> bool:
    """True when ``ARTICLES_LIVE_URL_VERIFICATION_ENABLED`` is on (code default off)."""
    return getattr(settings, "ARTICLES_LIVE_URL_VERIFICATION_ENABLED", False) is True


def selected(organization) -> bool:
    """Whether this organisation verifies its website by live URL instead of CI proof.

    True only with the flag on and a new-journey organisation. Legacy
    organisations, and every organisation while V2 is off or the org is in the
    per-org kill switch, keep CI proof.
    """
    if organization is None or not flag_enabled():
        return False
    from .founder_journey_cohort import is_new_journey
    return is_new_journey(organization)


def accepted(connection, target) -> bool:
    """Whether ``target`` carries a current live-URL proof this organisation may use.

    The proof must name the target's own source, which must be the connection's
    verified source, and the organisation must still be selected for live-URL
    verification. Activation, the authority guard and scan promotion accept it
    in place of a CI execution proof.
    """
    if connection is None or target is None or not getattr(target, "verified_at", None):
        return False
    proof = mapping(mapping(getattr(target, "contract", None)).get("verification"))
    return bool(proof.get("status") == "verified" and proof.get("attestation_origin") == ORIGIN
        and proof.get("source_sha") and proof.get("source_sha") == target.source_sha == connection.verified_sha
        and selected(getattr(connection, "organization", None)))


def _artifact_digest(contract):
    marker = mapping(contract.get("live_marker"))
    value = str(marker.get("value") or "")
    return value if _DIGEST.fullmatch(value) else ""


def _marker_present(digest, body, headers):
    if str((headers or {}).get("x-mlai-artifact-digest") or "") == digest:
        return True
    pattern = rb'<meta\s+name=["\x27]mlai-artifact-digest["\x27]\s+content=["\x27]' + digest.encode() + rb'["\x27]'
    return bool(re.search(pattern, body or b""))


def _article_for_detail(organization):
    """The newest live article whose saved HTML can be compared, or None."""
    from .models import WrittenArticle
    from workflow_runs.models import ContentFactoryRun
    rows = WrittenArticle.objects.filter(organization=organization).exclude(live_url__isnull=True).exclude(live_url="")
    for article in rows.order_by("-published_at", "-pk")[:5]:
        if not article.source_run_id:
            continue
        run = ContentFactoryRun.objects.filter(run_id=article.source_run_id, organization=organization).first()
        result = mapping(getattr(run, "result", None))
        package = mapping(result.get("content_package")) or mapping(mapping(result.get("result")).get("content_package"))
        html = package.get("article_html")
        if isinstance(html, str) and html:
            return article, html
    return None


def _merged_setup_run_ids(website, setup_run_id):
    from workflow_runs.models import ContentFactoryRun
    rows = ContentFactoryRun.objects.filter(organization_id=website.organization_id, workflow="article_system_setup",
        run_request__website_connection_id=str(website.pk), run_request__connection_generation=website.generation)
    ids = [setup_run_id] if setup_run_id else []
    ids += [run_id for run_id in rows.order_by("-updated_at").values_list("run_id", flat=True)[:5] if run_id not in ids]
    return ids


def _require_reviewed_lineage(website, source_sha, setup_run_id):
    """A changed source must be a reviewed setup's own merge, or contain a reviewed head.

    The same lineage rule CI attestation applies: a live page never certifies
    source MLAI did not review.
    """
    from .website_verification import _setup_merged_at, verify_reviewed_source_lineage
    run_ids = _merged_setup_run_ids(website, setup_run_id)
    if any(_setup_merged_at(website, run_id, source_sha) for run_id in run_ids):
        return
    heads = list(website.repository_mutations.filter(generation=website.generation, status="applied",
        run_id__in=run_ids).exclude(head_sha="").values_list("head_sha", flat=True))
    if not heads:
        raise WebsiteAuthorityError("website_source_review_required",
            "Review the changed source before activating articles.")
    verify_reviewed_source_lineage(website, heads, source_sha)


def verify_live_url(config, target, *, setup_run_id=""):
    """Verify ``target`` by its live public pages and record a ``live_url`` receipt.

    Fetches the target's listing route (and one live article, when one exists)
    with ``website_live_fetch.fetch_live_route``; requires the reviewed
    ``mlai-artifact-digest`` marker on the listing (header or meta) and, for the
    article, the saved body (``article_live_evidence.compare_live_body``). Then,
    under the authority guard, it writes the target's ``verified_at``,
    ``publishingReady`` and ``contract.verification`` (``attestation_origin:
    "live_url"``), and a completed ``deployment-verify`` operation whose
    receipt has ``method: "live_url"`` and ``provider_verified: false``.

    Failures raise only registered codes (``live_route_unavailable``,
    ``deployment_origin_invalid``, ``deployment_route_transient`` from the
    fetcher, and the existing verification codes) and leave the target
    unverified. Returns the receipt with its ``operation_id``.
    """
    from .website_live_fetch import fetch_live_route
    from .website_connections import verify_repository_head
    website = config.website_connection
    if website is None:
        raise WebsiteAuthorityError("website_connection_required", "Select and authorize your website repository first.")
    if target is None:
        raise WebsiteAuthorityError("verified_target_required", "Waiting for articles build and render proof.", retryable=True)
    source_sha = str(target.source_sha or "")
    contract = mapping(target.contract)
    route = str(contract.get("route_path") or contract.get("public_path") or "")
    digest = _artifact_digest(contract)
    if not SHA_PATTERN.fullmatch(source_sha):
        raise WebsiteAuthorityError("verified_target_required", "Waiting for articles build and render proof.", retryable=True)
    if not route.startswith("/"):
        raise WebsiteAuthorityError("public_route_required", "The target needs an explicit public route.")
    if not digest or not contract.get("contract_digest"):
        raise WebsiteAuthorityError("deployment_marker_required", "Prepare a certified route with a reviewed artifact digest marker.")
    binding = {**contract_for(website), "source_sha": source_sha, "target_id": target.target_key,
        "contract_digest": contract["contract_digest"], "method": METHOD}
    with authority_guard(binding, action="read") as current:
        current_target = current.targets.filter(generation=current.generation, target_key=target.target_key).first()
        if current_target is None or current_target.contract != target.contract or current_target.source_sha != source_sha:
            raise WebsiteAuthorityError("website_configuration_changed", "The verified target changed during the live check.")
        changed_source = current.verified_sha != source_sha
        domain = current.organization.domain
        organization = current.organization
    require_unlocked_remote_call()
    verify_repository_head(website, source_sha)
    if changed_source:
        _require_reviewed_lineage(website, source_sha, setup_run_id)
    url = f"https://{domain}{route}"
    body, headers = fetch_live_route(url, domain, expected_status=200)
    if not _marker_present(digest, body, headers):
        raise WebsiteAuthorityError("live_route_unavailable", "The public blog page has not deployed this reviewed articles setup yet.")
    observations = [{"kind": "listing", "path": route, "status": 200}]
    detail = _article_for_detail(organization)
    if detail is not None:
        from .article_live_evidence import compare_live_body
        article, expected_html = detail
        page, _ = fetch_live_route(article.live_url, domain, expected_status=200)
        try:
            observed = compare_live_body(expected_html, page, canonical_url=article.live_url)
        except ValueError:
            observed = {"state": "unreadable"}
        if observed.get("state") != "verified":
            raise WebsiteAuthorityError("live_route_unavailable", "The live article does not match its saved version yet.")
        from urllib.parse import urlsplit
        observations.append({"kind": "detail", "path": urlsplit(article.live_url).path, "status": 200,
            "body_sha256": observed.get("observed_body_sha256")})
    now = timezone.now()
    receipt = {"status": "passed", "method": METHOD, "attestation_origin": ORIGIN, "provider_verified": False,
        "source_sha": source_sha, "connection_generation": website.generation, "target_id": target.target_key,
        "public_url": url, "checked_at": now.isoformat(), "artifact_digest": digest,
        "contract_digest": contract["contract_digest"], "verified_routes": observations}
    with authority_guard(binding, action="read") as current:
        target = current.targets.filter(generation=current.generation, target_key=binding["target_id"]).first()
        if target is None or target.contract != current_target.contract or target.source_sha != source_sha:
            raise WebsiteAuthorityError("website_configuration_changed", "The verified target changed during the live check.")
        target.verified_at = now
        target.contract = {**target.contract, "verification": {"status": "verified", "attestation_origin": ORIGIN,
            "observed_at": now.isoformat(), "source_sha": source_sha, "target_id": target.target_key,
            "contract_digest": contract["contract_digest"], "artifact_digest": digest, "public_url": url}}
        target.capabilities = {**(target.capabilities or {}), "publishingReady": True, "previewSupported": True}
        target.save(update_fields=["verified_at", "contract", "capabilities", "updated_at"])
        from .models import OrganizationContentConfig
        saved = OrganizationContentConfig.objects.get(website_connection=current)
        if not saved.default_publish_target_id:
            saved.default_publish_target_id = target.target_key
        saved.publish_targets = [row for row in (saved.publish_targets or []) if row.get("target_id") != target.target_key] + [target.contract]
        saved.save(update_fields=["default_publish_target_id", "publish_targets", "updated_at"])
        if current.verified_sha != source_sha:
            previous_revision = current.configuration_version
            current.configuration_version += 1
            from .website_goal_scope import record_source_transition
            record_source_transition(current, previous_revision=previous_revision, source_sha=source_sha)
        current.verified_sha, current.last_verified_at = source_sha, now
        current.blockers = [row for row in current.blockers if row.get("code") != "repository_source_changed"]
        connected = current.state == "connected"
        current.capabilities = {**(current.capabilities or {}), "publishingReady": connected,
            "generationReady": connected, "previewSupported": True}
        current.save(update_fields=["verified_sha", "last_verified_at", "configuration_version", "blockers", "capabilities", "updated_at"])
        operation, _ = WebsiteConnectionOperation.objects.update_or_create(
            idempotency_key=f"{current.pk}:live-url:{evidence_digest(binding)}", defaults={
                "connection": current, "generation": current.generation, "action": "deployment-verify",
                "state": "completed", "payload": binding, "receipt": receipt})
        from .website_prepare import wake_prepare_after_commit
        wake_prepare_after_commit(current.pk)
    logger.info("website_live_url_verified org=%s connection=%s target=%s", current.organization_id, current.pk, target.target_key)
    return {**receipt, "operation_id": str(operation.pk)}
