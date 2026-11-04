"""Inspect or pause legacy blogs during retirement; new hosting is unavailable."""
import json

from django.core.management.base import BaseCommand, CommandError

from content_factory.hosted_article_models import HostedArticleSite
from content_factory.hosted_articles import normalized_domain, site_render_state

class Command(BaseCommand):
    help = "Manage MLAI-hosted article sites."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["show", "pause"])
        parser.add_argument("domain")

    def handle(self, *args, action, domain, profile=None, **options):
        domain = normalized_domain(domain)
        site = HostedArticleSite.objects.filter(domain=domain).first()
        if site is None:
            raise CommandError(f"No hosted site for {domain}.")
        if action == "pause" and site.state != HostedArticleSite.State.RETIRED:
            site.state = HostedArticleSite.State.PAUSED
            site.save(update_fields=["state", "updated_at"])
        state = site_render_state(site)
        state["profile"] = sorted(state["profile"])
        state["publications"] = [{key: row[key] for key in ("runId", "slug", "status")} for row in state["publications"]]
        self.stdout.write(json.dumps({**state, "proxyVerifiedAt": site.proxy_verified_at.isoformat() if site.proxy_verified_at else None}, indent=2))
