"""Advance one scheduled pass of the managed channel worker."""
import json

from django.core.management.base import BaseCommand

from integrations.services.managed_channels.worker import tick


class Command(BaseCommand):
    help = "Advance managed channel inbox, inventories and pending deliveries."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(tick()))
