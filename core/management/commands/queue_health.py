"""Read-only aggregate health for required delivery and Jobs consumers."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import PasswordResetEmailDelivery, PasswordResetDeliveryStatus
from core.password_delivery import DELIVERY_LEASE_SECONDS
from jobs.models import JobRun


class Command(BaseCommand):
    help = "Report queue age and stale email claims without exposing payloads."

    def add_arguments(self, parser):
        parser.add_argument("--max-pending-seconds", type=int, default=300)
        parser.add_argument("--fail-on-degraded", action="store_true")

    def handle(self, *args, **options):
        maximum = options["max_pending_seconds"]
        if maximum <= 0:
            raise CommandError("--max-pending-seconds must be positive.")
        now = timezone.now()
        pending_email = PasswordResetEmailDelivery.objects.filter(
            status=PasswordResetDeliveryStatus.PENDING, available_at__lte=now,
        )
        stale_email = PasswordResetEmailDelivery.objects.filter(
            status=PasswordResetDeliveryStatus.SENDING,
            claimed_at__lt=now - timedelta(seconds=DELIVERY_LEASE_SECONDS),
        ).count()
        pending_jobs = JobRun.objects.filter(status="queued")
        oldest_email = pending_email.order_by("available_at").values_list("available_at", flat=True).first()
        oldest_job = pending_jobs.order_by("created_at").values_list("created_at", flat=True).first()
        email_age = max(0, int((now - oldest_email).total_seconds())) if oldest_email else 0
        job_age = max(0, int((now - oldest_job).total_seconds())) if oldest_job else 0
        degraded = bool(stale_email or email_age > maximum or job_age > maximum)
        self.stdout.write(json.dumps({
            "status": "degraded" if degraded else "ok",
            "password_email": {
                "pending": pending_email.count(), "oldest_due_seconds": email_age,
                "expired_claims": stale_email,
            },
            "jobs": {
                "queued": pending_jobs.count(), "oldest_queued_seconds": job_age,
                "running": JobRun.objects.filter(status="running").count(),
            },
        }, sort_keys=True))
        if degraded and options["fail_on_degraded"]:
            raise CommandError("A required worker queue needs attention.")
