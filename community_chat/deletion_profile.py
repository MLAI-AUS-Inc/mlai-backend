"""Erase the member's Chat profile, usage data and appearance selections."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import deletion_tasks
from .models import (
    AccountDeletionRequest,
    AccountDeletionTask,
    AiConsentRecord,
    CommunityMemberProfile,
    TokenUsageAccount,
    TokenUsageDailyBucket,
    TokenUsageSession,
)
from .roo_avatar.models import CommunityRooAvatar
from .roo_avatar.policy import SLOTS, empty_outfit
from .roo_avatar.services import _owned


TARGET = "chat_profile_and_preferences"


@transaction.atomic
def _cleanup(task):
    # Match Chat preference and authentication writes which lock the user first.
    user = get_user_model().objects.select_for_update().filter(pk=task.request.user_id).first()
    request = AccountDeletionRequest.objects.select_for_update().get(pk=task.request_id)
    active = AccountDeletionTask.objects.select_for_update().get(pk=task.pk)
    if not (
        user and request.user_id == user.pk and active.attempts == task.attempts
        and active.status == AccountDeletionTask.Status.PROCESSING
        and deletion_tasks.targets_verified(request, ("chat_access",))
    ):
        return False

    usage = TokenUsageAccount.objects.filter(user_id=user.pk)
    usage_ids = tuple(usage.select_for_update().values_list("pk", flat=True))
    targets = [
        CommunityMemberProfile.objects.filter(user_id=user.pk),
        AiConsentRecord.objects.filter(user_id=user.pk),
        usage,
    ]
    removed = sum(query.delete()[0] for query in targets)
    # A previously worn item can be an owned purchase on older builds.
    # Retain ownership and advance the version so stale writes cannot restore the outfit.
    outfits = CommunityRooAvatar.objects.select_for_update().filter(user_id=user.pk)
    for row in outfits:
        if all(getattr(row, slot) is None for slot in SLOTS):
            continue
        row.owned_items = sorted(_owned(row))
        for slot, value in empty_outfit().items():
            setattr(row, slot, value)
        row.version += 1
        row.updated_at = timezone.now()
        row.save(update_fields=(*SLOTS, "owned_items", "version", "updated_at"))
    readback = [
        *targets,
        TokenUsageSession.objects.filter(account_id__in=usage_ids),
        TokenUsageDailyBucket.objects.filter(account_id__in=usage_ids),
        *(outfits.filter(**{f"{slot}__isnull": False}) for slot in SLOTS),
    ]
    if any(query.exists() for query in readback):
        # Do not commit a partial cleanup.
        transaction.set_rollback(True)
        return False
    return deletion_tasks.complete_task(task.pk, attempt=task.attempts, verified_counts={
        "deleted_rows": removed, "remaining_rows": 0, "verified_targets": len(readback),
    })


def execute_task(task_id):
    """Retry profile cleanup only after Chat access was independently revoked."""
    if not AccountDeletionTask.objects.filter(pk=task_id, target=TARGET).exists():
        return False
    task = deletion_tasks.claim_task(task_id)
    if task is None:
        return False
    if _cleanup(task):
        return True
    deletion_tasks.fail_task(task.pk, attempt=task.attempts, error_code="verification_failed")
    return False
