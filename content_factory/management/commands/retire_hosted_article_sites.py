"""Migrate legacy blog sites without taking down their Worker before verification."""
import json
from django.core.management.base import BaseCommand, CommandError
from content_factory.hosted_article_models import HostedArticleSite
from content_factory.hosted_articles import normalized_domain
from content_factory.hosted_retirement import retire_site


class Command(BaseCommand):
    help = 'Freeze and retire legacy blogs; rerun after the founder merges their PRs.'

    def add_arguments(self, parser):
        parser.add_argument('--domain', required=True)
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, domain, dry_run=False, **options):
        site = HostedArticleSite.objects.select_related('organization').filter(domain=normalized_domain(domain)).first()
        if site is None:
            raise CommandError('No hosted site exists for this domain.')
        self.stdout.write(json.dumps(retire_site(site, dry_run=dry_run), indent=2, default=str))
