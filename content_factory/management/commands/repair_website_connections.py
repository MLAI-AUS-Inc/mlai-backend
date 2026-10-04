"""Inspect legacy website state; apply only scoped, explicitly reviewed repairs."""
import hashlib
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from content_factory.models import OrganizationContentConfig
from organizations.models import Organization
from content_factory.website_contract import template_validation
from content_factory.website_models import WebsiteConnection, WebsiteTemplateRevision


class Command(BaseCommand):
    help = 'Dry-run legacy website/template inventory. --apply archives invalid seeds and creates disconnected records; never grants consent or edits GitHub.'

    def add_arguments(self, parser):
        parser.add_argument('--domain', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        config = OrganizationContentConfig.objects.select_related('organization').filter(organization__domain=options['domain']).first()
        if not config:
            raise CommandError('No company configuration matches this domain.')
        def inspect_locked_state(config):
            findings = []
            for purpose in ('article_template', 'design_guide', 'resource_prompt'):
                body = getattr(config, purpose) or ''
                verdict = template_validation(body)
                findings.append({'artifact': purpose, 'present': bool(body), 'digest': hashlib.sha256(body.encode()).hexdigest(),
                    'valid': verdict['valid'], 'code': verdict['code']})
            return {'domain': config.organization.domain, 'github_repo': config.github_repo,
                'connection_present': bool(config.website_connection_id), 'templates': findings,
                'mode': 'apply' if options['apply'] else 'dry_run', 'repository_modified': False}
        report = inspect_locked_state(config)
        if options['apply']:
            with transaction.atomic():
                # No verification means no access. Owner must explicitly reconnect.
                Organization.objects.select_for_update().get(pk=config.organization_id)
                config = OrganizationContentConfig.objects.select_for_update().select_related('organization').get(pk=config.pk)
                report = inspect_locked_state(config)
                if not config.website_connection_id:
                    config.website_connection = WebsiteConnection.objects.create(organization=config.organization,
                        github_repo=config.github_repo or '', installation_id=config.github_installation_id or '',
                        site_url=config.organization.domain, state='disconnected',
                        blockers=[{'code': 'legacy_reconnect_required', 'message': 'Reconnect and scan to verify this website.'}])
                    config.save(update_fields=['website_connection', 'updated_at'])
                for row in report["templates"]:
                    if row['present'] and not row['valid']:
                        body = getattr(config, row['artifact'])
                        WebsiteTemplateRevision.objects.get_or_create(connection=config.website_connection,
                            purpose=row['artifact'], digest=row['digest'], defaults={'generation': config.website_connection.generation,
                            'body': body, 'provenance': 'legacy_saved', 'status': 'quarantined', 'validation': template_validation(body)})
                        setattr(config, row['artifact'], '')
                        config.save(update_fields=[row['artifact'], 'updated_at'])
                report['repair'] = 'Invalid legacy templates archived. No source extraction guessed. Reconnect/scan required.'
        self.stdout.write(json.dumps(report, indent=2))
