"""Backfill WrittenArticle.source_run_id from each domain's completed article runs.

Articles created before 0016 have no run linkage. Match each article to the
newest completed article run whose packaged slug equals the article's slug
(mirrors the slug precedence in vibe_marketing_views._content_package_from_run,
inlined here so the migration stays self-contained). Articles with no match
keep "" — the dashboard simply hides the Edit link for them.
"""
from django.db import migrations

# Copy of vibe_marketing_views.ARTICLE_WORKFLOWS at the time of writing.
ARTICLE_WORKFLOWS = {
    "article_generation",
    "content_factory_article",
    "direct_generate",
    "confirmed_topic",
    "article_revision",
}

RUNS_SCANNED_PER_DOMAIN = 200


def _mapping(value):
    return value if isinstance(value, dict) else {}


def extract_run_slug(run_result, acceptance_summary=None):
    """Best-effort copy of _content_package_from_run's slug precedence."""
    result = _mapping(run_result)
    delivery_package = (
        _mapping(result.get("delivery_package"))
        or _mapping(result.get("deliveryPackage"))
        or _mapping(result.get("content_package"))
        or _mapping(result.get("contentPackage"))
    )
    article_meta = (
        _mapping(result.get("article_meta"))
        or _mapping(result.get("articleMeta"))
        or _mapping(delivery_package.get("article_meta"))
    )
    evidence_summary = _mapping(_mapping(acceptance_summary).get("evidence_summary"))
    slug = (
        delivery_package.get("slug")
        or article_meta.get("slug")
        or evidence_summary.get("content_package_slug")
        or result.get("slug")
        or ""
    )
    return str(slug).strip()


def backfill_source_run_ids(apps, schema_editor):
    WrittenArticle = apps.get_model("content_factory", "WrittenArticle")
    ContentFactoryRun = apps.get_model("workflow_runs", "ContentFactoryRun")

    slug_maps_by_domain = {}

    def slug_map_for_domain(domain):
        if domain in slug_maps_by_domain:
            return slug_maps_by_domain[domain]
        slug_map = {}
        runs = (
            ContentFactoryRun.objects.filter(
                domain=domain,
                workflow__in=ARTICLE_WORKFLOWS,
                status="completed",
            )
            .order_by("-created_at")
            .only("run_id", "result", "acceptance_summary", "created_at")[:RUNS_SCANNED_PER_DOMAIN]
        )
        for run in runs:
            try:
                slug = extract_run_slug(run.result, run.acceptance_summary)
            except Exception:
                continue
            # Newest-first iteration: keep the first (newest) run per slug.
            if slug and slug not in slug_map:
                slug_map[slug] = run.run_id
        slug_maps_by_domain[domain] = slug_map
        return slug_map

    articles = (
        WrittenArticle.objects.filter(source_run_id="")
        .select_related("organization")
        .iterator()
    )
    for article in articles:
        domain = getattr(article.organization, "domain", "") or ""
        if not domain:
            continue
        run_id = slug_map_for_domain(domain).get(str(article.slug or "").strip())
        if run_id:
            article.source_run_id = run_id
            article.save(update_fields=["source_run_id"])


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0016_writtenarticle_source_run_id'),
        ('workflow_runs', '0003_contentfactoryrun_last_event_emitted_at'),
    ]

    operations = [
        migrations.RunPython(backfill_source_run_ids, migrations.RunPython.noop),
    ]
