"""Administrator account bans and retryable revocation of every Chat device."""

import logging

from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from core.models import AccountBan, PasswordResetChallenge, User
from integrations.models import SlackDmMirrorGrant
from integrations.services.slack_dm_mirror import pause_grant
from .adapter import (
    MembershipAdapterError,
    revoke_member_invite,
    revoke_relay_membership,
)
from .device_revocation import revoke_device_authority
from .models import (
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatDevice,
)
from .permissions import chat_role

logger = logging.getLogger(__name__)


def _audit(ban, actor, message):
    LogEntry.objects.log_action(
        user_id=actor.pk,
        content_type_id=ContentType.objects.get_for_model(AccountBan).pk,
        object_id=ban.pk,
        object_repr=f"Account ban {ban.pk}",
        action_flag=CHANGE,
        change_message=message,
    )


def ban_account(*, actor, user_id, reason=""):
    """Disable access first; keep remote revocations durable until acknowledged."""
    if chat_role(actor) != "admin":
        raise PermissionDenied("Only MLAI administrators can ban accounts.")
    if not isinstance(reason, str) or len(reason) > 1000:
        raise ValidationError({"reason": "Use at most 1,000 characters."})
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if (
            user.pk == actor.pk
            or chat_role(user) == "admin"
            or user.is_staff
            or user.is_superuser
        ):
            raise PermissionDenied("This administrator account is protected.")
        now = timezone.now()
        ban, created = AccountBan.objects.select_for_update().get_or_create(
            user=user,
            defaults={"email": user.email, "banned_by": actor, "reason": reason},
        )
        if created or ban.revoked_at is not None:
            ban.email = user.email
            ban.reason = reason
            ban.banned_by = actor
            ban.revoked_at = None
            ban.revoked_by = None
            ban.revocation_pending = True
            ban.save()
            user.is_active = False
            user.auth_version += 1
            user.save(update_fields=("is_active", "auth_version", "updated_at"))
            _audit(ban, actor, "MLAI account banned; email retained")
        # Include sessions whose new device was never enrolled.
        CommunityChatAccountSession.objects.filter(
            user=user, revoked_at__isnull=True
        ).update(revoked_at=now)
        CommunityChatBootstrapToken.objects.filter(
            user=user, revoked_at__isnull=True
        ).update(revoked_at=now)
        PasswordResetChallenge.objects.filter(
            user=user, consumed_at__isnull=True
        ).update(consumed_at=now)
        for grant in (
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(user=user, status="active")
            .order_by("id")
        ):
            pause_grant(grant)
    finish_ban_revocations(ban.pk)
    ban.refresh_from_db()
    return ban


def finish_ban_revocations(ban_id):
    """Retry idempotently; an adapter failure never rolls back account denial."""
    user_id = AccountBan.objects.values_list("user_id", flat=True).get(pk=ban_id)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        ban = AccountBan.objects.select_for_update().get(pk=ban_id)
        if ban.revoked_at is not None or not ban.revocation_pending:
            return
        try:
            for device in CommunityChatDevice.objects.filter(user=user).order_by("id"):
                # The revocation service refuses keys subsequently owned by
                # another account, including historical revoked bindings.
                revoke_device_authority(
                    user,
                    device_id=device.pk,
                    public_key=device.public_key,
                    reason="mlai_account_ban",
                    allow_already_revoked=True,
                    revoke_member_invite_callback=revoke_member_invite,
                    revoke_relay_membership_callback=revoke_relay_membership,
                )
        except MembershipAdapterError:
            logger.warning("account_ban_revocation_pending ban_id=%s", ban.pk)
            ban.save(update_fields=("updated_at",))
            return
        ban.revocation_pending = False
        ban.save(update_fields=("revocation_pending", "updated_at"))


def process_account_ban_revocations(limit=20):
    """Bounded maintenance turn run by the existing bridge worker."""
    ids = list(
        AccountBan.objects.filter(
            revoked_at__isnull=True,
            revocation_pending=True,
        )
        .order_by("updated_at")
        .values_list("pk", flat=True)[:limit]
    )
    for ban_id in ids:
        finish_ban_revocations(ban_id)


def lift_account_ban(*, actor, ban_id):
    """Restore eligibility, requiring fresh login and device enrollment."""
    if chat_role(actor) != "admin":
        raise PermissionDenied("Only MLAI administrators can lift account bans.")
    user_id = AccountBan.objects.values_list("user_id", flat=True).get(pk=ban_id)
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        ban = AccountBan.objects.select_for_update().get(pk=ban_id)
        if ban.revoked_at is not None:
            return ban
        if ban.revocation_pending:
            raise ValidationError(
                "Chat revocation is still pending. Retry after it completes."
            )
        ban.revoked_at = timezone.now()
        ban.revoked_by = actor
        ban.save(update_fields=("revoked_at", "revoked_by", "updated_at"))
        user.is_active = True
        user.auth_version += 1
        user.save(update_fields=("is_active", "auth_version", "updated_at"))
        _audit(ban, actor, "MLAI account ban lifted; fresh sign-in required")
        return ban
