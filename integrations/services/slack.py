import os
import logging
from typing import Optional, Dict, Any
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from django.conf import settings

logger = logging.getLogger(__name__)

class SlackService:
    """Service for interacting with Slack API."""
    
    _client: Optional[WebClient] = None

    @classmethod
    def get_client(cls) -> WebClient:
        if cls._client is None:
            token = os.environ.get('SLACK_BOT_TOKEN')
            if not token:
                logger.warning("SLACK_BOT_TOKEN not found in environment variables")
            cls._client = WebClient(token=token)
        return cls._client

    @classmethod
    def get_user_profile(cls, slack_user_id: str) -> Optional[Dict[str, Any]]:
        """
        Fetch user profile from Slack.
        
        Returns:
            Dict containing 'real_name', 'email', 'image_512' etc.
            Returns None if user not found or API error.
        """
        client = cls.get_client()
        try:
            response = client.users_info(user=slack_user_id)
            if response['ok']:
                user = response['user']
                profile = user.get('profile', {})
                return {
                    'slack_id': user['id'],
                    'real_name': user.get('real_name') or profile.get('real_name'),
                    'email': profile.get('email'),
                    'image_url': profile.get('image_512') or profile.get('image_192'),
                    'is_bot': user.get('is_bot', False),
                    'tz': user.get('tz'),
                }
            else:
                logger.error(f"Slack API error fetching user {slack_user_id}: {response.get('error')}")
                return None
        except SlackApiError as e:
            logger.error(f"Slack API exception fetching user {slack_user_id}: {e.response['error']}")
            return None
        except Exception as e:
            logger.error(f"Exception fetching Slack user {slack_user_id}: {str(e)}")
            return None
