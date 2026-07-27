import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from org_memory.drive_watch import DriveWatchError, register_drive_watch
from org_memory.models import MemoryConnectionConfiguration, MemoryProvider


class Command(BaseCommand):
    help = "Register a verified Google Drive changes notification channel."

    def add_arguments(self, parser):
        parser.add_argument("configuration_id")
        parser.add_argument("--callback-url")
        parser.add_argument("--days", type=int, default=6)

    def handle(self, *args, **options):
        days = options["days"]
        if days < 1 or days > 7:
            raise CommandError("--days must be between 1 and 7.")
        configuration = (
            MemoryConnectionConfiguration.objects.select_related("external_connection")
            .filter(
                pk=options["configuration_id"],
                provider=MemoryProvider.GOOGLE_DRIVE,
            )
            .first()
        )
        if configuration is None:
            raise CommandError("Google Drive memory configuration was not found.")
        try:
            channel = register_drive_watch(
                configuration,
                callback_url=options.get("callback_url"),
                lifetime=timedelta(days=days),
            )
        except DriveWatchError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {
                    "channel_id": channel.channel_id,
                    "configuration_id": str(channel.configuration_id),
                    "resource_id": channel.resource_id,
                    "expiration_at": channel.expiration_at.isoformat(),
                    "status": channel.status,
                },
                sort_keys=True,
            )
        )
