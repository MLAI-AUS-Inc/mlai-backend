"""Install configured stream records without enabling email."""

from django.core.management.base import BaseCommand, CommandError

from crm.deliverability.streams import sync_streams


class Command(BaseCommand):
    help = "Sync configured sending streams; new mailboxes remain disabled."

    def add_arguments(self, parser):
        parser.add_argument("operation", choices=("sync",))

    def handle(self, *args, **options):
        try:
            count = sync_streams()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Checked {count} sending streams. No mailbox was enabled.")
