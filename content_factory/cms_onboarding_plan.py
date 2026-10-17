"""Prepare original native intent metadata; an initial plan grants no authority."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

from . import cms_onboarding_offer as offers
from . import cms_setup_contract as setup
from .cms_connection_contract import validate_binding
from .cms_goal_contract import _time
from .channel_artifact_contract import canonical_bytes, digest
from .website_contract import WebsiteAuthorityError

TTL = timedelta(days=1)
FIELDS = {"version", "kind", "operation_id", "root_operation_id", "credential_operation_id", "binding", "offer", "native_intent", "accepted_at", "expires_at", "digest"}


def _deny():
    return WebsiteAuthorityError("website_operation_changed", "The original initial CMS setup journal changed.")


def prepare(offer, *, binding, operation_id, root_operation_id, credential_operation_id, request_id, user_id, answers, now):
    """Freeze one server-selected provisional scope for its original native setup.

    The caller must re-read the actual offer, allocate these real identities
    under its owner transaction and reserve the original subject separately.
    A validated plan/checksum never admits a provider, model or financial call.
    """
    try:
        original, bound = offers.validate(offer, now=now), validate_binding(binding)
        resources = [operation_id, root_operation_id, credential_operation_id, bound["connection_id"]]
        if (len(set(resources)) != 4 or any(not isinstance(value, str) or str(UUID(value)) != value for value in [*resources, request_id]) or
                str(user_id) != bound["owner_user_id"] or bound["generation"] != 1 or
                bound["company_id"] != original["scope"]["company_id"] or bound["organization_id"] != original["scope"]["organization_id"] or
                any(bound[key] != original[key] for key in ("platform", "public_origin", "api_origin"))):
            raise _deny()
        quoted = {"version": 1, "kind": "cms_setup_quote", "companyId": bound["company_id"], "connectionId": bound["connection_id"],
            "connectionGeneration": 1, "configurationRevision": 1, "platform": bound["platform"], "goalTarget": "first_article_live",
            "firstArticlePoints": original["first_article_points"], "setupPoints": 0, "imagePlacement": "article", "policy": None}
        native = setup.make_intent({**quoted, "digest": digest(quoted)}, operation_id=root_operation_id, request_id=request_id,
            binding=bound, credential_operation_id=credential_operation_id, user_id=user_id, answers=answers, now=now)
        at = _time(now.isoformat())
        value = {"version": 1, "kind": "cms_onboarding_intent", "operation_id": operation_id, "root_operation_id": root_operation_id,
            "credential_operation_id": credential_operation_id, "binding": bound, "offer": original, "native_intent": native,
            "accepted_at": at.isoformat(), "expires_at": (at + TTL).isoformat()}
        return {**value, "digest": digest(value)}
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None


def validate(value):
    """Reconstruct only original planning metadata, without extending its lifetime.

    The original display offer must have been valid at actual acceptance.
    A worker must independently validate its current private lease, owner,
    selected pending connection/key, original hold, budget and 24-hour expiry.
    """
    try:
        if (not isinstance(value, dict) or set(value) != FIELDS or type(value["version"]) is not int or value["version"] != 1 or
                value["kind"] != "cms_onboarding_intent"):
            raise _deny()
        intent = setup.validate_intent(value["native_intent"], expected_binding=value["binding"])
        expected = prepare(value["offer"], binding=value["binding"], operation_id=value["operation_id"], root_operation_id=value["root_operation_id"],
            credential_operation_id=value["credential_operation_id"], request_id=intent["request_id"], user_id=intent["owner_user_id"],
            answers=intent["answers"], now=_time(value["accepted_at"]))
        if canonical_bytes(value) != canonical_bytes(expected):
            raise _deny()
        return deepcopy(expected)
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise _deny() from None
