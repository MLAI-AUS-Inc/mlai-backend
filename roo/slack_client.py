"""
Slack Client Utilities for Roo

Handles Slack API interactions including posting messages and user lookups.
"""
import os
from typing import Optional, Dict, Any
from functools import lru_cache


# Lazy-loaded Slack client
_slack_client = None


def get_slack_client():
    """Get the Slack WebClient instance."""
    global _slack_client
    if _slack_client is None:
        from slack_sdk import WebClient
        
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise ValueError("SLACK_BOT_TOKEN environment variable is not set")
        
        _slack_client = WebClient(token=token)
        print("🔌 Slack client initialized")
    
    return _slack_client


def post_message(
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
    **kwargs
) -> Dict[str, Any]:
    """
    Post a message to a Slack channel or thread.
    
    Args:
        channel: Channel ID
        text: Message text
        thread_ts: Thread timestamp (for replies)
        **kwargs: Additional Slack API parameters
    
    Returns:
        Slack API response
    """
    client = get_slack_client()
    
    try:
        response = client.chat_postMessage(
            channel=channel,
            text=text,
            thread_ts=thread_ts,
            unfurl_links=False,
            unfurl_media=False,
            **kwargs
        )
        
        if response.get("ok"):
            print(f"✅ Message posted to {channel}" + (f" (thread: {thread_ts})" if thread_ts else ""))
        else:
            print(f"❌ Failed to post message: {response}")
        
        return response
        
    except Exception as e:
        print(f"❌ Slack post error: {e}")
        raise


def post_ephemeral(
    channel: str,
    user: str,
    text: str,
    **kwargs
) -> Dict[str, Any]:
    """
    Post an ephemeral message visible only to one user.
    
    Args:
        channel: Channel ID
        user: User ID to show message to
        text: Message text
        **kwargs: Additional Slack API parameters
    
    Returns:
        Slack API response
    """
    client = get_slack_client()
    
    try:
        response = client.chat_postEphemeral(
            channel=channel,
            user=user,
            text=text,
            **kwargs
        )
        return response
        
    except Exception as e:
        print(f"❌ Slack ephemeral error: {e}")
        raise


@lru_cache(maxsize=100)
def get_user_info(user_id: str) -> Dict[str, Any]:
    """
    Get user information from Slack.
    
    Results are cached to avoid repeated API calls.
    
    Args:
        user_id: Slack user ID
    
    Returns:
        User info dict with id, name, real_name, etc.
    """
    client = get_slack_client()
    
    try:
        response = client.users_info(user=user_id)
        
        if response.get("ok"):
            user = response["user"]
            profile = user.get("profile", {})
            
            return {
                "id": user_id,
                "name": user.get("name", ""),
                "real_name": user.get("real_name", profile.get("real_name", "")),
                "display_name": profile.get("display_name", ""),
                "email": profile.get("email", ""),
                "avatar": profile.get("image_72", ""),
            }
        
        return {"id": user_id, "name": "Unknown"}
        
    except Exception as e:
        print(f"❌ User lookup error for {user_id}: {e}")
        return {"id": user_id, "name": "Unknown"}


def get_display_name(user_id: str) -> str:
    """Get the best display name for a user."""
    info = get_user_info(user_id)
    return (
        info.get("display_name") or 
        info.get("real_name") or 
        info.get("name") or 
        "Unknown"
    )


def verify_request_signature(
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes
) -> bool:
    """
    Verify a Slack request signature.
    
    Args:
        signing_secret: Slack signing secret
        timestamp: X-Slack-Request-Timestamp header
        signature: X-Slack-Signature header
        body: Raw request body bytes
    
    Returns:
        True if signature is valid
    """
    import hmac
    import hashlib
    import time
    
    # Check timestamp is recent (within 5 minutes)
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            print("❌ Request timestamp too old")
            return False
    except ValueError:
        return False
    
    # Compute expected signature
    sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    computed = 'v0=' + hmac.new(
        signing_secret.encode(),
        sig_basestring.encode(),
        hashlib.sha256
    ).hexdigest()
    
    return hmac.compare_digest(computed, signature)


def open_dm(user_id: str) -> Optional[str]:
    """
    Open a DM channel with a user.
    
    Args:
        user_id: Slack user ID
    
    Returns:
        DM channel ID, or None if failed
    """
    client = get_slack_client()
    
    try:
        response = client.conversations_open(users=user_id)
        if response.get("ok"):
            return response["channel"]["id"]
        return None
    except Exception as e:
        print(f"❌ Failed to open DM with {user_id}: {e}")
        return None


def send_dm(user_id: str, text: str, **kwargs) -> Optional[Dict[str, Any]]:
    """
    Send a direct message to a user.
    
    Args:
        user_id: Slack user ID
        text: Message text
        **kwargs: Additional parameters
    
    Returns:
        Slack API response, or None if failed
    """
    dm_channel = open_dm(user_id)
    if dm_channel:
        return post_message(dm_channel, text, **kwargs)
    return None
