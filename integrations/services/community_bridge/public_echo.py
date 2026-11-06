"""Exact public member echoes shared by callbacks, history and repair ingress.

Only server-owned outgoing stamps and message links are evidence. No provider
lookup or membership inference occurs under the ingress Channel transaction.
"""

from datetime import timedelta

from django.utils import timezone
from django.db.models import Q
from .member_attempts import ATTEMPT_KEY, command_hash
from integrations.models import CommunityBridgeDelivery, CommunityBridgeMessageLink
from .member_delivery import MEMBER_STAMP, text_hash

COMMUNITY_STAMP = "_community_delivery_v1"


class PublicReactionSuperseded(RuntimeError):
    """A newer signed observation makes this queued reaction effect obsolete."""


def _command(payload):
    return (payload or {}).get(MEMBER_STAMP) or (payload or {}).get(COMMUNITY_STAMP)


def _command_lookup(suffix, value):
    return (Q(**{f"payload__{MEMBER_STAMP}__{suffix}": value})
            | Q(**{f"payload__{COMMUNITY_STAMP}__{suffix}": value}))


def is_public_slack_echo(channel, event, raw):
    if not isinstance(event, dict) or channel.slack_visibility != "public":
        return False
    if raw.get("team_id") not in (None, "", channel.slack_workspace_id):
        return False
    operation = event.get("delivery_type")
    actor = str(event.get("source_author_id") or "")
    if not actor:
        return False
    target = str(
        (event.get("metadata") or {}).get("slack_message_id")
        or event.get("source_message_id")
        or ""
    )
    metadata = event.get("metadata") or {}
    links = CommunityBridgeMessageLink.objects.filter(
        channel=channel,
        source_platform="buzz",
        destination_platform="slack",
        destination_channel_id=channel.slack_channel_id,
        destination_message_id=target,
    )
    for link in links:
        destination = link.destination_payload or {}
        poster = destination.get("poster") or {}
        if (
            poster.get("kind") != "member"
            or poster.get("slack_user_id") != actor
            or poster.get("workspace_id") != channel.slack_workspace_id
        ):
            continue
        if operation == "create":
            return True
        if operation == "edit" and destination.get("text_hash") == text_hash(
            metadata.get("slack_raw_text", event.get("text", ""))
        ):
            return True  # unchanged-text unfurl, not a different manual edit
    rows = CommunityBridgeDelivery.objects.filter(
        channel=channel,
        source_platform="buzz",
        target_platform="slack",
        delivery_type=operation,
    ).filter(_command_lookup("actor__slack_user_id", actor))
    if operation == "create":
        client_id = metadata.get("slack_client_msg_id")
        if not isinstance(client_id, str) or not client_id:
            return False
        rows = rows.filter(_command_lookup("wire__kwargs__client_msg_id", client_id))
    else:
        rows = rows.filter(_command_lookup("wire__kwargs__channel", channel.slack_channel_id))
        if operation in {"edit", "delete"}:
            rows = rows.filter(_command_lookup("wire__kwargs__ts", target))
        else:
            rows = rows.filter(_command_lookup("wire__kwargs__timestamp", target))
    if operation != "create":
        rows = rows.filter(
            ~Q(status="completed")
            | Q(completed_at__gte=timezone.now() - timedelta(hours=1))
        )
    for row in rows.iterator(chunk_size=100):
        attempt = (row.payload or {}).get(ATTEMPT_KEY)
        if not isinstance(attempt, dict) or attempt.get("status") not in {
            "armed",
            "uncertain",
            "acknowledged",
        }:
            continue
        command = _command(row.payload)
        if not isinstance(command, dict):
            continue
        if attempt.get("command_hash") != command_hash(command):
            continue
        bound, wire = command.get("actor"), command.get("wire")
        if not isinstance(bound, dict) or not isinstance(wire, dict):
            continue
        kwargs = wire.get("kwargs")
        if (
            not isinstance(kwargs, dict)
            or bound.get("kind") != "member"
            or bound.get("workspace_id") != channel.slack_workspace_id
            or bound.get("slack_user_id") != actor
            or kwargs.get("channel") != channel.slack_channel_id
        ):
            continue
        # A failed/dead/processing attempt may have been accepted before a
        # timeout. Preserve its exact unresolved identity until normal retention
        # removes the outbox; only completed mutation windows are time bounded.
        # Completed edits are current only through the exact link fast path
        # above. An obsolete edit A must not hide a manual return to A after B.
        if operation == "edit" and row.status == "completed":
            continue
        if operation in {"create", "edit"} and wire.get("text_hash") != text_hash(
            metadata.get("slack_raw_text", event.get("text", ""))
        ):
            continue
        if operation in {"reaction_add", "reaction_remove"}:
            if not _reaction_effect_current(row, wire, actor, raw):
                continue
            from integrations.services.slack_emoji import (
                emoji_to_slack_reaction,
                slack_reaction_to_emoji,
            )

            if emoji_to_slack_reaction(
                slack_reaction_to_emoji(kwargs.get("name"))
            ) != emoji_to_slack_reaction(
                slack_reaction_to_emoji(metadata.get("slack_reaction"))
            ):
                continue
        if operation == "create" and kwargs.get("client_msg_id") != metadata.get(
            "slack_client_msg_id"
        ):
            continue
        return True
    return False


def _reaction_effect_current(row, wire, actor, raw):
    """An opposite observed effect invalidates an older mutation fingerprint.

    Reactions have no Slack per-mutation client ID. A completed add/remove
    fingerprint alone therefore cannot identify a later manual action. Native
    links plus later opposite callbacks/attempts are conservative invalidators;
    a delayed observation can cause a visible extra import, never erase a new
    manual reaction by treating it as an old echo.
    """
    from datetime import datetime
    from integrations.models import CommunityBridgeReceipt
    from integrations.services.slack_emoji import (
        emoji_to_slack_reaction,
        slack_reaction_to_emoji,
    )

    operation = row.delivery_type
    opposite = "reaction_remove" if operation == "reaction_add" else "reaction_add"
    link = CommunityBridgeMessageLink.objects.filter(
        channel_id=row.channel_id,
        source_platform="buzz",
        source_channel_id=row.source_channel_id,
        source_message_id=row.source_message_id,
        destination_platform="slack",
    ).first()
    if row.status == "completed":
        if link is None:
            return False
        deleted = (
            link.source_deleted_at is not None
            or link.destination_deleted_at is not None
        )
        if (operation == "reaction_add" and deleted) or (
            operation == "reaction_remove" and not deleted
        ):
            return False
    reaction = emoji_to_slack_reaction(
        slack_reaction_to_emoji(wire["kwargs"].get("name"))
    )
    target = wire["kwargs"].get("timestamp")
    attempt = (row.payload or {}).get(ATTEMPT_KEY) or {}
    try:
        started = datetime.fromisoformat(attempt["started_at"])
    except (KeyError, ValueError, TypeError):
        return False
    if timezone.is_naive(started):
        return False
    later = CommunityBridgeDelivery.objects.filter(
        channel_id=row.channel_id,
        source_platform="buzz",
        target_platform="slack",
        delivery_type=opposite,
        created_at__gt=row.created_at,
    ).filter(_command_lookup("actor__slack_user_id", actor),
             _command_lookup("wire__kwargs__timestamp", target))
    for candidate in later.iterator(chunk_size=100):
        command = _command(candidate.payload) or {}
        later_attempt = (candidate.payload or {}).get(ATTEMPT_KEY) or {}
        other = (command.get("wire") or {}).get("kwargs") or {}
        if (
            later_attempt.get("status") in {"armed", "uncertain", "acknowledged"}
            and later_attempt.get("command_hash") == command_hash(command)
            and emoji_to_slack_reaction(slack_reaction_to_emoji(other.get("name")))
            == reaction
        ):
            return False
    receipts = CommunityBridgeReceipt.objects.filter(
        channel_id=row.channel_id,
        platform="slack",
        created_at__gte=started,
        event_type="reaction_removed"
        if operation == "reaction_add"
        else "reaction_added",
        payload__event__user=actor,
        payload__event__item__ts=target,
    )
    for receipt in receipts.iterator(chunk_size=100):
        event = (receipt.payload or {}).get("event") or {}
        if (
            emoji_to_slack_reaction(slack_reaction_to_emoji(event.get("reaction")))
            == reaction
        ):
            return False
    return True


def _reaction_observation(channel, raw):
    """Closed authenticated public-reaction identity; no callback ordering guess."""
    from decimal import Decimal
    import re
    from django.conf import settings
    from integrations.services.slack_emoji import (
        emoji_to_slack_reaction,
        slack_reaction_to_emoji,
    )

    if not isinstance(raw, dict) or channel.audience not in {"members", "community"}:
        return None
    if channel.audience == "community" and not CommunityBridgeDelivery.objects.filter(
        channel=channel, payload___community_delivery_v1__actor__kind="member",
    ).exists():
        return None
    event = raw.get("event")
    if not isinstance(event, dict) or event.get("type") not in {
        "reaction_added",
        "reaction_removed",
    }:
        return None
    item = event.get("item")
    if not isinstance(item, dict) or item.get("type") != "message":
        return None
    apps = {
        value
        for value in (
            getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID", ""),
            getattr(settings, "MESSAGE_SYNC_SLACK_USER_APP_ID", ""),
        )
        if value
    }
    if (
        not isinstance(raw.get("api_app_id"), str)
        or raw["api_app_id"] not in apps
        or raw.get("team_id") != channel.slack_workspace_id
        or item.get("channel") != channel.slack_channel_id
        or channel.slack_visibility != "public"
    ):
        return None
    if any(
        value.get(key)
        for value in (raw, event, item)
        for key in (
            "is_private",
            "is_im",
            "is_mpim",
            "is_group",
            "is_shared",
            "is_ext_shared",
            "is_org_shared",
            "is_pending_ext_shared",
            "pending_shared",
        )
    ):
        return None
    revision, target, actor = event.get("event_ts"), item.get("ts"), event.get("user")
    timestamp = re.compile(r"^[0-9]{1,20}\.[0-9]{1,9}$")
    if (
        not isinstance(revision, str)
        or not timestamp.fullmatch(revision)
        or not isinstance(target, str)
        or not timestamp.fullmatch(target)
        or not isinstance(actor, str)
        or not re.fullmatch(r"[UW][A-Z0-9]{1,23}", actor)
    ):
        return None
    reaction = event.get("reaction")
    if not isinstance(reaction, str):
        return None
    reaction = emoji_to_slack_reaction(slack_reaction_to_emoji(reaction))
    if not reaction:
        return None
    return actor, target, reaction, Decimal(revision)


def newer_reaction_observed(channel, raw):
    """Use every genuine receipt, including echo-ignored ones, as revision proof.

    Call under the exact Channel lock at receipt acceptance and before adapter
    mutation. Same-revision receipts remain idempotent; only strictly newer
    source revisions supersede an effect. No native-event deletion is granted.
    """
    from integrations.models import CommunityBridgeReceipt

    current = _reaction_observation(channel, raw)
    if current is None:
        return False
    actor, target, reaction, revision = current
    rows = CommunityBridgeReceipt.objects.filter(
        channel=channel,
        platform="slack",
        source_channel_id=channel.slack_channel_id,
        event_type__in=("reaction_added", "reaction_removed"),
        payload__team_id=channel.slack_workspace_id,
        payload__event__user=actor,
        payload__event__item__ts=target,
    )
    if channel.audience == "members":
        rows = rows.filter(receipt_key__startswith=f"slack:{channel.slack_workspace_id}:{channel.slack_channel_id}:{target}:reaction:")
    # Community's retained bot lane uses event-ID keys after rollout narrowing.
    # Exact app/T/C/U/target/canonical-reaction evidence below remains mandatory;
    # a legacy key must not erase newer source causality during rollback.
    for receipt in rows.iterator(chunk_size=100):
        observation = _reaction_observation(channel, receipt.payload)
        if (
            observation is not None
            and observation[:3] == (actor, target, reaction)
            and observation[3] > revision
        ):
            return True
    return False
