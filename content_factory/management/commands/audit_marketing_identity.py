from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from content_factory.models import (
    OrganizationContentConfig,
    ResearchedKeyword,
    WebsiteBaselineSnapshot,
    WrittenArticle,
)
from founder_tools.models import VibeRaisingCompany
from integrations.models import UserIntegration
from organizations.models import Organization
from startup_updates.models import StartupProfile, UserStartupBinding
from workflow_runs.models import ContentFactoryRun


class Command(BaseCommand):
    help = "Audit Vibe Marketing identity rows for a domain without changing data."

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True)

    def handle(self, *args, **options):
        domain = str(options["domain"] or "").strip().lower()
        if not domain:
            raise CommandError("--domain is required")

        orgs = list(Organization.objects.filter(domain__iexact=domain).order_by("id"))
        companies = list(
            VibeRaisingCompany.objects.select_related("profile__user", "organization")
            .filter(domain__iexact=domain)
            .order_by("id")
        )
        org_ids = [org.id for org in orgs]
        config_qs = OrganizationContentConfig.objects.select_related("organization").filter(organization_id__in=org_ids)
        profile_qs = StartupProfile.objects.select_related("organization").filter(organization_id__in=org_ids)
        binding_qs = UserStartupBinding.objects.select_related("user", "organization").filter(organization_id__in=org_ids)
        runs_qs = ContentFactoryRun.objects.filter(domain__iexact=domain).order_by("-updated_at")
        keyword_qs = ResearchedKeyword.objects.filter(organization_id__in=org_ids).order_by("organization_id", "keyword")
        article_qs = WrittenArticle.objects.filter(organization_id__in=org_ids).order_by("-created_at")
        baseline_qs = WebsiteBaselineSnapshot.objects.filter(organization_id__in=org_ids).order_by("-collected_at")

        self.stdout.write(f"Domain: {domain}")
        self.stdout.write(f"Organizations: {len(orgs)}")
        for org in orgs:
            self.stdout.write(f"  org={org.id} name={org.name!r} domain={org.domain!r}")

        self.stdout.write(f"Founder companies: {len(companies)}")
        for company in companies:
            user = getattr(company.profile, "user", None)
            self.stdout.write(
                "  "
                f"company={company.id} name={company.name!r} org={company.organization_id} "
                f"user={getattr(user, 'email', '')!r} slack={getattr(user, 'slack_id', '')!r}"
            )

        self.stdout.write(f"OrganizationContentConfig: {config_qs.count()}")
        for config in config_qs:
            self.stdout.write(
                "  "
                f"config={config.id} org={config.organization_id} actor={config.connected_slack_user_id!r} "
                f"brand={config.brand_name!r} repo={config.github_repo!r} scanned={bool(config.last_scanned_at)}"
            )

        self.stdout.write(f"StartupProfile: {profile_qs.count()}")
        for profile in profile_qs:
            self.stdout.write(f"  profile={profile.id} org={profile.organization_id} stage={profile.stage!r}")

        self.stdout.write(f"UserStartupBinding: {binding_qs.count()}")
        for binding in binding_qs:
            self.stdout.write(
                "  "
                f"binding={binding.id} org={binding.organization_id} user={binding.user.email!r} "
                f"slack={binding.user.slack_id!r} default={binding.is_default_for_gmail}"
            )

        self.stdout.write(
            "UserIntegration rows for domain users: "
            f"{UserIntegration.objects.filter(user__startup_bindings__organization_id__in=org_ids).distinct().count()}"
        )
        self.stdout.write(f"ContentFactoryRun rows: {runs_qs.count()}")
        for run in runs_qs[:12]:
            self.stdout.write(f"  run={run.run_id} workflow={run.workflow} status={run.status} actor={run.slack_user_id!r}")

        self.stdout.write(f"ResearchedKeyword rows: {keyword_qs.count()}")
        for keyword in keyword_qs[:30]:
            self.stdout.write(
                f"  keyword={keyword.keyword!r} status={keyword.status} org={keyword.organization_id} "
                f"written_article={keyword.written_article_id or ''}"
            )

        self.stdout.write(f"WrittenArticle rows: {article_qs.count()}")
        for article in article_qs[:30]:
            self.stdout.write(
                f"  article={article.title!r} keyword={article.primary_keyword!r} slug={article.slug!r} org={article.organization_id}"
            )

        self.stdout.write(f"WebsiteBaselineSnapshot rows: {baseline_qs.count()}")
        if len(orgs) > 1 or len({company.organization_id for company in companies if company.organization_id}) > 1:
            self.stdout.write(self.style.WARNING("Likely duplicate organization context detected for this domain."))
