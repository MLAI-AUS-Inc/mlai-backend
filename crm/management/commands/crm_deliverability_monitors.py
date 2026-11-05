"""Run one due, bounded monitor pass; disabled by default."""
from django.core.management.base import BaseCommand
from crm.deliverability.monitors.runner import run_deliverability_monitors


class Command(BaseCommand):
    help = "Run one due sending health check (requires CRM_MONITORS_ENABLED)."

    def handle(self, *args, **options):
        self.stdout.write(str(run_deliverability_monitors()))
