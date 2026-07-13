"""Delete expired Health Hack dialogue while preserving contest/prize state."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from hospital.models import SimConversation, SimConversationTurn


class Command(BaseCommand):
    help = (
        "Delete Health Hack conversation turns older than the configured "
        "retention period. Diagnosis guesses, prize claims, and winners are untouched."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help="Override HEALTH_HACK_CHAT_RETENTION_DAYS for this run.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report rows that would be removed without deleting them.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days is None:
            days = int(getattr(settings, "HEALTH_HACK_CHAT_RETENTION_DAYS", 30))
        if days < 1:
            raise CommandError("retention days must be at least 1")

        cutoff = timezone.now() - timedelta(days=days)
        turns = SimConversationTurn.objects.filter(created_at__lt=cutoff)
        turn_count = turns.count()
        empty_conversations = SimConversation.objects.filter(
            last_turn_at__lt=cutoff,
            turns__isnull=True,
        )
        existing_empty_count = empty_conversations.count()

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "dry-run: expired_turns=%s existing_empty_conversations=%s cutoff=%s"
                    % (turn_count, existing_empty_count, cutoff.isoformat())
                )
            )
            return

        with transaction.atomic():
            turns.delete()
            # Deleting expired turns can make more conversations empty. Empty
            # transcript shells carry no contest state and can be removed.
            deleted_conversations, _ = SimConversation.objects.filter(
                last_turn_at__lt=cutoff,
                turns__isnull=True,
            ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                "deleted_turns=%s deleted_empty_conversations=%s retention_days=%s cutoff=%s"
                % (turn_count, deleted_conversations, days, cutoff.isoformat())
            )
        )
