from django.core.management.base import BaseCommand
from integrations.models import GoogleConnection
from integrations.services import fetch_recent_subject_lines

class Command(BaseCommand):
    help = 'Process monthly updates for users with connected Google accounts'

    def handle(self, *args, **options):
        connections = GoogleConnection.objects.all()
        for conn in connections:
            self.stdout.write(f"Processing updates for {conn.user.email}...")
            try:
                subjects = fetch_recent_subject_lines(conn.user, days=30)
                if subjects:
                    self.stdout.write(f"  Found {len(subjects)} emails.")
                    self.stdout.write(f"  Sample subjects: {subjects[:3]}")
                    # TODO: Pass these emails to the Roo agent for summary generation
                else:
                    self.stdout.write("  No recent emails found.")
            except Exception as e:
                self.stderr.write(f"  Error processing {conn.user.email}: {e}")
