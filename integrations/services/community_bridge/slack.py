from typing import Optional

from django.conf import settings
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from integrations.services.message_sync.slack_client import budgeted_client

from integrations.services.community_bridge.formatting import (
    resolve_slack_markdown_entities,
    sanitize_slack_text,
)



class SlackBridgeClient:
    _client: Optional[WebClient] = None

    @classmethod
    def is_configured(cls) -> bool:
        return bool(str(getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", "") or "").strip())

    @classmethod
    def get_client(cls) -> WebClient:
        if cls._client is None:
            cls._client = budgeted_client(
                WebClient(token=getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", ""), timeout=20),
                workspace_id=str(getattr(settings, "MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", "") or ""),
            )
        return cls._client

    @classmethod
    def validate_signature(cls, body: bytes, timestamp: str, signature: str) -> bool:
        from integrations.services.message_sync.configuration import valid_callback_signature
        return valid_callback_signature(body, timestamp, signature)

    @classmethod
    def get_user_display_name(cls, user_id: str, *, workspace_id=None) -> str:
        return cls.get_user_profile(user_id, workspace_id=workspace_id)["display_name"]

    @classmethod
    def get_user_profile(cls, user_id: str, *, workspace_id=None) -> dict[str, str]:
        from .member_metadata import profile
        return profile(user_id, workspace_id=workspace_id)

    @classmethod
    def get_channel_display_name(cls, channel_id: str, *, workspace_id=None) -> str:
        from .member_metadata import channel_name
        return channel_name(channel_id, workspace_id=workspace_id)

    @classmethod
    def resolve_message_text(cls, value: str, *, workspace_id=None) -> str:
        return sanitize_slack_text(value,
            user_name_resolver=lambda user: cls.get_user_display_name(user, workspace_id=workspace_id),
            channel_name_resolver=lambda channel: cls.get_channel_display_name(channel, workspace_id=workspace_id))

    @classmethod
    def resolve_markdown_entities(cls, value: str, *, workspace_id=None) -> str:
        """Resolve display mentions using current member authority."""
        return resolve_slack_markdown_entities(value,
            user_name_resolver=lambda user: cls.get_user_display_name(user, workspace_id=workspace_id),
            channel_name_resolver=lambda channel: cls.get_channel_display_name(channel, workspace_id=workspace_id))

    @classmethod
    def get_thread_messages(cls, *, channel_id: str, root_message_id: str) -> list[dict]:
        """Return one complete Slack thread, failing closed on partial pagination."""

        from integrations.services.message_sync.member_history import reject_legacy_bot_read
        reject_legacy_bot_read(channel_id)
        messages: list[dict] = []
        cursor = ""
        while True:
            payload = {
                "channel": str(channel_id or "").strip(),
                "ts": str(root_message_id or "").strip(),
                "limit": 200,
            }
            if cursor:
                payload["cursor"] = cursor
            response = cls.get_client().conversations_replies(**payload)
            if not response.get("ok"):
                raise SlackApiError("conversations.replies failed", response)
            messages.extend(
                dict(message)
                for message in (response.get("messages") or [])
                if isinstance(message, dict)
            )
            cursor = str(
                (response.get("response_metadata") or {}).get("next_cursor") or ""
            ).strip()
            if not cursor:
                return messages
            if len(messages) >= 1000:
                raise RuntimeError("Slack thread exceeds the 1000-message repair limit")

    @classmethod
    def get_channel_history(
        cls,
        *,
        channel_id: str,
        oldest: str = "",
        latest: str = "",
        maximum_messages: int = 10_000,
    ) -> list[dict]:
        """Return paginated Slack channel history for bounded reconciliation."""

        from integrations.services.message_sync.member_history import reject_legacy_bot_read
        reject_legacy_bot_read(channel_id)
        maximum = max(1, min(int(maximum_messages), 50_000))
        messages: list[dict] = []
        cursor = ""
        while True:
            payload = {
                "channel": str(channel_id or "").strip(),
                "inclusive": True,
                "limit": min(200, maximum - len(messages)),
            }
            if oldest:
                payload["oldest"] = str(oldest).strip()
            if latest:
                payload["latest"] = str(latest).strip()
            if cursor:
                payload["cursor"] = cursor
            response = cls.get_client().conversations_history(**payload)
            if not response.get("ok"):
                raise SlackApiError("conversations.history failed", response)
            messages.extend(
                dict(message)
                for message in (response.get("messages") or [])
                if isinstance(message, dict)
            )
            if len(messages) >= maximum:
                return messages[:maximum]
            cursor = str(
                (response.get("response_metadata") or {}).get("next_cursor") or ""
            ).strip()
            if not cursor:
                return messages

    @classmethod
    def post_message(
        cls,
        *,
        channel_id: str,
        text: str,
        thread_ts: str = "",
        client_msg_id: str = "",
    ) -> dict:
        client = cls.get_client()
        payload = {
            "channel": channel_id,
            "text": text,
            # Let Slack build its native link cards for messages originating in
            # MLAI Chat.  Slack still applies the workspace's unfurl policy and
            # the destination page's robots/metadata rules.
            "unfurl_links": True,
            "unfurl_media": True,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if client_msg_id:
            payload["client_msg_id"] = client_msg_id
        response = client.chat_postMessage(**payload)
        return {
            "channel": str(response.get("channel") or channel_id),
            "message_id": str(response.get("ts") or ""),
        }

    @classmethod
    def update_message(cls, *, channel_id: str, message_id: str, text: str) -> None:
        cls.get_client().chat_update(
            channel=channel_id,
            ts=message_id,
            text=text,
            unfurl_links=True,
            unfurl_media=True,
        )

    @classmethod
    def delete_message(cls, *, channel_id: str, message_id: str) -> None:
        cls.get_client().chat_delete(channel=channel_id, ts=message_id)

    @staticmethod
    def delete_message_as_user(
        *, access_token: str, channel_id: str, message_id: str
    ) -> dict:
        """Delete through the requesting member's narrowly scoped user token."""

        response = WebClient(token=access_token).chat_delete(
            channel=channel_id,
            ts=message_id,
        )
        return {
            "ok": bool(response.get("ok")),
            "channel": str(response.get("channel") or channel_id),
            "message_id": str(response.get("ts") or message_id),
        }

    @classmethod
    def add_reaction(cls, *, channel_id: str, message_id: str, reaction: str) -> None:
        cls.get_client().reactions_add(
            channel=channel_id,
            timestamp=message_id,
            name=reaction,
        )

    @classmethod
    def remove_reaction(cls, *, channel_id: str, message_id: str, reaction: str) -> None:
        cls.get_client().reactions_remove(
            channel=channel_id,
            timestamp=message_id,
            name=reaction,
        )
