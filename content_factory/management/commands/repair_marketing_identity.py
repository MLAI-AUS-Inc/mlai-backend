from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content_factory.models import (
    ContentFactoryHealingRecord,
    OrganizationContentConfig,
    ResearchedKeyword,
    WebsiteBaselineSnapshot,
    WrittenArticle,
)
from content_factory.vibe_marketing_views import ARTICLE_WORKFLOWS, _persist_article_memory_from_run
from founder_tools.models import VibeRaisingCompany
from organizations.models import Organization
from startup_updates.models import StartupProfile, UserStartupBinding
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus


class Command(BaseCommand):
    help = "Move Vibe Marketing memory rows into a canonical organization. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True)
        parser.add_argument("--canonical-org-id", type=int, required=True)
        parser.add_argument("--source-org-id", type=int, action="append", default=[])
        parser.add_argument("--commit", action="store_true")

    def handle(self, *args, **options):
        domain = str(options["domain"] or "").strip().lower()
        canonical = Organization.objects.filter(pk=options["canonical_org_id"]).first()
        if canonical is None:
            raise CommandError("Canonical organization not found.")

        source_org_ids = set(options["source_org_id"] or [])
        source_org_ids.update(
            Organization.objects.filter(domain__iexact=domain).exclude(pk=canonical.pk).values_list("id", flat=True)
        )
        source_org_ids.update(
            VibeRaisingCompany.objects.filter(domain__iexact=domain)
            .exclude(organization_id__isnull=True)
            .exclude(organization_id=canonical.pk)
            .values_list("organization_id", flat=True)
        )
        source_org_ids.discard(None)
        source_org_ids.discard(canonical.pk)

        commit = bool(options["commit"])
        self.stdout.write(
            f"{'Committing' if commit else 'Dry-run'} repair for {domain}: canonical={canonical.pk}, sources={sorted(source_org_ids)}"
        )
        if not source_org_ids:
            with transaction.atomic():
                self._backfill_written_articles(domain, canonical, commit=commit)
                if not commit:
                    transaction.set_rollback(True)
            return

        with transaction.atomic():
            self._merge_written_articles(source_org_ids, canonical, commit=commit)
            self._merge_keywords(source_org_ids, canonical, commit=commit)
            self._bulk_move("WebsiteBaselineSnapshot", WebsiteBaselineSnapshot.objects.filter(organization_id__in=source_org_ids), canonical, commit)
            self._merge_one_to_one("OrganizationContentConfig", OrganizationContentConfig, source_org_ids, canonical, commit=commit)
            self._merge_one_to_one("StartupProfile", StartupProfile, source_org_ids, canonical, commit=commit)
            self._merge_user_bindings(source_org_ids, canonical, commit=commit)
            self._bulk_move("VibeRaisingCompany", VibeRaisingCompany.objects.filter(organization_id__in=source_org_ids), canonical, commit)
            self._bulk_move("ContentFactoryHealingRecord", ContentFactoryHealingRecord.objects.filter(organization_id__in=source_org_ids), canonical, commit)
            self._backfill_written_articles(domain, canonical, commit=commit)
            if not commit:
                transaction.set_rollback(True)

    def _bulk_move(self, label, queryset, canonical, commit):
        count = queryset.count()
        self.stdout.write(f"  {label}: {count} row(s) -> org {canonical.pk}")
        if commit and count:
            queryset.update(organization=canonical)

    def _merge_one_to_one(self, label, model, source_org_ids, canonical, *, commit):
        rows = list(model.objects.filter(organization_id__in=source_org_ids))
        self.stdout.write(f"  {label}: {len(rows)} row(s) -> org {canonical.pk}")
        if not commit or not rows:
            return
        canonical_row = model.objects.filter(organization=canonical).first()
        if canonical_row is None:
            canonical_row = rows.pop(0)
            canonical_row.organization = canonical
            canonical_row.save(update_fields=["organization"])
        for row in rows:
            changed = []
            for field in row._meta.fields:
                name = field.name
                if name in {"id", "organization", "created_at", "updated_at"}:
                    continue
                current = getattr(canonical_row, name, None)
                incoming = getattr(row, name, None)
                if current in (None, "", [], {}) and incoming not in (None, "", [], {}):
                    setattr(canonical_row, name, incoming)
                    changed.append(name)
            if changed:
                canonical_row.save(update_fields=changed)
            row.delete()

    def _merge_user_bindings(self, source_org_ids, canonical, *, commit):
        bindings = list(UserStartupBinding.objects.filter(organization_id__in=source_org_ids))
        self.stdout.write(f"  UserStartupBinding: {len(bindings)} row(s) -> org {canonical.pk}")
        for binding in bindings:
            if not commit:
                continue
            existing = UserStartupBinding.objects.filter(user=binding.user, organization=canonical).first()
            if existing:
                if binding.is_default_for_gmail and not existing.is_default_for_gmail:
                    existing.is_default_for_gmail = True
                    existing.save(update_fields=["is_default_for_gmail"])
                binding.delete()
            else:
                binding.organization = canonical
                binding.save(update_fields=["organization"])

    def _merge_written_articles(self, source_org_ids, canonical, *, commit):
        articles = list(WrittenArticle.objects.filter(organization_id__in=source_org_ids))
        self.stdout.write(f"  WrittenArticle: {len(articles)} row(s) -> org {canonical.pk}")
        for article in articles:
            existing = WrittenArticle.objects.filter(organization=canonical, slug=article.slug).first()
            if not commit:
                continue
            if existing:
                changed = []
                for field in ("article_url", "pr_url", "primary_keyword", "title"):
                    if not getattr(existing, field) and getattr(article, field):
                        setattr(existing, field, getattr(article, field))
                        changed.append(field)
                if changed:
                    existing.save(update_fields=changed)
                article.delete()
            else:
                article.organization = canonical
                article.save(update_fields=["organization"])

    def _merge_keywords(self, source_org_ids, canonical, *, commit):
        keywords = list(ResearchedKeyword.objects.filter(organization_id__in=source_org_ids).select_related("written_article"))
        self.stdout.write(f"  ResearchedKeyword: {len(keywords)} row(s) -> org {canonical.pk}")
        for keyword in keywords:
            existing = ResearchedKeyword.objects.filter(
                organization=canonical,
                keyword_normalized=keyword.keyword_normalized,
            ).first()
            if not commit:
                continue
            if existing:
                if keyword.status == "written" and existing.status != "written":
                    existing.status = keyword.status
                    existing.written_article = keyword.written_article
                    existing.status_changed_at = keyword.status_changed_at
                    existing.save(update_fields=["status", "written_article", "status_changed_at"])
                keyword.delete()
            else:
                keyword.organization = canonical
                keyword.save(update_fields=["organization"])

    def _backfill_written_articles(self, domain, canonical, *, commit):
        runs = list(
            ContentFactoryRun.objects.filter(
                domain__iexact=domain,
                workflow__in=ARTICLE_WORKFLOWS,
                status=ContentFactoryRunStatus.COMPLETED,
            )
        )
        self.stdout.write(f"  Completed article runs for written-memory backfill: {len(runs)}")
        if not commit:
            return
        for run in runs:
            _persist_article_memory_from_run(organization=canonical, run=run)
