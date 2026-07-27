import json

from django.core.management.base import BaseCommand, CommandError

from org_memory.runtime import MemoryRuntimeError, requeue_dead_letter


class Command(BaseCommand):
    help = "Requeue one resolved-safe source work dead letter by UUID."

    def add_arguments(self, parser):
        parser.add_argument("dead_letter_id")

    def handle(self, *args, **options):
        try:
            work_item = requeue_dead_letter(options["dead_letter_id"])
        except Exception as exc:
            if isinstance(exc, (MemoryRuntimeError, ValueError)) or exc.__class__.__name__ == "DoesNotExist":
                raise CommandError(str(exc)) from exc
            raise
        self.stdout.write(
            json.dumps(
                {"status": "requeued", "work_item_id": str(work_item.pk)},
                sort_keys=True,
            )
        )
