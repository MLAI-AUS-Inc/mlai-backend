from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from integrations.models import CommunityBridgeReceipt
from integrations.services.community_bridge.community_source import retained_payload


class Command(BaseCommand):
    help = "Clear expired raw community-bridge webhook payloads while retaining receipt metadata."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=getattr(settings, "COMMUNITY_BRIDGE_RAW_PAYLOAD_RETENTION_DAYS", 30),
            help="Clear payloads older than this many days (default: configured retention).",
        )
        parser.add_argument("--batch-size", type=int, default=500)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        days = int(options["days"])
        batch_size = int(options["batch_size"])
        if days < 1:
            raise CommandError("--days must be at least 1")
        if batch_size < 1 or batch_size > 5000:
            raise CommandError("--batch-size must be between 1 and 5000")

        cutoff = timezone.now() - timedelta(days=days)
        queryset = CommunityBridgeReceipt.objects.filter(
            created_at__lt=cutoff,
        ).exclude(payload={})
        # Keyset progress visits metadata-only rows once, so retained evidence
        # cannot make a batch loop forever. Lock and reread every payload after
        # selection; a concurrent authenticated role merge must survive purge.
        cleared = 0
        last_id = 0
        while True:
            ids = list(queryset.filter(id__gt=last_id).order_by("id")
                       .values_list("id", flat=True)[:batch_size])
            if not ids:
                break
            last_id = ids[-1]
            with transaction.atomic():
                rows = (CommunityBridgeReceipt.objects.filter(id__in=ids)
                        .select_for_update(of=("self",)).select_related("channel").order_by("id"))
                for receipt in rows:
                    kept = retained_payload(receipt)
                    if receipt.payload != kept:
                        cleared += 1
                        if not options["dry_run"]:
                            receipt.payload = kept
                            receipt.save(update_fields=("payload", "updated_at"))

        if options["dry_run"]:
            self.stdout.write(
                f"Would clear {cleared} community bridge payload(s) older than {days} day(s)."
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Cleared {cleared} community bridge payload(s) older than {days} day(s)."
            )
        )
