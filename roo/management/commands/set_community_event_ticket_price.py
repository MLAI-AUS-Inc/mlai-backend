"""Set the approved community-event reward price without changing schema."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from roo.models import RewardsCatalog


class Command(BaseCommand):
    help = "Preview or apply the community event ticket price of 15 Roo Points."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Persist the price change."
        )

    def handle(self, *args, **options):
        with transaction.atomic():
            reward = (
                RewardsCatalog.objects.select_for_update()
                .filter(code="EVENT_TICKET")
                .first()
            )
            if reward is None:
                raise CommandError("EVENT_TICKET is missing; no reward was changed.")
            previous = reward.cost_points
            if previous == 15:
                self.stdout.write("EVENT_TICKET already costs 15 Roo Points.")
                return
            if not options["apply"]:
                self.stdout.write(
                    f"Preview: EVENT_TICKET {previous} -> 15 Roo Points. Run with --apply to save."
                )
                return
            reward.cost_points = 15
            reward.save(update_fields=["cost_points"])
            self.stdout.write(
                self.style.SUCCESS(
                    f"EVENT_TICKET price changed: {previous} -> 15 Roo Points."
                )
            )
