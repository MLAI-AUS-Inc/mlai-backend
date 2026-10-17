"""Stable native first-article money identity, separate from provider permission."""

from uuid import UUID

from .cms_connection_contract import validate_binding
from . import cms_goal_contract as goals
from . import cms_goal_consent as consent
from .cms_setup_contract import validate_intent

KIND = "cms_first_article_subject"


def make_subject(*, operation_id, binding, owner_user_id, publish_mode, expected_points, accepted):
    """Identify one real site operation and its accepted price; grant no work lease."""
    try:
        selected = validate_binding(binding)
        if (not isinstance(operation_id, str) or str(UUID(operation_id)) != operation_id or
                str(owner_user_id) != selected["owner_user_id"] or publish_mode not in {"auto", "review"} or
                type(expected_points) is not int or not 0 <= expected_points < 10**9 or accepted is not True):
            raise ValueError("The native first-article financial subject changed.")
        return {"kind": KIND, "goal_id": operation_id, "target": "first_article_live", "scope": {"binding": selected},
            "consent": {"owner_user_id": selected["owner_user_id"], "answers": {"publish_mode": publish_mode,
                "billing": {"expected_points": expected_points, "accepted": True}}}}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ValueError("The native first-article financial subject changed.") from None


def from_goal(value):
    """Project verified native ledger/choice data into its monetary identity only.

    Callers still require the owning current goal and private worker lease before
    generation or any provider write. A financial subject is not a native goal.
    """
    goal = goals.validate_goal(value.goal, value.scope)
    choice = consent.validate(value.consent, goal, review_policy=value.consent["answers"]["publish_mode"])
    return make_subject(operation_id=goal["goal_id"], binding=value.scope["binding"], owner_user_id=choice["owner_user_id"],
        publish_mode=choice["answers"]["publish_mode"], expected_points=choice["answers"]["billing"]["expected_points"],
        accepted=choice["answers"]["billing"]["accepted"])


def from_setup_intent(value, *, expected_binding):
    """Identify an original accepted setup before a policy exists, without reserving funds."""
    original = validate_intent(value, expected_binding=expected_binding)
    return make_subject(operation_id=original["operation_id"], binding=original["binding"], owner_user_id=original["owner_user_id"],
        publish_mode=original["answers"]["review_policy"], expected_points=original["answers"]["expected_cost_points"],
        accepted=original["answers"]["billing_confirmed"])
