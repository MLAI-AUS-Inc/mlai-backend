"""Recover only historical evidence recorded on an exact organization writing run."""
from django.core.management.base import BaseCommand
from workflow_runs.models import ContentFactoryRun
from content_factory.models import WrittenArticle
from content_factory.article_editorial import ArticleEditorialConflict, snapshot_from_run, upsert_written_article


class Command(BaseCommand):
    help = "Preview recoverable article audience history. Use --apply to persist; never infers from current profiles."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", required=True)
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        rows = WrittenArticle.objects.filter(organization_id=options["organization_id"], editorial_snapshot__isnull=True).select_related("organization").order_by("id")
        recovered = skipped = 0
        for article in rows.iterator(chunk_size=100):
            if not article.source_run_id:
                skipped += 1
                continue
            run = ContentFactoryRun.objects.filter(organization=article.organization, run_id=article.source_run_id).first()
            if run is None:
                skipped += 1
                continue
            try:
                snapshot = snapshot_from_run(run, article.organization)
                if snapshot is None:
                    skipped += 1
                    continue
                if options["apply"]:
                    upsert_written_article(organization=article.organization, slug=article.slug, defaults={}, source_run_id=run.run_id, analytics_id=article.analytics_id)
                recovered += 1
                self.stdout.write(f"{article.id}: {snapshot['provenance_status']} from {run.run_id}")
            except ArticleEditorialConflict:
                skipped += 1
                self.stderr.write(f"{article.id}: conflicting historical evidence; unchanged")
        label = "Recovered" if options["apply"] else "Would recover"
        self.stdout.write(f"{label} {recovered}; skipped {skipped}. Profiles with no recorded evidence remain unknown.")
