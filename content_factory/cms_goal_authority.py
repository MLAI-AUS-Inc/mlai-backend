"""Fresh native goal authority; historical financial state is validated separately."""

from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

from django.conf import settings
from django.utils import timezone

from . import cms_goal_contract as goals
from . import cms_goal_consent as consent
from .channel_artifacts import enabled as artifacts_enabled
from .channel_artifact_contract import canonical_bytes
from .cms_operation_scope import current_scope
from .website_goal import enrolled
from .website_contract import WebsiteAuthorityError

ACTION = "cms-goal"
ACTIVE = frozenset({"pending", "running", "needs_user"})
FIELDS = {"schema_version", "binding", "credential_operation_id", "policy_operation_id", "policy_digest", "company_id",
    "requested_by_user_id", "consent", "capture_operation_id", "generation_data", "source_run_id", "failures"}


@dataclass(frozen=True)
class CmsGoalDispatch:
    """A server-only lease reference, never constructed from owner/worker JSON."""

    operation_id: str
    lease_id: str = field(repr=False)


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original native articles goal or setup choice changed.")


def enabled(organization_id):
    """Native runtime enrollment is independent and disabled until acceptance."""
    return bool(artifacts_enabled() and getattr(settings, "ARTICLES_CMS_GOALS_ENABLED", False) and
        getattr(settings, "ARTICLES_FIRST_ARTICLE_GOAL_ENABLED", False) and enrolled(organization_id))


def historical(operation):
    """Validate immutable private references without requiring a still-live key.

    Refund settlement must remain possible after revocation or rollout changes.
    This is not current provider permission or an original-spend observation.
    """
    try:
        payload = operation.payload
        if (operation.action != ACTION or not isinstance(payload, dict) or set(payload) - {"lease_id", "financial_subject_version"} != FIELDS or
                type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or
                type(payload["failures"]) is not int or not 0 <= payload["failures"] <= 5):
            raise _deny()
        financial_version = payload.get("financial_subject_version", 1)
        if type(financial_version) is not int or financial_version not in {1, 2}:
            raise _deny()
        expected = goals.scope(payload["binding"], policy_operation_id=payload["policy_operation_id"], policy_digest=payload["policy_digest"])
        goal = goals.validate_goal(operation.receipt.get("goal"), expected)
        if (goal["goal_id"] != str(operation.pk) or str(operation.connection_id) != expected["binding"]["connection_id"] or
                operation.generation != expected["binding"]["generation"] or payload["company_id"] != expected["binding"]["company_id"] or
                payload["requested_by_user_id"] != expected["binding"]["owner_user_id"] or
                not isinstance(payload["credential_operation_id"], str) or str(UUID(payload["credential_operation_id"])) != payload["credential_operation_id"] or
                payload["capture_operation_id"] is not None and (not isinstance(payload["capture_operation_id"], str) or
                    str(UUID(payload["capture_operation_id"])) != payload["capture_operation_id"]) or
                payload["source_run_id"] is not None and (not isinstance(payload["source_run_id"], str) or not 0 < len(payload["source_run_id"]) <= 100) or
                payload["generation_data"] is not None and (not isinstance(payload["generation_data"], dict) or len(canonical_bytes(payload["generation_data"])) > 32768)):
            raise _deny()
        selected = payload["consent"]
        review_policy = selected["answers"]["publish_mode"]
        selected = consent.validate(selected, goal, review_policy=review_policy)
        if (goal["source"] is not None and goal["source"]["run_id"] != payload["source_run_id"] or
                goals._time(selected["accepted_at"]) < goals._time(goal["started_at"])):
            raise _deny()
        return SimpleNamespace(goal=goal, consent=selected, scope=expected, financial_subject_version=financial_version)
    except WebsiteAuthorityError:
        raise
    except (goals.CmsGoalError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def current(operation, *, user=None, lease_id=None, states=ACTIVE):
    """Recheck the selected native owner/policy and exact durable goal lease."""
    from .cms_goal_evidence import current_scope as selected_scope
    value = historical(operation)
    if (not enabled(operation.connection.organization_id) or operation.state not in states or
            lease_id is not None and (operation.state != "running" or operation.payload.get("lease_id") != lease_id or
                operation.next_attempt_at is None or operation.next_attempt_at <= timezone.now())):
        raise _deny()
    scoped = current_scope(operation, allowed_states=states)
    expected, policy = selected_scope(scoped.context, scoped.config, user=scoped.user)
    if (canonical_bytes(expected) != canonical_bytes(value.scope) or
            policy["policy"]["credential_operation_id"] != operation.payload["credential_operation_id"] or
            policy["policy"]["review_policy"] != value.consent["answers"]["publish_mode"] or
            user is not None and str(user.pk) != str(scoped.user.pk) or
            goals._time(value.consent["accepted_at"]) > timezone.now() + timedelta(seconds=30)):
        raise _deny()
    return SimpleNamespace(**value.__dict__, context=scoped.context, config=scoped.config, connection=scoped.connection,
        user=scoped.user, policy=policy)
