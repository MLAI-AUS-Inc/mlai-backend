"""Slack/Unicode reactions using the same pinned emoji-mart data as the clients."""

import json
import re
from functools import lru_cache
from pathlib import Path

# Preserve the existing bounded pass-through for workspace-specific shortcodes.
SLACK_REACTION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_+\-]{0,61}$")


@lru_cache(maxsize=1)
def _emoji_maps() -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(
        (Path(__file__).parent / "community_bridge" / "slack_emoji.json").read_text()
    )
    to_native = data["shortcodes"]
    to_slack = {native: name for name, native in reversed(list(to_native.items()))}
    # Preserve established spellings and tolerate text-style heart reactions.
    to_slack.update({"👍": "thumbsup", "❤": "heart"})
    return to_native, to_slack


def slack_reaction_to_emoji(value: str) -> str:
    """Decode standard Slack names/skin tones; retain bounded custom names."""
    reaction = str(value or "").strip()
    mapped = _emoji_maps()[0].get(reaction)
    if mapped:
        return mapped
    return f":{reaction}:" if SLACK_REACTION_NAME_RE.fullmatch(reaction) else ""


def emoji_to_slack_reaction(value: str) -> str:
    """Encode every supported Unicode variant without dropping its skin tone."""
    reaction = str(value or "").strip()
    mapped = _emoji_maps()[1].get(reaction)
    if mapped:
        return mapped
    if reaction.startswith(":") and reaction.endswith(":"):
        shortcode = reaction[1:-1]
        if shortcode in _emoji_maps()[0] or SLACK_REACTION_NAME_RE.fullmatch(shortcode):
            return shortcode
    return ""
