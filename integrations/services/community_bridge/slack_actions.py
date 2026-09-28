"""Safe references to Roo's existing Slack actions (never client-supplied values)."""

import hashlib
import json
import re
from urllib.parse import urlencode


def supported_action(action_id):
    """Only topic selection and its delivery-mode follow-up are bridged."""
    return bool(
        re.fullmatch(
            r"confirm_topic_btn_\d+|cancel_topic_btn|select_article_delivery_mode",
            str(action_id or ""),
        )
    )


def action_digest(message, action):
    """Bind a click to exactly the content and option displayed to the user."""
    material = {
        "blocks": message.get("blocks") or [],
        "action_id": action.get("action_id"),
        "value": action.get("value"),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def message_action_linker(message, *, workspace_id, channel_id):
    """Old clients open Slack; updated clients can handle approved Roo actions."""
    from integrations.services.slack_roo import public_roo_target

    ts = str(message.get("ts") or "")
    if not (
        re.fullmatch(r"T[A-Z0-9]+", workspace_id)
        and re.fullmatch(r"[CDG][A-Z0-9]+", channel_id)
        and re.fullmatch(r"\d+\.\d+", ts)
    ):
        return None
    roo = public_roo_target() == (workspace_id, str(message.get("user") or ""))

    def link(action):
        params = {
            "message_ts": ts,
            "mlai_thread_ts": str(message.get("thread_ts") or ts),
        }
        if (
            roo
            and supported_action(action.get("action_id"))
            and not action.get("confirm")
        ):
            params.update(
                mlai_action=action["action_id"],
                mlai_action_hash=action_digest(message, action),
            )
        return f"https://app.slack.com/client/{workspace_id}/{channel_id}?{urlencode(params)}"

    return link
