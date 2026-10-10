from __future__ import annotations

import json

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from content_factory.offer_page_checks import recheck_offer_pages
from organizations.models import Organization


class Command(BaseCommand):
    help = (
        "Check that one organisation's active article offers match their linked pages. "
        "Stores advisory findings for the owner UI; never changes the offers. Makes page and model requests."
    )

    def add_arguments(self, parser):
        parser.add_argument("--domain", required=True)
        parser.add_argument("--offer", action="append", default=[], help="Offer id; repeat to limit the check.")

    def handle(self, *args, **options):
        domain = str(options["domain"] or "").strip().lower()
        if not domain:
            raise CommandError("--domain is required")
        if not getattr(settings, "OPENAI_API_KEY", ""):
            raise CommandError("OPENAI_API_KEY is required to check offer pages")
        organizations = list(Organization.objects.filter(domain__iexact=domain).order_by("id"))
        if len(organizations) != 1:
            raise CommandError(f"Expected one organisation for {domain}; found {len(organizations)}")
        checks = recheck_offer_pages(organizations[0], tuple(options["offer"]), now=timezone.now())
        self.stdout.write(json.dumps(checks, indent=2, ensure_ascii=False))
