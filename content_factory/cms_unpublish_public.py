"""Exact credential-free public withdrawal proof, separate from a CMS draft.

Callers must refresh current owner/site/lease authority around the request and
persist only a winning result. Neither these JSON values nor a past observation
grants HTTP permission, a refund, a new post, or an original goal milestone.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import http.client
from urllib.parse import urlsplit, urlunsplit

from . import cms_unpublish_contract as contract
from .cms_api_transport import CmsApiError
from .channel_artifact_contract import digest
from .website_contract import WebsiteAuthorityError
from .website_live_fetch import public_addresses, PinnedHTTPSConnection

FIELDS = {"schema_version", "kind", "status", "withdrawal_operation_id", "publication_operation_id", "intent_digest",
    "scope_sha256", "provider_id", "public_url", "provider_observation_digest", "http_status", "checked_at", "digest"}
TTL = timedelta(minutes=5)


def _draft(intent, state, *, publication, published_state):
    value = contract.validate_state(state, intent, publication=publication, published_state=published_state)
    if value["phase"] != "provider_draft":
        raise CmsApiError("public_article_unverified")
    return value


def fetch_absence(intent, state, *, publication, published_state):
    """Read only the original exact URL; only an actual 404 or 410 is proof."""
    _draft(intent, state, publication=publication, published_state=published_state)
    parsed = urlsplit(intent["public_url"])
    bare = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        _origin, addresses = public_addresses(bare, urlsplit(intent["binding"]["public_origin"]).hostname)
    except (WebsiteAuthorityError, ValueError, TypeError, OSError):
        raise CmsApiError("public_article_unavailable") from None
    client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
    response = None
    try:
        client.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
            "Host": parsed.hostname, "Connection": "close", "Accept": "text/html", "Accept-Encoding": "identity", "Cache-Control": "no-cache"})
        response = client.getresponse()
        if type(response.status) is not int:
            raise CmsApiError("invalid_response")
        if response.status not in {404, 410}:
            raise CmsApiError("public_article_unavailable" if response.status == 429 or response.status >= 500 else "public_article_unverified")
        # A bounded status observation needs no error-page body. Closing both
        # handles prevents a large/streaming response from extending the budget.
        return response.status
    except CmsApiError:
        raise
    except (OSError, http.client.HTTPException):
        raise CmsApiError("public_article_unavailable") from None
    finally:
        if response is not None: response.close()
        client.close()


def _time(value):
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        raise CmsApiError("invalid_response")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise CmsApiError("invalid_response") from None
    return parsed


def observation(intent, state, status, *, publication, published_state, checked_at=None):
    """Bind an actual private fetch result to the observed provider draft."""
    state = _draft(intent, state, publication=publication, published_state=published_state)
    if type(status) is not int or status not in {404, 410}:
        raise CmsApiError("public_article_unverified")
    checked_at = checked_at if checked_at is not None else datetime.now(timezone.utc).isoformat()
    _time(checked_at)
    value = {"schema_version": 1, "kind": "cms_public_withdrawal", "status": "withdrawn",
        "withdrawal_operation_id": intent["operation_id"], "publication_operation_id": intent["publication_operation_id"],
        "intent_digest": intent["digest"], "scope_sha256": digest(intent["binding"]), "provider_id": intent["provider_id"],
        "public_url": intent["public_url"], "provider_observation_digest": state["observation_digest"],
        "http_status": status, "checked_at": checked_at}
    return {**value, "digest": digest(value)}


def validate_observation(value, intent, state, *, publication, published_state, now=None):
    """A current proof must match its original draft, scope, URL and clock."""
    try:
        if not isinstance(value, dict) or set(value) != FIELDS:
            raise CmsApiError("invalid_response")
        expected = observation(intent, state, value["http_status"], publication=publication, published_state=published_state, checked_at=value["checked_at"])
        if digest(value) != digest(expected):
            raise CmsApiError("invalid_response")
        checked = _time(value["checked_at"])
        if now is not None and (now.tzinfo is None or not checked <= now <= checked + TTL):
            raise CmsApiError("public_article_unverified")
        return deepcopy(expected)
    except CmsApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_response") from None
