"""Server-only durable network attempts and exact read reconciliation.

An armed attempt is never evidence of rejection. Result CAS does not require a
live old lease: it preserves only its exact attempt/command evidence, never a
lease, current settlement, wire command or actor. No credential/body is stored.
"""

import hashlib
import json
import re
import time
import uuid
from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from integrations.models import (
    BridgeSyncState,
    CommunityBridgeChannel,
    CommunityBridgeDelivery,
)
from integrations.services.message_sync.delivery import guard_delivery
from integrations.services.message_sync.scheduler import BudgetDeferred

ATTEMPT_KEY = "_member_network_attempt_v1"
CONFLICT_KEY = "_member_network_conflicts_v1"
APP_ID = re.compile(r"^A[A-Z0-9]{1,99}$")


def _module():
    from . import member_delivery

    return member_delivery


def command_hash(command):
    return hashlib.sha256(
        json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def arm(delivery, original, command, *, implementation=None):
    m = implementation or _module()
    with transaction.atomic():
        _, channel, row = m._suffix(delivery["id"], m._identity(original))
        if (row.payload or {}).get(m.MEMBER_STAMP) != command:
            raise m.MemberDeliveryUnavailable("immutable_member_command_changed")
        previous = (row.payload or {}).get(ATTEMPT_KEY)
        digest = command_hash(command)
        if previous is not None:
            if (
                not isinstance(previous, dict)
                or previous.get("command_hash") != digest
                or previous.get("status")
                not in {"armed", "uncertain", "acknowledged", "not_sent"}
            ):
                raise m.MemberDeliveryUnavailable("invalid_network_attempt")
            if previous["status"] != "not_sent":
                return dict(previous), True
        attempt = {
            "version": 1,
            "id": str(uuid.uuid4()),
            "command_hash": digest,
            "lease_token": str(row.lease_token),
            "started_at": timezone.now().isoformat(),
            "status": "armed",
        }
        row.payload = {**row.payload, ATTEMPT_KEY: attempt}
        row.save(update_fields=("payload", "updated_at"))
        return attempt, False


def record(delivery_id, command, attempt, status, receipt=None, *, implementation=None):
    """Evidence-only CAS; a late response cannot overwrite a newer result."""
    m = implementation or _module()
    if status not in {"not_sent", "uncertain", "acknowledged"}:
        raise ValueError("invalid_attempt_status")
    with transaction.atomic():
        hint = CommunityBridgeDelivery.objects.only("channel_id").get(pk=delivery_id)
        BridgeSyncState.objects.select_for_update().filter(
            public_channel_id=hint.channel_id
        ).first()
        CommunityBridgeChannel.objects.select_for_update().get(pk=hint.channel_id)
        row = CommunityBridgeDelivery.objects.select_for_update().get(pk=delivery_id)
        payload = dict(row.payload or {})
        current = payload.get(ATTEMPT_KEY)
        exact = bool(
            isinstance(current, dict)
            and current.get("id") == attempt.get("id")
            and current.get("command_hash") == command_hash(command)
            and payload.get(m.MEMBER_STAMP) == command
        )
        if (
            not exact
            or current.get("status") == "acknowledged"
            and (status != "acknowledged" or receipt != current.get("receipt"))
        ):
            # Bounded closed conflict proof, separate from current outcome.
            conflicts = payload.get(CONFLICT_KEY)
            conflicts = list(conflicts) if isinstance(conflicts, list) else []
            fact = {
                "attempt_id": attempt["id"],
                "command_hash": command_hash(command),
                "status": status,
            }
            if receipt is not None:
                fact["receipt"] = receipt
            payload[CONFLICT_KEY] = (conflicts + [fact])[-4:]
            row.payload = payload
            row.save(update_fields=("payload", "updated_at"))
            return False
        if current.get("status") == "acknowledged":
            return True  # exact idempotent evidence, never rewrite the result
        result = {**current, "status": status}
        if receipt is not None:
            result["receipt"] = receipt
        elif current.get("receipt") is not None:
            result["receipt"] = current["receipt"]
        row.payload = {**payload, ATTEMPT_KEY: result}
        row.save(update_fields=("payload", "updated_at"))
        return True


def actor_proven(message, actor, response=None):
    """A legitimate API user post may carry its originating app's markers."""
    if not isinstance(message, dict) or message.get("user") != actor["slack_user_id"]:
        return False
    bot = actor.get("kind") == "bot"
    expected = str(getattr(settings, "MESSAGE_SYNC_SLACK_APP_ID" if bot else "MESSAGE_SYNC_SLACK_USER_APP_ID", "") or "")
    if bot and (actor.get("slack_user_id") != getattr(settings, "SLACK_BRIDGE_BOT_USER_ID", "")
                or not any(key in message for key in ("bot_id", "bot_profile"))):
        return False
    fields = []
    marked = False
    for item in (message, response):
        if item is None:
            continue
        if not isinstance(item, dict):
            item = getattr(item, "data", None)
        if not isinstance(item, dict):
            return False
        if "app_id" in item:
            fields.append(item["app_id"])
        if "bot_id" in item:
            bot_id = item["bot_id"]
            if not isinstance(bot_id, str) or not re.fullmatch(
                r"B[A-Z0-9]{1,99}", bot_id
            ):
                return False
            marked = True
        if "bot_profile" in item:
            marked = True
            profile = item["bot_profile"]
            if not isinstance(profile, dict) or "app_id" not in profile:
                return False
            fields.append(profile["app_id"])
    if fields or marked:
        if not APP_ID.fullmatch(expected) or not fields:
            return False
        if any(not isinstance(value, str) or value != expected for value in fields):
            return False
    return True


def receipt(response, channel, actor, wire, operation, *, outcome="accepted"):
    """Retain only bounded response facts; unknown actor remains uncertain."""
    m = _module()
    kwargs = wire["kwargs"]
    if outcome != "accepted":
        return {
            "version": 1,
            "channel": channel.slack_channel_id,
            "ts": str(kwargs.get("ts") or kwargs.get("timestamp") or ""),
            "operation": operation,
            "actor_verified": True,
            "outcome": outcome,
            "text_hash": wire["text_hash"],
        }
    if not hasattr(response, "get") or response.get("ok") is not True:
        return None
    target = str(response.get("ts") or kwargs.get("timestamp") or "")
    actual_channel = response.get(
        "channel", channel.slack_channel_id if operation.startswith("reaction") else ""
    )
    if (
        not isinstance(actual_channel, str)
        or not re.fullmatch(r"C[A-Z0-9]{1,99}", actual_channel)
        or not m.TS.fullmatch(target)
    ):
        return None
    verified = actual_channel == channel.slack_channel_id
    if operation in {"edit", "delete"}:
        verified = verified and target == kwargs["ts"]
    message = response.get("message")
    if operation == "create":
        verified = bool(
            verified
            and actor_proven(message, actor, response)
            and message.get("client_msg_id") in (None, "", kwargs["client_msg_id"])
            and message.get("ts", target) == target
            and (
                "text" not in message
                or isinstance(message["text"], str)
                and m.text_hash(message["text"]) == wire["text_hash"]
            )
        )
    fact = {
        "version": 1,
        "channel": actual_channel,
        "ts": target,
        "operation": operation,
        "actor_verified": bool(verified),
        "outcome": "accepted",
        "text_hash": wire["text_hash"],
    }
    if operation == "create":
        fact["client_msg_id"] = kwargs["client_msg_id"]
    return fact


def response_from_receipt(fact, actor, wire, operation):
    m = _module()
    allowed = {
        "version",
        "channel",
        "ts",
        "operation",
        "actor_verified",
        "outcome",
        "text_hash",
    }
    if operation == "create":
        allowed.add("client_msg_id")
    outcomes = {"accepted"}
    if operation == "reaction_add":
        outcomes.add("already_reacted")
    elif operation == "reaction_remove":
        outcomes.update({"no_reaction", "message_not_found"})
    elif operation == "delete":
        outcomes.add("message_not_found")
    if (
        not isinstance(fact, dict)
        or set(fact) != allowed
        or type(fact.get("version")) is not int
        or fact["version"] != 1
        or fact.get("actor_verified") is not True
        or fact.get("channel") != wire["kwargs"]["channel"]
        or fact.get("operation") != operation
        or not isinstance(fact.get("outcome"), str)
        or fact["outcome"] not in outcomes
        or fact.get("text_hash") != wire["text_hash"]
        or not isinstance(fact.get("ts"), str)
        or not m.TS.fullmatch(fact["ts"])
    ):
        return None
    if (
        operation == "create"
        and fact.get("client_msg_id") != wire["kwargs"]["client_msg_id"]
    ):
        return None
    if operation in {"edit", "delete"} and fact["ts"] != wire["kwargs"]["ts"]:
        return None
    if operation.startswith("reaction") and fact["ts"] != wire["kwargs"]["timestamp"]:
        return None
    return {
        "ok": True,
        "channel": fact["channel"],
        "ts": fact["ts"],
        "message": {
            "user": actor["slack_user_id"],
            "client_msg_id": wire["kwargs"].get("client_msg_id"),
        },
    }


def _post_read_info(client):
    try:
        client.verify()
    except BudgetDeferred as error:
        if error.before_request_method != "conversations.info" or error.retry_after > 3:
            raise
        guard_delivery(client.row)
        time.sleep(error.retry_after)
        guard_delivery(client.row)
        client.verify()


def reconcile(client, row, channel, actor, wire, attempt):
    """Read-only exact completion proof; absence is never permission to resend."""
    m = _module()
    operation = row.delivery_type
    cached = attempt.get("receipt")
    response = response_from_receipt(cached, actor, wire, operation)
    if response is not None:
        return response, cached.get("outcome", "accepted")
    options = {"channel": channel.slack_channel_id, "limit": 100}
    if wire.get("parent"):
        method = "conversations_replies"
        options["ts"] = wire["parent"]
    else:
        method = "conversations_history"
        if operation == "create":
            try:
                started = datetime.fromisoformat(attempt["started_at"])
            except (ValueError, TypeError, KeyError):
                raise m.MemberDeliveryUnavailable("invalid_network_attempt")
            if started.tzinfo is None:
                raise m.MemberDeliveryUnavailable("invalid_network_attempt")
            options["oldest"] = str(max(0, started.timestamp() - 10))
        else:
            options.update(
                oldest=wire["kwargs"].get("ts") or wire["kwargs"].get("timestamp"),
                latest=wire["kwargs"].get("ts") or wire["kwargs"].get("timestamp"),
                inclusive=True,
            )
    response = client.call(method, **options)
    if (
        not hasattr(response, "get")
        or response.get("ok") is not True
        or not isinstance(response.get("messages"), list)
    ):
        raise m.MemberDeliveryDeferred(60)
    # Normally no privacy metadata is returned; reject any contradictory hints
    # and then independently prove the source again before using these bodies.
    for item in (response, response.get("channel")):
        if isinstance(item, dict) and any(
            item.get(key)
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
            raise m.MemberDeliveryDeferred(60)
    if "channel" in response:
        returned = response["channel"]
        if isinstance(returned, dict):
            returned = returned.get("id")
        if not isinstance(returned, str) or returned != channel.slack_channel_id:
            raise m.MemberDeliveryDeferred(60)
    _post_read_info(client)
    messages = response["messages"]
    matches = []
    for item in messages:
        if not isinstance(item, dict):
            raise m.MemberDeliveryDeferred(60)
        if operation == "create":
            if (
                item.get("client_msg_id") == wire["kwargs"]["client_msg_id"]
                and actor_proven(item, actor)
                and isinstance(item.get("text"), str)
                and m.text_hash(item["text"]) == wire["text_hash"]
                and m.TS.fullmatch(str(item.get("ts") or ""))
            ):
                matches.append(item)
        elif item.get("ts") == (
            wire["kwargs"].get("ts") or wire["kwargs"].get("timestamp")
        ):
            if (
                operation == "edit"
                and actor_proven(item, actor)
                and isinstance(item.get("text"), str)
                and m.text_hash(item["text"]) == wire["text_hash"]
            ):
                matches.append(item)
            elif operation == "reaction_add":
                from integrations.services.slack_emoji import (
                    emoji_to_slack_reaction,
                    slack_reaction_to_emoji,
                )

                reactions = item.get("reactions")
                if isinstance(reactions, list) and any(
                    isinstance(reaction, dict)
                    and isinstance(reaction.get("name"), str)
                    and emoji_to_slack_reaction(
                        slack_reaction_to_emoji(reaction["name"])
                    )
                    == emoji_to_slack_reaction(slack_reaction_to_emoji(wire["reaction"]))
                    and isinstance(reaction.get("users"), list)
                    and actor["slack_user_id"] in reaction["users"]
                    for reaction in reactions
                ):
                    matches.append(item)
    if len(matches) != 1:
        # Delete/remove absence, pagination gaps and no matching create remain
        # explicitly unresolved. No negative scan is a strong dedup guarantee.
        raise m.MemberDeliveryDeferred(60)
    item = matches[0]
    return {
        "ok": True,
        "channel": channel.slack_channel_id,
        "ts": item["ts"],
        "message": item,
    }, "accepted"
