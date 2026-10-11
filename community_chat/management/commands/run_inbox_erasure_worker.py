"""Execute one bounded pass of already-authorized durable inbox deletion work."""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from community_chat.inbox_erasure import TARGET, execute_task
from community_chat.deletion_tasks import schedule_deletion
from community_chat.models import AccountDeletionRequest, AccountDeletionTask


class Command(BaseCommand):
    help = "Verify relay cursor erasure after chat access cleanup (default off)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 100:
            raise CommandError("limit must be between 1 and 100")
        if not settings.COMMUNITY_CHAT_INBOX_ERASURE_ENABLED:
            self.stdout.write("Inbox erasure is disabled; no work claimed.")
            return
        # Existing open requests must gain the new target too; no schema change
        # or re-submitted client receipt is needed to schedule that boundary.
        missing = AccountDeletionRequest.objects.exclude(status=AccountDeletionRequest.Status.COMPLETED)\
            .exclude(tasks__target=TARGET).order_by("requested_at", "id")[:limit]
        for record in missing:
            schedule_deletion(record)
        ids = list(AccountDeletionTask.objects.filter(target=TARGET)
                   .exclude(status=AccountDeletionTask.Status.COMPLETED)
                   .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now()))
                   .order_by("request__requested_at", "id").values_list("pk", flat=True)[:limit])
        completed = sum(execute_task(task_id) for task_id in ids)
        self.stdout.write(f"Inbox erasure pass: {completed} verified, {len(ids) - completed} pending.")
