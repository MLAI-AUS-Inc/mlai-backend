"""Revoke Chat authority for an accepted deletion before erasing its data."""

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from integrations.models import SlackDmMirrorGrant, SlackDmMirrorGrantStatus
from integrations.services.slack_dm_mirror import pause_grant

from . import adapter, deletion_tasks
from .device_revocation import revoke_device_authority
from .models import (
    AccountDeletionRequest,
    AccountDeletionTask,
    AiConsentRecord,
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    CommunityMemberProfile,
    DeviceBindingStatus,
    EmailCodeDeliveryStatus,
)
from .permissions import appointed_chat_role

TARGET = "chat_access"


def execute_task(task_id):
    """Apply the revocation under the enrollment fence, then verify local state."""
    task = deletion_tasks.claim_task(task_id)
    if task is None:
        return False
    failure = "verification_failed"
    try:
        if task.target == TARGET and task.request.user_id is not None:
            with transaction.atomic():
                user = get_user_model().objects.select_for_update().get(pk=task.request.user_id)
                request = AccountDeletionRequest.objects.select_for_update().get(pk=task.request_id)
                current = AccountDeletionTask.objects.select_for_update().get(pk=task.pk)
                if current.status != AccountDeletionTask.Status.PROCESSING or current.attempts != task.attempts:
                    return False
                if request.user_id == user.pk and appointed_chat_role(user) != "owner":
                    counts = _revoke(user)
                    if counts["remaining_rows"] == 0:
                        return deletion_tasks.complete_task(task.pk, attempt=task.attempts, verified_counts=counts)
                else:
                    failure = "operator_review_required"
    except adapter.MembershipAdapterConflict:
        failure = "operator_review_required"
    except adapter.MembershipAdapterError:
        failure = "provider_unavailable"
    except get_user_model().DoesNotExist:
        pass
    deletion_tasks.fail_task(task.pk, attempt=task.attempts, error_code=failure)
    return False


def _revoke(user):
    now = timezone.now()
    devices = CommunityChatDevice.objects.filter(user=user)
    sessions = CommunityChatAccountSession.objects.filter(user=user, revoked_at__isnull=True)
    tokens = CommunityChatBootstrapToken.objects.filter(user=user, revoked_at__isnull=True)
    handoffs = CommunityChatDeviceAuthRequest.objects.filter(user=user, consumed_at__isnull=True, expires_at__gt=now)
    challenges = CommunityChatChallenge.objects.filter(user=user, used_at__isnull=True)
    codes = CommunityChatEmailCodeChallenge.objects.filter(user=user, consumed_at__isnull=True, invalidated_at__isnull=True)
    deliveries = CommunityChatEmailCodeDelivery.objects.filter(
        challenge__user=user, status__in=(EmailCodeDeliveryStatus.PENDING, EmailCodeDeliveryStatus.SENDING),
    )
    grants = SlackDmMirrorGrant.objects.filter(user=user, status=SlackDmMirrorGrantStatus.ACTIVE)
    ai_grants = AiConsentRecord.objects.filter(
        user=user, granted_at__isnull=False, withdrawn_at__isnull=True,
    )
    revoked = sessions.update(revoked_at=now, updated_at=now) + tokens.update(revoked_at=now)
    revoked += ai_grants.update(withdrawn_at=now)
    revoked += handoffs.update(expires_at=now) + challenges.update(used_at=now) + codes.update(invalidated_at=now)
    revoked += deliveries.update(
        status=EmailCodeDeliveryStatus.CANCELLED, encrypted_code="", claimed_at=None, updated_at=now,
    )
    for grant in grants.order_by("pk"):
        pause_grant(grant)
    # Onboarding preferences are Chat-scoped. Keep consent/suppression history
    # and shared MLAI profile fields available to their owning services.
    CommunityMemberProfile.objects.filter(user=user).update(
        city="", interests=[], personalisation_skipped=True,
    )
    # Include historical keys so a lost earlier adapter acknowledgement cannot
    # leave a relay membership live. The shared revoker checks current ownership.
    for public_key in devices.order_by("public_key").values_list("public_key", flat=True).distinct():
        revoke_device_authority(
            user, device_id=None, public_key=public_key,
            reason="account_deletion", allow_already_revoked=True,
            revoke_member_invite_callback=adapter.revoke_member_invite,
            revoke_relay_membership_callback=adapter.revoke_relay_membership,
        )
    active_devices = devices.exclude(status=DeviceBindingStatus.REVOKED)
    remaining = sum(q.count() for q in (
        active_devices, sessions, tokens, handoffs, challenges, codes, deliveries, grants, ai_grants,
    ))
    return {"remaining_rows": remaining, "revoked_credentials": revoked, "verified_targets": 1}
