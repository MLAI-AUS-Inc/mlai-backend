from django.core.management.base import BaseCommand
from integrations.models import GoogleConnection
from integrations.services.gmail import fetch_last_month_emails

class Command(BaseCommand):
    help = 'Crawls the last 30 days of emails for all connected Google users.'

    def handle(self, *args, **options):
        connections = GoogleConnection.objects.all()
        self.stdout.write(f"Found {connections.count()} connections to process.")

        for conn in connections:
            self.stdout.write(f"Processing {conn.user.email} ({conn.google_email})...")
            try:
                messages = fetch_last_month_emails(conn)
                self.stdout.write(self.style.SUCCESS(f"  - Fetched {len(messages)} messages for {conn.google_email}"))
                
                # Here you would typically process the messages (e.g., save to DB, analyze, etc.)
                # For now, we just log the count.
                
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  - Failed to process {conn.google_email}: {e}"))

        self.stdout.write(self.style.SUCCESS("Done."))
