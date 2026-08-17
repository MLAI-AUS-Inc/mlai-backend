from django.core.management.base import BaseCommand

from roo.coding import reconcile_coding_reservations


class Command(BaseCommand):
    help = "Expire abandoned MLAI Coding turns and release 24-hour ambiguity holds."

    def handle(self, *args, **options):
        result = reconcile_coding_reservations()
        self.stdout.write(
            self.style.SUCCESS(
                "Expired {expired_turns} stale coding turn(s); released "
                "{released_unstarted_calls} unstarted dispatch lease(s) and "
                "{released_ambiguous_calls} ambiguous call reservation(s) "
                "({released_calls} total).".format(**result)
            )
        )
