"""Finish a verified whole-account deletion using a narrow dependency allowlist."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, transaction
from django.db.models import Q
from django.db.models.functions import Lower
from django.db.models.deletion import Collector, ProtectedError, RestrictedError

from core.models import PasswordResetChallenge, UserEmailAlias
from system_email.models import SystemEmail

from .deletion_inventory import related_record_counts
from .deletion_tasks import CHAT_TARGETS, SHARED_ACCOUNT_TARGET, complete_task, targets_verified
from .models import (
    AccountDeletionCompletionNotice,
    AccountDeletionRequest,
    AccountDeletionTask,
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatEmailCodeChallenge,
    CommunityChatInviteAudit,
    DeviceBindingStatus,
)


# Protected identity rows may be removed only after all live and external work
# is verified. Retained business and moderation records need their own disposition.
PROTECTED_IDENTITY = (
    CommunityChatAccountSession, CommunityChatBootstrapToken, CommunityChatChallenge,
    CommunityChatDevice, CommunityChatDeviceAuthRequest, CommunityChatEmailCodeChallenge,
    PasswordResetChallenge, UserEmailAlias,
)
OWNED_CASCADES = {
    "core.passwordresetchallenge": {"core.passwordresetemaildelivery"},
    "community_chat.communitychatemailcodechallenge": {"community_chat.communitychatemailcodedelivery"},
}
AUTH_EMAIL_LOG_KEYS = ("chat_sign_in_code", "magic_link", "password_reset")
UNLINK_ONLY = {"community_chat.accountdeletionrequest.user"}


class AccountErasureBlocked(ValueError):
    """The reviewed account erasure cannot safely proceed."""

    def __init__(self, message, *, dependencies=()):
        super().__init__(message)
        self.dependencies = tuple(sorted(dependencies))


class ErasureCollector(Collector):
    def can_fast_delete(self, *args, **kwargs):
        # Include every dependent model in the policy check before deletion.
        return False


def _delete_reviewed(rows):
    roots = list(rows)
    if not roots:
        return 0
    root = type(roots[0])
    collector = ErasureCollector(using=DEFAULT_DB_ALIAS)
    try:
        collector.collect(roots)
    except (ProtectedError, RestrictedError) as error:
        blockers = error.protected_objects if isinstance(error, ProtectedError) else error.restricted_objects
        raise AccountErasureBlocked(
            "Protected dependencies require a reviewed disposition.",
            dependencies={row._meta.label_lower for row in blockers},
        ) from None

    permitted = OWNED_CASCADES.get(root._meta.label_lower, set())
    unexpected = {
        model._meta.label_lower for model in collector.data
        if model is not root and model._meta.label_lower not in permitted
    }
    if collector.data[root] != set(roots):
        unexpected.add(root._meta.label_lower)
    unexpected.update(
        f"{field.model._meta.label_lower}.{field.name}"
        for (field, value), groups in collector.field_updates.items()
        if (value is not None or f"{field.model._meta.label_lower}.{field.name}" not in UNLINK_ONLY)
        and any(group for group in groups)
    )
    if unexpected:
        raise AccountErasureBlocked("Dependent records need a reviewed disposition.", dependencies=unexpected)
    return collector.delete()[0]


@transaction.atomic
def finish_account(request_id, *, apply=False):
    """Preview or execute one case. No shared-account removal runs in background."""
    candidate = AccountDeletionRequest.objects.filter(pk=request_id, scope=AccountDeletionRequest.Scope.ACCOUNT).first()
    if candidate is None:
        raise AccountErasureBlocked("A whole-account deletion case is required.")
    if candidate.status == AccountDeletionRequest.Status.COMPLETED and candidate.user_id is None:
        return {"status": "already_completed"}
    if candidate.user_id is None:
        raise AccountErasureBlocked("The case's account could not be verified.")

    users = get_user_model().objects
    user = users.select_for_update().filter(pk=candidate.user_id).first()
    request = AccountDeletionRequest.objects.select_for_update().get(pk=request_id)
    if request.status == AccountDeletionRequest.Status.COMPLETED and request.user_id is None:
        return {"status": "already_completed"}
    if user is None or request.user_id != candidate.user_id or request.scope != AccountDeletionRequest.Scope.ACCOUNT:
        raise AccountErasureBlocked("The case's account could not be verified.")
    if not targets_verified(request, CHAT_TARGETS):
        raise AccountErasureBlocked("Verify all Chat, bridge, backup and processor targets before removing the account.")
    if user.is_staff or user.is_superuser or user.groups.exists() or user.user_permissions.exists():
        raise AccountErasureBlocked("Transfer or remove account administration before erasure.")
    if AccountDeletionRequest.objects.filter(user=user).exclude(
        pk=request.pk,
    ).exclude(status=AccountDeletionRequest.Status.COMPLETED).exists():
        raise AccountErasureBlocked("Resolve the other open deletion case before removing the account.")
    if (
        CommunityChatDevice.objects.filter(user=user).exclude(status=DeviceBindingStatus.REVOKED).exists()
        or CommunityChatAccountSession.objects.filter(user=user, revoked_at__isnull=True).exists()
        or CommunityChatBootstrapToken.objects.filter(user=user, revoked_at__isnull=True).exists()
    ):
        raise AccountErasureBlocked("Active Chat authority was found after revoked-access verification.")

    inventory = related_record_counts(user.pk)
    allowed = {f"{model._meta.label_lower}.user" for model in PROTECTED_IDENTITY} | UNLINK_ONLY
    own_revocation = "community_chat.communitychatdevice.revoked_by"
    if not CommunityChatDevice.objects.filter(revoked_by=user).exclude(user=user).exists():
        allowed.add(own_revocation)
    unexpected = set(inventory) - allowed
    addresses = {user.email.lower()}
    addresses.update(
        UserEmailAlias.objects.filter(user=user).annotate(normalized=Lower("email"))
        .values_list("normalized", flat=True)
    )
    address_logs = SystemEmail.objects.alias(normalized=Lower("to_email")).filter(
        Q(user_id=user.pk) | Q(normalized__in=addresses),
    )
    auth_logs = address_logs.filter(
        user_id=user.pk, normalized__in=addresses, category="auth", message_key__in=AUTH_EMAIL_LOG_KEYS,
    )
    if address_logs.exclude(pk__in=auth_logs.values("pk")).exists():
        unexpected.add("system_email.systememail")
    if unexpected:
        raise AccountErasureBlocked("Account records need a reviewed disposition.", dependencies=unexpected)
    if not apply:
        return {
            "status": "ready_for_review", "identity_records": inventory,
            "authentication_email_logs": auth_logs.count(),
        }

    if settings.COMMUNITY_CHAT_ACCOUNT_DELETION_NOTICE_ENABLED and not AccountDeletionCompletionNotice.objects.filter(
        request=request,
    ).exists():
        raise AccountErasureBlocked("Record the legacy case's completion-notice disposition before erasure.")
    task = AccountDeletionTask.objects.select_for_update().get(request=request, target=SHARED_ACCOUNT_TARGET)
    task.status = AccountDeletionTask.Status.PROCESSING
    task.attempts += 1
    task.save(update_fields=("status", "attempts", "updated_at"))

    # Invite audit rows protect their device and challenge. Capture both sides
    # for ownership checks before removing any audit records.
    invites = CommunityChatInviteAudit.objects.filter(Q(device__user=user) | Q(challenge__user=user))
    if invites.exclude(device__user=user, challenge__user=user).exists():
        raise AccountErasureBlocked("An enrollment audit links more than one account.")
    removed = _delete_reviewed(invites)
    CommunityChatDevice.objects.filter(user=user, revoked_by=user).update(revoked_by=None)
    for model in PROTECTED_IDENTITY:
        removed += _delete_reviewed(model.objects.filter(user=user))
    user_id = user.pk
    auth_logs.update(to_email="", user_id=None)
    removed += _delete_reviewed([user])
    if users.filter(pk=user_id).exists() or related_record_counts(user_id) or address_logs.exists():
        raise AccountErasureBlocked("Account removal could not be verified.")
    if not complete_task(task.pk, attempt=task.attempts, verified_counts={
        "deleted_rows": removed, "remaining_rows": 0, "verified_targets": 1,
    }):
        raise AccountErasureBlocked("The verified account deletion could not be recorded.")
    return {"status": "completed", "deleted_rows": removed}
