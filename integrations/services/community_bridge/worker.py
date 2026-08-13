import asyncio
import logging
import uuid
from typing import Any, Optional

import discord
from discord.ext import tasks
from django.conf import settings
from slack_sdk.errors import SlackApiError

from integrations.models import CommunityBridgeDeliveryType, CommunityBridgePlatform
from integrations.services.community_bridge.buzz import BuzzBridgeClient
from integrations.services.community_bridge.formatting import (
    build_mirrored_text,
    emoji_to_slack_reaction,
    normalize_discord_attachments,
    reaction_object_id,
    sanitize_discord_text,
)
from integrations.services.community_bridge.identity import (
    verified_identity_for_buzz,
    verified_identity_for_slack,
)
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.community_bridge.store import (
    claim_ready_deliveries,
    complete_create_delivery,
    complete_delivery,
    ingest_discord_event,
    mark_delivery_retry,
    mark_link_deleted,
    reset_stale_processing_deliveries,
    resolve_mapped_message,
    resolve_message_link,
)


logger = logging.getLogger(__name__)


class CommunityBridgeDiscordClient(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(intents=intents, max_messages=5000)
        self._delivery_loop_started = False

    async def setup_hook(self) -> None:
        await asyncio.to_thread(reset_stale_processing_deliveries)
        if not self._delivery_loop_started:
            self.delivery_loop.start()
            self._delivery_loop_started = True

    async def on_ready(self) -> None:
        logger.info("community_bridge_discord_ready user=%s", getattr(self.user, "id", ""))

    async def on_message(self, message: discord.Message) -> None:
        if not self._should_process_message(message):
            return
        normalized = {
            "delivery_type": CommunityBridgeDeliveryType.CREATE,
            "source_channel_id": str(message.channel.id),
            "source_message_id": str(message.id),
            "source_parent_message_id": self._discord_parent_message_id(message),
            "source_author_id": str(message.author.id),
            "source_author_display_name": str(message.author.display_name or message.author.name or message.author.id),
            "text": sanitize_discord_text(message.content or ""),
            "attachments": normalize_discord_attachments(message.attachments),
        }
        raw_payload = {
            "message_id": str(message.id),
            "channel_id": str(message.channel.id),
            "guild_id": str(message.guild.id) if message.guild else "",
            "author_id": str(message.author.id),
            "edited_at": message.edited_at.isoformat() if message.edited_at else "",
        }
        await asyncio.to_thread(
            ingest_discord_event,
            receipt_key=f"message_create:{message.id}",
            source_channel_id=str(message.channel.id),
            event_type="message_create",
            normalized_event=normalized,
            raw_payload=raw_payload,
        )

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if not self._should_process_message(after):
            return
        if before.content == after.content and list(before.attachments) == list(after.attachments):
            return
        edit_marker = after.edited_at.isoformat() if after.edited_at else f"cache-{after.id}"
        normalized = {
            "delivery_type": CommunityBridgeDeliveryType.EDIT,
            "source_channel_id": str(after.channel.id),
            "source_message_id": str(after.id),
            "source_parent_message_id": self._discord_parent_message_id(after),
            "source_author_id": str(after.author.id),
            "source_author_display_name": str(after.author.display_name or after.author.name or after.author.id),
            "text": sanitize_discord_text(after.content or ""),
            "attachments": normalize_discord_attachments(after.attachments),
        }
        raw_payload = {
            "message_id": str(after.id),
            "channel_id": str(after.channel.id),
            "guild_id": str(after.guild.id) if after.guild else "",
            "author_id": str(after.author.id),
            "edited_at": edit_marker,
        }
        await asyncio.to_thread(
            ingest_discord_event,
            receipt_key=f"message_update:{after.id}:{edit_marker}",
            source_channel_id=str(after.channel.id),
            event_type="message_update",
            normalized_event=normalized,
            raw_payload=raw_payload,
        )

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        if payload.guild_id is None:
            return
        await asyncio.to_thread(
            ingest_discord_event,
            receipt_key=f"message_delete:{payload.message_id}",
            source_channel_id=str(payload.channel_id),
            event_type="message_delete",
            normalized_event={
                "delivery_type": CommunityBridgeDeliveryType.DELETE,
                "source_channel_id": str(payload.channel_id),
                "source_message_id": str(payload.message_id),
                "source_parent_message_id": "",
                "source_author_id": "",
                "source_author_display_name": "",
                "text": "",
                "attachments": [],
            },
            raw_payload={
                "message_id": str(payload.message_id),
                "channel_id": str(payload.channel_id),
                "guild_id": str(payload.guild_id),
            },
        )

    @tasks.loop(seconds=1.0)
    async def delivery_loop(self) -> None:
        await self.process_pending_deliveries_once(limit=10)

    async def process_pending_deliveries_once(self, limit: int = 10) -> None:
        deliveries = await asyncio.to_thread(claim_ready_deliveries, limit)
        for delivery in deliveries:
            try:
                await self._process_delivery(delivery)
            except Exception as exc:
                logger.exception(
                    "community_bridge_delivery_failed delivery_id=%s target_platform=%s",
                    delivery["id"],
                    delivery["target_platform"],
                )
                await asyncio.to_thread(
                    mark_delivery_retry,
                    delivery_id=delivery["id"],
                    error_text=f"{exc.__class__.__name__}: {exc}",
                    permanent=bool(getattr(exc, "permanent", False)),
                )

    async def _process_delivery(self, delivery: dict) -> None:
        if delivery["target_platform"] == CommunityBridgePlatform.DISCORD:
            await self._deliver_to_discord(delivery)
            return
        if delivery["target_platform"] == CommunityBridgePlatform.SLACK:
            await self._deliver_to_slack(delivery)
            return
        if delivery["target_platform"] == CommunityBridgePlatform.BUZZ:
            await self._deliver_to_buzz(delivery)
            return
        raise RuntimeError(f"No community bridge adapter for {delivery['target_platform']}")

    async def _deliver_to_discord(self, delivery: dict) -> None:
        target_channel = await self._get_channel_or_fetch(delivery["target_channel_id"])
        if target_channel is None:
            raise RuntimeError(f"Discord channel not found: {delivery['target_channel_id']}")

        payload = dict(delivery["payload"] or {})
        author_display_name = await self._resolve_author_display_name(
            payload,
            source_platform=delivery["source_platform"],
            channel=delivery.get("channel"),
        )
        body = await self._resolve_message_body(
            payload,
            source_platform=delivery["source_platform"],
        )
        content = build_mirrored_text(
            destination_platform=CommunityBridgePlatform.DISCORD,
            source_platform=delivery["source_platform"],
            author_display_name=author_display_name,
            body=body,
            attachments=payload.get("attachments") or [],
        )

        if delivery["delivery_type"] == CommunityBridgeDeliveryType.CREATE:
            parent_message_id = await self._resolve_parent_destination_message(delivery)
            message_reference = None
            if parent_message_id:
                message_reference = discord.MessageReference(
                    message_id=int(parent_message_id),
                    channel_id=int(delivery["target_channel_id"]),
                    fail_if_not_exists=False,
                )
            message = await target_channel.send(
                content=content,
                reference=message_reference,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await asyncio.to_thread(
                complete_create_delivery,
                delivery_id=delivery["id"],
                destination_message_id=str(message.id),
                destination_channel_id=str(message.channel.id),
                destination_parent_message_id=parent_message_id,
                destination_payload={"channel_id": str(message.channel.id), "message_id": str(message.id)},
            )
            return

        link = await asyncio.to_thread(
            resolve_message_link,
            source_platform=delivery["source_platform"],
            source_channel_id=delivery["source_channel_id"],
            source_message_id=delivery["source_message_id"],
            destination_platform=CommunityBridgePlatform.DISCORD,
        )
        if not link:
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

        message = target_channel.get_partial_message(int(link["destination_message_id"]))
        if delivery["delivery_type"] == CommunityBridgeDeliveryType.EDIT:
            try:
                await message.edit(content=content, allowed_mentions=discord.AllowedMentions.none())
            except discord.NotFound:
                await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
                return
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

        if delivery["delivery_type"] == CommunityBridgeDeliveryType.DELETE:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            await asyncio.to_thread(
                mark_link_deleted,
                source_platform=delivery["source_platform"],
                source_channel_id=delivery["source_channel_id"],
                source_message_id=delivery["source_message_id"],
                destination_platform=CommunityBridgePlatform.DISCORD,
            )
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

    async def _deliver_to_slack(self, delivery: dict) -> None:
        payload = dict(delivery["payload"] or {})
        if delivery["delivery_type"] in {
            CommunityBridgeDeliveryType.REACTION_ADD,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        }:
            await self._deliver_reaction_to_slack(delivery, payload)
            return
        author_display_name = await self._resolve_author_display_name(
            payload,
            source_platform=delivery["source_platform"],
            channel=delivery.get("channel"),
        )
        text = build_mirrored_text(
            destination_platform=CommunityBridgePlatform.SLACK,
            source_platform=delivery["source_platform"],
            author_display_name=author_display_name,
            body=str(payload.get("text") or ""),
            attachments=payload.get("attachments") or [],
        )

        if delivery["delivery_type"] == CommunityBridgeDeliveryType.CREATE:
            thread_ts = await self._resolve_parent_destination_message(delivery)
            response = await asyncio.to_thread(
                SlackBridgeClient.post_message,
                channel_id=delivery["target_channel_id"],
                text=text,
                thread_ts=thread_ts,
                client_msg_id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"mlai-community-bridge:{delivery['id']}",
                    )
                ),
            )
            await asyncio.to_thread(
                complete_create_delivery,
                delivery_id=delivery["id"],
                destination_message_id=str(response.get("message_id") or ""),
                destination_channel_id=str(response.get("channel") or delivery["target_channel_id"]),
                destination_parent_message_id=thread_ts,
                destination_payload=response,
            )
            return

        link = await asyncio.to_thread(
            resolve_message_link,
            source_platform=delivery["source_platform"],
            source_channel_id=delivery["source_channel_id"],
            source_message_id=delivery["source_message_id"],
            destination_platform=CommunityBridgePlatform.SLACK,
        )
        if not link:
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

        if delivery["delivery_type"] == CommunityBridgeDeliveryType.EDIT:
            try:
                await asyncio.to_thread(
                    SlackBridgeClient.update_message,
                    channel_id=link["destination_channel_id"],
                    message_id=link["destination_message_id"],
                    text=text,
                )
            except SlackApiError as exc:
                if exc.response.get("error") == "message_not_found":
                    await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
                    return
                raise
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

        if delivery["delivery_type"] == CommunityBridgeDeliveryType.DELETE:
            try:
                await asyncio.to_thread(
                    SlackBridgeClient.delete_message,
                    channel_id=link["destination_channel_id"],
                    message_id=link["destination_message_id"],
                )
            except SlackApiError as exc:
                if exc.response.get("error") != "message_not_found":
                    raise
            await asyncio.to_thread(
                mark_link_deleted,
                source_platform=delivery["source_platform"],
                source_channel_id=delivery["source_channel_id"],
                source_message_id=delivery["source_message_id"],
                destination_platform=CommunityBridgePlatform.SLACK,
            )
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

    async def _deliver_reaction_to_slack(self, delivery: dict, payload: dict) -> None:
        operation = delivery["delivery_type"]
        reaction = emoji_to_slack_reaction(str(payload.get("text") or ""))
        if not reaction:
            raise RuntimeError("reaction is not in the approved Slack bridge set")

        if operation == CommunityBridgeDeliveryType.REACTION_ADD:
            target_message_id = await self._resolve_parent_destination_message(delivery)
            if not target_message_id:
                await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
                return
            try:
                await asyncio.to_thread(
                    SlackBridgeClient.add_reaction,
                    channel_id=delivery["target_channel_id"],
                    message_id=target_message_id,
                    reaction=reaction,
                )
            except SlackApiError as exc:
                if exc.response.get("error") != "already_reacted":
                    raise
            destination_id = reaction_object_id(
                message_id=target_message_id,
                reaction=reaction,
                author_id=delivery["source_message_id"],
            )
            await asyncio.to_thread(
                complete_create_delivery,
                delivery_id=delivery["id"],
                destination_message_id=destination_id,
                destination_channel_id=delivery["target_channel_id"],
                destination_parent_message_id=target_message_id,
                destination_payload={
                    "channel_id": delivery["target_channel_id"],
                    "message_id": target_message_id,
                    "reaction": reaction,
                },
            )
            return

        link = await asyncio.to_thread(
            resolve_message_link,
            source_platform=delivery["source_platform"],
            source_channel_id=delivery["source_channel_id"],
            source_message_id=delivery["source_message_id"],
            destination_platform=CommunityBridgePlatform.SLACK,
        )
        if not link:
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return
        destination = dict(link.get("destination_payload") or {})
        try:
            await asyncio.to_thread(
                SlackBridgeClient.remove_reaction,
                channel_id=str(destination.get("channel_id") or link["destination_channel_id"]),
                message_id=str(destination.get("message_id") or ""),
                reaction=str(destination.get("reaction") or reaction),
            )
        except SlackApiError as exc:
            if exc.response.get("error") not in {"no_reaction", "message_not_found"}:
                raise
        await asyncio.to_thread(
            mark_link_deleted,
            source_platform=delivery["source_platform"],
            source_channel_id=delivery["source_channel_id"],
            source_message_id=delivery["source_message_id"],
            destination_platform=CommunityBridgePlatform.SLACK,
        )
        await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])

    async def _deliver_to_buzz(self, delivery: dict) -> None:
        payload = dict(delivery["payload"] or {})
        operation = delivery["delivery_type"]
        channel = dict(delivery.get("channel") or {})
        slack_workspace_id = str(channel.get("slack_workspace_id") or "").strip()
        source_author_id = str(payload.get("source_author_id") or "").strip()
        payload_metadata = dict(payload.get("metadata") or {})
        provenance_message_id = str(
            payload_metadata.get("slack_message_id")
            or delivery["source_message_id"]
        ).strip()
        identity = await asyncio.to_thread(
            verified_identity_for_slack,
            slack_workspace_id=slack_workspace_id,
            slack_user_id=source_author_id,
        )
        author_display_name = str(payload.get("source_author_display_name") or "").strip()
        author_avatar_url = str(payload.get("source_author_avatar_url") or "").strip()
        if (
            delivery["source_platform"] == CommunityBridgePlatform.SLACK
            and source_author_id
            and operation
            in {
                CommunityBridgeDeliveryType.CREATE,
                CommunityBridgeDeliveryType.EDIT,
            }
            and (not author_display_name or not author_avatar_url)
        ):
            slack_profile = await asyncio.to_thread(
                SlackBridgeClient.get_user_profile,
                source_author_id,
            )
            author_display_name = author_display_name or str(
                slack_profile.get("display_name") or ""
            ).strip()
            author_avatar_url = author_avatar_url or str(
                slack_profile.get("avatar_url") or ""
            ).strip()
        provenance = {
            "source_workspace_id": slack_workspace_id,
            "source_channel_id": delivery["source_channel_id"],
            "source_message_id": provenance_message_id,
            "source_author_id": source_author_id,
            "source_author_display_name": author_display_name,
            "source_author_avatar_url": author_avatar_url,
            "linked_pubkey": str((identity or {}).get("buzz_pubkey") or ""),
        }
        text = ""
        if operation not in {
            CommunityBridgeDeliveryType.DELETE,
            CommunityBridgeDeliveryType.REACTION_ADD,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        }:
            if not author_display_name:
                author_display_name = await self._resolve_author_display_name(
                    payload,
                    source_platform=delivery["source_platform"],
                    channel=channel,
                )
            body = await self._resolve_message_body(
                payload,
                source_platform=delivery["source_platform"],
            )
            text = build_mirrored_text(
                destination_platform=CommunityBridgePlatform.BUZZ,
                source_platform=delivery["source_platform"],
                author_display_name=author_display_name,
                body=body,
                attachments=payload.get("attachments") or [],
            )

        if operation in {
            CommunityBridgeDeliveryType.CREATE,
            CommunityBridgeDeliveryType.REACTION_ADD,
        }:
            parent_message_id = await self._resolve_parent_destination_message(delivery)
            if operation == CommunityBridgeDeliveryType.REACTION_ADD:
                if not parent_message_id:
                    await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
                    return
                text = str(payload.get("text") or "").strip()
            response = await asyncio.to_thread(
                BuzzBridgeClient.deliver,
                delivery_id=str(delivery["id"]),
                created_at=int(delivery["created_at"]),
                operation=operation,
                channel_id=delivery["target_channel_id"],
                text=text,
                parent_message_id=parent_message_id,
                target_message_id=(
                    parent_message_id
                    if operation == CommunityBridgeDeliveryType.REACTION_ADD
                    else ""
                ),
                **provenance,
            )
            await asyncio.to_thread(
                complete_create_delivery,
                delivery_id=delivery["id"],
                destination_message_id=response["message_id"],
                destination_channel_id=response["channel_id"],
                destination_parent_message_id=parent_message_id,
                destination_payload=response,
            )
            return

        link = await asyncio.to_thread(
            resolve_message_link,
            source_platform=delivery["source_platform"],
            source_channel_id=delivery["source_channel_id"],
            source_message_id=delivery["source_message_id"],
            destination_platform=CommunityBridgePlatform.BUZZ,
        )
        if not link:
            await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])
            return

        await asyncio.to_thread(
            BuzzBridgeClient.deliver,
            delivery_id=str(delivery["id"]),
            created_at=int(delivery["created_at"]),
            operation=operation,
            channel_id=link["destination_channel_id"],
            text=text,
            target_message_id=link["destination_message_id"],
            **provenance,
        )
        if operation in {
            CommunityBridgeDeliveryType.DELETE,
            CommunityBridgeDeliveryType.REACTION_REMOVE,
        }:
            await asyncio.to_thread(
                mark_link_deleted,
                source_platform=delivery["source_platform"],
                source_channel_id=delivery["source_channel_id"],
                source_message_id=delivery["source_message_id"],
                destination_platform=CommunityBridgePlatform.BUZZ,
            )
        await asyncio.to_thread(complete_delivery, delivery_id=delivery["id"])

    async def _resolve_parent_destination_message(self, delivery: dict) -> str:
        source_parent_message_id = str(delivery.get("source_parent_message_id") or "").strip()
        if not source_parent_message_id:
            return ""
        link = await asyncio.to_thread(
            resolve_mapped_message,
            source_platform=delivery["source_platform"],
            source_channel_id=delivery["source_channel_id"],
            source_message_id=source_parent_message_id,
            destination_platform=delivery["target_platform"],
        )
        if not link:
            return ""
        return str(link.get("destination_message_id") or "").strip()

    async def _get_channel_or_fetch(self, channel_id: str) -> Optional[discord.abc.Messageable]:
        normalized_channel_id = str(channel_id or "").strip()
        if not normalized_channel_id:
            return None
        channel = self.get_channel(int(normalized_channel_id))
        if channel is not None:
            return channel
        try:
            return await self.fetch_channel(int(normalized_channel_id))
        except Exception:
            return None

    async def _resolve_author_display_name(
        self,
        payload: dict,
        *,
        source_platform: str,
        channel: Optional[dict] = None,
    ) -> str:
        display_name = str(payload.get("source_author_display_name") or "").strip()
        if display_name:
            return display_name
        user_id = str(payload.get("source_author_id") or "").strip()
        if source_platform == CommunityBridgePlatform.BUZZ and user_id:
            identity = await asyncio.to_thread(
                verified_identity_for_buzz,
                slack_workspace_id=str((channel or {}).get("slack_workspace_id") or ""),
                buzz_pubkey=user_id,
            )
            if identity:
                return str(identity.get("display_name") or user_id)
        if source_platform == CommunityBridgePlatform.SLACK:
            if user_id:
                return await asyncio.to_thread(SlackBridgeClient.get_user_display_name, user_id)
        return user_id or "Unknown user"

    async def _resolve_message_body(self, payload: dict, *, source_platform: str) -> str:
        body = str(payload.get("text") or "")
        if source_platform != CommunityBridgePlatform.SLACK:
            return body
        metadata = dict(payload.get("metadata") or {})
        raw_text = str(metadata.get("slack_raw_text") or "")
        if not raw_text:
            return body
        return await asyncio.to_thread(SlackBridgeClient.resolve_message_text, raw_text)

    def _should_process_message(self, message: discord.Message) -> bool:
        if message.guild is None:
            return False
        if isinstance(message.channel, discord.Thread):
            return False
        if message.author.bot:
            return False
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return False
        return True

    def _discord_parent_message_id(self, message: discord.Message) -> str:
        if message.reference and message.reference.message_id:
            return str(message.reference.message_id)
        return ""


def run_bridge_worker() -> None:
    if not str(getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", "") or "").strip():
        raise RuntimeError("SLACK_BRIDGE_BOT_TOKEN is required")
    token = str(getattr(settings, "DISCORD_BRIDGE_BOT_TOKEN", "") or "").strip()
    client = CommunityBridgeDiscordClient()
    if token:
        client.run(token, log_handler=None)
        return
    if not BuzzBridgeClient.is_configured():
        raise RuntimeError("configure either Discord or the MLAI Chat bridge adapter")
    asyncio.run(_run_headless_delivery_worker(client))


async def _run_headless_delivery_worker(client: CommunityBridgeDiscordClient) -> None:
    await asyncio.to_thread(reset_stale_processing_deliveries)
    poll_seconds = max(
        0.25,
        min(float(getattr(settings, "COMMUNITY_BRIDGE_WORKER_POLL_SECONDS", 1.0)), 60.0),
    )
    logger.info("community_bridge_headless_worker_ready target=mlai_chat")
    while True:
        await client.process_pending_deliveries_once(limit=10)
        await asyncio.sleep(poll_seconds)
