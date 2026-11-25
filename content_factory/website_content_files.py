"""Tier 4 of the setup ladder: "delivered, not yet live" (plan step C7).

When Content Factory cannot wire a website (tiers 1 to 3 of its setup ladder
were all left), its last rung delivers the articles as files in a pull request:
Markdown with front matter, images, JSON-LD and ``WIRING.md``, all under
``mlai-content-files/``, a folder the site does not serve. That is not a
published article, and three rules follow:

- **Never Ready.** The preparation is not completed; it shows "delivered, not
  yet live" with the pull request, and Try again is not offered.
- **Never published.** A written article whose content is a content file counts
  as published only once a live check of its real URL passes (``LIVE``);
  merging the pull request onto main is not enough (``article_bucket``).
- **Never the free article.** A run delivered this way is not a delivery for
  the free first article (D7: free until published), so it is not consumed;
  when such an article does go live, the free article is settled then.

The founder-facing state is staged behind ``ARTICLES_SETUP_LADDER_ENABLED``
(off; owner decision D4). Off, a content-files setup from Content Factory is a
misconfiguration the founder must not be shown: it goes to staff fixing (B11),
still never Ready. The two guards above apply whatever the flag says.

No schema change: the state lives in the operation's JSON receipt and the
run's result.
"""
from __future__ import annotations

import logging
import re

from django.conf import settings

logger = logging.getLogger(__name__)

# Mirrors Content Factory's content_files_delivery.CONTENT_ROOT and STATE.
CONTENT_ROOT = "mlai-content-files"
STATE = "delivered_not_live"
HANDOFF = "handoff_prepared"
TIER = "content_files"
RECEIPT_KEY = "content_files"
# Founder copy; clients show a server sentence only up to 240 characters.
MESSAGE = ("Your articles are in a pull request on GitHub, but they are not live yet. We couldn't add article "
           "pages to this website automatically. The pull request says how to publish them. Nothing is charged "
           "until one is live.")
_PR_URL = re.compile(r"^https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/pull/[1-9][0-9]{0,9}$")


def enabled() -> bool:
    """``ARTICLES_SETUP_LADDER_ENABLED``, default off."""
    return getattr(settings, "ARTICLES_SETUP_LADDER_ENABLED", False) is True


def _mapping(value):
    return value if isinstance(value, dict) else {}


def requires_independent_verification(result):
    """A lower-tier change cannot turn its own merge into website publication authority."""
    for source in (_mapping(result), _mapping(_mapping(result).get("article_system_setup"))):
        ladder = _mapping(source.get("setup_ladder"))
        if ladder.get("tier") in ("content_files", "static_dropin"):
            return True
    return False


def static_candidate(result):
    for source in (_mapping(result), _mapping(_mapping(result).get("article_system_setup"))):
        ladder = _mapping(source.get("setup_ladder"))
        if ladder.get("tier") == "static_dropin":
            return _mapping(ladder.get("candidate_publish_target"))
    return {}


def verified_static_target(connection, candidate):
    """Only a certified, deployed target with the exact reviewed replay recipe qualifies."""
    from .models import OrganizationContentConfig
    digest = candidate.get("contract_digest")
    recipe = _mapping(_mapping(candidate.get("registration_strategy")).get("recipe"))
    key = candidate.get("target_id")
    if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not key or recipe.get("kind") != "static_dropin_v1" or not connection.verified_sha):
        return False
    if not OrganizationContentConfig.objects.filter(website_connection=connection,
            organization_id=connection.organization_id, default_publish_target_id=key).exists():
        return False
    target = connection.targets.filter(generation=connection.generation, source_sha=connection.verified_sha,
                                       target_key=key, verified_at__isnull=False).first()
    if (not target or target.contract.get("contract_digest") != digest or
            _mapping(target.contract.get("registration_strategy")).get("recipe") != recipe):
        return False
    return has_static_deployment_proof(connection, target)


def has_static_deployment_proof(connection, target):
    """The provider-verified live listing linked the real detail page at this exact head."""
    rows = connection.operations.filter(generation=connection.generation, action="deployment-verify", state="completed",
        receipt__target_id=target.target_key, receipt__source_sha=target.source_sha,
        receipt__contract_digest=target.contract.get("contract_digest"), receipt__static_directory_link_verified=True)
    if rows.filter(receipt__provider_verified=True).exists():
        return True
    from .reviewed_article_pr_verify import ORIGIN
    from .reviewed_article_pr import ACTION, canonical, sha256
    proof = _mapping(target.contract.get("verification"))
    if proof.get("attestation_origin") != ORIGIN or not proof.get("operation_id"):
        return False
    observed = rows.filter(receipt__attestation_origin=ORIGIN, receipt__operation_id=proof["operation_id"]).first()
    return bool(observed and sha256(canonical(observed.receipt).encode()) == proof.get("evidence_digest")
        and connection.operations.filter(action=ACTION, generation=connection.generation, state="completed",
            payload__wire__operation_id=proof["operation_id"], receipt__status="verified_live",
            receipt__merge_sha=target.source_sha, receipt__target_id=target.target_key).exists())


def setup_delivered_not_live(result) -> dict | None:
    """Content Factory's setup-ladder receipt when the setup ended on tier 4, else None.

    Read from ``setup_ladder`` on the setup result (or its
    ``article_system_setup``). Anything that does not say tier 4, delivered
    and not published is not this state.
    """
    for source in (_mapping(result), _mapping(_mapping(result).get("article_system_setup"))):
        ladder = _mapping(source.get("setup_ladder"))
        if (ladder.get("outcome") in (STATE, HANDOFF) and ladder.get("tier") == TIER
                and ladder.get("counts_as_published") is False):
            return ladder
    return None


def run_delivered_as_content_files(run) -> bool:
    """True when an article run's delivery was a content-files pull request (not a published page)."""
    result = _mapping(getattr(run, "result", None))
    for source in (result, _mapping(result.get("article_system_setup"))):
        marker = _mapping(source.get("content_files_delivery"))
        if marker.get("state") == STATE or marker.get("tier") == TIER:
            return True
        ladder = setup_delivered_not_live(source)
        if ladder and ladder.get("outcome") == STATE:
            return True
    return False


def is_content_files_article(article) -> bool:
    """A written article whose content path is a content file, not a page the site serves."""
    path = str(getattr(article, "content_path", "") or "")
    return path == CONTENT_ROOT or path.startswith(CONTENT_ROOT + "/")


def not_live_q():
    """A ``WrittenArticle`` filter for content-file articles that are not live yet.

    Querysets that count published articles exclude these: a merged content
    file is not a page anyone can read (``article_bucket`` applies the same rule).
    """
    from django.db.models import Q
    from .models import ArticlePublishStatus
    return ((Q(content_path=CONTENT_ROOT) | Q(content_path__startswith=CONTENT_ROOT + "/"))
            & ~Q(publish_status=ArticlePublishStatus.LIVE))


def run_has_live_article(run) -> bool:
    """True once any written article of this run passed the live check of its real URL."""
    from .models import ArticlePublishStatus, WrittenArticle
    run_id = str(getattr(run, "run_id", "") or "")
    organization_id = getattr(run, "organization_id", None)
    if not run_id or organization_id is None:
        return False
    return WrittenArticle.objects.filter(organization_id=organization_id, source_run_id=run_id,
                                         publish_status=ArticlePublishStatus.LIVE).exists()


def _pull_request(ladder) -> dict:
    files = _mapping(ladder.get("content_files"))
    url = str(files.get("pr_url") or "")
    value = {"content_root": CONTENT_ROOT}
    if _PR_URL.match(url):
        value["pr_url"] = url
    if type(files.get("pr_number")) is int and files["pr_number"] > 0:
        value["pr_number"] = files["pr_number"]
    return value


def settle_prepare(op, child, ladder, *, now):
    """Record a tier-4 setup on the preparation. The caller persists ``op``.

    Flag on: "delivered, not yet live" with the pull request, no Try again,
    no automatic retry. Staff see it through the stuck-operation sweep
    (a preparation resting in ``needs_attention``). Flag off: staff fixing, as for any setup we could not
    finish. Never Ready either way.
    """
    rungs = [{key: str(rung.get(key) or "")[:80] for key in ("tier", "outcome", "reason")}
             for rung in ladder.get("rungs") or [] if isinstance(rung, dict)][:4]
    if not enabled():
        from .website_staff_fixing import enter as enter_staff_fixing
        logger.warning("website_content_files_flag_off operation=%s run=%s", op.pk, getattr(child, "run_id", ""))
        enter_staff_fixing(op, now=now, code="ARTICLE_SETUP_STRATEGY_UNAVAILABLE", step="setup",
                           repair_receipt={"stop": "content_files_flag_off", "rounds": 0,
                                           "finding_codes": [rung["reason"] for rung in rungs if rung["reason"]]},
                           run_id=getattr(child, "run_id", ""))
        return op
    pull = _pull_request(ladder)
    if pull.get("pr_url") and not pull["pr_url"].casefold().startswith(
            f"https://github.com/{op.connection.github_repo}/pull/".casefold()):
        pull = {"content_root": CONTENT_ROOT}
    receipt = dict(op.receipt or {})
    first = _mapping(receipt.get(RECEIPT_KEY)).get("since")
    receipt[RECEIPT_KEY] = {"version": 1, "since": first or now.isoformat(), "run_id": str(getattr(child, "run_id", ""))[:120],
                            "rungs": rungs, **pull}
    # Not a failure: no failure envelope or code may project as a stop or a fix.
    for key in ("child_resume_pending", "user_action", "hold_owner", "failure", "reason_code", "failure_fingerprint"):
        receipt.pop(key, None)
    status = ladder["outcome"]
    if status == HANDOFF:
        message = ("The website needs a change to display articles. Review the wiring instructions in the pull request."
                   if pull.get("pr_url") else
                   "The website needs a change to display articles. Wiring instructions have been prepared for review.")
    else:
        message = MESSAGE
    receipt.update(status=status, current_step="setup", message=message, retryable=False, failure_class="needs_user")
    op.receipt = receipt
    # Terminal for the automatic path: nothing more runs by itself, and the
    # receipt's status (not a failure code) tells clients what to show. The
    # pull request link travels in receipt["content_files"]["pr_url"].
    op.state, op.next_attempt_at = "needs_attention", None
    if status == HANDOFF and not pull.get("pr_url") and child.status in {"needs_review", "awaiting_approval", "approval_required"}:
        op.state = "needs_user"
        receipt["user_action"] = {"id": "review_setup", "label": "Review wiring instructions", "run_id": child.run_id}
    from .website_staff_fixing import DELIVERED_NOT_LIVE, active as staff_fixing_active, close as close_staff_fixing
    if staff_fixing_active(op):
        # The bounded staff fix ends: the founder has an honest answer now.
        close_staff_fixing(op, DELIVERED_NOT_LIVE, now=now)
    return op

