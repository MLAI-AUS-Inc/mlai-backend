"""Validate durable Slack sync deployment inputs without printing credentials."""
import os
import re


def validate(env):
    enabled = env.get("MESSAGE_SYNC_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("MESSAGE_SYNC_ENABLED must be true or false")
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
