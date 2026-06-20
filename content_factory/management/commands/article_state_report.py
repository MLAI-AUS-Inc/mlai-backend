from __future__ import annotations

from django.core.management.base import BaseCommand

from content_factory.article_publish_status import article_bucket
from content_factory.models import ArticlePublishStatus, OrganizationContentConfig, WrittenArticle
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


def article_divergence_flags(article):
    """Ways an article's tracked state disagrees with the source of truth (main)."""
    flags = []
    if article.publish_status == ArticlePublishStatus.LIVE and not article.on_main_verified_at:
        # Marked live off the sitemap but never confirmed on origin/main.
        flags.append("LIVE_UNVERIFIED_ON_MAIN")
    if article.publish_status == ArticlePublishStatus.MERGED and not article.on_main_verified_at:
        flags.append("MERGED_NOT_ON_MAIN")
    if article.pr_url and not article.pr_number:
        # No PR number means the reconciler can never poll this PR's state.
        flags.append("PR_NUMBER_MISSING")
    if article.publish_status == ArticlePublishStatus.WRITTEN and article.pr_url:
        flags.append("WRITTEN_WITH_PR")
    return flags


class Command(BaseCommand):
    help = (
        "Report the authoritative publish state of every written article "
        "(bucket, PR, on-main, live) and flag divergences from origin/main. "
        "Read-only unless --reconcile is passed."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", default="", help="Limit to one organization domain.")
        parser.add_argument("--limit", type=int, default=200, help="Max articles per org to list.")
        parser.add_argument(
            "--reconcile",
            action="store_true",
            help="Refresh PR/on-main/live state against GitHub and the live site first (network).",
        )
        parser.add_argument(
            "--ghost-drafts",
            action="store_true",
            help="Also scan content-factory runs for drafts that duplicate a written article.",
        )

    def handle(self, *args, **options):
        domain = str(options["domain"] or "").strip().lower()
        limit = max(1, int(options["limit"]))
        do_reconcile = bool(options["reconcile"])
        do_ghost = bool(options["ghost_drafts"])

        if domain:
            orgs = list(Organization.objects.filter(domain__iexact=domain).order_by("domain"))
        else:
            org_ids = WrittenArticle.objects.values_list("organization_id", flat=True).distinct()
            orgs = list(Organization.objects.filter(id__in=list(org_ids)).order_by("domain"))

        if not orgs:
            self.stdout.write("No organizations with written articles found.")
            return

        totals = {"published": 0, "publishing": 0}
        flag_totals = {}
        ghost_total = 0

        for org in orgs:
            if do_reconcile:
                self._reconcile(org, limit)

            articles = list(
                WrittenArticle.objects.filter(organization=org).order_by("-created_at")[:limit]
            )
            self.stdout.write("")
            self.stdout.write(f"=== {org.domain or org.name} (org={org.id}) — {len(articles)} article(s) ===")

            org_buckets = {"published": 0, "publishing": 0}
            for article in articles:
                bucket = article_bucket(article)
                org_buckets[bucket] = org_buckets.get(bucket, 0) + 1
                totals[bucket] = totals.get(bucket, 0) + 1
                flags = article_divergence_flags(article)
                for flag in flags:
                    flag_totals[flag] = flag_totals.get(flag, 0) + 1
                pr = f"#{article.pr_number}" if article.pr_number else (article.pr_url or "-")
                on_main = "yes" if article.on_main_verified_at else "no"
                suffix = f"  !! {' '.join(flags)}" if flags else ""
                self.stdout.write(
                    f"  [{bucket:<10}] {article.publish_status:<9} on_main={on_main:<3} "
                    f"pr={pr:<8} {article.slug}{suffix}"
                )

            self.stdout.write(
                f"  -- buckets: published={org_buckets['published']} publishing={org_buckets['publishing']}"
            )

            if do_ghost:
                ghosts = self._ghost_drafts(org)
                ghost_total += len(ghosts)
                if ghosts:
                    self.stdout.write(f"  -- ghost drafts (would duplicate a written article): {len(ghosts)}")
                    for run in ghosts[:20]:
                        self.stdout.write(f"       run={run.run_id} status={run.status} workflow={run.workflow}")

        self.stdout.write("")
        self.stdout.write("=== TOTALS ===")
        self.stdout.write(f"  published (on main / live): {totals['published']}")
        self.stdout.write(f"  publishing (not yet on main): {totals['publishing']}")
        if do_ghost:
            self.stdout.write(f"  ghost drafts: {ghost_total}")
        if flag_totals:
            self.stdout.write("  divergences:")
            for flag, count in sorted(flag_totals.items()):
                self.stdout.write(f"    {flag}: {count}")
        else:
            self.stdout.write("  divergences: none")

    def _reconcile(self, org, limit):
        from content_factory.article_publish_status import refresh_publish_statuses

        config = OrganizationContentConfig.objects.filter(organization=org).first()
        try:
            refreshed = refresh_publish_statuses(org, config, force=True, limit=max(limit, 100))
            self.stdout.write(f"  (reconciled {len(refreshed)} article(s) for {org.domain})")
        except Exception as exc:  # best-effort: network calls must never abort the report
            self.stdout.write(f"  (reconcile failed for {org.domain}: {exc})")

    def _ghost_drafts(self, org):
        # Imported lazily: vibe_marketing_views is a heavy module and only needed
        # for the optional --ghost-drafts scan.
        from content_factory.vibe_marketing_views import (
            ARTICLE_WORKFLOWS,
            RUNNING_RUN_STATUSES,
            _article_draft_matches_written,
            _written_article_identity_keys,
        )

        written_keys = _written_article_identity_keys(org)
        runs = list(
            ContentFactoryRun.objects.filter(domain=org.domain, workflow__in=ARTICLE_WORKFLOWS)
            .exclude(status=ContentFactoryRunStatus.CANCELLED)
            .order_by("-updated_at")[:200]
        )
        return [
            run
            for run in runs
            if run.status not in RUNNING_RUN_STATUSES and _article_draft_matches_written(run, written_keys)
        ]
