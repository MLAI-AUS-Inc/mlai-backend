"""Validated presentation-only cover selections saved with update revisions."""
from __future__ import annotations

import copy
from urllib.parse import urlsplit

WATERCOLOR_ARTWORK = frozenset({
    "workspace", "mountains", "coast", "growth", "path", "botanicals",
    "fern-garden", "river", "orchard", "studio", "wildflowers", "harbour",
})


def normalize_cover_image(raw):
    """Return an allowlisted cover descriptor, or None for invalid input.

    Artwork identifiers are client-owned catalog keys, never paths. Uploaded
    images are already cropped and uploaded through the existing media service.
    No remote content is fetched while validating or serializing a cover.
    """
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "watercolor":
        artwork = raw.get("artwork")
        if isinstance(artwork, str) and artwork in WATERCOLOR_ARTWORK:
            return {"kind": kind, "artwork": artwork}
    elif kind == "minimal":
        month = raw.get("month")
        if isinstance(month, int) and not isinstance(month, bool) and 1 <= month <= 12:
            return {"kind": kind, "month": month}
    elif kind == "upload":
        url = raw.get("url")
        if not isinstance(url, str) or len(url) > 4096 or any(character.isspace() for character in url):
            return None
        try:
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                return {"kind": kind, "url": url}
        except ValueError:
            return None
    return None


def cover_image_from_config(config):
    """Read a cover from either the API or stored display-config spelling."""
    if not isinstance(config, dict):
        return None
    return normalize_cover_image(config.get("coverImage", config.get("cover_image")))


def retain_cover_image(memo, previous_memo):
    """Keep a saved cover when regeneration or an older client omits it.

    Both arguments remain unchanged. Explicit valid selections replace the old
    cover and therefore become part of the new exact-content revision hash.
    """
    result = copy.deepcopy(memo)
    config = result.get("display_config", result.get("displayConfig"))
    previous_config = (previous_memo or {}).get("display_config", (previous_memo or {}).get("displayConfig"))
    cover = cover_image_from_config(config) or cover_image_from_config(previous_config)
    if cover is None:
        return result
    # Regeneration without a display config also retains the user's existing
    # metric selection instead of introducing an empty selection accidentally.
    config = copy.deepcopy(config if isinstance(config, dict) else previous_config or {})
    config.pop("coverImage", None)
    config["cover_image"] = cover
    result.pop("displayConfig", None)
    result["display_config"] = config
    return result


def generated_cover_image(memo, run_request):
    """Apply the founder's run-pinned cover to generated prose before hashing."""
    result = copy.deepcopy(memo)
    cover = normalize_cover_image((run_request or {}).get("cover_image"))
    if cover is None:
        return result
    config = result.get("display_config", result.get("displayConfig"))
    config = copy.deepcopy(config if isinstance(config, dict) else {})
    config.pop("coverImage", None)
    config["cover_image"] = cover
    result.pop("displayConfig", None)
    result["display_config"] = config
    return result
