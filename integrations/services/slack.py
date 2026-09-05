import os
import logging
from typing import Optional, Dict, Any
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from django.conf import settings

logger = logging.getLogger(__name__)

_SLACK_USER_NOT_FOUND_ERRORS = frozenset({"user_not_found", "users_not_found"})


def _response_value(response: Any, key: str) -> Any:
    """Read an untrusted Slack response without allowing shape errors to escape."""
    try:
        return response.get(key)
    except (AttributeError, TypeError):
        return None


class SlackUserLookupError(RuntimeError):
    """Base error for strict Slack user-profile lookup."""


class SlackUserNotFoundError(SlackUserLookupError):
    """Raised when Slack confirms that the requested user does not exist."""


class SlackUserLookupUnavailableError(SlackUserLookupError):
    """Raised when Slack cannot authoritatively answer a user lookup."""


class SlackService:
    """Service for interacting with Slack API."""
    
    _client: Optional[WebClient] = None
    _channel_name_cache: Dict[str, str] = {}

    @classmethod
    def get_client(cls) -> WebClient:
        if cls._client is None:
            token = (
                os.environ.get('SLACK_BOT_TOKEN')
                or os.environ.get('JOBS_SLACK_BOT_TOKEN')
                or getattr(settings, 'SLACK_BOT_TOKEN', '')
                or getattr(settings, 'JOBS_SLACK_BOT_TOKEN', '')
            )
            if not token:
                logger.warning("SLACK_BOT_TOKEN or JOBS_SLACK_BOT_TOKEN not found in environment variables")
            cls._client = WebClient(token=token)
        return cls._client

    @classmethod
    def get_user_profile_strict(cls, slack_user_id: str) -> Dict[str, Any]:
        """Fetch a Slack profile while preserving not-found versus outage."""
        try:
            client = cls.get_client()
            response = client.users_info(user=slack_user_id)
        except SlackApiError as exc:
            error_code = str(_response_value(exc.response, 'error') or '')
            if error_code in _SLACK_USER_NOT_FOUND_ERRORS:
                logger.info(
                    "slack_user_lookup_not_found reason_code=user_not_found"
                )
                raise SlackUserNotFoundError("Slack user was not found") from exc
            logger.warning(
                "slack_user_lookup_unavailable reason_code=slack_api_error"
            )
            raise SlackUserLookupUnavailableError(
                "Slack user lookup is temporarily unavailable"
            ) from exc
        except Exception as exc:
            logger.warning(
                "slack_user_lookup_unavailable reason_code=%s",
                type(exc).__name__,
            )
            raise SlackUserLookupUnavailableError(
                "Slack user lookup is temporarily unavailable"
            ) from exc

        if _response_value(response, 'ok') is not True:
            error_code = str(_response_value(response, 'error') or '')
            if error_code in _SLACK_USER_NOT_FOUND_ERRORS:
                logger.info(
                    "slack_user_lookup_not_found reason_code=user_not_found"
                )
                raise SlackUserNotFoundError("Slack user was not found")
            logger.warning(
                "slack_user_lookup_unavailable reason_code=slack_api_error"
            )
            raise SlackUserLookupUnavailableError(
                "Slack user lookup is temporarily unavailable"
            )

        user = _response_value(response, 'user')
        if not isinstance(user, dict) or not user.get('id'):
            logger.warning(
                "slack_user_lookup_unavailable reason_code=malformed_success"
            )
            raise SlackUserLookupUnavailableError(
                "Slack user lookup returned an invalid response"
            )
        profile = user.get('profile')
        if not isinstance(profile, dict):
            logger.warning(
                "slack_user_lookup_unavailable reason_code=malformed_success"
            )
            raise SlackUserLookupUnavailableError(
                "Slack user lookup returned an invalid response"
            )
        return {
            'slack_id': user['id'],
            'real_name': user.get('real_name') or profile.get('real_name'),
            'display_name': profile.get('display_name') or user.get('name'),
            'name': user.get('name'),
            'email': profile.get('email'),
            'image_url': profile.get('image_512') or profile.get('image_192'),
            'is_bot': user.get('is_bot', False),
            'deleted': user.get('deleted', False),
            'tz': user.get('tz'),
        }

    @classmethod
    def get_user_profile(cls, slack_user_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch user profile from Slack.
        
        Returns:
            Dict containing 'real_name', 'email', 'image_512' etc.
            Returns None if user not found or API error.
        """
        try:
            return cls.get_user_profile_strict(slack_user_id)
        except SlackUserLookupError:
            return None

    @classmethod
    def lookup_user_by_email(cls, email: str) -> Optional[Dict[str, Any]]:
        """
        Find a workspace user by email via users.lookupByEmail.

        Requires the users:read.email bot scope.

        Returns:
            Same dict shape as get_user_profile, or None if not found,
            the scope is missing, or an API error occurs.
        """
        normalized = str(email or "").strip()
        if not normalized:
            return None
        client = cls.get_client()
        try:
            response = client.users_lookupByEmail(email=normalized)
            if response['ok']:
                user = response['user']
                profile = user.get('profile', {})
                return {
                    'slack_id': user['id'],
                    'real_name': user.get('real_name') or profile.get('real_name'),
                    'display_name': profile.get('display_name') or user.get('name'),
                    'name': user.get('name'),
                    'email': profile.get('email'),
                    'image_url': profile.get('image_512') or profile.get('image_192'),
                    'is_bot': user.get('is_bot', False),
                    'deleted': user.get('deleted', False),
                    'tz': user.get('tz'),
                }
            logger.error(f"Slack API error looking up user by email: {response.get('error')}")
            return None
        except SlackApiError as e:
            logger.warning(f"Slack API exception looking up user by email: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Exception looking up Slack user by email: {str(e)}")
            return None

    @classmethod
    def open_dm_channel(cls, slack_user_id: str) -> Optional[str]:
        """Open or resolve a DM channel for the given Slack user."""
        client = cls.get_client()
        try:
            response = client.conversations_open(users=[slack_user_id])
            if not response['ok']:
                logger.error(f"Failed to open DM with {slack_user_id}: {response.get('error')}")
                return None
            return response['channel']['id']
        except SlackApiError as e:
            logger.error(f"Slack API error opening DM for {slack_user_id}: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Exception opening DM for {slack_user_id}: {str(e)}")
            return None

    @classmethod
    def send_dm(cls, slack_user_id: str, text: str, blocks: list = None, thread_ts: str = None) -> tuple[bool, Optional[str]]:
        """
        Send a direct message to a user.
        
        Args:
            slack_user_id: The Slack user ID to DM.
            text: The message text.
            blocks: Optional Slack blocks for rich formatting.
            thread_ts: Optional thread timestamp to reply in a thread.
            
        Returns:
            Tuple of (success: bool, message_ts: Optional[str]).
            message_ts is the timestamp of the sent message, useful for starting threads.
        """
        client = cls.get_client()
        try:
            channel_id = cls.open_dm_channel(slack_user_id)
            if not channel_id:
                return False, None

            # Build message kwargs
            msg_kwargs = {
                "channel": channel_id,
                "text": text,
            }
            if blocks:
                msg_kwargs["blocks"] = blocks
            if thread_ts:
                msg_kwargs["thread_ts"] = thread_ts
            
            # Post message
            msg_response = client.chat_postMessage(**msg_kwargs)
            return True, msg_response.get('ts')
        except SlackApiError as e:
            logger.error(f"Slack API error sending DM to {slack_user_id}: {e.response['error']}")
            return False, None
        except Exception as e:
            logger.error(f"Exception sending DM to {slack_user_id}: {str(e)}")
            return False, None
    @classmethod
    def send_message(cls, channel_id: str, text: str, blocks: list = None, thread_ts: str = None) -> tuple[bool, Optional[str]]:
        """
        Send a message to a specific channel.
        
        Args:
            channel_id: The Slack channel ID.
            text: The message text.
            blocks: Optional Slack blocks for rich formatting.
            thread_ts: Optional thread timestamp to reply in a thread.
            
        Returns:
            Tuple of (success: bool, message_ts: Optional[str]).
        """
        client = cls.get_client()
        try:
            # Build message kwargs
            msg_kwargs = {
                "channel": channel_id,
                "text": text,
            }
            if blocks:
                msg_kwargs["blocks"] = blocks
            if thread_ts:
                msg_kwargs["thread_ts"] = thread_ts
            
            # Post message
            msg_response = client.chat_postMessage(**msg_kwargs)
            return True, msg_response.get('ts')
        except SlackApiError as e:
            logger.error(f"Slack API error sending message to {channel_id}: {e.response['error']}")
            return False, None
        except Exception as e:
            logger.error(f"Exception sending message to {channel_id}: {str(e)}")
            return False, None

    @classmethod
    def update_message(cls, channel_id: str, ts: str, text: str, blocks: list = None) -> bool:
        """
        Update an existing Slack message (e.g. to remove interactive buttons after click).

        Args:
            channel_id: The channel containing the message.
            ts: The timestamp of the message to update.
            text: Updated fallback text.
            blocks: Updated blocks (pass original section block without actions to remove buttons).

        Returns:
            True if successful, False otherwise.
        """
        client = cls.get_client()
        try:
            kwargs = {"channel": channel_id, "ts": ts, "text": text}
            if blocks is not None:
                kwargs["blocks"] = blocks
            client.chat_update(**kwargs)
            return True
        except SlackApiError as e:
            logger.error(f"Slack API error updating message in {channel_id}: {e.response['error']}")
            return False
        except Exception as e:
            logger.error(f"Exception updating message in {channel_id}: {str(e)}")
            return False

    @classmethod
    def get_channel_id_by_name(cls, channel_name: str) -> Optional[str]:
        """Resolve a public channel name (without #) to its Slack channel ID."""
        normalized_name = str(channel_name or "").strip().lstrip("#")
        if not normalized_name:
            return None
        if normalized_name in cls._channel_name_cache:
            return cls._channel_name_cache[normalized_name]

        client = cls.get_client()
        try:
            cursor = None
            while True:
                kwargs = {"types": "public_channel", "limit": 200}
                if cursor:
                    kwargs["cursor"] = cursor
                response = client.conversations_list(**kwargs)
                for ch in response["channels"]:
                    if ch["name"] == normalized_name:
                        cls._channel_name_cache[normalized_name] = ch["id"]
                        return ch["id"]
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            logger.warning(f"Channel '{normalized_name}' not found")
            return None
        except SlackApiError as e:
            logger.error(f"Error listing channels: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Exception listing channels: {str(e)}")
            return None

    @classmethod
    def get_channel_history(
        cls,
        channel_id: str,
        limit: int = 50,
        cursor: str = None,
        oldest: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch messages from a public channel.

        Args:
            channel_id: The Slack channel ID.
            limit: Max messages to return (1-100).
            cursor: Pagination cursor from a previous response.
            oldest: Optional Slack timestamp lower bound for the active event round.

        Returns:
            Dict with 'messages' list and 'next_cursor' for pagination, or None on error.
        """
        client = cls.get_client()
        try:
            kwargs = {"channel": channel_id, "limit": limit}
            if cursor:
                kwargs["cursor"] = cursor
            if oldest:
                kwargs["oldest"] = oldest
            response = client.conversations_history(**kwargs)
            messages = response.get("messages", [])
            next_cursor = response.get("response_metadata", {}).get("next_cursor")
            return {"messages": messages, "next_cursor": next_cursor}
        except SlackApiError as e:
            logger.error(f"Error fetching channel history for {channel_id}: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Exception fetching channel history for {channel_id}: {str(e)}")
            return None

    @classmethod
    def get_thread_replies(cls, channel_id: str, thread_ts: str) -> Optional[list]:
        """
        Fetch all replies in a message thread.

        Args:
            channel_id: The channel containing the thread.
            thread_ts: The timestamp of the parent message.

        Returns:
            List of message dicts (including the parent), or None on error.
        """
        client = cls.get_client()
        try:
            response = client.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=200
            )
            return response.get("messages", [])
        except SlackApiError as e:
            logger.error(f"Error fetching thread {thread_ts}: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Exception fetching thread {thread_ts}: {str(e)}")
            return None
