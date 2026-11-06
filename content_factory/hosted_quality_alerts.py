"""Owner-queued hosted review alerts, independent of the broader P2 detector."""
import logging

from django.conf import settings
from django.db import transaction

from workflow_runs.models import ContentFactoryRun
from .article_publish_approval import article_review_identity, _first_present, _generation
from .founder_journey_alerts import (
    ARTICLE_WORKFLOWS, YOUR_TURN_STATUSES, _receipts_in,
    founder_journey_alert_id, is_scheduler_run, queue_founder_journey_alert,
)

logger = logging.getLogger(__name__)
REVIEW_STATUSES = YOUR_TURN_STATUSES | {"blocked", "failed", "completed"}


def _held_review(run):
    if (getattr(run, "workflow", None) not in ARTICLE_WORKFLOWS
            or getattr(run, "status", None) not in REVIEW_STATUSES
            or not getattr(run, "organization_id", None) or is_scheduler_run(run)):
        return None
    result = run.result if isinstance(run.result, dict) else {}
    quality = result.get("article_preview_quality")
    if (not isinstance(quality, dict) or quality.get("status") != "blocking_findings"
            or quality.get("self_heal_exhausted") is not True):
        return None
    from .portable_drafts import original_portable_run
    from .run_reconciliation import authoritative_run
    from .run_review_quality import current_review_verified
    from .run_ownership import user_stop_intent
    if (original_portable_run(run) or user_stop_intent(run)
            or (authoritative_run(run) and not current_review_verified(run))):
        return None
    identity = article_review_identity(run)
    quality_url = _first_present(quality, "preview_url", "previewUrl")
    quality_generation = _generation(_first_present(quality, "resume_generation", "resumeGeneration"))
    if (not identity["preview_url"].startswith(("https://", "http://"))
            or quality_url != identity["preview_url"] or quality_generation is None
            or quality_generation != identity["resume_generation"]):
        return None
    reviewed_commit = quality.get("reviewed_preview_commit_sha")
    if reviewed_commit and reviewed_commit != identity["commit_sha"]:
        return None
    reason = ("article_safety_review_required" if quality.get("escalation") == "safety_review"
              else "article_quality_review_required")
    return reason, identity, quality.get("self_heal_fingerprint")


def queue_hosted_quality_review_alert(run):
    """Queue only a current held review after commit; the existing sweep delivers it."""
    if not getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", False):
        return
    expected = _held_review(run)
    if expected is None:
        return
    reason = expected[0]
    alert_id = founder_journey_alert_id(
        organization_id=run.organization_id, alert_reason=reason, run_id=run.run_id)
    receipt = _receipts_in(run.result).get(reason)
    if isinstance(receipt, dict) and receipt.get("alert_id") == alert_id:
        return
    pk, organization_id = run.pk, run.organization_id

    def after_commit():
        if not getattr(settings, "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED", False):
            return
        try:
            current = ContentFactoryRun.objects.select_related("organization").filter(pk=pk).first()
            if (current is None or current.organization_id != organization_id
                    or _held_review(current) != expected):
                return
            queue_founder_journey_alert(
                organization=current.organization, run=current, alert_reason=reason)
        except Exception as exc:
            # The next current-owner observation retries the same stable id.
            # No raw article, provider response or credential belongs in logs.
            logger.warning("hosted_quality_alert_queue_failed run_pk=%s reason=%s error_type=%s",
                           pk, reason, type(exc).__name__)

    transaction.on_commit(after_commit)
