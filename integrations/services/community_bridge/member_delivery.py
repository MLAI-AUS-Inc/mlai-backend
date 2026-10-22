"""Immutable Members delivery commands under the complete current authority fence.

Claim/stamp transactions commit before provider work. Selection/refresh happens
before lower locks; every real SDK call rechecks the lease after quota admission.
No method here selects a bot or changes a committed poster/scope on retry.
"""

from contextlib import contextmanager
import hashlib
import json
import re
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from community_chat.models import CommunityChatDevice, ChatRole, Moderator
from community_chat.privacy import has_ai_consent
from integrations.models import (
    BridgeSyncState,
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
    CommunityBridgeIdentityLink,
    CommunityBridgeMessageLink,
    SlackDmMirrorGrant,
)
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.slack_chat_catalog import (
    _member_room_acknowledged,
    public_channels_enabled,
)
from integrations.services.message_sync.delivery import guard_delivery
from integrations.services.message_sync.scheduler import BudgetDeferred, LeaseLost
from integrations.services.message_sync.member_history import _source_ready


class _SourceClosed(RuntimeError):
    def __init__(self, reason, observed_at):
        self.reason, self.observed_at = reason, observed_at
        super().__init__("member_delivery_source_closed")


from . import member_audiences as audiences
from .buzz import BuzzBridgeClient
from .formatting import emoji_to_slack_reaction, reaction_object_id, build_mirrored_text

SLACK_STAMP = "_slack_send_v1"
MEMBER_STAMP = "_members_delivery_v1"
RESERVED = frozenset(
    {
        SLACK_STAMP,
        MEMBER_STAMP,
        "_buzz_envelope_v1",
        "_member_network_attempt_v1",
        "_member_network_conflicts_v1",
    }
)
TS = re.compile(r"^[0-9]{1,20}\.[0-9]{1,9}$")
WRITES = {
    "create": "chat_postMessage",
    "edit": "chat_update",
    "delete": "chat_delete",
    "reaction_add": "reactions_add",
    "reaction_remove": "reactions_remove",
}
CONTENT = frozenset({"create", "edit", "reaction_add"})


class MemberDeliveryUnavailable(RuntimeError):
    permanent = True

    def __init__(self, code="poster_unavailable"):
        self.code = code
        super().__init__(code)


class MemberDeliveryDeferred(BudgetDeferred):
    pass


class MemberDeliveryRetry(RuntimeError):
    pass


def required(delivery):
    """Re-read mapping rather than trusting a serialized or caller-supplied kind."""
    return bool(
        (delivery.get("channel") or {}).get("audience") == "members"
        or (delivery.get("payload") or {}).get(MEMBER_STAMP) is not None
        or (delivery.get("payload") or {}).get(SLACK_STAMP) is not None
        or CommunityBridgeChannel.objects.filter(
            pk=delivery["channel_id"], audience="members"
        ).exists()
    )


def clean_metadata(value):
    visited = 0

    def clean(item, depth=0):
        nonlocal visited
        visited += 1
        if depth > 16 or visited > 2000:
            raise ValueError("bridge_metadata_bound")
        if isinstance(item, dict):
            return {
                key: clean(v, depth + 1)
                for key, v in item.items()
                if key not in RESERVED
            }
        if isinstance(item, list):
            return [clean(v, depth + 1) for v in item]
        return item

    return clean(value) if isinstance(value, dict) else {}


def _identity(row):
    return [
        row.pk,
        row.slack_workspace_id,
        row.slack_channel_id,
        row.destination_platform,
        row.destination_workspace_id,
        row.destination_channel_id,
        row.audience,
    ]


def scope_for(row):
    """Server-owned sidecar, independently authenticated by adapter NIP-98."""
    from .buzz import validate_member_scope

    return validate_member_scope(
        {
            "channel_id": row.destination_channel_id,
            "slack_workspace_id": row.slack_workspace_id,
            "slack_channel_id": row.slack_channel_id,
            "audience": "members",
            "expected_generation": str(row.audience_generation),
            "expected_audience_hash": row.audience_hash,
        }
    )


def _state(channel_id):
    state = (
        BridgeSyncState.objects.select_for_update()
        .filter(public_channel_id=channel_id)
        .first()
    )
    if state is None or state.status in {"paused", "revoked"}:
        raise MemberDeliveryDeferred(5)
    return state


def _suffix(delivery_id, identity=None):
    hint = CommunityBridgeDelivery.objects.only("channel_id").get(pk=delivery_id)
    state = _state(hint.channel_id)
    channel = CommunityBridgeChannel.objects.select_for_update().get(pk=hint.channel_id)
    row = CommunityBridgeDelivery.objects.select_for_update().get(pk=delivery_id)
    guard_delivery(row)
    if (
        row.status != "processing"
        or row.channel_id != channel.pk
        or state.public_channel_id != channel.pk
    ):
        raise LeaseLost("member_delivery_not_current")
    if identity is not None and _identity(channel) != identity:
        raise LeaseLost("member_delivery_mapping_changed")
    return state, channel, row


def _stamp_identity(delivery):
    """Commit deterministic identity before *any* Slack call, including probes."""
    with transaction.atomic():
        _, channel, row = _suffix(delivery["id"])
        if channel.audience != "members":
            raise MemberDeliveryUnavailable("member_mapping_changed")
        payload = dict(row.payload or {})
        stamp = payload.get(SLACK_STAMP)
        intent = [
            row.source_platform,
            row.target_platform,
            row.delivery_type,
            row.source_channel_id,
            row.source_message_id,
            row.source_parent_message_id,
            row.target_channel_id,
            {k: v for k, v in payload.items() if k not in RESERVED},
        ]
        expected = {
            "version": 1,
            "mapping": _identity(channel),
            "intent_hash": hashlib.sha256(
                json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "client_msg_id": str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"mlai-community-bridge:{row.pk}")
            ),
        }
        if stamp is None:
            payload[SLACK_STAMP] = expected
            row.payload = payload
            row.save(update_fields=("payload", "updated_at"))
            stamp = expected
        if not isinstance(stamp, dict) or any(
            stamp.get(k) != v for k, v in expected.items()
        ):
            raise MemberDeliveryUnavailable("immutable_member_identity_changed")
        return channel, dict(stamp), dict(payload.get(MEMBER_STAMP) or {})


def _binding(authority, public_key=""):
    # OAuth generation/token may refresh only on the same bound connection.
    return {
        "kind": "member",
        "grant_id": authority.grant_id,
        "user_id": authority.user_id,
        "connection_id": authority.connection_id,
        "workspace_id": authority.workspace_id,
        "slack_user_id": authority.slack_user_id,
        "consent_generation": authority.consent_generation,
        "consent_version": authority.consent_version,
        "public_key": public_key,
    }


def _poster_owner(value):
    """Stable account ownership, distinct from a particular consent/device epoch."""
    if not isinstance(value, dict) or value.get("kind") != "member":
        return None
    ids = tuple(value.get(key) for key in ("grant_id", "user_id", "connection_id"))
    if any(type(item) is not int or item <= 0 for item in ids):
        return None
    workspace, subject = value.get("workspace_id"), value.get("slack_user_id")
    if (
        not isinstance(workspace, str)
        or not re.fullmatch(r"T[A-Z0-9]{1,99}", workspace)
        or not isinstance(subject, str)
        or not re.fullmatch(r"[UW][A-Z0-9]{1,99}", subject)
    ):
        return None
    return ("member", *ids, workspace, subject)


def _poster_link(delivery):
    return CommunityBridgeMessageLink.objects.filter(
        channel_id=delivery["channel_id"],
        source_platform=delivery["source_platform"],
        source_channel_id=delivery["source_channel_id"],
        source_message_id=delivery["source_message_id"],
        destination_platform="slack",
    ).first()


def _choose(delivery, channel, saved):
    bound = saved.get("actor")
    original_owner = None
    public_key = str((delivery.get("payload") or {}).get("source_author_id") or "")
    if delivery["target_platform"] == "slack":
        if delivery["source_platform"] != "buzz":
            raise MemberDeliveryUnavailable("not_a_slack_member")
        if bound is None and delivery["delivery_type"] in {
            "edit",
            "delete",
            "reaction_remove",
        }:
            link = _poster_link(delivery)
            if link is None:
                if CommunityBridgeMessageLink.objects.filter(
                    channel_id=delivery["channel_id"],
                    source_platform="slack",
                    destination_platform="buzz",
                    destination_channel_id=delivery["source_channel_id"],
                    destination_message_id=delivery["source_message_id"],
                ).exists():
                    raise MemberDeliveryUnavailable(
                        "reverse_origin_mutation_unsupported"
                    )
                from .worker import ParentMappingPending

                raise ParentMappingPending(delivery["source_message_id"])
            bound = (link.destination_payload or {}).get("poster")
            if not isinstance(bound, dict) or bound.get("kind") != "member":
                raise MemberDeliveryUnavailable()
            # A fresh request may capture current consent/device authority for
            # this exact original account. Existing commands never take this
            # branch and retain every captured epoch and wire byte.
            original_owner = _poster_owner(bound)
            if original_owner is None:
                raise MemberDeliveryUnavailable("poster_unavailable")
            bound = {**bound, "public_key": public_key}
        if bound is not None:
            if not isinstance(bound, dict):
                raise MemberDeliveryUnavailable()
            grant = (
                SlackDmMirrorGrant.objects.select_related("user", "connection")
                .filter(pk=bound.get("grant_id"))
                .first()
            )
        else:
            grant = access.poster_for_author(
                public_key, channel.slack_workspace_id, channel.slack_channel_id
            )
        if grant is None:
            raise MemberDeliveryUnavailable(
                "poster_unavailable" if bound else "not_a_slack_member"
            )
    else:
        grant = (
            SlackDmMirrorGrant.objects.select_related("user", "connection")
            .filter(pk=bound.get("grant_id"))
            .first()
            if isinstance(bound, dict)
            else access.reader_for_channel(
                channel.slack_workspace_id, channel.slack_channel_id
            )
        )
        public_key = ""
        if grant is None:
            raise MemberDeliveryDeferred(60)
    try:
        authority = dm._capture_slack_grant_api_authority(grant, refresh_token=False)
        actor = _binding(authority, public_key)
        if original_owner is not None:
            if _poster_owner(actor) != original_owner:
                raise MemberDeliveryUnavailable("original_member_poster_changed")
        elif bound is not None and actor != bound:
            raise MemberDeliveryUnavailable("immutable_member_actor_changed")
        if delivery["target_platform"] == "slack":
            # A revoked signer or changed consent must not refresh credentials.
            # Lock this complete owner cohort before any OAuth provider work;
            # refresh reuses these already-held User/grant/connection locks.
            with transaction.atomic():
                users, grants, connections, memberships = audiences._lock_authority(
                    {authority.user_id},
                    channel.slack_workspace_id,
                    channel.slack_channel_id,
                )
                current = next(
                    (item for item in grants if item.pk == authority.grant_id), None
                )
                connection = next(
                    (
                        item
                        for item in connections
                        if item.pk == authority.connection_id
                    ),
                    None,
                )
                if (
                    current is None
                    or not any(
                        user.pk == authority.user_id and user.is_active
                        for user in users
                    )
                    or not dm._slack_grant_api_authority_is_current(
                        current,
                        connection,
                        authority,
                        required_scopes={"channels:read"},
                    )
                    or not public_channels_enabled(current, authority=authority)
                    or access._membership_state(
                        current,
                        next(
                            (
                                item
                                for item in memberships
                                if item.grant_id == current.pk
                            ),
                            None,
                        ),
                    )
                    != "joined"
                    or public_key
                    not in dm._owner_device_pubkeys(authority.user_id, limit=8)
                    or not access._poster_identity_current(current, public_key)
                ):
                    raise MemberDeliveryUnavailable("poster_unavailable")
                current.connection = connection
                authority = dm._capture_slack_grant_api_authority(
                    current, refresh_token=True
                )
        else:
            authority = dm._capture_slack_grant_api_authority(grant, refresh_token=True)
        if _binding(authority, public_key) != actor:
            raise MemberDeliveryUnavailable("immutable_member_actor_changed")
        return authority, actor
    except dm.SlackDmMirrorAuthorizationError:
        raise MemberDeliveryUnavailable("poster_unavailable") from None


@contextmanager
def _fence(delivery, original, authority, actor, *, read_reconcile=False):
    cohort = audiences._cohort(
        original.slack_workspace_id, original.slack_channel_id
    ) | {authority.user_id}
    if len(cohort) > audiences.MAX_AUTHORITY_USERS:
        raise MemberDeliveryDeferred(60)
    with transaction.atomic():
        locked = audiences._lock_authority(
            cohort, original.slack_workspace_id, original.slack_channel_id
        )
        users, grants, connections, memberships = locked
        # Direct link/admin changes take their row lock too; never reacquire a
        # User through single-owner MemberSlackClient below State/Channel.
        links = list(
            CommunityBridgeIdentityLink.objects.select_for_update()
            .filter(user_id__in=cohort)
            .order_by("user_id", "pk")
        )
        grant = next((g for g in grants if g.pk == authority.grant_id), None)
        connection = next(
            (c for c in connections if c.pk == authority.connection_id), None
        )
        user = next((u for u in users if u.pk == authority.user_id), None)
        scopes = {"channels:read"} | (
            {"chat:write"}
            if delivery["target_platform"] == "slack"
            and delivery["delivery_type"] in {"create", "edit", "delete"}
            else {"reactions:write"}
            if delivery["target_platform"] == "slack"
            else set()
        )
        if grant is None or user is None or not user.is_active:
            raise MemberDeliveryUnavailable()
        grant.user, grant.connection = user, connection
        if (
            not dm._slack_grant_api_authority_is_current(
                grant, connection, authority, required_scopes=scopes
            )
            or dm.current_slack_oauth_generation(user.pk) != authority.oauth_generation
            or not public_channels_enabled(grant, authority=authority)
            or access._membership_state(
                grant, next((m for m in memberships if m.grant_id == grant.pk), None)
            )
            != "joined"
        ):
            raise MemberDeliveryUnavailable()
        if delivery["target_platform"] == "slack":
            key = actor["public_key"]
            if (
                not CommunityChatDevice.objects.filter(
                    user=user,
                    public_key=key,
                    status="verified",
                    revoked_at__isnull=True,
                ).exists()
                or key not in dm._owner_device_pubkeys(user.pk, limit=8)
                or not any(
                    l.user_id == user.pk
                    and l.slack_workspace_id == authority.workspace_id
                    and l.slack_user_id == authority.slack_user_id
                    and l.revoked_at is None
                    for l in links
                )
            ):
                raise MemberDeliveryUnavailable()
            if (
                delivery["delivery_type"] in CONTENT
                and not read_reconcile
                and getattr(settings, "COMMUNITY_CHAT_AI_CONSENT_REQUIRED", True)
                and not has_ai_consent(user.pk)
            ):
                from .ai_consent import PublicBridgeConsentRequired

                raise PublicBridgeConsentRequired(
                    "Current AI sharing permission is required"
                )
        if delivery["target_platform"] == "slack":
            list(
                ChatRole.objects.select_for_update()
                .filter(user_id__in=cohort)
                .order_by("user_id", "pk")
            )
            list(
                Moderator.objects.select_for_update()
                .filter(user_id__in=cohort)
                .order_by("user_id", "pk")
            )
        state, channel, row = _suffix(delivery["id"], _identity(original))
        if (
            row.source_platform != delivery["source_platform"]
            or row.target_platform != delivery["target_platform"]
            or row.delivery_type != delivery["delivery_type"]
            or row.source_message_id != delivery["source_message_id"]
            or row.source_channel_id != delivery["source_channel_id"]
            or row.target_platform == "slack"
            and (row.payload or {}).get("source_author_id") != actor["public_key"]
        ):
            raise MemberDeliveryUnavailable("immutable_member_source_changed")
        payload = dict(row.payload or {})
        identity_stamp = payload.get(SLACK_STAMP)
        intent = [
            row.source_platform,
            row.target_platform,
            row.delivery_type,
            row.source_channel_id,
            row.source_message_id,
            row.source_parent_message_id,
            row.target_channel_id,
            {k: v for k, v in payload.items() if k not in RESERVED},
        ]
        if (
            not isinstance(identity_stamp, dict)
            or identity_stamp.get("mapping") != _identity(channel)
            or identity_stamp.get("intent_hash")
            != hashlib.sha256(
                json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        ):
            raise MemberDeliveryUnavailable("immutable_member_source_changed")
        row._member_display_user_ids = cohort
        from .member_channels import member_channel_in_scope

        if (
            not access.enabled()
            or not member_channel_in_scope(
                channel.slack_workspace_id, channel.slack_channel_id
            )
            or not _source_ready(channel)
            or channel.audience != "members"
            or state.workspace_id != channel.slack_workspace_id
            or state.source_channel_id != channel.slack_channel_id
            or not audiences._cohort(
                channel.slack_workspace_id, channel.slack_channel_id
            ).issubset(cohort)
        ):
            raise MemberDeliveryDeferred(5)
        eligible = audiences._eligible_grants(locked, channel.slack_workspace_id)
        keys = audiences._device_keys({g.user_id for g in eligible})
        if keys is None or not _member_room_acknowledged(
            channel, audiences.audience_hash(keys)
        ):
            raise MemberDeliveryDeferred(5)
        yield (
            row,
            channel,
            grant,
            _Client(row, channel, authority, connection, read_only=read_reconcile),
        )


class _Client:
    def __init__(self, row, channel, authority, connection, *, read_only=False):
        self.row, self.channel, self.authority, self.connection = (
            row,
            channel,
            authority,
            connection,
        )
        self.started_at = None
        self.mutation_started = False
        self.read_only = read_only

    def call(self, method, **kwargs):
        if self.read_only and method in WRITES.values():
            raise MemberDeliveryUnavailable("reconciliation_must_be_read_only")
        guard_delivery(self.row)
        if kwargs.get("channel") != self.channel.slack_channel_id:
            raise LeaseLost("member_delivery_source_changed")
        self.started_at = None

        def before_request():
            guard_delivery(self.row)
            self.started_at = timezone.now()
            if method in WRITES.values():
                self.mutation_started = True

        try:
            return dm._invoke_slack_with_locked_connection(
                self.authority,
                self.connection,
                method,
                before_request=before_request,
                **kwargs,
            )
        except dm.SlackApiError as error:
            error.member_delivery_observed_at = self.started_at
            raise

    def verify(self):
        response = self.call(
            "conversations_info",
            channel=self.channel.slack_channel_id,
            include_num_members=True,
        )
        raw = response.get("channel") if hasattr(response, "get") else None
        if not isinstance(raw, dict) or raw.get("id") != self.channel.slack_channel_id:
            raise MemberDeliveryDeferred(60)
        if any(
            raw.get(k)
            for k in (
                "is_shared",
                "is_ext_shared",
                "is_org_shared",
                "is_pending_ext_shared",
                "pending_shared",
            )
        ):
            raise _SourceClosed("slack_connect", self.started_at)
        if raw.get("is_private") is True or any(
            raw.get(k) is True for k in ("is_im", "is_mpim", "is_group")
        ):
            raise _SourceClosed("converted_to_private", self.started_at)
        if raw.get("is_archived") is True:
            raise _SourceClosed("archived", self.started_at)
        if (
            response.get("ok") is not True
            or raw.get("is_private") is not False
            or raw.get("is_channel") is not True
        ):
            raise MemberDeliveryDeferred(60)
        if raw.get("is_member") is False:
            raise dm.SlackApiError(
                "member_delivery_source_unavailable",
                response={"ok": False, "error": "not_in_channel"},
            )
        if raw.get("is_member") is not True:
            raise MemberDeliveryDeferred(60)
        return raw


def _target(row, source_id):
    direct = (
        CommunityBridgeMessageLink.objects.select_for_update()
        .filter(
            channel_id=row.channel_id,
            source_platform=row.source_platform,
            source_channel_id=row.source_channel_id,
            source_message_id=source_id,
            destination_platform=row.target_platform,
        )
        .order_by("pk")
        .first()
    )
    reverse = None
    if direct is None:
        reverse = (
            CommunityBridgeMessageLink.objects.select_for_update()
            .filter(
                channel_id=row.channel_id,
                source_platform=row.target_platform,
                destination_platform=row.source_platform,
                destination_channel_id=row.source_channel_id,
                destination_message_id=source_id,
            )
            .order_by("pk")
            .first()
        )
    destination = (
        direct.destination_message_id
        if direct
        else reverse.source_message_id
        if reverse
        else ""
    )
    destination_channel = (
        direct.destination_channel_id
        if direct
        else reverse.source_channel_id
        if reverse
        else ""
    )
    if not destination:
        from .worker import ParentMappingPending

        raise ParentMappingPending(source_id)
    if destination_channel != row.target_channel_id:
        raise MemberDeliveryUnavailable("target_mapping_changed")
    return destination


def _wire(row, channel, actor, client, info):
    payload = row.payload or {}
    operation = row.delivery_type
    target = ""
    if operation in {"edit", "delete", "reaction_remove"}:
        direct = (
            CommunityBridgeMessageLink.objects.select_for_update()
            .filter(
                channel=channel,
                source_platform=row.source_platform,
                source_channel_id=row.source_channel_id,
                source_message_id=row.source_message_id,
                destination_platform=row.target_platform,
            )
            .first()
        )
        if direct is None:
            # A reverse link points at a native human event. It grants reply
            # lookup, never permission for the bridge to mutate that event.
            if CommunityBridgeMessageLink.objects.filter(
                channel=channel,
                source_platform=row.target_platform,
                destination_platform=row.source_platform,
                destination_channel_id=row.source_channel_id,
                destination_message_id=row.source_message_id,
            ).exists():
                raise MemberDeliveryUnavailable("reverse_origin_mutation_unsupported")
            from .worker import ParentMappingPending

            raise ParentMappingPending(row.source_message_id)
        if direct.destination_channel_id != row.target_channel_id:
            raise MemberDeliveryUnavailable("target_mapping_changed")
        target = direct.destination_message_id
    parent = (
        _target(row, row.source_parent_message_id)
        if row.source_parent_message_id
        else ""
    )
    if operation in {"edit", "delete", "reaction_remove"} and not parent:
        parent = direct.destination_parent_message_id
    if row.target_platform == "slack":
        if operation in {"edit", "delete", "reaction_remove"}:
            link = (
                CommunityBridgeMessageLink.objects.select_for_update()
                .filter(
                    channel=channel,
                    source_platform="buzz",
                    source_channel_id=row.source_channel_id,
                    source_message_id=row.source_message_id,
                    destination_platform="slack",
                )
                .first()
            )
            poster = (link.destination_payload or {}).get("poster") if link else None
            if _poster_owner(poster) is None or _poster_owner(poster) != _poster_owner(
                actor
            ):
                raise MemberDeliveryUnavailable("original_member_poster_changed")
            if operation == "reaction_remove":
                destination = link.destination_payload or {}
                target = str(destination.get("message_id") or "")
                reaction = str(destination.get("reaction") or "")
            else:
                reaction = ""
        else:
            reaction = (
                emoji_to_slack_reaction(str(payload.get("text") or ""))
                if operation == "reaction_add"
                else ""
            )
        if operation.startswith("reaction") and (
            not reaction
            or not TS.fullmatch(parent if operation == "reaction_add" else target)
        ):
            raise MemberDeliveryUnavailable("invalid_reaction_target")
        kwargs = {"channel": channel.slack_channel_id}
        if operation == "create":
            kwargs["client_msg_id"] = row.payload[SLACK_STAMP]["client_msg_id"]
            if parent:
                kwargs["thread_ts"] = parent
        elif operation in {"edit", "delete"}:
            kwargs["ts"] = target
        else:
            kwargs.update(
                timestamp=parent if operation == "reaction_add" else target,
                name=reaction,
            )
        if operation in {"create", "edit"}:
            from integrations.services.slack_channel_mentions import (
                render_slack_mentions,
            )
            from .broadcast_mentions import (
                render_broadcast_mentions,
                has_broadcast_mentions,
            )
            from community_chat.permissions import device_chat_role

            text, ids = render_slack_mentions(
                str(payload.get("text") or ""),
                (payload.get("metadata") or {}).get("slack_mention_tags") or [],
            )
            if ids:
                from integrations.services.slack_mentions import validate_mention_users

                class Mentions:
                    def users_info(self, **options):
                        if "users:read" not in client.authority.scopes:
                            raise MemberDeliveryUnavailable("mention_scope_unavailable")
                        # users.info has no channel argument. Transport remains
                        # in this exact current channel/lease authority.
                        return dm._invoke_slack_with_locked_connection(
                            client.authority,
                            client.connection,
                            "users_info",
                            before_request=lambda: guard_delivery(row),
                            **options,
                        )

                validate_mention_users(
                    Mentions(), ids, channel.slack_workspace_id, scope="member"
                )
            role = device_chat_role(actor["public_key"])
            if (
                operation == "create"
                and not parent
                and has_broadcast_mentions(text)
                and role not in {"owner", "admin"}
            ):
                # Guest status is provider authority; missing users:read cannot
                # be guessed as permission to notify everyone.
                if "users:read" not in client.authority.scopes:
                    raise MemberDeliveryUnavailable("broadcast_scope_unavailable")
                response = dm._invoke_slack_with_locked_connection(
                    client.authority,
                    client.connection,
                    "users_info",
                    before_request=lambda: guard_delivery(row),
                    user=actor["slack_user_id"],
                )
                user = response.get("user") if hasattr(response, "get") else None
                if (
                    not isinstance(user, dict)
                    or user.get("id") != actor["slack_user_id"]
                ):
                    raise MemberDeliveryDeferred(60)
                if user.get("is_restricted") or user.get("is_ultra_restricted"):
                    role = "guest"
            text = render_broadcast_mentions(
                text,
                role=role,
                channel=info,
                is_thread=bool(parent) or operation != "create",
            )
            text += "".join(
                "\n" + str(item.get("url") or "")
                for item in payload.get("attachments", [])
                if isinstance(item, dict)
            )
            kwargs.update(text=text, unfurl_links=False, unfurl_media=False)
        return {
            "method": WRITES[operation],
            "kwargs": kwargs,
            "target": target,
            "parent": parent,
            "reaction": reaction,
            "text_hash": text_hash(kwargs.get("text", "")),
        }
    # Slack -> Chat does not use the bot for profile lookups. Missing names are
    # presentation-only stable Slack IDs, never another account's authority.
    from .identity import verified_identity_for_slack

    source_author = str(payload.get("source_author_id") or "")
    identity = (
        verified_identity_for_slack(
            slack_workspace_id=channel.slack_workspace_id, slack_user_id=source_author
        )
        if CommunityBridgeIdentityLink.objects.filter(
            user_id__in=row._member_display_user_ids,
            slack_workspace_id=channel.slack_workspace_id,
            slack_user_id=source_author,
            revoked_at__isnull=True,
        ).exists()
        else None
    )
    metadata = payload.get("metadata") or {}
    text = str(payload.get("text") or "")
    if operation in {"create", "edit"}:
        text = build_mirrored_text(
            destination_platform="buzz",
            source_platform="slack",
            author_display_name=str(
                payload.get("source_author_display_name")
                or payload.get("source_author_id")
                or ""
            ),
            body=text,
            attachments=payload.get("attachments") or [],
        )
    if operation in {"delete", "reaction_remove"}:
        text = ""
    return {
        "delivery_id": str(row.pk),
        "created_at": int(row.created_at.timestamp()),
        "operation": operation,
        "channel_id": channel.destination_channel_id,
        "text": text,
        "target_message_id": parent if operation == "reaction_add" else target,
        "parent_message_id": parent,
        "source_workspace_id": channel.slack_workspace_id,
        "source_channel_id": channel.slack_channel_id,
        "source_message_id": str(
            metadata.get("slack_message_id") or row.source_message_id
        ),
        "source_author_id": str(payload.get("source_author_id") or ""),
        "source_author_display_name": str(
            payload.get("source_author_display_name") or ""
        ),
        "source_author_avatar_url": str(payload.get("source_author_avatar_url") or ""),
        "linked_pubkey": str((identity or {}).get("buzz_pubkey") or ""),
        "linked_profile_id": str((identity or {}).get("user_profile_id") or ""),
        "source_created_at": int(metadata.get("slack_created_at") or 0),
        "broadcast": _verified_broadcast(row) if operation == "create" else False,
        "member_scope": scope_for(channel),
    }


def _check_frozen_broadcast(row, wire, actor, client, info):
    from .broadcast_mentions import has_broadcast_mentions, render_broadcast_mentions
    from community_chat.permissions import device_chat_role

    text = wire["kwargs"].get("text", "")
    if (
        row.delivery_type != "create"
        or wire.get("parent")
        or not has_broadcast_mentions(text)
    ):
        return
    role = device_chat_role(actor["public_key"])
    if role not in {"owner", "admin"}:
        if "users:read" not in client.authority.scopes:
            raise MemberDeliveryUnavailable("broadcast_scope_unavailable")
        response = dm._invoke_slack_with_locked_connection(
            client.authority,
            client.connection,
            "users_info",
            before_request=lambda: guard_delivery(row),
            user=actor["slack_user_id"],
        )
        user = response.get("user") if hasattr(response, "get") else None
        if not isinstance(user, dict) or user.get("id") != actor["slack_user_id"]:
            raise MemberDeliveryDeferred(60)
        if user.get("is_restricted") or user.get("is_ultra_restricted"):
            role = "guest"
    current = render_broadcast_mentions(
        text,
        role=role,
        channel=info,
        is_thread=bool(wire.get("parent")) or row.delivery_type != "create",
    )
    if current != text:
        raise MemberDeliveryUnavailable("immutable_broadcast_permission_changed")


def _verified_broadcast(row):
    if (row.payload or {}).get("metadata", {}).get(
        "broadcast"
    ) is not True or row.receipt_id is None:
        return False
    receipt = row.receipt
    raw = receipt.payload if isinstance(receipt.payload, dict) else {}
    event = raw.get("event")
    if not isinstance(event, dict):
        return False
    return bool(
        receipt.platform == "slack"
        and raw.get("team_id") == row.channel.slack_workspace_id
        and event.get("channel") == row.source_channel_id
        and event.get("ts") == row.source_message_id
        and (
            event.get("subtype") == "thread_broadcast"
            or event.get("reply_broadcast") is True
        )
    )


def text_hash(text):
    """Canonical Slack transport text, not rendered Chat source text."""
    import html

    value = html.unescape(str(text or "")).replace("\r\n", "\n")
    value = re.sub(
        r"<(https?://[^>|]+)\|([^>]*)>",
        lambda m: "<" + m[1] + ">" if m[2] == m[1] else m[0],
        value,
    )
    value = re.sub(r"<(https?://[^>|]+)>", r"\1", value)
    from integrations.services.slack_emoji import _emoji_maps

    names = _emoji_maps()[0]
    value = re.sub(r":([a-z0-9_+\-]+):", lambda m: names.get(m[1], m[0]), value)
    return hashlib.sha256(value.encode()).hexdigest()


def _same_command(saved, channel, actor):
    if (
        not isinstance(saved, dict)
        or saved.get("version") != 1
        or saved.get("actor") != actor
        or saved.get("scope") != scope_for(channel)
        or not isinstance(saved.get("wire"), dict)
    ):
        raise MemberDeliveryUnavailable("immutable_member_command_changed")


def _prepare(delivery, original, authority, actor, saved):
    if saved:
        return saved
    with _fence(delivery, original, authority, actor) as (row, channel, grant, client):
        info = client.verify()
        command = {
            "version": 1,
            "actor": actor,
            "scope": scope_for(channel),
            "wire": _wire(row, channel, actor, client, info),
        }
        payload = dict(row.payload or {})
        existing = payload.get(MEMBER_STAMP)
        if row.canonical_envelope and existing is None:
            raise MemberDeliveryUnavailable("missing_member_scope")
        if existing is not None:
            _same_command(existing, channel, actor)
            return existing
        payload[MEMBER_STAMP] = command
        row.payload = payload
        if row.target_platform == "buzz":
            row.canonical_envelope = command["wire"]
        row.save(update_fields=("payload", "canonical_envelope", "updated_at"))
        return command


def execute(delivery):
    """One immutable attempt; all external work and settlement remain fenced."""
    original, identity, saved = _stamp_identity(delivery)
    if not access.enabled():
        raise MemberDeliveryDeferred(60)
    authority, actor = _choose(delivery, original, saved)
    client = None
    attempt = None
    command = None
    receipt = None
    succeeded = False
    adapter_started = False
    provider_error = None
    from . import member_attempts as attempts

    try:
        command = _prepare(delivery, original, authority, actor, saved)
        attempt, reconcile = attempts.arm(delivery, original, command)
        with _fence(delivery, original, authority, actor, read_reconcile=reconcile) as (
            row,
            channel,
            grant,
            client,
        ):
            _same_command(command, channel, actor)
            if (row.payload or {}).get(MEMBER_STAMP) != command:
                raise MemberDeliveryUnavailable("immutable_member_command_changed")
            if row.source_platform == "slack" and row.delivery_type in {
                "reaction_add",
                "reaction_remove",
            }:
                from .public_echo import newer_reaction_observed

                if row.receipt_id and newer_reaction_observed(
                    channel, row.receipt.payload
                ):
                    from .store import complete_delivery

                    complete_delivery(delivery_id=row.pk)
                    succeeded = True
                    return
            info = client.verify()
            wire = command["wire"]
            if row.target_platform == "slack" and not reconcile:
                _check_frozen_broadcast(row, wire, actor, client, info)
            if row.source_parent_message_id and _target(
                row, row.source_parent_message_id
            ) != wire.get("parent", wire.get("parent_message_id")):
                raise MemberDeliveryUnavailable("target_mapping_changed")
            if row.delivery_type in {"edit", "delete", "reaction_remove"}:
                link = (
                    CommunityBridgeMessageLink.objects.select_for_update()
                    .filter(
                        channel=channel,
                        source_platform=row.source_platform,
                        source_channel_id=row.source_channel_id,
                        source_message_id=row.source_message_id,
                        destination_platform=row.target_platform,
                    )
                    .first()
                )
                if link is None or link.destination_channel_id != row.target_channel_id:
                    raise MemberDeliveryUnavailable("target_mapping_changed")
                expected = (
                    (link.destination_payload or {}).get("message_id")
                    if row.target_platform == "slack"
                    and row.delivery_type == "reaction_remove"
                    else link.destination_message_id
                )
                if expected != wire.get("target", wire.get("target_message_id")):
                    raise MemberDeliveryUnavailable("target_mapping_changed")
                if (
                    row.target_platform == "slack"
                    and row.delivery_type == "reaction_remove"
                    and CommunityBridgeMessageLink.objects.filter(
                        channel=channel,
                        source_platform="buzz",
                        destination_platform="slack",
                        source_deleted_at__isnull=True,
                        created_at__gt=row.created_at,
                        destination_payload__message_id=wire["target"],
                        destination_payload__reaction=wire["reaction"],
                        destination_payload__poster__slack_user_id=actor[
                            "slack_user_id"
                        ],
                    ).exists()
                ):
                    raise MemberDeliveryUnavailable("reaction_remove_superseded")
            if row.target_platform == "slack":
                outcome = "accepted"
                if reconcile:
                    response, outcome = attempts.reconcile(
                        client, row, channel, actor, wire, attempt
                    )
                else:
                    try:
                        response = client.call(wire["method"], **wire["kwargs"])
                    except dm.SlackApiError as error:
                        code = str(error.response.get("error") or "")
                        if (
                            (
                                row.delivery_type == "reaction_add"
                                and code == "already_reacted"
                            )
                            or (
                                row.delivery_type == "reaction_remove"
                                and code in {"no_reaction", "message_not_found"}
                            )
                            or (
                                row.delivery_type == "delete"
                                and code == "message_not_found"
                            )
                        ):
                            outcome = code
                            response = None
                        else:
                            raise
                receipt = attempts.receipt(
                    response, channel, actor, wire, row.delivery_type, outcome=outcome
                )
                if receipt is None or receipt["actor_verified"] is not True:
                    raise MemberDeliveryDeferred(60)
                guard_delivery(row)
                row.payload = {
                    **row.payload,
                    attempts.ATTEMPT_KEY: {
                        **attempt,
                        "status": "acknowledged",
                        "receipt": receipt,
                    },
                }
                row.save(update_fields=("payload", "updated_at"))
                _finish_slack(row, channel, actor, wire, response, outcome=outcome)
            elif row.target_platform == "buzz":
                # Relay's exact immutable inner event ID reconciles an already
                # committed atomic effect; it never resubmits a rebased scope.
                guard_delivery(row)
                adapter_started = True
                response = BuzzBridgeClient.deliver(**wire)
                guard_delivery(row)
                row.payload = {
                    **row.payload,
                    attempts.ATTEMPT_KEY: {**attempt, "status": "acknowledged"},
                }
                row.save(update_fields=("payload", "updated_at"))
                _finish_buzz(row, wire, response)
            else:
                raise MemberDeliveryUnavailable("unsupported_member_destination")
        succeeded = True
    except _SourceClosed as error:
        from .member_channels import mark_source_reduction

        if error.observed_at is not None:
            mark_source_reduction(
                original.slack_workspace_id,
                original.slack_channel_id,
                reason=error.reason,
                event_observed_at=error.observed_at,
            )
        raise MemberDeliveryDeferred(60) from None
    except dm.SlackApiError as error:
        provider_error = error
        # Locks have been released. Never infer departure from quota/timeout.
        grant = SlackDmMirrorGrant.objects.filter(pk=authority.grant_id).first()
        if grant is not None:
            access.record_reader_failure(
                grant,
                error,
                channel_id=original.slack_channel_id,
                authority=authority,
                purpose="write" if delivery["target_platform"] == "slack" else "info",
                observed_at=getattr(error, "member_delivery_observed_at", None),
            )
        if dm._is_slack_auth_error(error) or str(error.response.get("error") or "") in {
            "not_in_channel",
            "channel_not_found",
        }:
            raise MemberDeliveryUnavailable("poster_unavailable") from None
        raise MemberDeliveryRetry("member_provider_uncertain") from None
    finally:
        if attempt is not None and not succeeded:
            import sys

            error = sys.exc_info()[1]
            started = bool(
                adapter_started or client is not None and client.mutation_started
            )
            definitive = {
                "invalid_auth",
                "not_authed",
                "token_revoked",
                "token_expired",
                "not_in_channel",
                "channel_not_found",
                "missing_scope",
                "is_archived",
                "restricted_action",
                "ratelimited",
                "message_not_found",
                "no_reaction",
                "already_reacted",
            }
            provider = provider_error or (
                error
                if isinstance(error, dm.SlackApiError)
                else getattr(error, "__cause__", None)
            )
            rejected = (
                isinstance(provider, dm.SlackApiError)
                and str(provider.response.get("error") or "") in definitive
            )
            status = "not_sent" if not started or rejected else "uncertain"
            # A reused armed attempt remains uncertain even if this read-only
            # reconciliation turn made no mutation call itself.
            if reconcile and attempt["status"] != "not_sent":
                status = "uncertain"
            attempts.record(delivery["id"], command, attempt, status, receipt)


def _finish_slack(row, channel, actor, wire, response, *, outcome="accepted"):
    from .store import complete_create_delivery, complete_delivery, mark_link_deleted
    from .member_attempts import actor_proven

    if outcome == "accepted" and (
        not hasattr(response, "get") or response.get("ok") is not True
    ):
        raise MemberDeliveryDeferred(60)
    kwargs = wire["kwargs"]
    operation = row.delivery_type
    if operation in {"create", "edit", "delete"} and outcome == "accepted":
        target = str(response.get("ts") or "")
        if response.get("channel") != channel.slack_channel_id or not TS.fullmatch(
            target
        ):
            raise MemberDeliveryUnavailable("invalid_slack_ack")
        if operation != "create" and target != kwargs["ts"]:
            raise MemberDeliveryUnavailable("invalid_slack_ack")
        if operation == "create":
            message = response.get("message")
            if (
                not isinstance(message, dict)
                or message.get("user") != actor["slack_user_id"]
                or not actor_proven(message, actor, response)
                or message.get("client_msg_id")
                not in (None, "", kwargs["client_msg_id"])
            ):
                raise MemberDeliveryUnavailable("invalid_slack_actor_ack")
    if operation in {"create", "reaction_add"}:
        target = (
            str(response.get("ts") or "")
            if operation == "create"
            else kwargs["timestamp"]
        )
        destination = (
            target
            if operation == "create"
            else reaction_object_id(
                message_id=target,
                reaction=wire["reaction"],
                author_id=row.source_message_id,
            )
        )
        complete_create_delivery(
            delivery_id=row.pk,
            destination_message_id=destination,
            destination_channel_id=channel.slack_channel_id,
            destination_parent_message_id=wire["parent"],
            destination_payload={
                "channel_id": channel.slack_channel_id,
                "message_id": target,
                "reaction": wire["reaction"],
                "poster": actor,
                "text_hash": wire["text_hash"],
            },
        )
    else:
        link = (
            CommunityBridgeMessageLink.objects.select_for_update()
            .filter(
                channel=channel,
                source_platform="buzz",
                source_channel_id=row.source_channel_id,
                source_message_id=row.source_message_id,
                destination_platform="slack",
            )
            .first()
        )
        if link is None:
            raise MemberDeliveryUnavailable("target_mapping_changed")
        if operation == "edit":
            link.destination_payload = {
                **(link.destination_payload or {}),
                "text_hash": wire["text_hash"],
            }
            link.save(update_fields=("destination_payload", "updated_at"))
        if operation in {"delete", "reaction_remove"}:
            mark_link_deleted(
                source_platform="buzz",
                source_channel_id=row.source_channel_id,
                source_message_id=row.source_message_id,
                destination_platform="slack",
            )
        complete_delivery(delivery_id=row.pk)


def _finish_buzz(row, wire, response):
    from .store import complete_create_delivery, complete_delivery, mark_link_deleted

    if row.delivery_type in {"create", "reaction_add"}:
        complete_create_delivery(
            delivery_id=row.pk,
            destination_message_id=response["message_id"],
            destination_channel_id=response["channel_id"],
            destination_parent_message_id=wire["parent_message_id"],
            destination_payload=response,
        )
    else:
        if row.delivery_type in {"delete", "reaction_remove"}:
            mark_link_deleted(
                source_platform="slack",
                source_channel_id=row.source_channel_id,
                source_message_id=row.source_message_id,
                destination_platform="buzz",
            )
        complete_delivery(delivery_id=row.pk)


def legacy_send(delivery, send, *args, require_ai=False, **kwargs):
    """Negative Members fence for the retained Community provider boundaries.

    AI's existing author User fence comes first. No mutable callback is added
    to the shared bot SDK; its post-quota guard uses an operation ContextVar.
    """

    def guarded():
        with transaction.atomic():
            BridgeSyncState.objects.select_for_update().filter(
                public_channel_id=delivery["channel_id"]
            ).first()
            channel = CommunityBridgeChannel.objects.select_for_update().get(
                pk=delivery["channel_id"]
            )
            row = CommunityBridgeDelivery.objects.select_for_update().get(
                pk=delivery["id"]
            )
            guard_delivery(row)
            hint = delivery.get("channel") or {}
            if (
                not channel.enabled
                or channel.audience != "community"
                or (row.payload or {}).get(MEMBER_STAMP) is not None
                or row.channel_id != channel.pk
                or any(
                    hint.get(key, current) != current
                    for key, current in (
                        ("slack_workspace_id", channel.slack_workspace_id),
                        ("slack_channel_id", channel.slack_channel_id),
                        ("destination_platform", channel.destination_platform),
                        ("destination_workspace_id", channel.destination_workspace_id),
                        ("destination_channel_id", channel.destination_channel_id),
                    )
                )
            ):
                raise MemberDeliveryDeferred(5)
            from integrations.services.message_sync.delivery import (
                legacy_provider_fence,
            )

            with legacy_provider_fence(lambda: guard_delivery(row)):
                guard_delivery(row)
                return send(*args, **kwargs)

    if require_ai:
        from .ai_consent import send_with_ai_consent

        return send_with_ai_consent(delivery, guarded)
    return guarded()
