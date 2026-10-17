"""An explicit owned-post withdrawal has one status write and GET-only recovery.

These private journal values grant no ownership, credential or execution
authority. The coordinator must refresh the real current owner, publication and
lease before and after every effect, and separately verify public withdrawal.
"""

from copy import deepcopy
from datetime import datetime
from uuid import UUID

from . import cms_post_contract as posts
from .cms_api_transport import CmsApiError

FIELDS = {"schema_version", "operation_id", "request_id", "publication_operation_id", "publication_intent_digest",
    "binding", "provider_id", "public_url", "body_sha256", "mode", "digest"}
STATE_FIELDS = {"schema_version", "intent_digest", "phase", "issue_count", "probe_count", "provider_revision",
    "observation_digest", "outcome_unknown"}
PHASES = {"reserved", "ready", "issued", "provider_draft", "reconcile_exhausted"}
MAX_PROBES = 8


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise CmsApiError("invalid_request")
    return value


def _original(publication, state):
    posts.validate_intent(publication)
    posts.validate_post_state(state, publication)
    if (publication["status"] != "published" or state["phase"] != "provider_confirmed" or
            state["provider_status"] != "published"):
        raise CmsApiError("invalid_request")


def make_intent(*, operation_id, request_id, publication, state):
    """Bind a new explicit withdrawal to its original confirmed provider ID."""
    try:
        _original(publication, state)
        operation_id, request_id = _uuid(operation_id), _uuid(request_id)
        if operation_id in {publication["operation_id"], publication["binding"]["connection_id"], request_id}:
            raise CmsApiError("invalid_request")
        result = {"schema_version": 1, "operation_id": operation_id, "request_id": request_id,
            "publication_operation_id": publication["operation_id"], "publication_intent_digest": publication["digest"],
            "binding": deepcopy(publication["binding"]), "provider_id": state["provider_id"],
            "public_url": state["public_url"], "body_sha256": publication["body_sha256"], "mode": "draft"}
        result["digest"] = posts._digest(result)
        return result
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None


def validate_intent(value, *, publication, published_state):
    """A checksum cannot substitute another publication, owner or provider ID."""
    try:
        if not isinstance(value, dict) or set(value) != FIELDS or type(value["schema_version"]) is not int:
            raise CmsApiError("invalid_request")
        expected = make_intent(operation_id=value["operation_id"], request_id=value["request_id"],
            publication=publication, state=published_state)
        if posts._digest(value) != posts._digest(expected) or type(value["provider_id"]) is not type(expected["provider_id"]):
            raise CmsApiError("invalid_request")
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None


def _revision(value):
    if not isinstance(value, str) or len(value) > 64 or not value.endswith("Z"):
        raise CmsApiError("invalid_response")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if "T" not in value or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise CmsApiError("invalid_response") from None
    return value


def new_state(intent, *, publication, published_state):
    """Reserve no provider observation and no public-absence proof."""
    intent = validate_intent(intent, publication=publication, published_state=published_state)
    return {"schema_version": 1, "intent_digest": intent["digest"], "phase": "reserved", "issue_count": 0,
        "probe_count": 0, "provider_revision": None, "observation_digest": None, "outcome_unknown": False}


def validate_state(state, intent, *, publication, published_state):
    """A persisted issued write cannot be reset or converted into permission."""
    intent = validate_intent(intent, publication=publication, published_state=published_state)
    try:
        if (not isinstance(state, dict) or set(state) != STATE_FIELDS or type(state["schema_version"]) is not int or state["schema_version"] != 1 or
                state["intent_digest"] != intent["digest"] or not isinstance(state["phase"], str) or state["phase"] not in PHASES or
                type(state["issue_count"]) is not int or state["issue_count"] not in {0, 1} or
                type(state["probe_count"]) is not int or not 0 <= state["probe_count"] <= MAX_PROBES or
                type(state["outcome_unknown"]) is not bool):
            raise CmsApiError("invalid_request")
        phase, issued, probes = state["phase"], state["issue_count"], state["probe_count"]
        if ((phase in {"reserved", "ready"} and (issued or probes)) or
                phase in {"issued", "reconcile_exhausted"} and issued != 1 or
                phase == "issued" and probes == MAX_PROBES or
                phase == "reconcile_exhausted" and probes != MAX_PROBES or
                phase == "provider_draft" and (probes == MAX_PROBES or not issued and probes != 0) or
                state["outcome_unknown"] != (phase in {"issued", "reconcile_exhausted"})):
            raise CmsApiError("invalid_request")
        observed = state["observation_digest"]
        if ((phase == "reserved" and observed is not None) or
                phase != "reserved" and (not isinstance(observed, str) or posts.DIGEST.fullmatch(observed) is None)):
            raise CmsApiError("invalid_request")
        revision = state["provider_revision"]
        if intent["binding"]["platform"] == "ghost" and phase in {"ready", "issued", "reconcile_exhausted"}:
            _revision(revision)
        elif revision is not None:
            raise CmsApiError("invalid_request")
        return deepcopy(state)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None


def read_effect(intent, state, *, publication, published_state):
    """Only the original provider ID is read, with no slug search or new create."""
    validate_state(state, intent, publication=publication, published_state=published_state)
    return posts.PostEffect("GET", identifier=intent["provider_id"],
        query={"context": "edit"} if intent["binding"]["platform"] == "wordpress" else {"formats": "html"})


def observe(intent, state, response, *, publication, published_state):
    """An owned draft is provider evidence; it does not prove public withdrawal."""
    state = validate_state(state, intent, publication=publication, published_state=published_state)
    if state["phase"] == "reconcile_exhausted":
        raise CmsApiError("post_outcome_unknown")
    try:
        if intent["binding"]["platform"] == "wordpress":
            row = response
            actual_status = "published" if isinstance(row, dict) and row.get("status") == "publish" else row.get("status") if isinstance(row, dict) else None
        else:
            if not isinstance(response, dict) or not isinstance(response.get("posts"), list) or len(response["posts"]) != 1:
                raise CmsApiError("invalid_response")
            row = response["posts"][0]
            actual_status = row.get("status") if isinstance(row, dict) else None
        if actual_status not in {"published", "draft"}:
            raise CmsApiError("post_ownership_conflict")
        expected = deepcopy(publication)
        expected["status"] = actual_status
        expected["digest"] = posts._digest({key: value for key, value in expected.items() if key != "digest"})
        observed = posts._owned_record(row, expected)
        if observed["provider_id"] != intent["provider_id"] or type(observed["provider_id"]) is not type(intent["provider_id"]):
            raise CmsApiError("post_ownership_conflict")
        result = {**state, "observation_digest": posts._digest(observed)}
        if actual_status == "draft":
            result.update(phase="provider_draft", outcome_unknown=False, provider_revision=None)
        elif state["phase"] == "provider_draft":
            raise CmsApiError("post_ownership_conflict")
        elif state["issue_count"]:
            result["probe_count"] += 1
            result["phase"] = "reconcile_exhausted" if result["probe_count"] == MAX_PROBES else "issued"
        else:
            result.update(phase="ready", provider_revision=_revision(row.get("updated_at")) if intent["binding"]["platform"] == "ghost" else None)
        return validate_state(result, intent, publication=publication, published_state=published_state)
    except CmsApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_response") from None


def issue(intent, state, *, publication, published_state):
    """Persist the returned issued state before making this one status update."""
    state = validate_state(state, intent, publication=publication, published_state=published_state)
    if state["phase"] != "ready" or state["issue_count"] != 0:
        raise CmsApiError("post_outcome_unknown" if state["issue_count"] else "invalid_request")
    result = {**state, "phase": "issued", "issue_count": 1, "outcome_unknown": True}
    if intent["binding"]["platform"] == "wordpress":
        effect = posts.PostEffect("POST", identifier=intent["provider_id"], query={"context": "edit"}, payload={"status": "draft"})
    else:
        effect = posts.PostEffect("PUT", identifier=intent["provider_id"], query={"formats": "html"}, payload={"posts": [{"status": "draft", "updated_at": state["provider_revision"]}]})
    return validate_state(result, intent, publication=publication, published_state=published_state), effect
