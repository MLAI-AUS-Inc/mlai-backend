from django.core.management.base import BaseCommand

from core.firebase_utils import configure_storage_cors


class Command(BaseCommand):
    help = "Configure Firebase Storage CORS for direct browser uploads."

    def add_arguments(self, parser):
        parser.add_argument(
            "--origin",
            action="append",
            dest="origins",
            help="Allowed origin. Can be provided multiple times.",
        )

    def handle(self, *args, **options):
        cors = configure_storage_cors(origins=options.get("origins") or None)
        self.stdout.write(self.style.SUCCESS(f"Configured Firebase Storage CORS: {cors}"))
