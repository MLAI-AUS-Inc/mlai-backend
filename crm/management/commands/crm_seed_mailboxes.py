"""Explicit operator enrollment of inbox probes; never sends a message."""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from crm.deliverability.preflight.seeds import sync_mailboxes


class Command(BaseCommand):
    help = 'Sync explicitly configured CRM_SEED_MAILBOXES; credential values are never read or printed.'

    def handle(self, *args, **options):
        try:
            count = sync_mailboxes()
        except (ValueError, TypeError, ValidationError):
            raise CommandError('Invalid CRM_SEED_MAILBOXES configuration; no changes were applied.') from None
        self.stdout.write(f'{count} inbox probe configurations synced. No messages sent.')
