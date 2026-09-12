"""Render private mentions using only the authorized conversation's profiles."""

import hashlib
import re
from typing import Any

from integrations.services.community_bridge.formatting import SLACK_USER_MENTION_RE


_CODE = re.compile(r"(`{3,}[\s\S]*?(?:`{3,}|$)|`[^`\n]*`)")


def render_private_slack_mentions(text: str, profiles: dict[str, Any]) -> str:
    """Keep source IDs until names are known, without looking up private users."""

    def replace(match: re.Match) -> str:
        profile = profiles.get(match.group(1)) or {}
        name = " ".join(str(profile.get("display_name") or "").split())
        if not name or name == match.group(1):
            return match.group(0)
        # The chat mention parser treats the full name as one inline token.
        return "@" + name.replace(" ", "\u00a0")

    return "".join(
        part if index % 2 else SLACK_USER_MENTION_RE.sub(replace, part)
        for index, part in enumerate(_CODE.split(str(text or "")))
    )


def private_mention_repair(
    message: dict[str, Any],
    text: str,
    profiles: dict[str, Any],
    *,
    completed: bool,
    metadata: dict[str, Any],
) -> tuple[str, str] | None:
    """Identify one idempotent history edit for a legacy, lossy mention import.

    The caller runs within the existing authorized history scan. Retain the
    Slack revision time so a repair cannot supersede a newer edit or deletion.
    No original body is reconstructed from the old, ambiguous ``@user`` label.
    """
    if not completed or metadata.get("mention_format_version") == 1:
        return None
    if metadata.get("permanent_failure") or not metadata.get("destination_message_id"):
        return None
    rendered = render_private_slack_mentions(text, profiles)
    if rendered == text:
        return None
    message_id = str(message.get("ts") or "").strip()
    edited = message.get("edited") if isinstance(message.get("edited"), dict) else {}
    timestamp = str(edited.get("ts") or message_id).strip()
    material = "\0".join((message_id, timestamp, rendered)).encode("utf-8")
    return "mention-format-v1:" + hashlib.sha256(material).hexdigest(), timestamp
