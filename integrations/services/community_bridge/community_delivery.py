"""Immutable Community member-or-bot commands; no Members audience authority.

Only a new server-marked ordinary Chat delivery may choose an actor. Saved
commands retain their account and request across retries, rollback and opt-out.
"""

from contextlib import contextmanager
import hashlib
import json
import logging
import re
import sys
import uuid
from urllib.parse import quote, urlencode

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from slack_sdk import WebClient

from community_chat.models import ChatRole, Moderator
from community_chat.models import CommunityChatDevice
from community_chat.privacy import has_ai_consent
from integrations.models import (
    BridgeSyncState, CommunityBridgeChannel, CommunityBridgeDelivery,
    CommunityBridgeIdentityLink, CommunityBridgeMessageLink,
    SlackDmMirrorConversation,
)
from integrations.services import slack_dm_mirror as dm, slack_member_access as access
from integrations.services.message_sync.delivery import guard_delivery
from integrations.services.message_sync.slack_client import budgeted_client
from integrations.services.slack_chat_catalog import public_channels_enabled
from integrations.services.slack_member_devices import member_device_key_allowed

from . import member_delivery as member, member_attempts as attempts
from .ai_consent import PublicBridgeConsentRequired
from .coworking import is_coworking_request
from .formatting import build_mirrored_text, emoji_to_slack_reaction
from .identity import community_account_for_key


logger = logging.getLogger(__name__)
ENQUEUE = "_community_enqueue_v1"
IDENTITY = "_community_identity_v1"
MEMBER_STAMP = "_community_delivery_v1"  # internal attempt implementation contract
MemberDeliveryUnavailable = member.MemberDeliveryUnavailable
_identity = member._identity


class CommunityLegacyHold(member.MemberDeliveryDeferred):
    """Retain unproven legacy work without terminal rejection or a resend."""

    def __init__(self):
        super().__init__(300)
        self.args = ("legacy_community_send_unproven",)


def enabled():
    return bool(getattr(settings, "SLACK_COMMUNITY_SEND_AS_MEMBER", False))


def admitted(channel):
    """The existing public rollout cohort gates new Phase5 permission too."""
    from .member_channels import member_channel_in_scope

    return enabled() and member_channel_in_scope(channel.slack_workspace_id, channel.slack_channel_id)


def ordinary(delivery):
    """Keep Discord, private queues and the explicit Roo command separate."""
    return (delivery.get("source_platform") == "buzz"
            and delivery.get("target_platform") == "slack"
            and not is_coworking_request(delivery))


def required(delivery):
    """Use current server mapping and recorded commands, never a claimed kind."""
    if not ordinary(delivery):
        return False
    row = CommunityBridgeDelivery.objects.select_related("channel").get(pk=delivery["id"])
    payload = row.payload or {}
    recorded_poster = None
    if row.delivery_type in {"edit", "delete", "reaction_remove"}:
        link = member._poster_link({"channel_id": row.channel_id, "source_platform": row.source_platform,
                                   "source_channel_id": row.source_channel_id, "source_message_id": row.source_message_id})
        recorded_poster = (link.destination_payload or {}).get("poster") if link else None
    return bool(payload.get(MEMBER_STAMP) is not None or payload.get(ENQUEUE) is not None
                or isinstance(recorded_poster, dict) and (recorded_poster.get("kind") == "member" or recorded_poster.get("user_id") is not None)
                or admitted(row.channel) and row.channel.audience == "community")


def enqueue_marker(channel, event):
    """Mark only newly enqueued, ordinary, flag-on Community work as unsent."""
    candidate = {**event, "payload": event, "source_platform": "buzz", "target_platform": "slack",
                 "channel": {"destination_channel_id": channel.destination_channel_id}}
    if (not getattr(settings, "MESSAGE_SYNC_ENABLED", False)
            or channel.audience != "community" or not ordinary(candidate)):
        return None
    recorded = False
    if event.get("delivery_type") in {"edit", "delete", "reaction_remove"}:
        link = CommunityBridgeMessageLink.objects.filter(channel=channel, source_platform="buzz", destination_platform="slack",
            source_channel_id=event.get("source_channel_id"), source_message_id=event.get("source_message_id")).first()
        poster = (link.destination_payload or {}).get("poster") if link else None
        recorded = isinstance(poster, dict) and (poster.get("kind") == "member" or poster.get("user_id") is not None)
    if not admitted(channel) and not recorded:
        return None
    return {"version": 1, "mapping": _identity(channel)}


def _suffix(delivery_id, identity=None):
    hint = CommunityBridgeDelivery.objects.only("channel_id").get(pk=delivery_id)
    state = BridgeSyncState.objects.select_for_update().filter(public_channel_id=hint.channel_id).first()
    channel = CommunityBridgeChannel.objects.select_for_update().get(pk=hint.channel_id)
    row = CommunityBridgeDelivery.objects.select_for_update().get(pk=delivery_id)
    guard_delivery(row)
    if (row.status != "processing" or row.channel_id != channel.pk
            or state is not None and state.status in {"paused", "revoked"}
            or identity is not None and _identity(channel) != identity):
        raise member.MemberDeliveryDeferred(5)
    if (not channel.enabled or channel.audience != "community"
            or channel.destination_platform != "buzz" or channel.slack_visibility == "private"
            or channel.slack_archived or channel.slack_deleted or channel.hold_reason
            or channel.mirror_decision != "mirror"
            or channel.slack_workspace_id != getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "")
            or row.source_platform != "buzz" or row.target_platform != "slack"
            or row.source_channel_id != channel.destination_channel_id
            or row.target_channel_id != channel.slack_channel_id
            or SlackDmMirrorConversation.objects.filter(slack_workspace_id=channel.slack_workspace_id,
                                                       slack_conversation_id=channel.slack_channel_id).exists()
            or (row.payload or {}).get(member.MEMBER_STAMP) is not None):
        raise MemberDeliveryUnavailable("community_mapping_changed")
    return state, channel, row


def _expected_identity(row, channel):
    intent = [row.source_platform, row.target_platform, row.delivery_type,
              row.source_channel_id, row.source_message_id, row.source_parent_message_id,
              row.target_channel_id, {k: v for k, v in (row.payload or {}).items() if k not in member.RESERVED}]
    return {"version": 1, "mapping": _identity(channel),
            "intent_hash": hashlib.sha256(json.dumps(intent, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "client_msg_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mlai-community-bridge:{row.pk}"))}


def _stamp_identity(delivery):
    with transaction.atomic():
        _, channel, row = _suffix(delivery["id"])
        if (row.payload or {}).get(ENQUEUE) != {"version": 1, "mapping": _identity(channel)}:
            # Attempts/FAILED/dead are not proof a legacy bot request was unsent.
            raise CommunityLegacyHold()
        expected = _expected_identity(row, channel)
        existing = (row.payload or {}).get(IDENTITY)
        if existing is not None and existing != expected:
            raise MemberDeliveryUnavailable("immutable_community_identity_changed")
        row.payload = {**row.payload, IDENTITY: expected}
        row.save(update_fields=("payload", "updated_at"))
        from .store import _serialize_delivery

        return channel, dict((row.payload or {}).get(MEMBER_STAMP) or {}), _serialize_delivery(row)


def _bot_actor(account, channel):
    subject = str(getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "") or "")
    if not re.fullmatch(r"[UW][A-Z0-9]{1,99}", subject):
        raise MemberDeliveryUnavailable("bot_identity_unavailable")
    return {"kind": "bot", "user_id": account["user_id"], "public_key": account["public_key"],
            "workspace_id": channel.slack_workspace_id, "slack_user_id": subject}


def _choose(delivery, channel, saved):
    key = str((delivery.get("payload") or {}).get("source_author_id") or "")
    account = community_account_for_key(slack_workspace_id=channel.slack_workspace_id, buzz_pubkey=key)
    if account is None:
        raise PublicBridgeConsentRequired("Community sharing requires a current verified account device")
    bound = saved.get("actor")
    if bound is None and delivery["delivery_type"] in {"edit", "delete", "reaction_remove"}:
        link = member._poster_link(delivery)
        if link is None:
            from .worker import ParentMappingPending
            raise ParentMappingPending(delivery["source_message_id"])
        bound = (link.destination_payload or {}).get("poster")
        if bound == {"kind": "bot"}:
            # A fresh cleanup of a legacy bot link is allowed only for its exact
            # original signing device's account; no unproven send is replayed.
            original_owner = CommunityChatDevice.objects.filter(public_key=link.source_author_id).values_list("user_id", flat=True).first()
            if original_owner != account["user_id"]:
                raise MemberDeliveryUnavailable("original_bot_poster_unavailable")
            bound = _bot_actor(account, channel)
        if not isinstance(bound, dict):
            raise MemberDeliveryUnavailable("original_poster_unavailable")
    if bound is not None:
        if not isinstance(bound, dict) or bound.get("user_id") != account["user_id"]:
            raise MemberDeliveryUnavailable("original_poster_changed")
        if bound.get("kind") == "bot":
            actor = _bot_actor(account, channel)
            if any(bound.get(key) != actor[key] for key in ("workspace_id", "slack_user_id")):
                raise MemberDeliveryUnavailable("original_bot_poster_changed")
            # Fresh cleanup can use another verified device of the same owner;
            # an existing command stays pinned to every original actor field.
            if saved and actor != bound:
                raise MemberDeliveryUnavailable("immutable_community_actor_changed")
            return None, actor
        if bound.get("kind") != "member":
            raise MemberDeliveryUnavailable("original_poster_unavailable")
        return member._choose(delivery, channel, saved)
    if admitted(channel):
        try:
            grant = access.poster_for_author(key, channel.slack_workspace_id, channel.slack_channel_id,
                                            required_scopes={"reactions:write"} if delivery["delivery_type"].startswith("reaction") else set(),
                                            strict_membership=True)
        except dm.SlackDmMirrorCredentialError:
            raise member.MemberDeliveryRetry("community_credentials_unproven") from None
        except dm.SlackDmMirrorAuthorizationError:
            raise member.MemberDeliveryRetry("community_authority_unproven") from None
        if grant is not None:
            # A selected member's capture/refresh/provider failure propagates.
            # Never reinterpret it as definitive no-grant bot eligibility.
            authority = dm._capture_slack_grant_api_authority(grant, refresh_token=False)
            actor = member._binding(authority, key)
            return member._choose(delivery, channel, {"actor": actor})
    return None, _bot_actor(account, channel)


@contextmanager
def _fence(delivery, original, authority, actor, *, read_reconcile=False):
    owner = actor.get("user_id")
    if type(owner) is not int or owner <= 0:
        raise MemberDeliveryUnavailable("invalid_community_actor")
    with transaction.atomic():
        users, grants, connections, memberships = member.audiences._lock_authority(
            {owner}, original.slack_workspace_id, original.slack_channel_id)
        links = list(CommunityBridgeIdentityLink.objects.select_for_update().filter(user_id=owner).order_by("pk"))
        user = next((u for u in users if u.pk == owner), None)
        if user is None or not user.is_active or not member_device_key_allowed(owner, actor.get("public_key")):
            raise PublicBridgeConsentRequired("Community sender device is no longer current")
        if delivery["delivery_type"] in member.CONTENT and not read_reconcile and not has_ai_consent(owner):
            raise PublicBridgeConsentRequired("Current AI sharing permission is required")
        if delivery["delivery_type"] in member.CONTENT and not read_reconcile and not admitted(original):
            raise member.MemberDeliveryDeferred(60)
        grant = None
        connection = None
        if actor.get("kind") == "member":
            if not access.enabled():
                raise member.MemberDeliveryDeferred(60)
            grant = next((g for g in grants if g.pk == authority.grant_id), None)
            connection = next((c for c in connections if c.pk == authority.connection_id), None)
            scopes = {"channels:read", "reactions:write" if delivery["delivery_type"].startswith("reaction") else "chat:write"}
            if (grant is None or member._binding(authority, actor["public_key"]) != actor
                    or not dm._slack_grant_api_authority_is_current(grant, connection, authority, required_scopes=scopes)
                    or dm.current_slack_oauth_generation(owner) != authority.oauth_generation
                    or not public_channels_enabled(grant, authority=authority)
                    or access._membership_state(grant, next((m for m in memberships if m.grant_id == grant.pk), None)) != "joined"
                    or not any(l.slack_workspace_id == authority.workspace_id and l.slack_user_id == authority.slack_user_id and l.revoked_at is None for l in links)):
                raise MemberDeliveryUnavailable("original_member_poster_unavailable")
        elif actor.get("kind") != "bot" or actor != _bot_actor({"user_id": owner, "public_key": actor["public_key"]}, original):
            raise MemberDeliveryUnavailable("immutable_community_actor_changed")
        elif admitted(original) and access.enabled() and delivery["delivery_type"] in {"create", "reaction_add"}:
            # Selection was outside these locks. If a current joined member
            # appeared before a new bot command froze, retry selection instead
            # of publishing a stale fallback strategy. No provider call here.
            current = []
            for candidate in grants:
                conn = next((item for item in connections if item.pk == candidate.connection_id), None)
                candidate.user, candidate.connection = user, conn
                if (conn is not None and candidate.slack_workspace_id == original.slack_workspace_id and public_channels_enabled(candidate)
                        and access._membership_state(candidate, next((item for item in memberships if item.grant_id == candidate.pk), None)) == "joined"
                        and any(link.slack_workspace_id == candidate.slack_workspace_id and link.slack_user_id == candidate.slack_user_id and link.revoked_at is None for link in links)):
                    needed = "reactions:write" if delivery["delivery_type"].startswith("reaction") else "chat:write"
                    if needed in (conn.scopes or []):
                        current.append(candidate.pk)
            if current and not CommunityBridgeDelivery.objects.filter(pk=delivery["id"], payload__has_key=MEMBER_STAMP).exists():
                raise member.MemberDeliveryRetry("community_selection_changed")
        for model in (ChatRole, Moderator):
            list(model.objects.select_for_update().filter(user_id=owner).order_by("pk"))
        _, channel, row = _suffix(delivery["id"], _identity(original))
        if ((row.payload or {}).get("source_author_id") != actor["public_key"]
                or (row.payload or {}).get(IDENTITY) != _expected_identity(row, channel)):
            raise MemberDeliveryUnavailable("immutable_community_source_changed")
        client = (member._Client(row, channel, authority, connection, read_only=read_reconcile)
                  if authority is not None else _BotClient(row, channel, read_only=read_reconcile))
        yield row, channel, user, client


class _BotClient(member._Client):
    def __init__(self, row, channel, *, read_only=False):
        super().__init__(row, channel, None, None, read_only=read_only)

    def verify(self):
        # Reactions/delete responses carry no actor identity. Verify this exact
        # configured token's workspace and bot subject before their write too.
        response = self.call("auth_test")
        if (not hasattr(response, "get") or response.get("ok") is not True
                or response.get("team_id") != self.channel.slack_workspace_id
                or response.get("user_id") != getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "")
                or not isinstance(response.get("bot_id"), str)
                or not re.fullmatch(r"B[A-Z0-9]{1,99}", response["bot_id"])
                or response.get("app_id", getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "")) != getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", "")):
            raise MemberDeliveryUnavailable("bot_identity_unavailable")
        return super().verify()

    def call(self, method, **kwargs):
        if self.read_only and method in member.WRITES.values():
            raise MemberDeliveryUnavailable("reconciliation_must_be_read_only")
        guard_delivery(self.row)
        if method in {"conversations_history", "conversations_replies"}:
            from integrations.services.message_sync.member_history import reject_legacy_bot_read
            reject_legacy_bot_read(self.channel.slack_channel_id)
        if "channel" in kwargs and kwargs["channel"] != self.channel.slack_channel_id:
            raise MemberDeliveryUnavailable("community_source_changed")
        self.started_at = None

        def before_request():
            guard_delivery(self.row)
            self.started_at = timezone.now()
            if method in member.WRITES.values():
                self.mutation_started = True

        client = budgeted_client(WebClient(token=getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", ""), timeout=20),
                                 workspace_id=self.channel.slack_workspace_id, before_request=before_request)
        return getattr(client, method)(**kwargs)


def _bot_wire(row, channel, actor, client, info, name):
    payload = row.payload or {}
    operation = row.delivery_type
    target = ""
    parent = member._target(row, row.source_parent_message_id) if row.source_parent_message_id else ""
    reaction = ""
    if operation in {"edit", "delete", "reaction_remove"}:
        link = CommunityBridgeMessageLink.objects.select_for_update().filter(
            channel=channel, source_platform="buzz", destination_platform="slack",
            source_channel_id=row.source_channel_id, source_message_id=row.source_message_id,
        ).first()
        if link is None or link.destination_channel_id != channel.slack_channel_id:
            raise MemberDeliveryUnavailable("target_mapping_changed")
        poster = (link.destination_payload or {}).get("poster")
        if not _same_bot_owner(poster, actor):
            raise MemberDeliveryUnavailable("original_bot_poster_changed")
        target = link.destination_message_id
        parent = parent or link.destination_parent_message_id
        if operation == "reaction_remove":
            target = str((link.destination_payload or {}).get("message_id") or "")
            reaction = str((link.destination_payload or {}).get("reaction") or "")
    elif operation == "reaction_add":
        reaction = emoji_to_slack_reaction(str(payload.get("text") or ""))
    kwargs = {"channel": channel.slack_channel_id}
    if operation == "create":
        kwargs["client_msg_id"] = payload[IDENTITY]["client_msg_id"]
        if parent:
            kwargs["thread_ts"] = parent
    elif operation in {"edit", "delete"}:
        if not member.TS.fullmatch(target):
            raise MemberDeliveryUnavailable("invalid_slack_target")
        kwargs["ts"] = target
    else:
        if not reaction or not member.TS.fullmatch(parent if operation == "reaction_add" else target):
            raise MemberDeliveryUnavailable("invalid_reaction_target")
        kwargs.update(timestamp=parent if operation == "reaction_add" else target, name=reaction)
    if operation in {"create", "edit"}:
        from integrations.services.slack_channel_mentions import render_slack_mentions
        from integrations.services.slack_mentions import validate_mention_users
        from .broadcast_mentions import render_broadcast_mentions
        from community_chat.permissions import device_chat_role

        text, ids = render_slack_mentions(str(payload.get("text") or ""), (payload.get("metadata") or {}).get("slack_mention_tags") or [])
        if ids:
            class Mentions:
                def users_info(self, **options):
                    return client.call("users_info", **options)
            validate_mention_users(Mentions(), ids, channel.slack_workspace_id, scope="public-bot")
        role = device_chat_role(actor["public_key"])
        # An unlinked bot author cannot prove Slack guest authority. Ordinary
        # text remains deliverable; uncertain broadcast intent remains silent.
        text = render_broadcast_mentions(text, role=role, channel=info,
                                        is_thread=bool(parent) or operation != "create" or role not in {"owner", "admin"})
        url = f"{settings.COMMUNITY_CHAT_FRONTEND_URL.rstrip('/')}/channels/{quote(row.source_channel_id, safe='')}?{urlencode({'messageId': row.source_message_id})}"
        kwargs.update(text=build_mirrored_text(destination_platform="slack", source_platform="buzz", author_display_name=name,
                                             body=text, attachments=payload.get("attachments") or [], source_url=url),
                      unfurl_links=True, unfurl_media=True)
    return {"method": member.WRITES[operation], "kwargs": kwargs, "target": target,
            "parent": parent, "reaction": reaction, "text_hash": member.text_hash(kwargs.get("text", ""))}


def _same_bot_owner(poster, actor):
    if poster == {"kind": "bot"}:
        return True  # legacy ownership was checked against its source account
    return (isinstance(poster, dict) and poster.get("kind") == "bot"
            and all(poster.get(key) == actor.get(key) for key in ("user_id", "workspace_id", "slack_user_id")))


def _check_bot_broadcast(row, wire, actor):
    from community_chat.permissions import device_chat_role
    from .broadcast_mentions import BroadcastPermissionDenied

    text = wire["kwargs"].get("text", "")
    if re.search(r"<!(?:channel|here|everyone)>", text) and device_chat_role(actor["public_key"]) not in {"owner", "admin"}:
        raise BroadcastPermissionDenied("Recorded bot broadcast no longer has current authority")


def _same(command, original, actor):
    if (not isinstance(command, dict) or set(command) != {"version", "actor", "mapping", "wire"}
            or command.get("version") != 1 or command.get("actor") != actor
            or command.get("mapping") != _identity(original) or not isinstance(command.get("wire"), dict)):
        raise MemberDeliveryUnavailable("immutable_community_command_changed")


def _validate_target(row, channel, actor, wire):
    if row.source_parent_message_id and member._target(row, row.source_parent_message_id) != wire["parent"]:
        raise MemberDeliveryUnavailable("target_mapping_changed")
    if row.delivery_type not in {"edit", "delete", "reaction_remove"}:
        return
    link = CommunityBridgeMessageLink.objects.select_for_update().filter(
        channel=channel, source_platform="buzz", destination_platform="slack",
        source_channel_id=row.source_channel_id, source_message_id=row.source_message_id,
    ).first()
    if link is None or link.destination_channel_id != channel.slack_channel_id:
        raise MemberDeliveryUnavailable("target_mapping_changed")
    poster = (link.destination_payload or {}).get("poster")
    if actor["kind"] == "member":
        current_owner = member._poster_owner(poster)
        if current_owner is None or current_owner != member._poster_owner(actor):
            raise MemberDeliveryUnavailable("original_member_poster_changed")
    elif not _same_bot_owner(poster, actor):
        raise MemberDeliveryUnavailable("original_bot_poster_changed")
    target = (link.destination_payload or {}).get("message_id") if row.delivery_type == "reaction_remove" else link.destination_message_id
    if target != wire["target"]:
        raise MemberDeliveryUnavailable("target_mapping_changed")
    if row.delivery_type == "reaction_remove" and CommunityBridgeMessageLink.objects.filter(
        channel=channel, source_platform="buzz", destination_platform="slack",
        source_deleted_at__isnull=True, created_at__gt=row.created_at,
        destination_payload__message_id=wire["target"], destination_payload__reaction=wire["reaction"],
        destination_payload__poster__slack_user_id=actor["slack_user_id"],
    ).exists():
        raise MemberDeliveryUnavailable("reaction_remove_superseded")


def execute(delivery):
    """Execute one fixed Community actor; ambiguous writes only reconcile."""
    original, saved, delivery = _stamp_identity(delivery)
    prior_attempt = (delivery.get("payload") or {}).get(attempts.ATTEMPT_KEY)
    real_attempt = (isinstance(prior_attempt, dict) and prior_attempt.get("status") in {"armed", "uncertain", "acknowledged"}
                    and saved and prior_attempt.get("command_hash") == attempts.command_hash(saved))
    if delivery["delivery_type"] in member.CONTENT and not admitted(original) and not real_attempt:
        raise member.MemberDeliveryDeferred(60)
    authority, actor = _choose(delivery, original, saved)
    command = saved or None
    attempt = None
    client = None
    receipt = None
    succeeded = False
    reconcile = False
    provider_error = None
    implementation = sys.modules[__name__]
    try:
        if command is None:
            with _fence(delivery, original, authority, actor) as (row, channel, user, client):
                info = client.verify()
                wire = (member._wire(row, channel, actor, client, info, identity_key=IDENTITY) if authority is not None
                        else _bot_wire(row, channel, actor, client, info, user.full_name or "MLAI member"))
                command = {"version": 1, "actor": actor, "mapping": _identity(channel), "wire": wire}
                guard_delivery(row)
                existing = (row.payload or {}).get(MEMBER_STAMP)
                if existing is not None and existing != command:
                    raise MemberDeliveryUnavailable("immutable_community_command_changed")
                row.payload = {**row.payload, MEMBER_STAMP: command}
                row.save(update_fields=("payload", "updated_at"))
        _same(command, original, actor)
        attempt, reconcile = attempts.arm(delivery, original, command, implementation=implementation)
        with _fence(delivery, original, authority, actor, read_reconcile=reconcile) as (row, channel, user, client):
            if (row.payload or {}).get(MEMBER_STAMP) != command:
                raise MemberDeliveryUnavailable("immutable_community_command_changed")
            info = client.verify()
            wire = command["wire"]
            if authority is not None and not reconcile:
                member._check_frozen_broadcast(row, wire, actor, client, info)
            elif not reconcile:
                _check_bot_broadcast(row, wire, actor)
            _validate_target(row, channel, actor, wire)
            outcome = "accepted"
            if reconcile:
                response, outcome = attempts.reconcile(client, row, channel, actor, wire, attempt)
            else:
                try:
                    response = client.call(wire["method"], **wire["kwargs"])
                except dm.SlackApiError as error:
                    code = str(error.response.get("error") or "")
                    if (row.delivery_type == "reaction_add" and code == "already_reacted"
                            or row.delivery_type == "reaction_remove" and code in {"no_reaction", "message_not_found"}
                            or row.delivery_type == "delete" and code == "message_not_found"):
                        outcome, response = code, None
                    else:
                        raise
            cached = attempt.get("receipt") if reconcile else None
            if attempts.response_from_receipt(cached, actor, wire, row.delivery_type) is not None:
                receipt = dict(cached)
            else:
                receipt = attempts.receipt(response, channel, actor, wire, row.delivery_type, outcome=outcome)
            if receipt is None or receipt["actor_verified"] is not True:
                raise member.MemberDeliveryDeferred(60)
            guard_delivery(row)
            row.payload = {**row.payload, attempts.ATTEMPT_KEY: {**attempt, "status": "acknowledged", "receipt": receipt}}
            row.save(update_fields=("payload", "updated_at"))
            member._finish_slack(row, channel, actor, wire, response, outcome=outcome,
                                 verified_receipt=receipt)
        succeeded = True
        from .observability import send
        send(original, actor, delivery["id"], "settled", reconciled=reconcile)
    except member._SourceClosed as error:
        from .member_channels import mark_source_reduction
        if error.observed_at is not None:
            mark_source_reduction(original.slack_workspace_id, original.slack_channel_id,
                                  reason=error.reason, event_observed_at=error.observed_at)
        raise member.MemberDeliveryDeferred(60) from None
    except dm.SlackApiError as error:
        provider_error = error
        if authority is not None:
            from integrations.models import SlackDmMirrorGrant

            grant = SlackDmMirrorGrant.objects.filter(pk=authority.grant_id).first()
            if grant is not None:
                access.record_reader_failure(grant, error, channel_id=original.slack_channel_id,
                                             authority=authority, purpose="write",
                                             observed_at=getattr(error, "member_delivery_observed_at", None))
        raise member.MemberDeliveryRetry("community_provider_uncertain") from None
    finally:
        if attempt is not None and not succeeded:
            started = client is not None and client.mutation_started
            rejected = isinstance(provider_error, dm.SlackApiError) and str(provider_error.response.get("error") or "") in {
                "invalid_auth", "not_authed", "token_revoked", "token_expired", "not_in_channel", "channel_not_found",
                "missing_scope", "is_archived", "restricted_action", "ratelimited"}
            status = "not_sent" if not started or rejected else "uncertain"
            if reconcile and attempt["status"] != "not_sent":
                status = "uncertain"
            attempts.record(delivery["id"], command, attempt, status, receipt, implementation=implementation)
        if not succeeded:
            from .observability import send_failure
            send_failure(original, actor, delivery["id"], sys.exc_info()[1])
