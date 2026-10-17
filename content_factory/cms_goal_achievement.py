"""A recorded first publication is a past fact, separate from live health.

Only the private controller writes this snapshot after all milestones and the
owning public body validator pass. Readers recheck the original paid approval
and current owner. The snapshot grants no model, provider or retry permission.
"""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

from . import cms_goal_contract as goals
from . import cms_article_publication as publications
from . import cms_live_evidence as public
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError

FIELDS = {"schema_version", "kind", "goal_id", "goal_digest", "publication_operation_id", "completed_at", "observation", "digest"}


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original first publication could not be verified.")


def _validate(goal, publication, source, intent, state, observation, *, completed_at):
    scope = goal["scope"]
    expected_source = goals.source({"run_id": source.run.run_id, "run_generation": intent["run_generation"],
        "envelope_digest": source.admitted.envelope["digest"]})
    if (not goals.complete(goal, expected_scope=scope, now=completed_at) or goal["source"] != expected_source or
            intent["binding"] != scope["binding"] or intent["operation_id"] != str(publication.pk) or
            str(publication.connection_id) != scope["binding"]["connection_id"] or publication.generation != scope["binding"]["generation"]):
        raise _deny()
    observed_at = goals._time(observation["checked_at"])
    if observed_at < goals._time(goal["started_at"]) or observed_at > completed_at:
        raise _deny()
    public.validate_public_observation(observation, intent=intent, state=state, now=observed_at)
    subject = digest({"public_proof_digest": observation["digest"], "approval_digest": intent["approval_sha256"]})
    for name in ("live", "first_article_live"):
        proof = next(row["proof"] for row in goal["milestones"] if row["name"] == name)
        if proof["subject_digest"] != subject or proof["at"] != observation["checked_at"]:
            raise _deny()


def capture(goal, publication, *, now):
    """Freeze the actual owning public observation at native goal completion."""
    try:
        source, intent, state, observation = publications.current_publication(publication, now=now)
        _validate(goal, publication, source, intent, state, observation, completed_at=now)
        value = {"schema_version": 1, "kind": "cms_first_article_achievement", "goal_id": goal["goal_id"],
            "goal_digest": digest(goal), "publication_operation_id": str(publication.pk),
            "completed_at": goals._time(now.isoformat()).isoformat(), "observation": deepcopy(observation)}
        return {**value, "digest": digest(value)}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def read(operation, *, user, now):
    """Recheck private lineage and return a past publication without freshening it.

    A later GET-only health recheck may replace/clear the child's observation.
    The original snapshot stays bound to its exact intent, provider ID, paid
    source and original completion ledger. No provider or public HTTP is used.
    """
    from . import cms_goal_authority as authority
    from .website_models import WebsiteConnectionOperation
    try:
        current = authority.current(operation, user=user, states={"completed"})
        goal, value = current.goal, operation.receipt.get("publication_achievement")
        if (operation.receipt.get("status") != "first_article_live" or not isinstance(value, dict) or set(value) != FIELDS or
                type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["kind"] != "cms_first_article_achievement" or
                value["goal_id"] != str(operation.pk) or value["goal_digest"] != digest(goal) or
                str(UUID(value["publication_operation_id"])) != value["publication_operation_id"] or
                operation.receipt["references"]["first_article_live"] != value["publication_operation_id"] or
                value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise _deny()
        completed_at = goals._time(value["completed_at"])
        if completed_at > now + timedelta(seconds=30) or completed_at < goals._time(goal["started_at"]):
            raise _deny()
        publication = WebsiteConnectionOperation.objects.filter(pk=value["publication_operation_id"], action=publications.ACTION,
            connection_id=operation.connection_id, generation=operation.generation).first()
        actual = publications._current(publication, fresh_media=False)
        observation = value["observation"]
        _validate(goal, publication, actual.source, actual.intent, actual.post_state, observation, completed_at=completed_at)
        return {"goal": deepcopy(goal), "completed_at": completed_at, "published_at": observation["checked_at"],
            "public_url": observation["public_url"], "provider_id": observation["provider_id"]}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError, StopIteration):
        raise _deny() from None
