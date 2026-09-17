"""Article attribution from persisted writing decisions; never today's catalogue."""
from copy import deepcopy
from .editorial_contract import ArticleEditorialAdmission, ArticleEditorialBrief


class ArticleEditorialConflict(ValueError):
    pass


def snapshot_from_run(run, organization, incoming_admission=None):
    if run.organization_id != organization.pk:
        raise ArticleEditorialConflict("Writing run belongs to another organization")
    request = run.run_request or {}
    if not isinstance(request, dict):
        raise ArticleEditorialConflict("Saved writing request is invalid")
    try:
        if ArticleEditorialAdmission.normalized_domain(run.domain) != ArticleEditorialAdmission.normalized_domain(organization.domain):
            raise ValueError("Domain mismatch")
    except ValueError:
        raise ArticleEditorialConflict("Writing domain differs from organization") from None
    brief = request.get("editorial_brief") or request.get("editorialBrief")
    saved = request.get("editorial_admission")
    if incoming_admission is not None and incoming_admission != saved:
        raise ArticleEditorialConflict("Attribution differs from the saved writing decision")
    if not brief:
        if saved:
            raise ArticleEditorialConflict("Saved admission has no original brief")
        return None
    try:
        normalized = ArticleEditorialBrief.model_validate(brief).model_dump(mode="json")
        admission = ArticleEditorialAdmission.model_validate(saved) if saved else None
        if admission:
            admission.assert_request(normalized, run.domain, request.get("github_repo"))
            if admission.normalized_domain(organization.domain) != admission.normalized_domain(run.domain):
                raise ValueError("Domain mismatch")
    except (ValueError, TypeError):
        raise ArticleEditorialConflict("Saved editorial evidence is invalid") from None
    return {
        "schema_version": 1, "writing_run_id": run.run_id,
        "recorded_at": run.created_at.isoformat(), "brief": normalized,
        "admission": admission.model_dump(mode="json") if admission else None,
        "provenance_status": "recorded" if admission else "partial",
    }


def snapshot_fields(snapshot):
    if not snapshot:
        return {}
    brief = snapshot["brief"]
    return {"editorial_snapshot": snapshot, "audience_id": brief["audience_id"],
            "audience_version": brief["audience_version"], "offer_id": brief["offer_id"] or "",
            "offer_version": brief["offer_version"], "conversion_intent": brief["conversion_intent"],
            "editorial_provenance_status": snapshot["provenance_status"]}


def parent_run_id(run):
    request = run.run_request if isinstance(run.run_request, dict) else {}
    result = getattr(run, "result", None)
    result = result if isinstance(result, dict) else {}
    return str(request.get("revision_source_run_id") or request.get("source_run_id") or request.get("sourceRunId") or result.get("source_run_id") or result.get("sourceRunId") or "")


def _is_descendant(run, ancestor_id, organization):
    from workflow_runs.models import ContentFactoryRun
    seen = set()
    while run and run.run_id not in seen and len(seen) < 32:
        if run.run_id == ancestor_id:
            return True
        seen.add(run.run_id)
        parent = parent_run_id(run)
        run = ContentFactoryRun.objects.filter(organization=organization, run_id=parent).first() if parent else None
    return False


def upsert_written_article(*, organization, slug, defaults, source_run_id=None,
                           analytics_id=None, incoming_admission=None):
    """Serialize both completion paths by organization and preserve writing lineage."""
    from django.db import IntegrityError, transaction
    from organizations.models import Organization
    from workflow_runs.models import ContentFactoryRun
    from .models import WrittenArticle
    from .run_state import ARTICLE_WORKFLOWS
    from .article_publish_status import advance_publish_status

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        run = None
        if source_run_id:
            run = ContentFactoryRun.objects.filter(organization=organization, run_id=source_run_id).first()
            if not run or run.workflow not in ARTICLE_WORKFLOWS:
                raise ArticleEditorialConflict("Source writing run is unavailable; retry after run synchronization")
            request = run.run_request if isinstance(run.run_request, dict) else {}
            # Publishing children must retain the source writing decision.
            result = getattr(run, "result", None)
            result = result if isinstance(result, dict) else {}
            mode = request.get("delivery_mode") or request.get("resolved_delivery_mode") or result.get("resolved_delivery_mode") or result.get("delivery_mode")
            if run.workflow != "article_revision" and parent_run_id(run) and mode in {"publish_code", "publish_webflow"}:
                parent = ContentFactoryRun.objects.filter(organization=organization, run_id=parent_run_id(run)).first()
                if parent is None:
                    raise ArticleEditorialConflict("Source run is unavailable")
                # Discovery ancestry is research input to a new writing run.
                # Only another writing run can supply a publish child's identity.
                if parent.workflow in ARTICLE_WORKFLOWS:
                    parent_request = parent.run_request if isinstance(parent.run_request, dict) else {}
                    child_brief = request.get("editorial_brief") or request.get("editorialBrief")
                    parent_brief = parent_request.get("editorial_brief") or parent_request.get("editorialBrief")
                    if child_brief and child_brief != parent_brief:
                        raise ArticleEditorialConflict("A publish child cannot change its parent's writing decision")
                    run = parent
            saved_request = run.run_request if isinstance(run.run_request, dict) else {}
            expected_id = saved_request.get("analytics_article_id") or saved_request.get("analyticsArticleId")
            if expected_id and analytics_id and str(expected_id) != str(analytics_id):
                raise ArticleEditorialConflict("Stable article identity differs from writing run")
            analytics_id = expected_id or analytics_id
        elif incoming_admission is not None:
            raise ArticleEditorialConflict("Editorial evidence requires its writing run")
        snapshot = snapshot_from_run(run, organization, incoming_admission) if run else None
        article = WrittenArticle.objects.select_for_update().filter(organization=organization, analytics_id=analytics_id).first() if analytics_id else None
        by_slug = WrittenArticle.objects.select_for_update().filter(organization=organization, slug=slug).first()
        if article and by_slug and article.pk != by_slug.pk:
            raise ArticleEditorialConflict("Slug belongs to another article")
        article = article or by_slug
        if article is None:
            from .models import OrganizationContentConfig
            config = OrganizationContentConfig.objects.filter(organization=organization).first()
            strategy = config.pillar_strategy if config and isinstance(config.pillar_strategy, dict) else {}
            if "editorial_catalog" in strategy and (not snapshot or snapshot["provenance_status"] != "recorded"):
                raise ArticleEditorialConflict("A catalogue-backed article requires the saved writing admission; retry after run synchronization")
        preserve_writing = bool(article and article.editorial_snapshot and not snapshot)
        if article:
            if analytics_id and str(article.analytics_id) != str(analytics_id):
                raise ArticleEditorialConflict("An existing article cannot change its stable identity")
            previous = article.source_run_id
            if run and previous and previous != run.run_id:
                prior_run = ContentFactoryRun.objects.filter(organization=organization, run_id=previous).first()
                if prior_run and _is_descendant(prior_run, run.run_id, organization):
                    return article, False  # Late callback from an older writing revision.
                if not _is_descendant(run, previous, organization):
                    raise ArticleEditorialConflict("An unrelated writing run cannot replace this article")
            if article.editorial_snapshot and not snapshot:
                # Sparse legacy/publishing observers cannot rewrite known content.
                allowed = {"pr_url", "pr_number", "article_url", "content_path", "publish_status"}
                defaults = {k: v for k, v in defaults.items() if k in allowed}
            if snapshot and article.editorial_snapshot and previous == run.run_id and snapshot != article.editorial_snapshot:
                old = article.editorial_snapshot
                # A late persisted admission may complete an earlier brief-only observation.
                completion = old.get("provenance_status") == "partial" and snapshot["provenance_status"] == "recorded" and old.get("brief") == snapshot["brief"]
                if not completion:
                    raise ArticleEditorialConflict("The same writing run has conflicting attribution")
        created = article is None
        if created:
            article = WrittenArticle(organization=organization, slug=slug)
            if analytics_id:
                article.analytics_id = analytics_id
        elif slug != article.slug:
            if not run or not article.source_run_id or not _is_descendant(run, article.source_run_id, organization):
                raise ArticleEditorialConflict("A renamed article requires explicit revision lineage")
            article.slug = slug
        desired_status = defaults.get("publish_status")
        for key, value in defaults.items():
            if key == "published_at" and article.published_at:
                continue
            if key not in {"publish_status", "source_run_id", "analytics_id"} and (created or value not in (None, "")):
                setattr(article, key, value)
        if desired_status:
            advance_publish_status(article, desired_status, pr_number=defaults.get("pr_number"))
        if run and not preserve_writing:
            article.source_run_id = run.run_id
        for key, value in snapshot_fields(snapshot).items():
            setattr(article, key, value)
        if snapshot and not article.original_editorial_snapshot:
            article.original_editorial_snapshot = deepcopy(snapshot)
        try:
            # A stable ID is globally unique. Different tenants hold different
            # organization locks, so a concurrent collision must also be caught
            # at the database constraint, without exposing the other article.
            with transaction.atomic():
                article.save()
        except IntegrityError:
            if created and WrittenArticle.objects.filter(analytics_id=article.analytics_id).exclude(pk=article.pk).exists():
                raise ArticleEditorialConflict("Stable article identity is already in use") from None
            raise
        return article, created


def distinct_reader_task(brief, previous_snapshots):
    """A profile change alone never clears keyword coverage.

    This admits an explicit new brief, not the finished content. The worker's
    portfolio comparison must still reject semantic duplicates before delivery.
    Missing historical task evidence cannot prove distinctness.
    """
    import re
    def key(value):
        return " ".join(re.findall(r"\w+", str(value or "").casefold()))
    if not brief or not previous_snapshots:
        return False
    task, contribution = key(brief.get("reader_task")), key(brief.get("distinct_contribution"))
    if not task or not contribution:
        return False
    for snapshot in previous_snapshots:
        previous = snapshot.get("brief") if isinstance(snapshot, dict) else None
        if not previous or not key(previous.get("reader_task")) or not key(previous.get("distinct_contribution")):
            return False
        if task == key(previous["reader_task"]) or contribution == key(previous["distinct_contribution"]):
            return False
    return True


def custom_article_has_distinct_task(organization, article, brief, title):
    if not article or str(article.title).strip().casefold() == str(title).strip().casefold():
        return False
    from .models import WrittenArticle
    history = WrittenArticle.objects.filter(organization=organization, primary_keyword__iexact=article.primary_keyword)
    snapshots = [row.editorial_snapshot for row in history]
    return distinct_reader_task(brief, snapshots)
