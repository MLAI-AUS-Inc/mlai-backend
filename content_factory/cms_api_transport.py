"""Bounded credential-bearing CMS JSON requests pinned to one selected origin."""

import base64
import http.client
import json
import time
from urllib.parse import urlencode, urlsplit

from .cms_connection_contract import validate_binding, validate_private_credential, CmsContractError
from .website_live_fetch import PinnedHTTPSConnection, public_addresses
from .website_contract import WebsiteAuthorityError

MAX_JSON_BYTES = 2 * 1024 * 1024


class CmsApiError(ValueError):
    """A safe classified transport diagnostic with no URL, headers or body."""

    def __init__(self, category):
        self.category = category
        super().__init__(f"CMS request {category}")


def _authorization(credential):
    if credential.platform == "wordpress":
        encoded = base64.b64encode(f"{credential.username}:{credential.secret}".encode()).decode("ascii")
        return f"Basic {encoded}"
    if credential.platform == "ghost":
        import jwt
        identifier, secret = credential.secret.split(":")
        now = int(time.time())
        token = jwt.encode({"iat": now, "exp": now + 60, "aud": "/admin/"},
            bytes.fromhex(secret), algorithm="HS256", headers={"kid": identifier, "typ": "JWT"})
        return f"Ghost {token}"
    raise CmsApiError("unsupported")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CmsApiError("invalid_response")
        result[key] = value
    return result


def request_json(binding, credential, *, method, resource, identifier=None, query=None, payload=None):
    """Request a fixed API resource without redirects, arbitrary URLs or logging.

    This transport grants no authority: its caller must load the current private
    reservation/credential and recheck site/generation immediately before use.
    """
    binding = validate_binding(binding)
    platform = binding["platform"]
    public_site_read = method == "GET" and resource == "site" and identifier is None and payload is None
    if resource == "site" and not public_site_read:
        raise CmsApiError("invalid_request")
    if credential is None:
        if not public_site_read:
            raise CmsApiError("credential_context_changed")
    else:
        try:
            credential = validate_private_credential(credential, binding=binding)
        except CmsContractError:
            raise CmsApiError("credential_context_changed") from None
    allowed = {"wordpress": {"site", "posts", "users", "categories", "tags", "media"},
        "ghost": {"posts", "users", "tags", "site"}}
    if (not isinstance(method, str) or method not in {"GET", "POST", "PUT", "DELETE", "OPTIONS"} or
            not isinstance(resource, str) or resource not in allowed[platform]):
        raise CmsApiError("invalid_request")
    if identifier is not None and not (platform == "wordpress" and resource == "users" and identifier == "me"):
        if (platform == "wordpress" and (type(identifier) is not int or not 1 <= identifier < 10**19) or
                platform == "ghost" and (not isinstance(identifier, str) or len(identifier) != 24 or any(c not in "0123456789abcdef" for c in identifier))):
            raise CmsApiError("invalid_request")
    prefix = "wp-json/wp/v2/" if platform == "wordpress" else "ghost/api/admin/"
    base = binding["api_origin"] + ("wp-json/" if platform == "wordpress" and resource == "site" else prefix + resource)
    if identifier is not None:
        base += "/" + str(identifier)
    if platform == "ghost":
        base += "/"
    if query is not None and (not isinstance(query, dict) or len(query) > 12 or any(
            not isinstance(key, str) or key not in {"page", "per_page", "limit", "context", "status", "slug", "search", "filter", "formats", "source", "include", "fields", "order", "orderby", "hide_empty"} or
            not isinstance(value, (str, int)) or type(value) is bool or len(str(value)) > 512 or
            any(ord(c) < 32 for c in str(value)) for key, value in query.items())):
        raise CmsApiError("invalid_request")
    encoded_query = urlencode(query or {})
    if len(encoded_query) > 2048:
        raise CmsApiError("invalid_request")
    try:
        body = None if payload is None else json.dumps(payload, allow_nan=False, separators=(",", ":")).encode()
    except (ValueError, TypeError, RecursionError):
        raise CmsApiError("invalid_request") from None
    if body is not None and (method not in {"POST", "PUT"} or len(body) > MAX_JSON_BYTES):
        raise CmsApiError("invalid_request")
    return _send_json_response(binding, credential, method=method, base=base, encoded_query=encoded_query,
        body=body, content_type="application/json")


def _send_json_response(binding, credential, *, method, base, encoded_query="", body=None, content_type="application/json"):
    """Internal pinned transport after a typed resource/body validation boundary.

    Both JSON operations and image upload use the same response limits, TLS/DNS
    pinning, credential/error handling and no-redirect rule. This grants no lease.
    """
    platform = binding["platform"]
    try:
        parsed, addresses = public_addresses(base, urlsplit(binding["api_origin"]).hostname)
    except CmsApiError:
        raise
    except (ValueError, TypeError, OSError, WebsiteAuthorityError):
        raise CmsApiError("origin_unavailable") from None
    client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
    response = None
    try:
        headers = {"Host": parsed.hostname, "Connection": "close", "Accept": "application/json",
            "Accept-Encoding": "identity"}
        if credential is not None:
            headers["Authorization"] = _authorization(credential)
        if platform == "ghost":
            headers["Accept-Version"] = "v6.0"
        if body is not None:
            headers["Content-Type"] = content_type
        target = parsed.path + ("?" + encoded_query if encoded_query else "")
        client.request(method, target, body=body, headers=headers)
        response = client.getresponse()
        if response.status in {401, 403}:
            raise CmsApiError("credentials_required")
        if response.status == 429 or response.status >= 500:
            raise CmsApiError("transient")
        # Credential-bearing redirects are never followed, including same-origin.
        if not 200 <= response.status < 300:
            raise CmsApiError("rejected")
        if response.getheader("Content-Encoding", "identity").lower() != "identity":
            raise CmsApiError("invalid_response")
        content_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise CmsApiError("invalid_response")
        raw = response.read(MAX_JSON_BYTES + 1)
        if len(raw) > MAX_JSON_BYTES:
            raise CmsApiError("invalid_response")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(CmsApiError("invalid_response")))
        if not isinstance(value, (dict, list)):
            raise CmsApiError("invalid_response")
        # Keep only bounded numeric pagination hints, never arbitrary headers.
        pages = response.getheader("X-WP-TotalPages", "")
        return value, {"pages": int(pages) if pages.isascii() and pages.isdigit() and len(pages) <= 8 else None}
    except CmsApiError:
        raise
    except (OSError, http.client.HTTPException):
        raise CmsApiError("transient") from None
    except (ValueError, TypeError, RecursionError):
        raise CmsApiError("invalid_response") from None
    finally:
        if response is not None:
            response.close()
        client.close()
