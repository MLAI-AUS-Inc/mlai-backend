from django.core.management.base import BaseCommand
from content_factory.website_reconciliation import process_website_connection_operations


class Command(BaseCommand):
    help = 'Retry durable website cancellation/revocation and prepare requested cleanup proposals.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20)

    def handle(self, *args, **options):
        self.stdout.write(str(process_website_connection_operations(limit=max(1, min(100, options['limit'])))))
