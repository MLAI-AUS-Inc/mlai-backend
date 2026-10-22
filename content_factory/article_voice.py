"""Founder-owned article voice, stored in reserved configuration JSON without schema changes.

The desktop "Voice" setup step edits this profile and Content Factory compiles it into the
article writer's instructions. The shape (schemaVersion 1) is shared by all three:

    {"schemaVersion": 1, "version": 3, "updatedAt": "2026-10-23T04:12:00Z",
     "preset": "builder_peer", "category": "general",
     "dimensions": {"register": 2, "stance": 2, "energy": 0, "humour": 1, "convention": 0},
     "readerExpertise": "practitioner", "narrator": "brand_we",
     "voiceCard": [{"is": "Direct", "isNot": "blunt"}], "stances": ["..."],
     "avoidWords": ["leverage"], "preferWords": ["builders"]}

Each trait is -2..2. A lean (+/-1) costs one voice point, a strong lean (+/-2) costs three,
and a profile may spend at most seven. Sensitive categories cap the high end of some traits.
"""
import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

VOICE_KEY = "article_voice"
SCHEMA_VERSION = 1
POINT_BUDGET = 7
DIMENSIONS = ("register", "stance", "energy", "humour", "convention")
LEVELS = (-2, -1, 0, 1, 2)
CATEGORIES = ("general", "health", "finance", "legal")
READER_EXPERTISE = ("newcomer", "practitioner", "expert")
NARRATORS = ("brand_we", "founder_i", "impersonal")
PRESETS = ("builder_peer", "calm_guide", "challenger", "trusted_expert", "upbeat_coach")
CATEGORY_CAPS = {
    "general": {},
    "health": {"humour": 0, "energy": 1, "convention": 1},
    "finance": {"humour": 0, "energy": 1},
    "legal": {"humour": 0, "energy": 0, "convention": 1},
}
LIMITS = {
    "voiceCard": 3,
    "voiceCardChars": 40,
    "stances": 5,
    "stanceChars": 200,
    "words": 15,
    "wordChars": 40,
}
_CLIENT_KEYS = {
    "schemaVersion", "preset", "category", "dimensions", "readerExpertise", "narrator",
    "voiceCard", "stances", "avoidWords", "preferWords",
}
_SERVER_KEYS = {"version", "updatedAt"}


class ArticleVoiceError(ValueError):
    """A submitted voice profile that cannot be saved, with a message for the founder."""


def level_cost(level):
    """Voice points one trait level costs: neutral is free, a lean 1, a strong lean 3."""
    return 0 if level == 0 else (1 if abs(level) == 1 else 3)


def _clean_text(value, limit, field):
    if not isinstance(value, str):
        raise ArticleVoiceError(f"{field} must be text.")
    text = "".join(" " if ord(char) < 32 or ord(char) == 127 else char for char in value).strip()
    if not text:
        raise ArticleVoiceError(f"{field} cannot be empty.")
    if len(text) > limit:
        raise ArticleVoiceError(f"Keep {field} to {limit} characters.")
    return text


def _clean_words(value, field):
    if not isinstance(value, list):
        raise ArticleVoiceError(f"{field} must be a list of words.")
    words, seen = [], set()
    for raw in value:
        word = _clean_text(raw, LIMITS["wordChars"], field)
        key = word.casefold()
        if key in seen:
            continue
        seen.add(key)
        words.append(word)
    if len(words) > LIMITS["words"]:
        raise ArticleVoiceError(f"Use up to {LIMITS['words']} {field}.")
    return words


def _choice(data, key, options, default):
    value = data.get(key, default)
    if value not in options:
        raise ArticleVoiceError(f"Choose a supported {key}.")
    return value


def validate_article_voice(raw):
    """Return a clean profile from founder input, or raise ArticleVoiceError.

    Client-sent ``version`` and ``updatedAt`` are ignored; the save sets both.
    """
    if not isinstance(raw, dict):
        raise ArticleVoiceError("Send the voice as an object.")
    unknown = sorted(set(raw) - _CLIENT_KEYS - _SERVER_KEYS)
    if unknown:
        raise ArticleVoiceError(f"Unsupported voice fields: {', '.join(unknown)}.")
    if raw.get("schemaVersion") != SCHEMA_VERSION:
        raise ArticleVoiceError("Use voice schemaVersion 1.")
    preset = raw.get("preset")
    if preset is not None and preset not in PRESETS:
        raise ArticleVoiceError("Choose a supported preset.")
    category = _choice(raw, "category", CATEGORIES, "general")
    dimensions = raw.get("dimensions")
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSIONS):
        raise ArticleVoiceError("Set all five voice traits.")
    for key in DIMENSIONS:
        level = dimensions[key]
        if type(level) is not int or level not in LEVELS:
            raise ArticleVoiceError(f"Set {key} between -2 and 2.")
        cap = CATEGORY_CAPS[category].get(key)
        if cap is not None and level > cap:
            raise ArticleVoiceError(f"{key.capitalize()} is limited for {category} topics.")
    spent = sum(level_cost(dimensions[key]) for key in DIMENSIONS)
    if spent > POINT_BUDGET:
        raise ArticleVoiceError(f"This voice uses {spent} points. Stay within {POINT_BUDGET}.")
    card = raw.get("voiceCard", [])
    if not isinstance(card, list) or len(card) > LIMITS["voiceCard"]:
        raise ArticleVoiceError(f"Use up to {LIMITS['voiceCard']} voice lines.")
    voice_card = []
    for line in card:
        if not isinstance(line, dict) or set(line) != {"is", "isNot"}:
            raise ArticleVoiceError("Each voice line needs what you are and what you are not.")
        voice_card.append({
            "is": _clean_text(line["is"], LIMITS["voiceCardChars"], "voice lines"),
            "isNot": _clean_text(line["isNot"], LIMITS["voiceCardChars"], "voice lines"),
        })
    stances = raw.get("stances", [])
    if not isinstance(stances, list) or len(stances) > LIMITS["stances"]:
        raise ArticleVoiceError(f"Use up to {LIMITS['stances']} positions.")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "preset": preset,
        "category": category,
        "dimensions": {key: dimensions[key] for key in DIMENSIONS},
        "readerExpertise": _choice(raw, "readerExpertise", READER_EXPERTISE, "practitioner"),
        "narrator": _choice(raw, "narrator", NARRATORS, "brand_we"),
        "voiceCard": voice_card,
        "stances": [_clean_text(stance, LIMITS["stanceChars"], "positions") for stance in stances],
        "avoidWords": _clean_words(raw.get("avoidWords", []), "words to avoid"),
        "preferWords": _clean_words(raw.get("preferWords", []), "words you like"),
    }


def article_voice_payload(pillar_strategy):
    """The stored profile, or None. Malformed stored data reads as None and never blocks a caller."""
    stored = (pillar_strategy or {}).get(VOICE_KEY) if isinstance(pillar_strategy, dict) else None
    if stored is None:
        return None
    try:
        profile = validate_article_voice(stored)
    except ArticleVoiceError as exc:
        logger.warning("Ignoring unreadable stored article voice: %s", exc)
        return None
    version = stored.get("version")
    updated_at = stored.get("updatedAt")
    if type(version) is int and version > 0:
        profile["version"] = version
    if isinstance(updated_at, str):
        profile["updatedAt"] = updated_at
    return profile


def canonical_sha256(profile):
    """Hex sha256 of the profile's canonical JSON (sorted keys, compact separators, UTF-8)."""
    text = json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def article_voice_for_dispatch(pillar_strategy):
    """The stored profile plus its sha256, for an article run payload, or None when unset."""
    profile = article_voice_payload(pillar_strategy)
    if profile is None:
        return None
    return {**profile, "sha256": canonical_sha256(profile)}


def save_article_voice(organization, voice, *, now=None):
    """Lock and replace only the voice namespace; ``voice=None`` clears it.

    ``voice`` must already be validated. The version increments on every save.
    """
    from django.db import transaction

    from content_factory.models import OrganizationContentConfig
    from organizations.models import Organization

    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        config, _ = OrganizationContentConfig.objects.select_for_update().get_or_create(organization=organization)
        strategy = deepcopy(config.pillar_strategy) if isinstance(config.pillar_strategy, dict) else {}
        previous = strategy.get(VOICE_KEY)
        if voice is None:
            if VOICE_KEY not in strategy:
                return None
            strategy.pop(VOICE_KEY)
            saved = None
        else:
            prior = previous.get("version") if isinstance(previous, dict) else None
            saved = {
                **deepcopy(voice),
                "version": (prior if type(prior) is int and prior > 0 else 0) + 1,
                "updatedAt": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            strategy[VOICE_KEY] = saved
        config.pillar_strategy = strategy
        config.save(update_fields=["pillar_strategy", "updated_at"])
    return saved
