from django.core.management.base import BaseCommand, CommandError

from integrations.services.community_bridge.worker import run_bridge_worker


class Command(BaseCommand):
    help = "Run the Slack-Discord community bridge worker."

    def handle(self, *args, **options):
        try:
            run_bridge_worker()
        except Exception as exc:
            raise CommandError(str(exc)) from exc
