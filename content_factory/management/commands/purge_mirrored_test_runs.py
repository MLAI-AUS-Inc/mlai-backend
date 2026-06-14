from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


class Command(BaseCommand):
    help = (
        "Delete ContentFactoryRun rows that show as draft articles on the Vibe "
        "Marketing dashboard but cannot be removed from the UI (the cancel guard "
        "trips on any recorded PR url, even a closed/merged one). Dry-run by "
        "default; pass --apply to delete. Deleting bypasses the publish-evidence "
        "guard, so the PR url is printed for every row — close any still-open PR "
        "on GitHub yourself, deleting the run row does not touch it.\n\n"
        "Selection (first non-empty wins):\n"
        "  --run-id        exact run id(s)\n"
        "  --keyword       all runs whose dashboard keyword matches (targets a "
        "stuck card by its subtitle; clears the whole topic group)\n"
        "  --run-id-prefix default 'run-', the content-factory test-fixture prefix "
        "(production ids are uuid4 / 'publish-<uuid>' / 'vibe-*' and never start "
        "with 'run-', so the default cannot match real runs)."
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
            help="Exact run_id to purge (repeatable). Highest-priority selector.",
        )
        parser.add_argument(
            "--keyword",
            action="append",
            default=[],
            dest="keywords",
            help=(
                "Select every non-cancelled run whose dashboard keyword matches "
                "(repeatable). Use the card's keyword subtitle, e.g. "
                "--keyword 'builders club'. Clears the whole topic group."
            ),
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
        # human-readable keyword / PR-evidence columns and keyword matching.
        from content_factory.vibe_marketing_views import (
            _article_draft_title_keyword,
            _normalize_keyword_memory,
            _publish_evidence_from_run,
            _run_has_external_publish_evidence,
        )

        run_ids = [str(r).strip() for r in options["run_ids"] if str(r).strip()]
        keywords = [str(k).strip() for k in options["keywords"] if str(k).strip()]
        prefix = str(options["run_id_prefix"] or "")
        domain = str(options["domain"] or "").strip().lower()
        older_than_days = options["older_than_days"]
        limit = options["limit"]
        apply = bool(options["apply"])

        keyword_filter = None
        base = ContentFactoryRun.objects.all()
        if run_ids:
            base = base.filter(run_id__in=run_ids)
            selection = f"{len(run_ids)} explicit run id(s)"
        elif keywords:
            base = base.exclude(status=ContentFactoryRunStatus.CANCELLED)
            keyword_filter = {_normalize_keyword_memory(k) for k in keywords}
            keyword_filter.discard("")
            selection = f"keyword(s) {sorted(keyword_filter)}"
        else:
            if not prefix:
                raise CommandError(
                    "Refusing to select every run: pass --run-id, --keyword, or a "
                    "non-empty --run-id-prefix."
                )
            base = base.filter(run_id__startswith=prefix)
            selection = f"run_id prefix {prefix!r}"

        if domain:
            base = base.filter(domain__iexact=domain)
        if older_than_days is not None:
            cutoff = timezone.now() - timedelta(days=older_than_days)
            base = base.filter(updated_at__lt=cutoff)

        runs = []
        for run in base.order_by("-updated_at"):
            if keyword_filter is not None:
                _, kw = _article_draft_title_keyword(run)
                if _normalize_keyword_memory(kw) not in keyword_filter:
                    continue
            runs.append(run)
            if limit and len(runs) >= limit:
                break

        self.stdout.write(f"Selection: {selection}" + (f", domain={domain!r}" if domain else ""))
        if not runs:
            self.stdout.write("No matching runs found. Nothing to do.")
            return

        self.stdout.write(f"Matched {len(runs)} run(s):")
        self.stdout.write("")
        protected = 0
        for run in runs:
            _, keyword = _article_draft_title_keyword(run)
            has_pr = _run_has_external_publish_evidence(run)
            result = run.result or {}
            evidence = _publish_evidence_from_run(run)
            pr_url = (
                evidence.get("prUrl")
                or result.get("pr_url")
                or result.get("pull_request_url")
                or result.get("draft_pr_url")
                or ""
            )
            if has_pr:
                protected += 1
            updated = run.updated_at.isoformat() if run.updated_at else "?"
            self.stdout.write(
                f"  {run.run_id}  [{run.workflow}/{run.status}]  "
                f"domain={run.domain!r}  keyword={keyword!r}  updated={updated}"
                f"{('  pr=' + pr_url) if pr_url else ''}"
            )

        self.stdout.write("")
        self.stdout.write(
            f"{len(runs)} run(s) selected; {protected} carry external publish "
            "evidence (the ones the dashboard refuses to cancel)."
        )
        if protected:
            self.stdout.write(
                self.style.WARNING(
                    "Deleting a run row does NOT close its PR. Confirm each PR above is "
                    "already closed/merged on GitHub, or close it manually first."
                )
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
