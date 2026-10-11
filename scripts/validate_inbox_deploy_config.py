"""Validate inbox release inputs before any host configuration is changed."""

import os

FLAGS = (
    "COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED",
    "COMMUNITY_CHAT_INBOX_ERASURE_ENABLED",
    "MESSAGE_SYNC_TARGETED_READ_POLLING",
    "MESSAGE_SYNC_INBOX_READ_EXPORT",
    "MESSAGE_SYNC_INBOX_CURSOR_PUSH",
    "MESSAGE_SYNC_INBOX_MENTIONS",
    "MESSAGE_SYNC_RELAY_READ_COUNTS",
)


def validate(env):
    enabled = {}
    for name in FLAGS:
        value = env.get(name, "false")
        if value not in {"true", "false"}:
            raise ValueError(f"{name} must be true or false")
        enabled[name] = value == "true"
    account_features = any(enabled[name] for name in (
        "COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED", "COMMUNITY_CHAT_INBOX_ERASURE_ENABLED",
        "MESSAGE_SYNC_INBOX_READ_EXPORT", "MESSAGE_SYNC_INBOX_CURSOR_PUSH",
        "MESSAGE_SYNC_RELAY_READ_COUNTS",
    ))
    if account_features:
        secret = env.get("MLAI_CHAT_ACCOUNT_KEY_SECRET", "")
        if len(secret.encode("utf-8")) < 32 or "\n" in secret or "\r" in secret:
            raise ValueError("Inbox account features require a stable single-line account secret")
        if secret == env.get("DJANGO_SECRET_KEY", env.get("SECRET_KEY", "")):
            raise ValueError("Inbox account secret must be distinct from Django signing secret")
    if any(enabled[name] for name in FLAGS if name.startswith("MESSAGE_SYNC_")):
        if env.get("MESSAGE_SYNC_ENABLED", "false") != "true":
            raise ValueError("Inbox sync features require MESSAGE_SYNC_ENABLED=true")
    if enabled["MESSAGE_SYNC_RELAY_READ_COUNTS"] and not enabled["MESSAGE_SYNC_INBOX_CURSOR_PUSH"]:
        raise ValueError("Relay read counts require inbox source cursor push")


if __name__ == "__main__":
    try:
        validate(os.environ)
    except ValueError as error:
        raise SystemExit(str(error)) from None
