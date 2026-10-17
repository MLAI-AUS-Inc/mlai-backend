"""One native setup choice, including the original first-article price."""

from copy import deepcopy

from .channel_artifact_contract import canonical_bytes, digest
from . import cms_goal_contract as goals

FIELDS = {"version", "goal_id", "scope", "owner_user_id", "accepted_at", "answers", "digest"}


def seal(goal, *, user_id, review_policy, expected_points, confirmed, now):
    """Only the authenticated start controller seals verified current-price choices."""
    scope = goals.validate_scope(goal["scope"])
    if (confirmed is not True or not isinstance(review_policy, str) or review_policy not in {"auto", "review"} or
            type(expected_points) is not int or not 0 <= expected_points < 10**9 or
            str(user_id) != scope["binding"]["owner_user_id"]):
        raise goals.CmsGoalError("Review the current native article price and publication choice.")
    value = {"version": 1, "goal_id": goal["goal_id"], "scope": scope, "owner_user_id": str(user_id),
        "accepted_at": goals._time(now.isoformat()).isoformat(), "answers": {"publish_mode": review_policy,
            "billing": {"policy": "reserved", "expected_points": expected_points, "accepted": True}}}
    return {**value, "digest": digest(value)}


def validate(value, goal, *, review_policy):
    """A receipt hash alone cannot replace the private original native choice."""
    try:
        if not isinstance(value, dict) or set(value) != FIELDS or set(value["answers"]) != {"publish_mode", "billing"}:
            raise goals.CmsGoalError("The native setup choice changed.")
        billing = value["answers"]["billing"]
        if not isinstance(billing, dict) or set(billing) != {"policy", "expected_points", "accepted"} or billing["policy"] != "reserved":
            raise goals.CmsGoalError("The native original price changed.")
        expected = seal(goal, user_id=value["owner_user_id"], review_policy=review_policy, expected_points=billing["expected_points"],
            confirmed=billing["accepted"], now=goals._time(value["accepted_at"]))
        if canonical_bytes(value) != canonical_bytes(expected):
            raise goals.CmsGoalError("The native setup choice changed.")
        return deepcopy(expected)
    except goals.CmsGoalError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise goals.CmsGoalError("The native setup choice changed.") from None
