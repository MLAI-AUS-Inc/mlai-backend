"""Inspect source inventory; explicitly request a reconciliation pass to write."""
import json

from django.core.management.base import BaseCommand, CommandError

from integrations.services.managed_channels.config import installations
from integrations.services.managed_channels.worker import scan, tick


class Command(BaseCommand):
    help = "Inspect or synchronize an enabled managed channel installation"

    def add_arguments(self, parser):
        parser.add_argument("--installation", required=True)
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--apply", action="store_true")
        mode.add_argument("--slack-capabilities", action="store_true",
                          help="Read-only check of the configured workspace management installation")

    def handle(self, *args, **options):
        name = options["installation"]
        config = installations().get(name)
        if not config:
            raise CommandError("Unknown or disabled installation")
        if options["slack_capabilities"]:
            if config["provider"] != "luma":
                raise CommandError("Slack provisioning is only supported for event sources")
            from integrations.services.managed_channels.slack import capabilities, SlackHold
            try:
                capabilities(config)
            except SlackHold as exc:
                raise CommandError(str(exc)) from exc
            result = {"workspace_id": config["slack_workspace_id"], "public_channel_management": True}
        else:
            result = tick(only=name, force=True) if options["apply"] else scan(config, dry_run=True)
        self.stdout.write(json.dumps(result))
