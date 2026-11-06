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

import re

from django.conf import settings

from .activation import mapping

ORIGIN = "live_url"
METHOD = "live_url"
# The target contract key that holds the live-URL proof beside the build proof.
PROOF_KEY = "live_verification"
# How many of the newest live articles one check may compare.
DETAIL_CANDIDATES = 5
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


def applicable(target) -> bool:
    """Whether a live-URL proof may stand in for this target's CI execution proof.

    A publishing-inputs candidate, or a target that already requires execution
    proof version three, needs that sealed proof: a live page never replaces it.
    """
    if target is None:
        return False
    from .website_publishing_inputs import has_candidate
    from .website_sealed_workflow import REQUIRED_VERSION
    contract = mapping(getattr(target, "contract", None))
    return not has_candidate(contract) and REQUIRED_VERSION not in mapping(getattr(target, "capabilities", None))


def build_proof(target) -> bool:
    """Whether ``target`` carries MLAI's own build and render proof at its own source.

    That is the proof the scan (or an earlier CI or hosted attestation)
    recorded: ``verified_at``, ``publishingReady`` and a passed verification
    naming the target's exact ``source_sha``. Live-URL proof only adds the
    deployment check to it; it never certifies a build that failed or was
    stored without certification.
    """
    if target is None or not getattr(target, "verified_at", None):
        return False
    if mapping(getattr(target, "capabilities", None)).get("publishingReady") is not True:
        return False
    proof = mapping(mapping(getattr(target, "contract", None)).get("verification"))
    return bool(proof.get("status") in {"passed", "verified"} and proof.get("attestation_origin") != ORIGIN
        and proof.get("source_sha") and proof.get("source_sha") == target.source_sha)


def live_proof(target) -> dict:
    """The live-URL proof stored beside the target's build proof, or ``{}``."""
    return mapping(mapping(getattr(target, "contract", None)).get(PROOF_KEY))


def accepted(connection, target) -> bool:
    """Whether ``target`` carries a current live-URL proof this organisation may use.

    The target must keep its build proof, may not need sealed execution proof
    (:func:`applicable`), and its live proof must name the target's own source,
    contract and marker, which must be the connection's verified source. The
    organisation must still be selected for live-URL verification. Activation,
    the authority guard and scan promotion accept it in place of a CI
    execution proof.
    """
    if connection is None or not build_proof(target) or not applicable(target):
        return False
    contract = mapping(target.contract)
    proof = live_proof(target)
    return bool(proof.get("status") == "verified" and proof.get("attestation_origin") == ORIGIN
        and proof.get("source_sha") and proof.get("source_sha") == target.source_sha == connection.verified_sha
        and proof.get("contract_digest") == contract.get("contract_digest")
        and proof.get("artifact_digest") and proof.get("artifact_digest") == artifact_digest(contract)
        and selected(getattr(connection, "organization", None)))


def artifact_digest(contract):
    """The reviewed ``mlai-artifact-digest`` marker value of a target contract, or ``""``."""
    marker = mapping(mapping(contract).get("live_marker"))
    value = str(marker.get("value") or "")
    return value if _DIGEST.fullmatch(value) else ""


def marker_present(digest, body, headers):
    """Whether a fetched page carries ``digest`` as the header or the meta marker."""
    if str((headers or {}).get("x-mlai-artifact-digest") or "") == digest:
        return True
    pattern = rb'<meta\s+name=["\x27]mlai-artifact-digest["\x27]\s+content=["\x27]' + digest.encode() + rb'["\x27]'
    return bool(re.search(pattern, body or b""))


def detail_candidates(organization, *, limit=DETAIL_CANDIDATES):
    """The newest live articles whose saved HTML can be compared, as ``(article, html)``, newest first."""
    from .models import WrittenArticle
    from workflow_runs.models import ContentFactoryRun
    rows = WrittenArticle.objects.filter(organization=organization).exclude(live_url__isnull=True).exclude(live_url="")
    found = []
    for article in rows.order_by("-published_at", "-pk")[:limit]:
        if not article.source_run_id:
            continue
        run = ContentFactoryRun.objects.filter(run_id=article.source_run_id, organization=organization).first()
        result = mapping(getattr(run, "result", None))
        package = mapping(result.get("content_package")) or mapping(mapping(result.get("result")).get("content_package"))
        html = package.get("article_html")
        if isinstance(html, str) and html:
            found.append((article, html))
    return found


def verify_live_url(config, target, *, setup_run_id=""):
    """Verify ``target`` by its live public pages and record a ``live_url`` receipt.

    The target must already carry MLAI's build proof at its source
    (:func:`build_proof`) and may not need sealed execution proof
    (:func:`applicable`). This fetches the listing route (and up to
    ``DETAIL_CANDIDATES`` live articles) with the pinned fetcher, requires the
    reviewed ``mlai-artifact-digest`` marker on the listing (header or meta),
    and compares one article's body with its saved version when one matches.
    Under the authority guard it then keeps the build proof, writes the live
    proof beside it (``contract["live_verification"]`` with ``attestation_origin:
    "live_url"``), renews ``verified_at`` and ``publishingReady``, and records
    a completed ``deployment-verify`` operation whose receipt has ``method:
    "live_url"`` and ``provider_verified: false``.

    The checks run in ``website_verification.verify_live_url_deployment``, the
    audited emitter of these verification codes. Failures leave the target
    unverified. Returns the receipt with its ``operation_id``.
    """
    from .website_verification import verify_live_url_deployment
    return verify_live_url_deployment(config, target, setup_run_id=setup_run_id)
