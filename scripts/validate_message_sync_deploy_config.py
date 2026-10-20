"""Validate durable Slack sync deployment inputs without printing credentials."""
import os
import re


def validate(env):
    enabled = env.get("MESSAGE_SYNC_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("MESSAGE_SYNC_ENABLED must be true or false")
    quiet_backoff = env.get("MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED", "false").lower()
    if quiet_backoff not in {"true", "false"}:
        raise ValueError("MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED must be true or false")
    if quiet_backoff == "true" and enabled != "true":
        raise ValueError("MESSAGE_SYNC_QUIET_HEAD_BACKOFF_ENABLED requires MESSAGE_SYNC_ENABLED=true")
    inventory_enabled = env.get("SLACK_OWNER_INVENTORY_ENABLED", "false").lower()
    if inventory_enabled not in {"true", "false"}:
        raise ValueError("SLACK_OWNER_INVENTORY_ENABLED must be true or false")
    if inventory_enabled == "true" and enabled != "true":
        raise ValueError("SLACK_OWNER_INVENTORY_ENABLED requires MESSAGE_SYNC_ENABLED=true")
    for key, default in (("MESSAGE_SYNC_OPEN_PRIORITY_ENABLED", "true"), ("MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED", "false"), ("MESSAGE_SYNC_RECENT_HISTORY_ENABLED", "false"), ("MESSAGE_SYNC_TARGETED_STAGED_ROUTING", "false"), ("COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED", "false")):
        if env.get(key, default).lower() not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    cap = env.get("MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR", "6")
    if not re.fullmatch(r"[1-9][0-9]{0,2}", cap) or int(cap) > 256:
        raise ValueError("MESSAGE_SYNC_UNREAD_PREFETCH_PER_HOUR must be an integer from 1 to 256")
    if env.get("MESSAGE_SYNC_RECENT_HISTORY_ENABLED", "false").lower() == "true" and enabled != "true":
        raise ValueError("MESSAGE_SYNC_RECENT_HISTORY_ENABLED requires MESSAGE_SYNC_ENABLED=true")
    if env.get("COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED", "false").lower() == "true" and enabled != "true":
        raise ValueError("COMMUNITY_BRIDGE_CHANNEL_LIFECYCLE_ENABLED requires MESSAGE_SYNC_ENABLED=true")
    if env.get("MESSAGE_SYNC_TARGETED_STAGED_ROUTING", "false").lower() == "true" and enabled != "true":
        raise ValueError("MESSAGE_SYNC_TARGETED_STAGED_ROUTING requires MESSAGE_SYNC_ENABLED=true")
    if env.get("MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED", "false").lower() == "true" and (enabled != "true" or inventory_enabled != "true"):
        raise ValueError("MESSAGE_SYNC_UNREAD_PREFETCH_ENABLED requires MESSAGE_SYNC_ENABLED=true and SLACK_OWNER_INVENTORY_ENABLED=true")
    fanout = env.get("MESSAGE_SYNC_DEVICE_FANOUT_BATCH", "0")
    if not re.fullmatch(r"0|[1-9][0-9]?", fanout) or int(fanout) > 50:
        raise ValueError("MESSAGE_SYNC_DEVICE_FANOUT_BATCH must be an integer from 0 to 50")
    if int(fanout) > 0 and enabled != "true":
        raise ValueError("MESSAGE_SYNC_DEVICE_FANOUT_BATCH requires MESSAGE_SYNC_ENABLED=true")
    user_app = env.get("MESSAGE_SYNC_SLACK_USER_APP_ID", "")
    if user_app:
        if not re.fullmatch(r"A[A-Z0-9]+", user_app) or not re.fullmatch(r"[0-9a-f]{32}", env.get("MESSAGE_SYNC_SLACK_USER_SIGNING_SECRET", "")):
            raise ValueError("Configure the private OAuth app ID and its signing secret together")
        if enabled == "true" and user_app != env.get("MESSAGE_SYNC_SLACK_APP_ID") and not env.get("MESSAGE_SYNC_SLACK_USER_APP_TOKEN", "").startswith("xapp-"):
            raise ValueError("Configure MESSAGE_SYNC_SLACK_USER_APP_TOKEN with authorizations:read")
    if enabled == "false":
        return
    if env.get("COMMUNITY_BRIDGE_PRODUCTION_ENABLED", "false") != "true":
        raise ValueError("Enable the community bridge worker before durable message sync")
    for name, pattern in [("MESSAGE_SYNC_SLACK_APP_ID", r"A[A-Z0-9]+"),
                          ("MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID", r"T[A-Z0-9]+")]:
        if not re.fullmatch(pattern, env.get(name, "")):
            raise ValueError(f"Configure {name} in deployment variables")
    if not env.get("MESSAGE_SYNC_SLACK_APP_TOKEN", "").startswith("xapp-"):
        raise ValueError("Configure MESSAGE_SYNC_SLACK_APP_TOKEN with authorizations:read in deployment secrets")
    if env.get("MESSAGE_SYNC_SLACK_DISTRIBUTION", "restricted") not in {"restricted", "internal", "marketplace"}:
        raise ValueError("Invalid MESSAGE_SYNC_SLACK_DISTRIBUTION")
    if env.get("MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID") != env.get("SLACK_BRIDGE_WORKSPACE_ID"):
        raise ValueError("Durable sync workspace must match the configured public bridge")


if __name__ == "__main__":
    try:
        validate(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
