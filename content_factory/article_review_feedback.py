"""Carry unresolved feedback into a revision without rewriting its history."""


class _ViewsProxy:
    def __getattr__(self, name):
        from content_factory import vibe_marketing_views

        return getattr(vibe_marketing_views, name)


views = _ViewsProxy()


def inherited_feedback(run, own_comments):
    """Return source comments and their outcomes, scoped to the same company."""
    if run.workflow != "article_revision" or run.status not in {"completed", "awaiting_approval"}:
        return []
    request, result = run.run_request or {}, run.result or {}
    source_id = request.get("source_run_id") or result.get("source_run_id")
    batch_id = request.get("feedback_batch_id") or result.get("feedback_batch_id")
    source = views.ContentFactoryRun.objects.filter(run_id=source_id).first() if source_id else None
    if not source or (source.organization_id != run.organization_id) or (not run.organization_id and source.domain != run.domain):
        return []
    copied = {str((comment.context or {}).get("sourceCommentId")) for comment in own_comments}
    outcomes = {str(item.get("commentId")): item for item in result.get("comment_outcomes", []) if isinstance(item, dict)}
    waived = {str(item.get("id")) for item in result.get("article_review_approval", {}).get("waivedComments", [])}
    records = []
    for comment in views.VibeMarketingComponentComment.objects.filter(run=source).order_by("created_at", "id"):
        key = str(comment.id)
        if key in copied or (comment.status != "draft" and comment.batch_id != batch_id):
            continue
        record = views._serialize_component_comment(comment)
        outcome = outcomes.get(key, {})
        if key in waived:
            record["status"] = "waived"
        elif record["status"] not in {"resolved", "applied"}:
            record["status"] = "addressed" if outcome.get("status") == "addressed" else "draft"
        record["outcome"] = outcome.get("summary") or "This request still needs review."
        record["context"] = {**(record["context"] or {}), "sourceCommentId": key}
        records.append((comment, record))
    return records


def materialize_feedback(run, own_comments, comment_id=None):
    """Copy an unresolved request only when it is edited or submitted again."""
    created = []
    for original, record in inherited_feedback(run, own_comments):
        if record["status"] != "draft" or (comment_id is not None and str(original.id) != str(comment_id)):
            continue
        context = dict(record["context"])
        context.pop("operationId", None)
        created.append(views.VibeMarketingComponentComment.objects.create(
            run=run, actor=original.actor, body=original.body,
            component_id=original.component_id, component_type=original.component_type,
            component_label=original.component_label, source_section_id=original.source_section_id,
            selector=original.selector, anchor=original.anchor, context=context,
        ))
    return created


def accept_addressed_feedback(run, source, batch_id):
    """Accept only confirmed outcomes; never learn from an unresolved request."""
    outcomes = (run.result or {}).get("comment_outcomes")
    comments = views.VibeMarketingComponentComment.objects.filter(run=source, batch_id=batch_id)
    # Legacy revisions had no per-comment report and keep their existing behavior.
    addressed = None if outcomes is None else [str(item.get("commentId")) for item in outcomes if item.get("status") == "addressed"]
    promoted, archived = 0, 0
    if addressed is None or set(addressed) == {str(key) for key in comments.values_list("id", flat=True)}:
        promoted, archived = views._promote_editorial_feedback_batch(run=source, batch_id=batch_id, revision_run_id=run.run_id)
    if addressed is not None:
        comments = comments.filter(id__in=addressed)
    comments.update(status=views.VibeMarketingComponentCommentStatus.APPLIED, updated_at=views.timezone.now())
    return promoted, archived
