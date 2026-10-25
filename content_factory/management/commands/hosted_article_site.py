"""Register, inspect, switch on or pause an MLAI-hosted article site.

  manage.py hosted_article_site show pawshareclub.com
  manage.py hosted_article_site register pawshareclub.com --profile profile.json
  manage.py hosted_article_site activate pawshareclub.com
  manage.py hosted_article_site pause pawshareclub.com

A site is registered as "draft": Content Factory can deploy it, but owners
cannot publish until it is activated, normally after the customer's proxy
serves the hosted directory.
"""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from content_factory.hosted_article_models import HostedArticleSite
from content_factory.hosted_articles import normalized_domain, site_render_state
from organizations.models import Organization

REQUIRED_PROFILE_KEYS = {"domain", "base_path", "page_size", "card_template", "detail_main_template",
                         "previous_link_template", "next_link_template", "page_label_template"}


class Command(BaseCommand):
    help = "Manage MLAI-hosted article sites."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["show", "register", "activate", "pause"])
        parser.add_argument("domain")
        parser.add_argument("--profile", help="Renderer profile JSON (register only).")

    def handle(self, *args, action, domain, profile=None, **options):
        domain = normalized_domain(domain)
        if action == "register":
            return self._register(domain, profile)
        site = HostedArticleSite.objects.filter(domain=domain).first()
        if site is None:
            raise CommandError(f"No hosted site for {domain}.")
        if action == "activate":
            if not site.worker_url:
                raise CommandError("Deploy the hosted directory before activating the site.")
            site.state = HostedArticleSite.State.ACTIVE
        elif action == "pause":
            site.state = HostedArticleSite.State.PAUSED
        if action in {"activate", "pause"}:
            site.save(update_fields=["state", "updated_at"])
        state = site_render_state(site)
        state["profile"] = sorted(state["profile"])
        state["publications"] = [{key: row[key] for key in ("runId", "slug", "status")} for row in state["publications"]]
        self.stdout.write(json.dumps({**state, "proxyVerifiedAt": site.proxy_verified_at.isoformat() if site.proxy_verified_at else None}, indent=2))

    def _register(self, domain, profile_path):
        if not profile_path:
            raise CommandError("--profile is required to register a site.")
        profile = json.loads(Path(profile_path).read_text())
        missing = REQUIRED_PROFILE_KEYS - set(profile)
        if missing:
            raise CommandError(f"Profile is missing {sorted(missing)}.")
        if normalized_domain(profile["domain"]) != domain:
            raise CommandError("The profile is for a different domain.")
        organization = Organization.objects.filter(domain=domain).first()
        if organization is None:
            raise CommandError(f"No organization owns {domain}.")
        site, created = HostedArticleSite.objects.get_or_create(
            domain=domain, defaults={"organization": organization, "base_path": profile["base_path"], "profile": profile})
        if not created:
            if site.organization_id != organization.pk:
                raise CommandError(f"{domain} is hosted for another organization.")
            site.base_path, site.profile = profile["base_path"], profile
            site.save(update_fields=["base_path", "profile", "updated_at"])
        self.stdout.write(f"{'Registered' if created else 'Updated'} {domain} ({site.state}).")
