"""Linearized local revocation for private-chat device authority."""

from collections.abc import Callable

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from community_chat.models import (
    CommunityChatAccountSession,
    CommunityChatBootstrapToken,
    CommunityChatChallenge,
    CommunityChatDevice,
    CommunityChatDeviceAuthRequest,
    CommunityChatEmailCodeChallenge,
    CommunityChatEmailCodeDelivery,
    CommunityChatInviteAudit,
    DeviceBindingStatus,
    EmailCodeDeliveryStatus,
)
from integrations.models import (
    CommunityBridgeDeliveryStatus,
    CommunityBridgePlatform,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorDelivery,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services.slack_dm_registration_ledger import (
    REGISTRATION_STATE_CLEANED,
    REGISTRATION_STATE_PREFIX,
    mark_registration_cleanup_pending_locked,
    prepare_conversation_registration_cleanup_locked,
    registration_channel_id,
    registration_request,
    registration_rows_for_grant,
    registration_state,
)

HISTORY_STATE_PREFIX = "history-state:"


def revoke_device_credentials_locked(
    user,
    *,
    public_key: str,
    installation_id=None,
    revoked_at=None,
) -> None:
    """Fence every credential or pending authorization for one device intent.

    The caller must already hold the user's row lock. Credential issuance and
    enrollment mutation take that same user-first boundary, so rows inserted or
    rotated before this call are included and later issuance is necessarily a
    new, deliberate authorization after the completed delete.
    """

    normalized_key = str(public_key or "").strip().lower()
    now = revoked_at or timezone.now()
    credential_scope = Q(public_key=normalized_key)
    if installation_id is not None:
        credential_scope |= Q(installation_id=installation_id)

    sessions = list(
        CommunityChatAccountSession.objects.select_for_update()
        .filter(user_id=user.pk, revoked_at__isnull=True)
        .filter(credential_scope)
        .order_by("id")
    )
    if sessions:
        CommunityChatAccountSession.objects.filter(
            id__in=[session.pk for session in sessions]
        ).update(revoked_at=now, updated_at=now)

    bootstrap_tokens = list(
        CommunityChatBootstrapToken.objects.select_for_update()
        .filter(user_id=user.pk, revoked_at__isnull=True)
        .filter(credential_scope)
        .order_by("id")
    )
    if bootstrap_tokens:
        CommunityChatBootstrapToken.objects.filter(
            id__in=[token.pk for token in bootstrap_tokens]
        ).update(revoked_at=now)

    # A handoff request has no installation column, but its public key is
    # state/PKCE-bound. Invalidate every pre-authorization row for this exact
    # key and every authorized, unconsumed request for the user. The latter is
    # deliberately conservative because an authorized request can target a
    # regenerated key on the deleted installation, which is not stored in a
    # directly queryable column.
    auth_request_scope = Q(public_key=normalized_key) | Q(
        user_id=user.pk,
        authorized_at__isnull=False,
    )
    auth_requests = list(
        CommunityChatDeviceAuthRequest.objects.select_for_update()
        .filter(
            consumed_at__isnull=True,
            expires_at__gt=now,
        )
        .filter(auth_request_scope)
        .order_by("id")
    )
    if auth_requests:
        CommunityChatDeviceAuthRequest.objects.filter(
            id__in=[auth_request.pk for auth_request in auth_requests]
        ).update(expires_at=now)

    enrollment_scope = Q(user_id=user.pk, public_key=normalized_key)
    if installation_id is not None:
        enrollment_scope |= Q(user_id=user.pk, installation_id=installation_id)
    enrollment_challenges = list(
        CommunityChatChallenge.objects.select_for_update()
        .filter(used_at__isnull=True)
        .filter(enrollment_scope)
        .order_by("id")
    )
    if enrollment_challenges:
        CommunityChatChallenge.objects.filter(
            id__in=[challenge.pk for challenge in enrollment_challenges]
        ).update(used_at=now)

    email_scope = Q(user_id=user.pk, public_key=normalized_key)
    if installation_id is not None:
        email_scope |= Q(user_id=user.pk, installation_id=installation_id)
    email_challenges = list(
        CommunityChatEmailCodeChallenge.objects.select_for_update()
        .filter(
            consumed_at__isnull=True,
            invalidated_at__isnull=True,
        )
        .filter(email_scope)
        .order_by("id")
    )
    if email_challenges:
        challenge_ids = [challenge.pk for challenge in email_challenges]
        CommunityChatEmailCodeChallenge.objects.filter(id__in=challenge_ids).update(
            invalidated_at=now
        )
        CommunityChatEmailCodeDelivery.objects.filter(
            challenge_id__in=challenge_ids,
            status__in=(
                EmailCodeDeliveryStatus.PENDING,
                EmailCodeDeliveryStatus.SENDING,
            ),
        ).update(
            status=EmailCodeDeliveryStatus.CANCELLED,
            encrypted_code="",
            claimed_at=None,
            updated_at=now,
        )


def revoke_device_authority(
    user,
    *,
    device_id,
    public_key: str,
    reason: str,
    allow_already_revoked: bool = False,
    revoke_member_invite_callback: Callable[[str], tuple[str, object]] | None = None,
    revoke_relay_membership_callback: Callable[[str], tuple[str, object]] | None = None,
) -> CommunityChatDevice | None:
    """Revoke one device and fence every private registration containing it.

    This boundary shares the user->grant->conversation->device lock order used
    by Slack activation, provisioning, callback, delivery, and consent
    revocation. It cancels unconfirmed member invites before removing relay
    membership so no capability can remain live once revocation returns.
    """

    normalized_key = str(public_key or "").strip().lower()
    now = timezone.now()
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=user.pk)
        grants = list(
            SlackDmMirrorGrant.objects.select_for_update()
            .filter(user_id=user.pk)
            .order_by("id")
        )
        grant_by_id = {grant.pk: grant for grant in grants}
        conversations = list(
            SlackDmMirrorConversation.objects.select_for_update()
            .filter(grant_id__in=grant_by_id)
            .order_by("grant_id", "id")
        )
        device_query = CommunityChatDevice.objects.select_for_update().filter(
            user_id=user.pk,
            public_key=normalized_key,
        )
        active_binding = (
            CommunityChatDevice.objects.select_for_update()
            .filter(
                public_key=normalized_key,
                status__in=(
                    DeviceBindingStatus.PENDING,
                    DeviceBindingStatus.VERIFIED,
                ),
            )
            .order_by("-id")
            .first()
        )
        # A revoked historical binding does not authorize its former owner to
        # revoke a later enrollment of the same key by another account.
        if active_binding is not None and active_binding.user_id != user.pk:
            return None
        # Re-resolve authority after taking the shared user lock. The caller's
        # row id can become stale if that binding is revoked and the same key
        # is re-enrolled before this transaction starts.
        device = active_binding
        if device is None and allow_already_revoked:
            device = (
                device_query.filter(status=DeviceBindingStatus.REVOKED)
                .order_by("-revoked_at", "-id")
                .first()
            )
        if device is not None and not allow_already_revoked and device.pk != device_id:
            return None
        if device is None:
            return None

        revoke_device_credentials_locked(
            user,
            public_key=normalized_key,
            installation_id=device.installation_id,
            revoked_at=now,
        )

        pending_invites = list(
            CommunityChatInviteAudit.objects.select_for_update()
            .filter(device=device, confirmed_at__isnull=True)
            .order_by("issued_at", "id")
        )
        if revoke_member_invite_callback is not None:
            for invite in pending_invites:
                revoke_member_invite_callback(invite.adapter_invite_id)

        device.relay_revocation_status = "already_revoked"
        if revoke_relay_membership_callback is not None:
            # DELETE is also the adapter's durable per-key generation fence.
            # Invoke it again for an owned historical binding: a previous
            # backend request may have committed local revocation while an
            # older invite mint was still ambiguous upstream.
            relay_status, _ = revoke_relay_membership_callback(normalized_key)
            if device.status != DeviceBindingStatus.REVOKED:
                device.relay_revocation_status = relay_status

        if device.status != DeviceBindingStatus.REVOKED:
            device.status = DeviceBindingStatus.REVOKED
            device.revoked_at = now
            device.revoked_by = user
            device.revocation_reason = str(reason or "")[:500]
            device.save(
                update_fields=(
                    "status",
                    "revoked_at",
                    "revoked_by",
                    "revocation_reason",
                    "updated_at",
                )
            )

        registration_rows_by_conversation: dict[int, list[SlackDmMirrorDelivery]] = {}
        for grant in grants:
            for row in registration_rows_for_grant(grant.pk, for_update=True):
                registration_rows_by_conversation.setdefault(
                    row.conversation_id,
                    [],
                ).append(row)

        def registration_contains_revoked_key(row: SlackDmMirrorDelivery) -> bool:
            if registration_state(row) == REGISTRATION_STATE_CLEANED:
                return False
            request = registration_request(row)
            return normalized_key in {
                str(value or "").strip().lower()
                for value in (
                    request["participant_pubkeys"] + request["callback_author_pubkeys"]
                )
            }

        affected_conversations = [
            conversation
            for conversation in conversations
            if normalized_key
            in {
                str(value or "").strip().lower()
                for value in conversation.participant_buzz_pubkeys or []
            }
            or normalized_key
            in {
                str(value or "").strip().lower()
                for value in (conversation.participant_identity_map or {}).values()
            }
            or any(
                registration_contains_revoked_key(row)
                for row in registration_rows_by_conversation.get(
                    conversation.pk,
                    [],
                )
            )
        ]
        affected_ids = [conversation.pk for conversation in affected_conversations]
        touched_grants: set[int] = set()
        for conversation in affected_conversations:
            grant = grant_by_id[conversation.grant_id]
            conversation.grant = grant
            prepare_conversation_registration_cleanup_locked(
                grant,
                conversation,
                reason="MLAI Chat device participant was revoked",
            )
            # Registration I/O shares these locks, but a timed-out request can
            # still be executing server-side. Fence every row now while the
            # ledger helper preserves any conservative ambiguity lease.
            for row in registration_rows_by_conversation.get(conversation.pk, []):
                if registration_state(row) == REGISTRATION_STATE_CLEANED:
                    continue
                mark_registration_cleanup_pending_locked(
                    row,
                    reason="MLAI Chat device participant was revoked",
                    channel_id=registration_channel_id(row),
                    available_at=now,
                )
            conversation.status = (
                SlackDmMirrorConversationStatus.PROVISIONING
                if grant.status == SlackDmMirrorGrantStatus.ACTIVE
                and grant.revoked_at is None
                else SlackDmMirrorConversationStatus.PAUSED
            )
            conversation.mlai_channel_id = None
            conversation.history_backfilled_at = None
            conversation.oldest_synced_ts = ""
            conversation.latest_synced_ts = ""
            conversation.last_error = ""
            conversation.save(
                update_fields=(
                    "status",
                    "mlai_channel_id",
                    "history_backfilled_at",
                    "oldest_synced_ts",
                    "latest_synced_ts",
                    "last_error",
                    "updated_at",
                )
            )
            touched_grants.add(grant.pk)

        for grant_id in touched_grants:
            grant = grant_by_id[grant_id]
            if grant.status == SlackDmMirrorGrantStatus.ACTIVE:
                grant.last_discovery_at = None
                grant.save(update_fields=("last_discovery_at", "updated_at"))

        SlackDmMirrorDelivery.objects.filter(
            conversation_id__in=affected_ids,
            source_platform=CommunityBridgePlatform.SLACK,
            source_message_id__startswith=HISTORY_STATE_PREFIX,
        ).delete()
        SlackDmMirrorDelivery.objects.filter(
            conversation_id__in=affected_ids,
        ).exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX).update(
            encrypted_text="",
            status=CommunityBridgeDeliveryStatus.DEAD,
            completed_at=None,
            last_error="Private conversation device authority changed",
            updated_at=now,
        )
        SlackDmMirrorDelivery.objects.filter(
            conversation__grant__user_id=user.pk,
            source_platform=CommunityBridgePlatform.BUZZ,
            source_author_id=normalized_key,
        ).exclude(source_message_id__startswith=REGISTRATION_STATE_PREFIX).update(
            encrypted_text="",
            status=CommunityBridgeDeliveryStatus.DEAD,
            completed_at=None,
            last_error="Originating MLAI Chat device was revoked",
            updated_at=now,
        )
        device.registration_cleanup_grant_ids = tuple(sorted(touched_grants))
        return device
