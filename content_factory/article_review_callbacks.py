"""Narrow reconciliation for setup review batches on independent child runs."""
import hashlib
import json
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun
from .models import ContentFactoryCallbackEvent, VibeMarketingComponentComment


def _callback_digest(data):
    payload = {key: value for key, value in data.items() if not key.startswith("_")}
    payload["slack_user_id"] = payload.get("slack_user_id") or ""
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def processed_setup_review_replay(data, *, child_id):
    """Recognize only an identical, acknowledged callback for its fenced child."""
    event = data.get("event_type") or data.get("event")
    event_id = data.get("event_id")
    operation = data.get("review_update_operation_id")
    source_id = data.get("source_setup_run_id")
    if (event != "article_system_setup_revision_ready" or not isinstance(event_id, str)
            or not event_id or len(event_id) > 100 or not operation
            or operation != data.get("feedback_batch_id") or not source_id
            or source_id == child_id or data.get("job_id") != child_id
            or data.get("run_id") != child_id or data.get("workflow") != "article_system_setup"):
        return False
    source = ContentFactoryRun.objects.filter(run_id=source_id, workflow="article_system_setup").first()
    if not source or source.domain != data.get("domain") or source.github_repo != data.get("github_repo"):
        return False
    result = source.result or {}
    ledger = (result.get("article_review_updates") or {}).get(operation) or {}
    batch = (result.get("article_review_outcomes") or {}).get(operation) or {}
    contract = ledger.get("dispatchContract") or {}
    if (ledger.get("status") != "accepted" or ledger.get("revisionRunId") != child_id
            or batch.get("id") != operation or batch.get("revisionRunId") != child_id
            or batch.get("callbackEventId") != event_id or batch.get("callbackDigest") != _callback_digest(data)
            or not contract.get("operation_id") or any(data.get(key) != value for key, value in contract.items())):
        return False
    return ContentFactoryCallbackEvent.objects.filter(event_id=event_id, job_id=child_id,
        event_type=event, processed_at__isnull=False).exists()


def reconcile_setup_review_outcomes(data, child):
    """Save scoped feedback outcomes while preserving the original setup state."""
    operation = str(data.get("review_update_operation_id") or "")
    source_id = str(data.get("source_setup_run_id") or "")
    outcomes = data.get("comment_outcomes", data.get("commentOutcomes"))
    if (not child or child.workflow != "article_system_setup" or not operation
            or operation != data.get("feedback_batch_id") or not source_id
            or source_id == child.run_id or not isinstance(outcomes, list) or len(outcomes) > 100):
        return False
    clean, identities = {}, set()
    for outcome in outcomes:
        if not isinstance(outcome, dict) or outcome.get("status") not in {"addressed", "unaddressed"}:
            return False
        try:
            identity = str(UUID(str(outcome.get("commentId") or outcome.get("comment_id") or "")))
        except (ValueError, TypeError, AttributeError):
            return False
        if identity in identities:
            return False
        identities.add(identity)
        clean[identity] = {"commentId": identity, "status": outcome["status"],
            "summary": str(outcome.get("summary") or "")[:2000], "revisionRunId": child.run_id}
    with transaction.atomic():
        source = ContentFactoryRun.objects.select_for_update().filter(
            run_id=source_id, workflow="article_system_setup",
        ).first()
        if (not source or source.domain != child.domain
                or (getattr(source, "organization_id", None) and getattr(child, "organization_id", None)
                    and source.organization_id != child.organization_id)
                or (source.github_repo and child.github_repo and source.github_repo != child.github_repo)):
            return False
        result = dict(source.result or {})
        ledger = (result.get("article_review_updates") or {}).get(operation) or {}
        if not clean and (not ledger or ledger.get("commentIds")):
            return False
        if ledger.get("revisionRunId") and ledger["revisionRunId"] != child.run_id:
            return False
        comments = list(VibeMarketingComponentComment.objects.select_for_update().filter(
            run=source, batch_id=operation, id__in=identities,
        ))
        if {str(comment.id) for comment in comments} != identities:
            return False
        # Outcomes describe regeneration. APPLIED remains reserved for the
        # existing explicit feedback acceptance flow.
        for comment in comments:
            comment.context = {**(comment.context or {}), "reviewOutcome": clean[str(comment.id)]}
            comment.save(update_fields=["context", "updated_at"])
        batch = {"id": operation, "sourceRunId": source.run_id,
            "revisionRunId": child.run_id, "status": "completed", "outcomes": list(clean.values()),
            "completedAt": timezone.now().isoformat(),
            "commentsPath": str(data.get("review_comments_path") or "")}
        if data.get("event_id"):
            batch.update(callbackEventId=data["event_id"], callbackDigest=_callback_digest(data))
        if ledger:
            updates = dict(result.get("article_review_updates") or {})
            updates[operation] = {**ledger, "status": "accepted", "revisionRunId": child.run_id}
            result["article_review_updates"] = updates
        result.setdefault("article_review_outcomes", {})[operation] = batch
        current = result.get("component_feedback_latest_batch") or {}
        if not current or current.get("id") == operation:
            result["component_feedback_latest_batch"] = {**current, **batch,
                "status": "accepted" if current.get("status") == "accepted" else "completed"}
        source.result = result
        source.save(update_fields=["result", "updated_at"])
        fresh_child = ContentFactoryRun.objects.select_for_update().get(pk=child.pk)
        child_result = dict(fresh_child.result or {})
        child_result["source_setup_run_id"] = source.run_id
        child_result["source_run_id"] = source.run_id
        child_result["component_feedback_latest_batch"] = batch
        fresh_child.result = child_result
        fresh_child.save(update_fields=["result", "updated_at"])
        child.result = child_result
    return True
