"""Company-owned article review facade over Content Factory's canonical draft."""
from urllib.parse import quote

from rest_framework.response import Response
from rest_framework.views import APIView

from . import vibe_marketing_views as views


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
                with views.authority_guard(original, action="read"):
                    pass
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
        return Response({"detail": data.get("detail", "The article changed. Reload and try again.")},
                        status=response.status_code if response.status_code in (400, 404, 409, 422, 503) else 502)
    return data


class VibeMarketingArticleReviewView(views.VibeMarketingRunCommentsMixin, APIView):
    """Read or edit the canonical article using the existing ownership boundary."""

    def get(self, request, run_id):
        _context, run, error = self._resolve_run(request, run_id)
        if error is not None:
            return error
        result = remote_review(run)
        if isinstance(result, Response):
            return result
        return Response({**result, "componentFeedback": views._component_feedback_from_run(run)})

    def post(self, request, run_id):
        context, run, error = self._resolve_run(request, run_id)
        if error is not None:
            return error
        if run.workflow not in views.ARTICLE_WORKFLOWS:
            return Response({"detail": "This is not an article draft."}, status=400)
        if views._run_has_external_publish_evidence(run):
            return Response({"detail": "This article has entered publication. Create a new revision to edit it."}, status=409)
        latest = views._latest_review_ready_component_revision(run, context)
        if latest is not None:
            return Response({"detail": "A newer draft is ready. Open that revision before editing.",
                             "latestRunId": latest.run_id}, status=409)
        payload = dict(request.data)
        if payload.get("action") not in {"editText", "regenerateImage", "chooseImage", "refresh", "undo", "discardImage", "allowAI"}:
            return Response({"detail": "Unknown article operation."}, status=400)
        if payload.get("action") == "regenerateImage":
            billing_error = views._reuse_roo_points_authorization_for_article_job(
                run=run, payload={}, domain=context.organization.domain,
                failure_detail="The original article payment could not be verified.",
            )
            if billing_error is not None:
                return billing_error
        result = remote_review(run, payload=payload)
        return result if isinstance(result, Response) else Response(result)


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
