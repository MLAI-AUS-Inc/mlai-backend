"""Realtime Database I/O for the Watt *Smart Home (Beginner)* device-command bus.

This module is intentionally separate from ``watt_views.py`` (the existing streamed-game
challenge code). The only thing the two share is team/household identity
(``class_id="WATT"`` / ``household="TEAM{n}"``) — because smart-home commands must land on
the exact Firebase path the streamed Unity game already listens to.

Path contract (verified against Unity's ``HackathonFirebasePaths`` / ``HackathonHouseAuthority``)::

    classes/{classId}/hackathon/households/{householdId}/
        observations/current        # game publishes live state here (incl. `tick`)
        commands/{commandId}         # we WRITE device commands here
        command_results/{commandId}  # game writes accept/reject verdicts here

Command JSON (snake_case) is parsed by ``HackathonCommandParser`` in Unity:
``command_id`` MUST equal the Firebase key, ``target_id`` MUST be non-empty, and a command
is rejected as *stale* if ``current_tick - tick_seen > ttl_ticks`` (max 8). Callers therefore
stamp ``tick_seen`` with the freshly-read observation tick (≈6.25 real s/tick → ~50s window).
"""
import re
import time
from uuid import uuid4

from core.firebase_utils import rtdb_delete, rtdb_get, rtdb_set, rtdb_update

# Mirror Unity's HackathonFirebasePaths.CleanSegment ( . # $ [ ] / -> _ ).
_SEGMENT_RE = re.compile(r"[.#$\[\]/]")
DEFAULT_TTL_TICKS = 8


def clean_segment(value):
    cleaned = _SEGMENT_RE.sub("_", str(value or "").strip())
    return cleaned or "unknown"


def household_root(class_id, household_id):
    return f"classes/{clean_segment(class_id)}/hackathon/households/{clean_segment(household_id)}"


def observations_current_path(class_id, household_id):
    return f"{household_root(class_id, household_id)}/observations/current"


def command_path(class_id, household_id, command_id):
    return f"{household_root(class_id, household_id)}/commands/{clean_segment(command_id)}"


def command_result_path(class_id, household_id, command_id):
    return f"{household_root(class_id, household_id)}/command_results/{clean_segment(command_id)}"


def read_observation(class_id, household_id):
    """Return the current observation dict, or ``None`` if the game isn't publishing."""
    return rtdb_get(observations_current_path(class_id, household_id))


def score_current_path(class_id, household_id):
    return f"{household_root(class_id, household_id)}/score/current"


def read_score(class_id, household_id):
    """Return the current published score summary dict (wallet/cost/mood/day/...), or ``None``."""
    return rtdb_get(score_current_path(class_id, household_id))


def shop_current_path(class_id, household_id):
    return f"{household_root(class_id, household_id)}/shop/current"


def read_shop(class_id, household_id):
    """Return the current published shop state (visible catalog items + wallet), or ``None``."""
    return rtdb_get(shop_current_path(class_id, household_id))


def policy_current_path(class_id, household_id):
    return f"{household_root(class_id, household_id)}/policy/current"


def write_policy(class_id, household_id, policy):
    """Publish the active brain/policy so the streamed Unity game knows which AI brain is
    running and can feature it in cutscenes (deploy / cold-shower / etc.)."""
    rtdb_set(policy_current_path(class_id, household_id), policy)


def read_current_tick(observation):
    """Extract the integer game tick from an observation dict (defaults to 0)."""
    if not isinstance(observation, dict):
        return 0
    try:
        return int(observation.get("tick") or 0)
    except (TypeError, ValueError):
        return 0


def now_ms():
    return int(time.time() * 1000)


STALE_OBSERVATION_MS = 20000


def observation_published_at_ms(observation):
    """Return the observation's ``published_at_ms`` as int, or None if missing/unparseable."""
    if not isinstance(observation, dict):
        return None
    try:
        published = int(observation.get("published_at_ms") or 0)
    except (TypeError, ValueError):
        return None
    return published or None


def observation_liveness(observation, now_ms_value, max_age_ms=STALE_OBSERVATION_MS):
    """Classify whether — and *why* — an observation counts as live.

    Returns a dict ``{"live", "reason", "published_at_ms", "age_ms"}`` where ``reason`` is:

    - ``"live"``             fresh observation — an authority is currently publishing.
    - ``"no_observation"``   no ``observations/current`` node at all (no game has run for this
                             household, or the wrong household was resolved).
    - ``"missing_timestamp"`` node exists but has no ``published_at_ms`` (an old/incompatible
                             build that doesn't stamp it).
    - ``"stale"``            node exists but is older than ``max_age_ms`` — a leftover frozen in
                             the DB by a stream that has since closed.

    ``age_ms = now - published`` (positive ⇒ observation is in the past; a large negative value
    would indicate the backend clock is well behind the game's). ``abs()`` keeps modest
    backend/game clock skew either way from reading a live game as dead.
    """
    if not isinstance(observation, dict):
        return {"live": False, "reason": "no_observation", "published_at_ms": None, "age_ms": None}
    published = observation_published_at_ms(observation)
    if published is None:
        return {"live": False, "reason": "missing_timestamp", "published_at_ms": None, "age_ms": None}
    age_ms = now_ms_value - published
    if abs(age_ms) <= max_age_ms:
        return {"live": True, "reason": "live", "published_at_ms": published, "age_ms": age_ms}
    return {"live": False, "reason": "stale", "published_at_ms": published, "age_ms": age_ms}


def is_observation_live(observation, now_ms_value, max_age_ms=STALE_OBSERVATION_MS):
    """True if the game published this observation recently (i.e. an authority is running).

    Thin wrapper over :func:`observation_liveness` (kept for existing callers/tests).
    """
    return observation_liveness(observation, now_ms_value, max_age_ms)["live"]


def build_command(action, target_type, target_id, params, tick_seen,
                  ttl_ticks=DEFAULT_TTL_TICKS, command_id=None):
    """Build a single Unity-compatible device-command node."""
    command_id = command_id or f"sh_{uuid4().hex[:16]}"
    return {
        "command_id": command_id,
        "submitted_at_ms": now_ms(),
        "tick_seen": int(tick_seen),
        "ttl_ticks": int(ttl_ticks),
        "target_type": target_type,
        "target_id": target_id or target_type,  # must be non-empty
        "action": action,
        "status": "pending",
        "params": params or {},
    }


def write_command(class_id, household_id, command):
    """Write a command node (``command_id`` becomes the Firebase key); returns its id."""
    command_id = command["command_id"]
    rtdb_set(command_path(class_id, household_id, command_id), command)
    return command_id


def read_command_result(class_id, household_id, command_id):
    return rtdb_get(command_result_path(class_id, household_id, command_id))


def delete_command(class_id, household_id, command_id):
    rtdb_delete(command_path(class_id, household_id, command_id))


def commands_root_path(class_id, household_id):
    return f"{household_root(class_id, household_id)}/commands"


def write_commands(class_id, household_id, commands):
    """Write multiple command nodes in a single update; returns the list of command ids.

    Keys are the command ids, so Unity's ``commands`` listener fires ``ChildAdded`` per
    command and the authority drains them all in one publish cycle.
    """
    if not commands:
        return []
    payload = {command["command_id"]: command for command in commands}
    rtdb_update(commands_root_path(class_id, household_id), payload)
    return list(payload.keys())
