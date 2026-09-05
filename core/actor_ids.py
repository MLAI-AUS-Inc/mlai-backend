from __future__ import annotations

import re


_CANONICAL_ACTOR_PATTERN = re.compile(r"mlai_user:([1-9][0-9]*)\Z")
_LEGACY_WEB_ACTOR_PATTERN = re.compile(r"web_([1-9][0-9]*)\Z")


def synthetic_actor_id_for_user_id(user_id: int) -> str:
    return f"mlai_user:{int(user_id)}"


def legacy_web_actor_id_for_user_id(user_id: int) -> str:
    return f"web_{int(user_id)}"


def internal_actor_user_id(value: str | None) -> int | None:
    normalized = str(value or "").strip()
    for pattern in (_CANONICAL_ACTOR_PATTERN, _LEGACY_WEB_ACTOR_PATTERN):
        match = pattern.fullmatch(normalized)
        if match:
            return int(match.group(1))
    return None


def is_internal_actor_id(value: str | None) -> bool:
    return internal_actor_user_id(value) is not None


def actor_ids_for_user(user) -> list[str]:
    user_id = int(user.pk)
    canonical = synthetic_actor_id_for_user_id(user_id)
    legacy = legacy_web_actor_id_for_user_id(user_id)
    slack_id = str(getattr(user, "slack_id", "") or "").strip()
    actor_ids = []
    if slack_id and slack_id not in {canonical, legacy}:
        actor_ids.append(slack_id)
    actor_ids.extend([canonical, legacy])
    return list(dict.fromkeys(actor_ids))


def preferred_actor_id_for_user(user) -> str:
    return actor_ids_for_user(user)[0]
