"""Closed original CMS setup choices; validation alone grants no execution scope."""

from copy import deepcopy
from uuid import UUID

from .channel_artifact_contract import canonical_bytes, digest
from .cms_connection_contract import validate_binding
from .cms_goal_contract import _time

QUOTE_FIELDS = {"version", "kind", "companyId", "connectionId", "connectionGeneration", "configurationRevision",
    "platform", "goalTarget", "firstArticlePoints", "setupPoints", "imagePlacement", "policy", "digest"}
POLICY_FIELDS = {"id", "reviewPolicy", "author", "category", "imagePlacement"}
ANSWER_FIELDS = {"review_policy", "publication_confirmed", "native_defaults_confirmed", "billing_confirmed", "expected_cost_points"}
INTENT_FIELDS = {"version", "kind", "operation_id", "request_id", "binding", "credential_operation_id",
    "owner_user_id", "quote", "accepted_at", "answers", "digest"}


class CmsSetupContractError(ValueError):
    """A fixed safe diagnostic, without owner inputs, provider data or credentials."""


def _deny():
    return CmsSetupContractError("The original native setup choices changed.")


def _uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value < maximum


def validate_quote(value):
    """Validate display metadata, without treating its digest as consent or authority."""
    try:
        if (not isinstance(value, dict) or set(value) != QUOTE_FIELDS or type(value["version"]) is not int or value["version"] != 1 or
                value["kind"] != "cms_setup_quote" or not _uuid(value["companyId"]) or not _uuid(value["connectionId"]) or
                not _integer(value["connectionGeneration"], 1, 10**12) or not _integer(value["configurationRevision"], 1, 10**12) or
                value["platform"] not in {"wordpress", "ghost"} or value["goalTarget"] != "first_article_live" or
                not _integer(value["firstArticlePoints"], 0, 10**9) or type(value["setupPoints"]) is not int or value["setupPoints"] != 0 or
                value["imagePlacement"] != "article"):
            raise _deny()
        policy = value["policy"]
        if policy is not None and (not isinstance(policy, dict) or set(policy) != POLICY_FIELDS or not _uuid(policy["id"]) or
                policy["reviewPolicy"] not in {"auto", "review"} or policy["imagePlacement"] != "article" or
                any(not isinstance(policy[key], str) or len(policy[key]) > 200 or any(ord(char) < 32 for char in policy[key])
                    for key in ("author", "category"))):
            raise _deny()
        if len(canonical_bytes(value)) > 4096 or value["digest"] != digest({key: item for key, item in value.items() if key != "digest"}):
            raise _deny()
        return deepcopy(value)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def make_intent(quote, *, operation_id, request_id, binding, credential_operation_id, user_id, answers, now):
    """Seal one server-read quote and explicit choices for an actual setup reservation.

    The caller must obtain the quote through the current owning read and persist
    the result in its private operation transaction. Neither owner-supplied JSON
    nor a checksum supplies permission to configure a provider or reserve funds.
    """
    try:
        selected, scope = validate_quote(quote), validate_binding(binding)
        if (not _uuid(operation_id) or not _uuid(request_id) or not _uuid(credential_operation_id) or str(user_id) != scope["owner_user_id"] or
                any(selected[public] != scope[private] for public, private in (("companyId", "company_id"),
                    ("connectionId", "connection_id"), ("connectionGeneration", "generation"), ("platform", "platform"))) or
                not isinstance(answers, dict) or set(answers) != ANSWER_FIELDS or answers["review_policy"] not in {"auto", "review"} or
                any(answers[key] is not True for key in ("publication_confirmed", "native_defaults_confirmed", "billing_confirmed")) or
                type(answers["expected_cost_points"]) is not int or answers["expected_cost_points"] != selected["firstArticlePoints"]):
            raise _deny()
        value = {"version": 1, "kind": "cms_setup_intent", "operation_id": operation_id, "request_id": request_id,
            "binding": scope, "credential_operation_id": credential_operation_id, "owner_user_id": str(user_id),
            "quote": selected, "accepted_at": _time(now.isoformat()).isoformat(), "answers": deepcopy(answers)}
        return {**value, "digest": digest(value)}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def validate_intent(value, *, expected_binding):
    """Recheck the immutable original record against a separately resolved scope.

    This does not validate a current lease, owner selection, policy, balance or
    payment. Those remain responsibilities of the owning setup coordinator.
    """
    try:
        if (not isinstance(value, dict) or set(value) != INTENT_FIELDS or type(value["version"]) is not int or value["version"] != 1 or
                value["kind"] != "cms_setup_intent" or canonical_bytes(validate_binding(value["binding"])) != canonical_bytes(validate_binding(expected_binding))):
            raise _deny()
        expected = make_intent(value["quote"], operation_id=value["operation_id"], request_id=value["request_id"],
            binding=expected_binding, credential_operation_id=value["credential_operation_id"], user_id=value["owner_user_id"],
            answers=value["answers"], now=_time(value["accepted_at"]))
        if canonical_bytes(expected) != canonical_bytes(value):
            raise _deny()
        return deepcopy(expected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
