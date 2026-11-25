"""Run one bounded pass over the verified account deletion executors."""

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from community_chat import deletion_access, deletion_profile, deletion_tasks, inbox_erasure
from community_chat.models import AccountDeletionRequest, AccountDeletionTask


class Command(BaseCommand):
    help = "Revoke Chat access and verify profile and inbox cleanup on existing deletion requests."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=20)

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 100:
            raise CommandError("limit must be between 1 and 100")
        if not settings.COMMUNITY_CHAT_ACCOUNT_DELETION_WORKER_ENABLED:
            self.stdout.write("Account deletion worker is disabled.")
            return
        eligible = AccountDeletionTask.objects.exclude(
            request__status=AccountDeletionRequest.Status.COMPLETED,
        ).exclude(status=AccountDeletionTask.Status.COMPLETED).filter(
            Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=timezone.now()),
        )
        executors = [deletion_access, deletion_profile]
        if settings.COMMUNITY_CHAT_INBOX_ERASURE_ENABLED:
            executors.append(inbox_erasure)
        for executor in executors:
            task_ids = list(eligible.filter(target=executor.TARGET)
                            .order_by("request__requested_at", "id").values_list("pk", flat=True)[:limit])
            completed = sum(executor.execute_task(pk) for pk in task_ids)
            self.stdout.write(f"{executor.TARGET}: {completed} verified, {len(task_ids) - completed} pending.")
        now = timezone.now()
        open_tasks = AccountDeletionTask.objects.exclude(
            status=AccountDeletionTask.Status.COMPLETED,
        ).exclude(request__status=AccountDeletionRequest.Status.COMPLETED)
        finished_deadline = now - timedelta(days=deletion_tasks.DELETION_DEADLINE_DAYS)
        live_deadline = now - timedelta(days=deletion_tasks.LIVE_ERASURE_DAYS)
        overdue = open_tasks.filter(
            Q(request__requested_at__lte=finished_deadline)
            | (Q(request__requested_at__lte=live_deadline) & ~Q(target="backups_and_processor_copies"))
        ).count()
        if overdue:
            self.stderr.write(f"{overdue} deletion tasks have passed their target. Escalate to the MLAI committee.")
