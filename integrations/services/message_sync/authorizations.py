"""Expand Slack's truncated recipient list one durable page at a time."""
from slack_sdk import WebClient

from .slack_client import budgeted_client
from .configuration import authorization_token


class AuthorizationConfigurationError(RuntimeError):
    pass


def expand_authorization_page(payload):
    """Return (updated ciphertext payload, complete) without guessing recipients."""
    context = str(payload.get("event_context") or "")
    if not context or payload.get("_sync_authorizations_complete"):
        return payload, True
    configured_app = str(payload.get("api_app_id") or "")
    token = authorization_token(configured_app)
    if not token:
        raise AuthorizationConfigurationError("slack_authorizations_configuration_required")
    workspace = str(payload.get("team_id") or "")
    cursor = str(payload.get("_sync_authorizations_cursor") or "")
    client = budgeted_client(WebClient(token=token, timeout=20), workspace_id=workspace, app_id=configured_app)
    response = client.apps_event_authorizations_list(event_context=context, cursor=cursor, limit=200)
    if not response.get("ok"):
        raise RuntimeError("slack_authorizations_failed")
    recipients = {
        str(item.get("user_id"))
        for item in payload.get("_sync_authorizations", [])
        if isinstance(item, dict) and item.get("team_id") == workspace and item.get("user_id")
    }
    for item in response.get("authorizations") or []:
        if isinstance(item, dict) and item.get("team_id") == workspace and item.get("user_id") and not item.get("is_bot"):
            recipients.add(str(item["user_id"]))
    next_cursor = str((response.get("response_metadata") or {}).get("next_cursor") or "")
    if next_cursor and next_cursor == cursor:
        raise RuntimeError("slack_authorizations_cursor_stalled")
    result = dict(payload)
    result["_sync_authorizations"] = [{"team_id": workspace, "user_id": user} for user in sorted(recipients)]
    result["_sync_authorizations_cursor"] = next_cursor
    result["_sync_authorizations_complete"] = not next_cursor
    if not next_cursor:
        result["authorizations"] = result["_sync_authorizations"]
        result.pop("authorized_users", None)
        result.pop("authed_users", None)
    return result, not next_cursor
