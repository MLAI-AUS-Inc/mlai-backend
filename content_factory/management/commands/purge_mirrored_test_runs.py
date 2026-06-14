from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun


class Command(BaseCommand):
    help = (
        "Purge content-factory TEST runs that leaked into the backend DB via the "
        "MlaiRunMirror. These appear as draft articles on the Vibe Marketing "
        "dashboard and some carry simulated PR evidence, so the UI refuses to "
        "cancel them. Dry-run by default; pass --apply to delete.\n\n"
        "Selection defaults to run_ids starting with 'run-' (the content-factory "
        "test-fixture prefix). Production run ids are uuid4 / 'publish-<uuid>' / "
        "'vibe-*' and never start with 'run-', so this cannot match real runs."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--run-id-prefix",
            default="run-",
            help="Select runs whose run_id starts with this prefix (default 'run-').",
        )
        parser.add_argument(
            "--run-id",
            action="append",
            default=[],
            dest="run_ids",
            help="Exact run_id to purge (repeatable). Overrides --run-id-prefix.",
        )
        parser.add_argument("--domain", default="", help="Restrict to this domain (optional).")
        parser.add_argument(
            "--older-than-days",
            type=int,
            default=None,
            help="Only purge runs whose updated_at is older than N days (optional).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Cap the number of runs affected (optional).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually delete. Without it the command only lists candidates (dry-run).",
        )

    def handle(self, *args, **options):
        # Imported lazily: the views module is heavy and only needed for the
        # human-readable keyword / PR-evidence columns in the listing.
        from content_factory.vibe_marketing_views import (
            _article_draft_title_keyword,
            _run_has_external_publish_evidence,
        )

        run_ids = [str(r).strip() for r in options["run_ids"] if str(r).strip()]
        prefix = str(options["run_id_prefix"] or "")
        domain = str(options["domain"] or "").strip().lower()
        older_than_days = options["older_than_days"]
        limit = options["limit"]
        apply = bool(options["apply"])

        if run_ids:
            qs = ContentFactoryRun.objects.filter(run_id__in=run_ids)
        else:
            if not prefix:
                raise CommandError(
                    "Refusing to select every run: pass a non-empty --run-id-prefix "
                    "or one or more --run-id values."
                )
            qs = ContentFactoryRun.objects.filter(run_id__startswith=prefix)

        if domain:
            qs = qs.filter(domain__iexact=domain)
        if older_than_days is not None:
            cutoff = timezone.now() - timedelta(days=older_than_days)
            qs = qs.filter(updated_at__lt=cutoff)

        qs = qs.order_by("-updated_at")
        runs = list(qs[:limit] if limit else qs)

        if not runs:
            self.stdout.write("No matching runs found. Nothing to do.")
            return

        self.stdout.write(f"Matched {len(runs)} run(s):")
        self.stdout.write("")
        protected = 0
        for run in runs:
            _, keyword = _article_draft_title_keyword(run)
            has_pr = _run_has_external_publish_evidence(run)
            if has_pr:
                protected += 1
            updated = run.updated_at.isoformat() if run.updated_at else "?"
            self.stdout.write(
                f"  {run.run_id}  [{run.workflow}/{run.status}]  "
                f"domain={run.domain!r}  keyword={keyword!r}  updated={updated}"
                f"{'  PR_EVIDENCE' if has_pr else ''}"
            )

        self.stdout.write("")
        self.stdout.write(
            f"{len(runs)} run(s) selected; {protected} carry external publish "
            "evidence (the ones the dashboard refuses to cancel)."
        )

        if not apply:
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN — nothing deleted. Re-run with --apply to delete these "
                    "runs (steps cascade automatically)."
                )
            )
            return

        with transaction.atomic():
            deleted, details = ContentFactoryRun.objects.filter(
                pk__in=[run.pk for run in runs]
            ).delete()
        self.stdout.write(self.style.SUCCESS(f"Deleted {deleted} row(s): {details}"))
