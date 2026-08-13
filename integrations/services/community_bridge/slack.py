import hashlib
import hmac
import logging
import time
from typing import Optional
from urllib.parse import urlparse

from django.conf import settings
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from integrations.services.community_bridge.formatting import sanitize_slack_text


logger = logging.getLogger(__name__)


class SlackBridgeClient:
    _client: Optional[WebClient] = None
    _profile_cache: dict[str, dict[str, str]] = {}
    _channel_name_cache: dict[str, str] = {}

    @classmethod
    def is_configured(cls) -> bool:
        return bool(str(getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", "") or "").strip())

    @classmethod
    def get_client(cls) -> WebClient:
        if cls._client is None:
            cls._client = WebClient(token=getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", ""))
        return cls._client

    @classmethod
    def validate_signature(cls, body: bytes, timestamp: str, signature: str) -> bool:
        signing_secret = str(getattr(settings, "SLACK_BRIDGE_SIGNING_SECRET", "") or "").strip()
        if not signing_secret or not timestamp or not signature:
            return False
        try:
            ts_value = int(str(timestamp).strip())
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts_value) > 60 * 5:
            return False
        request_body = body.decode("utf-8")
        base_string = f"v0:{ts_value}:{request_body}"
        computed = "v0=" + hmac.new(
            signing_secret.encode("utf-8"),
            base_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, str(signature).strip())

    @classmethod
    def get_user_display_name(cls, user_id: str) -> str:
        return cls.get_user_profile(user_id)["display_name"]

    @classmethod
    def get_user_profile(cls, user_id: str) -> dict[str, str]:
        normalized_user_id = str(user_id or "").strip()
        if not normalized_user_id:
            return {"display_name": "Unknown user", "avatar_url": ""}
        cached = cls._profile_cache.get(normalized_user_id)
        if cached:
            return dict(cached)
        client = cls.get_client()
        try:
            response = client.users_info(user=normalized_user_id)
            if not response.get("ok"):
                return {"display_name": normalized_user_id, "avatar_url": ""}
            user = response.get("user") or {}
            profile = user.get("profile") or {}
            display_name = (
                str(profile.get("display_name") or "").strip()
                or str(profile.get("real_name") or "").strip()
                or str(user.get("real_name") or "").strip()
                or normalized_user_id
            )
            avatar_url = cls._approved_avatar_url(profile)
            resolved = {"display_name": display_name, "avatar_url": avatar_url}
            cls._profile_cache[normalized_user_id] = resolved
            return dict(resolved)
        except SlackApiError as exc:
            logger.warning(
                "community_bridge_slack_user_lookup_failed user_id=%s error=%s",
                normalized_user_id,
                exc.response.get("error"),
            )
            return {"display_name": normalized_user_id, "avatar_url": ""}
        except Exception as exc:
            logger.warning(
                "community_bridge_slack_user_lookup_failed user_id=%s exc_type=%s exc=%r",
                normalized_user_id,
                exc.__class__.__name__,
                exc,
            )
            return {"display_name": normalized_user_id, "avatar_url": ""}

    @classmethod
    def get_channel_display_name(cls, channel_id: str) -> str:
        normalized_channel_id = str(channel_id or "").strip()
        if not normalized_channel_id:
            return ""
        cached = cls._channel_name_cache.get(normalized_channel_id)
        if cached:
            return cached
        try:
            response = cls.get_client().conversations_info(channel=normalized_channel_id)
            if not response.get("ok"):
                return normalized_channel_id
            channel = response.get("channel") or {}
            display_name = str(channel.get("name") or "").strip() or normalized_channel_id
            cls._channel_name_cache[normalized_channel_id] = display_name
            return display_name
        except SlackApiError as exc:
            logger.warning(
                "community_bridge_slack_channel_lookup_failed channel_id=%s error=%s",
                normalized_channel_id,
                exc.response.get("error"),
            )
            return normalized_channel_id
        except Exception as exc:
            logger.warning(
                "community_bridge_slack_channel_lookup_failed channel_id=%s exc_type=%s exc=%r",
                normalized_channel_id,
                exc.__class__.__name__,
                exc,
            )
            return normalized_channel_id

    @classmethod
    def resolve_message_text(cls, value: str) -> str:
        return sanitize_slack_text(
            value,
            user_name_resolver=cls.get_user_display_name,
            channel_name_resolver=cls.get_channel_display_name,
        )

    @staticmethod
    def _approved_avatar_url(profile: dict) -> str:
        for field_name in ("image_192", "image_512", "image_72", "image_48", "image_original"):
            value = str(profile.get(field_name) or "").strip()
            if not value or len(value) > 2048:
                continue
            parsed = urlparse(value)
            host = str(parsed.hostname or "").lower()
            is_slack_cdn = host == "avatars.slack-edge.com"
            is_gravatar = host == "secure.gravatar.com"
            if (
                parsed.scheme == "https"
                and parsed.netloc
                and not parsed.username
                and not parsed.password
                and (is_slack_cdn or is_gravatar)
            ):
                return value
        return ""

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
            "unfurl_links": False,
            "unfurl_media": False,
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
            unfurl_links=False,
            unfurl_media=False,
        )

    @classmethod
    def delete_message(cls, *, channel_id: str, message_id: str) -> None:
        cls.get_client().chat_delete(channel=channel_id, ts=message_id)

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
