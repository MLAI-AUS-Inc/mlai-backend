"""Company-owned article review facade over Content Factory's canonical draft."""
import hashlib
import json
import re
from copy import deepcopy
from uuid import UUID
from django.db import transaction
from urllib.parse import quote

from rest_framework.response import Response
from rest_framework.views import APIView

from . import vibe_marketing_views as views
from .article_export import article_export


def remote_review(run, *, payload=None):
    """Forward a scoped review operation without passing browser credentials."""
    views.require_unlocked_remote_call()
    if payload is not None:
        original = views.scoped_run_contract(run)
        try:
            supplied = views.connection_contract(payload)
            binding = views.connection_contract(original)
            if supplied and any(binding.get(key) != value for key, value in supplied.items()):
                raise views.WebsiteAuthorityError("website_connection_changed", "The article belongs to a previous website connection.")
            if binding or original.get("delivery_mode") != "content_only":
                guarded = {**original, **{key: payload[key] for key in (
                    "operation_id", "operation_attempt", "deletion_epoch",
                ) if key in payload}}
                with views.authority_guard(guarded, action="read"):
                    pass
                if (binding and payload.get("action") == "refresh"
                        and run.status in {"failed", "blocked"}):
                    # Inspect before reserving: a completed/no-op refresh must
                    # not consume an attempt. Recheck the same revision remotely.
                    snapshot = remote_review(run)
                    if isinstance(snapshot, Response):
                        return snapshot
                    if not snapshot.get("previewPending") or not snapshot.get("refreshError"):
                        return snapshot
                    from .website_operations import advance_workflow_attempt
                    attempt = advance_workflow_attempt(run)
                    payload = {**payload, **attempt, "expectedRevision": snapshot["revision"]}
                payload = {**payload, **binding}
        except views.WebsiteAuthorityError as exc:
            return Response(exc.as_dict(), status=exc.status)
    config = views._content_factory_remote_config()
    if not config["enabled"]:
        return Response({"detail": "Article editing is temporarily unavailable."}, status=503)
    url = f"{config['base_url']}/api/runs/{quote(run.run_id, safe='')}/article-review"
    try:
        response = views.http_client.request(
            "GET" if payload is None else "POST", url,
            json=payload, headers=views._content_factory_headers(), timeout=(5, 30),
        )
        data = response.json()
    except (views.http_client.RequestException, ValueError):
        return Response({"detail": "Article editing could not connect. Your input has been kept; retry."}, status=502)
    if response.status_code not in (200, 202):
        data = data if isinstance(data, dict) else {}
        failure = {key: data[key] for key in ("code", "error_code", "reasonCode", "retryable", "next_action", "failure", "errors", "revision") if key in data}
        return Response({**failure, "detail": data.get("detail") or data.get("error") or "The article changed. Reload and try again."},
                        status=response.status_code if response.status_code in (400, 404, 409, 422, 503) else 502)
    return data


def normalize_review_update(data):
    """Validate the update batch without trusting caller-supplied comment bodies."""
    operation = data.get("operationId")
    revision = data.get("expectedRevision")
    if not isinstance(operation, str) or not re.fullmatch(r"[\w-]{8,100}", operation):
        raise ValueError("A stable update identity is required.")
    if not isinstance(revision, str) or not revision or len(revision) > 200:
        raise ValueError("Reload this draft before applying changes.")
    edits = data.get("textEdits", data.get("edits", []))
    comment_ids = data.get("commentIds", [])
    restored = data.get("restoredSentences", [])
    if not isinstance(restored, list) or len(restored) > 50 or any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in restored):
        raise ValueError("Choose at most 50 removed sentences to restore.")
    restored = list(dict.fromkeys(item.strip() for item in restored))
    if not isinstance(edits, list) or len(edits) > 300:
        raise ValueError("Use a text edit list with at most 300 fields.")
    if not isinstance(comment_ids, list) or len(comment_ids) > 100:
        raise ValueError("Use a comment list with at most 100 comments.")
    clean_edits = []
    fields = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("Every text edit must identify a field and its text.")
        field_id = edit.get("fieldId")
        value = edit.get("value")
        original = edit.get("originalValue")
        if not isinstance(field_id, str) or not field_id or len(field_id) > 255 or field_id in fields:
            raise ValueError("Every text edit must identify a different field.")
        if any(not isinstance(text, str) or len(text) > 100000 for text in (value, original)):
            raise ValueError("Text and its original value must be strings of at most 100,000 characters.")
        fields.add(field_id)
        clean_edits.append({"fieldId": field_id, "value": value, "originalValue": original})
    try:
        ids = [str(UUID(item)) for item in comment_ids if isinstance(item, str)]
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Choose valid saved comments before applying changes.") from exc
    if len(ids) != len(comment_ids) or len(set(ids)) != len(ids):
        raise ValueError("Choose each saved comment once before applying changes.")
    if not clean_edits and not ids and not restored:
        raise ValueError("Add a comment or edit some text before updating.")
    return {"action": "applyUpdate", "operationId": operation,
            "expectedRevision": revision, "textEdits": clean_edits, "commentIds": ids,
            **({"restoredSentences": restored} if restored else {})}



def _fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()



def _claim_comments(run, payload):
    """Freeze only this run's selected comments while a stable batch is submitted."""
    ids = payload["commentIds"]
    if not ids:
        return [], [], None
    operation = payload["operationId"]
    with transaction.atomic():
        comments = list(views.VibeMarketingComponentComment.objects.select_for_update().filter(
            run=run, id__in=ids,
        ).order_by("created_at", "id"))
        if {str(comment.id) for comment in comments} != set(ids):
            return [], [], Response({"detail": "A selected comment no longer belongs to this draft."}, status=409)
        if any(not str(comment.body or "").strip() or not (
            comment.status == views.VibeMarketingComponentCommentStatus.DRAFT
            or (comment.status == views.VibeMarketingComponentCommentStatus.SUBMITTED
                and comment.batch_id == operation)
        ) for comment in comments):
            return [], [], Response({"detail": "Comments changed or were already submitted. Reload before updating."}, status=409)
        claimed = [comment.id for comment in comments
                   if comment.status == views.VibeMarketingComponentCommentStatus.DRAFT]
        if claimed:
            views.VibeMarketingComponentComment.objects.filter(
                run=run, id__in=claimed, status=views.VibeMarketingComponentCommentStatus.DRAFT,
            ).update(status=views.VibeMarketingComponentCommentStatus.SUBMITTED,
                     batch_id=operation, updated_at=views.timezone.now())
    return comments, claimed, None



def _revision_authorization(request, context, run, payload):
    """Preserve the existing AI revision gates; direct text changes need no AI."""
    if not payload["commentIds"] and not payload.get("restoredSentences"):
        return None
    if run.workflow == "article_system_setup":
        error, balance = views._require_roo_points_for_ai_agent(
            request.user, domain=context.organization.domain, action="article_system_revision",
        )
        if error is not None:
            return error
        views._mark_roo_points_gate_authorized(
            payload, domain=context.organization.domain, action="article_system_revision",
            current_balance=balance,
        )
        return None
    error = views._setup_blocked_response_for_generation(
        context, views._get_config(context.organization), run=run,
    )
    if error is not None:
        return error
    editorial, error = views._revision_editorial_payload_from_run(context=context, run=run)
    if error is not None:
        return error
    error = views._reuse_roo_points_authorization_for_article_job(
        run=run, payload=editorial, domain=context.organization.domain,
        failure_detail="The original article payment could not be verified.",
    )
    if error is not None:
        return error
    payload.update(editorial)
    return None



def _record_update_result(run, operation, entry, feedback=None):
    """Merge one dispatch outcome into fresh metadata without erasing callbacks."""
    with transaction.atomic():
        fresh = views.ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = dict(fresh.result or {})
        updates = dict(result.get("article_review_updates") or {})
        previous = updates.get(operation) or {}
        if previous.get("requestHash") and previous["requestHash"] != entry["requestHash"]:
            return Response({"detail": "This update identity was used for different changes."}, status=409)
        # An uncertain retry must not downgrade a dispatch already acknowledged
        # by another request or callback.
        if previous.get("status") == "accepted" and entry["status"] != "accepted":
            entry = previous
        else:
            if "remoteComments" in previous:
                entry = {**entry, "remoteComments": previous["remoteComments"]}
            if not previous:
                entry = {**entry, "previousFeedbackBatch": result.get("component_feedback_latest_batch")}
        updates[operation] = {**previous, **entry}
        result["article_review_updates"] = updates
        if feedback is not None:
            current = result.get("component_feedback_latest_batch") or {}
            if current.get("id") == operation:
                feedback = {**feedback, **current,
                            "revisionRunId": current.get("revisionRunId") or feedback.get("revisionRunId")}
                if current.get("status") == "submitted" and entry["status"] == "accepted":
                    feedback["status"] = "running"
            result["component_feedback_latest_batch"] = feedback
        fresh.result = result
        fresh.save(update_fields=["result", "updated_at"])
        run.result = result
    return None



def _revert_update_submission(run, operation, request_hash):
    """Discard only a definitely rejected submission, preserving concurrent acknowledgments."""
    with transaction.atomic():
        fresh = views.ContentFactoryRun.objects.select_for_update().get(pk=run.pk)
        result = dict(fresh.result or {})
        updates = dict(result.get("article_review_updates") or {})
        entry = updates.get(operation) or {}
        if entry.get("requestHash") != request_hash or entry.get("status") == "accepted":
            return False
        updates.pop(operation, None)
        if updates:
            result["article_review_updates"] = updates
        else:
            result.pop("article_review_updates", None)
        batch = result.get("component_feedback_latest_batch") or {}
        if batch.get("id") == operation and batch.get("status") == "submitted":
            if entry.get("previousFeedbackBatch"):
                result["component_feedback_latest_batch"] = entry["previousFeedbackBatch"]
            else:
                result.pop("component_feedback_latest_batch", None)
        fresh.result = result
        fresh.save(update_fields=["result", "updated_at"])
        run.result = result
    return True



def _review_dispatch_contract(run, payload):
    """Reserve fresh review work under the source's original website consent."""
    original = views.scoped_run_contract(run)
    try:
        binding = views.connection_contract(original)
        if not binding and original.get("delivery_mode") == "content_only":
            return {}
        if run.workflow != "article_system_setup" and not payload.get("commentIds") and not payload.get("restoredSentences"):
            # Deterministic edits remain on this article's existing run. Only
            # a child revision may acquire a separate immutable operation.
            return {**binding, **{key: original[key] for key in (
                "operation_id", "operation_attempt", "deletion_epoch", "client_request_id",
            ) if key in original}}
        with views.authority_guard(original, action="read") as website:
            from .website_operations import reserve_workflow_operation, OPERATION_FIELDS
            reservation = {**binding, "domain": run.domain, "github_repo": run.github_repo,
                "client_request_id": "review-" + payload["operationId"], "source_run_id": run.run_id,
                "textEdits": payload["textEdits"], "comments": payload["comments"]}
            reserve_workflow_operation(website, workflow="article_system_setup" if run.workflow == "article_system_setup"
                                       else "article_revision", payload=reservation)
            return {**binding, "client_request_id": reservation["client_request_id"],
                    **{key: reservation[key] for key in OPERATION_FIELDS}}
    except views.WebsiteAuthorityError as exc:
        return Response(exc.as_dict(), status=exc.status)


class VibeMarketingArticleReviewView(views.VibeMarketingRunCommentsMixin, APIView):
    allows_setup_review_updates = True
    allows_accepted_update_retries = True
    """Read or edit the canonical article using the existing ownership boundary."""

    def get(self, request, run_id):
        context, run, error = self._resolve_run(request, run_id)
        if error is not None:
            return error
        result = remote_review(run)
        if isinstance(result, Response):
            return result
        if run.workflow == "article_system_setup":
            return Response({**result, "componentFeedback": views._component_feedback_from_run(run)}, headers={"Cache-Control": "private, no-store"})
        from .article_review_billing import reconcile_image_charges
        reconcile_image_charges(user=request.user, run=run, snapshot=result)
        latest = views._latest_review_ready_component_revision(run, context)
        from .article_review_billing import image_regeneration_cost
        response = Response({**result, "imageRegenerationCostPoints": image_regeneration_cost(context.organization.domain), "componentFeedback": views._component_feedback_from_run(run),
            "articleExport": article_export(result, run_id=run.run_id, latest_run_id=latest.run_id if latest else None)})
        response["Cache-Control"] = "private, no-store"
        return response

    def post(self, request, run_id):
        context, run, error = self._resolve_run(request, run_id)
        if error is not None:
            return error
        if run.workflow not in {*views.ARTICLE_WORKFLOWS, "article_system_setup"}:
            return Response({"detail": "This run does not have an article preview."}, status=400)
        setup_revision = (run.workflow == "article_system_setup"
                          and isinstance(request.data, dict)
                          and request.data.get("action") in {"applyUpdate", "applyReview"})
        if views._run_has_external_publish_evidence(run) and not setup_revision:
            return Response({"detail": "This draft has entered publication. Create a revision to edit it."}, status=409)
        data = request.data
        if not isinstance(data, dict):
            return Response({"detail": "Use a JSON review update."}, status=400)
        if data.get("action") in {"applyUpdate", "applyReview"}:
            return self._apply_update(request, context, run, data)
        latest = views._latest_review_ready_component_revision(run, context)
        if latest is not None:
            return Response({"detail": "A newer draft is ready. Open it before editing.",
                             "latestRunId": latest.run_id}, status=409)
        payload = dict(request.data)
        if payload.get("action") not in {"editText", "editTextBatch", "regenerateImage", "chooseImage", "refresh", "undo", "discardImage", "allowAI"}:
            return Response({"detail": "Unknown article operation."}, status=400)
        if payload.get("action") == "regenerateImage":
            billing_error = views._reuse_roo_points_authorization_for_article_job(
                run=run, payload={}, domain=context.organization.domain,
                failure_detail="The original article payment could not be verified.",
            )
            if billing_error is not None:
                return billing_error
        receipt = None
        if payload.get("action") == "regenerateImage":
            from .article_review_billing import reserve_image_charge
            receipt, error = reserve_image_charge(user=request.user, context=context, run=run, payload=payload)
            if error is not None:
                return error
        result = remote_review(run, payload=payload)
        if receipt is not None:
            from .article_review_billing import settle_image_charge
            rejected = isinstance(result, Response) and result.status_code in {400, 404, 409, 422}
            pending = isinstance(result, Response) and not rejected
            settle_image_charge(user=request.user, run=run, receipt=receipt, rejected=rejected, pending=pending)
        return result if isinstance(result, Response) else Response(result)

    def _apply_update(self, request, context, run, data):
        try:
            payload = normalize_review_update(data)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        request_hash = _fingerprint({key: value for key, value in payload.items() if key != "expectedRevision"})
        updates = (run.result or {}).get("article_review_updates") or {}
        existing = updates.get(payload["operationId"]) or {}
        if existing.get("requestHash") and existing["requestHash"] != request_hash:
            return Response({"detail": "This update identity was used for different changes."}, status=409)
        if existing.get("status") == "accepted":
            result = remote_review(run)
            if isinstance(result, Response):
                return result
            return Response({**result, "revisionRunId": existing.get("revisionRunId")})
        latest = views._latest_review_ready_component_revision(run, context)
        if latest is not None:
            return Response({"detail": "A newer draft is ready. Open it before editing.",
                             "latestRunId": latest.run_id}, status=409)
        error = _revision_authorization(request, context, run, payload)
        if error is not None:
            return error
        comments, claimed, error = _claim_comments(run, payload)
        if error is not None:
            return error
        remote_payload = {**payload, "source_run_id": run.run_id,
                          "feedback_batch_id": payload["operationId"],
                          "request_source": "founder_tools_review_update"}
        convert = views._article_system_remote_comment_payload if run.workflow == "article_system_setup" else views._remote_comment_payload
        remote_payload["comments"] = [convert(comment) if run.workflow == "article_system_setup"
                                      else convert(comment, run=run) for comment in comments]
        # Setup's older comment payload lacks component identity; retain it for
        # matching comment targets to text changes in the combined worker update.
        for remote, comment in zip(remote_payload["comments"], comments):
            remote.setdefault("component_id", comment.component_id)
            remote.setdefault("component_type", comment.component_type)
        remote_payload["comments"].extend({
            "comment_id": f"{payload['operationId']}-restore-{index}", "component_id": "article",
            "component_type": "article_body", "component_label": "Removed sentence",
            "body": f"Restore sentence: {sentence}; recheck authoritative sources and safety before acceptance.",
            "requested_action": "restore_sentence", "context": {"restoredSentence": sentence},
        } for index, sentence in enumerate(payload.get("restoredSentences", [])))
        submission = {"requestHash": request_hash, "status": "submitted", "revisionRunId": None,
                      "commentIds": payload["commentIds"], "remoteComments": deepcopy(remote_payload["comments"]),
                      "recordedAt": views.timezone.now().isoformat()}
        feedback = {"id": payload["operationId"], "sourceRunId": run.run_id,
                    "revisionRunId": None, "status": "submitted"} if comments or payload.get("restoredSentences") else None
        error = _record_update_result(run, payload["operationId"], submission, feedback)
        if error is not None:
            return error
        recorded = run.result["article_review_updates"][payload["operationId"]]
        if recorded["status"] == "accepted":
            result = remote_review(run)
            return result if isinstance(result, Response) else Response({**result, "revisionRunId": recorded.get("revisionRunId")})
        # A fast callback can add outcome context to saved rows. Replay the
        # original wire comment payload, never a newly serialized retry.
        remote_payload["comments"] = deepcopy(recorded["remoteComments"])
        contract = recorded.get("dispatchContract")
        if contract is None:
            contract = _review_dispatch_contract(run, remote_payload)
            if isinstance(contract, Response):
                if _revert_update_submission(run, payload["operationId"], request_hash) and payload["commentIds"]:
                    views.VibeMarketingComponentComment.objects.filter(run=run, id__in=payload["commentIds"],
                        status=views.VibeMarketingComponentCommentStatus.SUBMITTED, batch_id=payload["operationId"]).update(
                            status=views.VibeMarketingComponentCommentStatus.DRAFT, batch_id="", updated_at=views.timezone.now())
                return contract
            if contract:
                error = _record_update_result(run, payload["operationId"], {**submission, "dispatchContract": contract}, feedback)
                if error is not None:
                    return error
        remote_payload.update(contract)
        if claimed and run.workflow != "article_system_setup":
            # The worker can start as soon as dispatch is accepted. Create its
            # learning candidates first, once, as the existing comments path does.
            views._create_editorial_feedback_candidates(
                organization=context.organization, run=run, comments=comments,
                batch_id=payload["operationId"],
            )
        result = remote_review(run, payload=remote_payload)
        definitive_rejection = isinstance(result, Response) and result.status_code in {400, 404, 409, 422}
        if definitive_rejection:
            if _revert_update_submission(run, payload["operationId"], request_hash) and payload["commentIds"]:
                views.VibeMarketingComponentComment.objects.filter(
                    run=run, id__in=payload["commentIds"], status=views.VibeMarketingComponentCommentStatus.SUBMITTED,
                    batch_id=payload["operationId"],
                ).update(status=views.VibeMarketingComponentCommentStatus.DRAFT, batch_id="",
                         updated_at=views.timezone.now())
            return result
        accepted = not isinstance(result, Response)
        revision_id = (result.get("revisionRunId") or result.get("newRunId")) if accepted else None
        entry = {"requestHash": request_hash,
            "status": "accepted" if accepted else "submitted", "revisionRunId": revision_id,
            "commentIds": payload["commentIds"], "recordedAt": views.timezone.now().isoformat()}
        feedback = {"id": payload["operationId"], "sourceRunId": run.run_id,
                    "revisionRunId": revision_id, "status": "running" if accepted else "submitted"} if comments or payload.get("restoredSentences") else None
        if accepted and revision_id and revision_id != run.run_id:
            child = views._create_local_run(
                workflow="article_system_setup" if run.workflow == "article_system_setup" else "article_revision",
                domain=context.organization.domain, github_repo=run.github_repo or "",
                actor_id=views.founder_actor_id_for_user(request.user), payload=remote_payload,
                remote_data={"run_id": revision_id, "status": "queued"},
                preserve_existing=True,
            )
            if contract.get("operation_id"):
                from .website_models import WebsiteConnectionOperation
                from .website_operations import bind_operation_run
                operation = WebsiteConnectionOperation.objects.get(pk=contract["operation_id"])
                bind_operation_run(operation, child)
        error = _record_update_result(run, payload["operationId"], entry, feedback)
        if error is not None:
            return error
        return result if isinstance(result, Response) else Response(result, status=202 if result.get("previewPending") else 200)



def check_approval_comments(run, payload):
    """Require an exact waiver for every current unresolved comment."""
    comments = list(views.VibeMarketingComponentComment.objects.filter(
        run=run, status=views.VibeMarketingComponentCommentStatus.DRAFT).order_by("id"))
    from .article_review_feedback import inherited_feedback
    own = list(views.VibeMarketingComponentComment.objects.filter(run=run)) if run.workflow == "article_revision" else []
    inherited = [record for _, record in inherited_feedback(run, own) if record["status"] == "draft"]
    expected = sorted([(str(comment.id), comment.body) for comment in comments] + [(record["id"], record["body"]) for record in inherited])
    waived = payload.get("waivedComments", [])
    if not isinstance(waived, list) or any(not isinstance(item, dict) for item in waived):
        return Response({"detail": "Review unresolved comments before approving."}, status=409)
    actual = sorted((str(item.get("id", "")), item.get("body", "")) for item in waived)
    if actual != expected:
        return Response({"detail": "Comments changed. Apply them or explicitly approve without the current comments.",
                         "code": "unresolved_comments"}, status=409)
    batch = (run.result or {}).get("component_feedback_latest_batch") or {}
    if batch.get("status") in {"submitted", "running"} and batch.get("revisionRunId") != run.run_id:
        return Response({"detail": "A revision is in progress. Review that draft before approving."}, status=409)
    return None


def record_review_approval(run, context, payload):
    """Record explicit waivers and accept the completed revision being approved."""
    result = dict(run.result or {})
    result["article_review_approval"] = {
        "revision": payload.get("reviewedRevision"), "waivedComments": payload.get("waivedComments", []),
        "recordedAt": views.timezone.now().isoformat(),
    }
    run.result = result
    run.save(update_fields=["result", "updated_at"])
    if run.workflow != "article_revision" or not payload.get("acceptDisplayedRevision"):
        return
    source_id = (run.run_request or {}).get("source_run_id") or result.get("source_run_id")
    batch_id = (run.run_request or {}).get("feedback_batch_id") or result.get("feedback_batch_id")
    source = views.ContentFactoryRun.objects.filter(run_id=source_id).first() if source_id else None
    if not batch_id or not source or not views._run_belongs_to_context(source, context):
        return
    from .article_review_feedback import accept_addressed_feedback
    promoted, archived = accept_addressed_feedback(run, source, batch_id)
    for target in (source, run):
        target.result = {**(target.result or {}), "component_feedback_latest_batch": {
            "id": batch_id, "sourceRunId": source.run_id, "revisionRunId": run.run_id,
            "status": "accepted", "promotedLearningCount": promoted, "archivedLearningCount": archived,
        }}
        target.save(update_fields=["result", "updated_at"])
