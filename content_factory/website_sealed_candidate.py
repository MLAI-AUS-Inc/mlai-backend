"""Authenticated private hosted transport and untrusted proposal validation.

A validated candidate is still pending input, never execution or publication
proof. The caller owns the original setup lookup, operation fences and atomic
state transitions; no database state is read or written here.
"""

from copy import deepcopy
import json
import math
import re
import time
from urllib.parse import urlsplit

from django.conf import settings
import requests

from .website_connections import require_unlocked_remote_call
from .website_contract import WebsiteAuthorityError
from .website_failure_contract import canonical_bytes
from .website_publishing_inputs import candidate_for_target
from .website_sealed_verification import DIGEST, SHA, _exact, _integer, _match, _text, _uuid, _verifier_supported

CANDIDATE_PATH = "/api/runs/hosted-native-verification/candidate"
EXECUTION_PATH = "/api/runs/hosted-native-verification"
SCOPE_FIELDS = frozenset({"website_connection_id", "connection_generation", "repository_id", "github_repo",
    "operation_id", "operation_attempt", "deletion_epoch", "expected_source_sha", "target_id", "contract_digest",
    "domain", "branch", "app_root"})
REQUEST_FIELDS = SCOPE_FIELDS | {"source_run_id"}
MAX_RESPONSE_BYTES = 128 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_SECONDS = 10
INVALID = "Hosted inputs do not match the original setup and supported verifier."


def _invalid():
    return WebsiteAuthorityError("ci_attestation_contract_mismatch", INVALID)


def _unavailable():
    return WebsiteAuthorityError("worker_unavailable", "The authenticated hosted verifier is temporarily unavailable.", retryable=True)


def _tree(value, *, depth=0, budget=None):
    """Bound JSON before serialization and after parsing, without coercion."""
    budget = [5000] if budget is None else budget
    budget[0] -= 1
    if depth > 16 or budget[0] < 0:
        raise ValueError
    if type(value) is dict:
        if len(value) > 100 or any(type(key) is not str or not 0 < len(key) <= 100 for key in value):
            raise ValueError
        for child in value.values():
            _tree(child, depth=depth + 1, budget=budget)
    elif type(value) is list:
        if len(value) > 100:
            raise ValueError
        for child in value:
            _tree(child, depth=depth + 1, budget=budget)
    elif type(value) is str:
        if len(value) > 4096:
            raise ValueError
    elif type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError
    elif value is not None and type(value) is not bool:
        raise ValueError


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonfinite(_):
    raise ValueError


def _route(method, path, data):
    if type(method) is not str or type(path) is not str:
        raise ValueError
    if method.lower() == "post" and path in {CANDIDATE_PATH, EXECUTION_PATH} and type(data) is dict:
        _tree(data)
        if len(canonical_bytes(data)) > MAX_REQUEST_BYTES:
            raise ValueError
        return "POST", 200 if path == CANDIDATE_PATH else 202
    if method.lower() == "get" and data is None:
        match = re.fullmatch(r"/api/runs/([^/]+)/hosted-native-verification", path)
        if match and _uuid(match[1]):
            return "GET", 200
    raise ValueError


def authenticated_request(method, path, *, data=None):
    """Read or dispatch only fixed hosted routes using explicit service HTTPS.

    This transport never uses ambient proxy/netrc credentials, follows redirects,
    exposes provider text or runs while an authority transaction holds locks.
    The returned JSON still requires its route-specific scope/proof validator.
    """
    try:
        method, expected_status = _route(method, path, data)
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise _invalid() from None
    require_unlocked_remote_call()
    try:
        base, key = getattr(settings, "CONTENT_FACTORY_URL", ""), getattr(settings, "CONTENT_FACTORY_API_KEY", None)
        if (type(base) is not str or not 0 < len(base) <= 2048 or type(key) is not str or not 0 < len(key) <= 4096
                or any(ord(char) < 33 or ord(char) > 126 for char in key) or any(ord(char) < 33 for char in base)):
            raise _unavailable()
        parsed = urlsplit(base)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in {"", "/"} or "\\" in base):
            raise _unavailable()
        # Validate malformed ports before a request can retain/log private data.
        parsed.port
        started, raw = time.monotonic(), bytearray()
        with requests.Session() as client:
            client.trust_env = False
            headers = {"X-API-Key": key, "Accept": "application/json", "Accept-Encoding": "identity"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            with client.request(method, base.rstrip("/") + path,
                    headers=headers, **({"data": canonical_bytes(data)} if data is not None else {}),
                    stream=True, allow_redirects=False, timeout=(3, 8)) as response:
                if response.status_code in {408, 429} or 500 <= response.status_code <= 599:
                    raise _unavailable()
                if response.status_code != expected_status:
                    raise _invalid()
                if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise _invalid()
                if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                    raise _invalid()
                length = response.headers.get("Content-Length")
                if length is not None and (not re.fullmatch(r"[0-9]{1,10}", length) or int(length) > MAX_RESPONSE_BYTES):
                    raise _invalid()
                for chunk in response.iter_content(chunk_size=8192):
                    if time.monotonic() - started > MAX_SECONDS:
                        raise _unavailable()
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _invalid()
                    raw.extend(chunk)
        if time.monotonic() - started > MAX_SECONDS:
            raise _unavailable()
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_nonfinite)
        if type(value) is not dict:
            raise ValueError
        _tree(value)
        # Reject unpaired surrogate escapes as well as non-finite decoded data.
        canonical_bytes(value)
        return value
    except WebsiteAuthorityError:
        raise
    except requests.RequestException:
        raise _unavailable() from None
    except (ValueError, TypeError, AttributeError, OverflowError, RecursionError, UnicodeError):
        raise _invalid() from None


def _request(request):
    if (type(request) is not dict or not REQUEST_FIELDS <= set(request)
            or set(request) - REQUEST_FIELDS - {"connection_target_id"}):
        raise ValueError
    if (any(not _uuid(request[key]) for key in ("website_connection_id", "operation_id"))
            or any(not _integer(request[key], 0 if key == "deletion_epoch" else 1)
                for key in ("connection_generation", "repository_id", "operation_attempt", "deletion_epoch"))
            or not _match(SHA, request["expected_source_sha"]) or not _match(DIGEST, request["contract_digest"])
            or not _text(request["target_id"], 200) or not _text(request["branch"], 255)
            or not _text(request["domain"], 253) or not _text(request["source_run_id"], 100)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", request["source_run_id"])
            or not _text(request["github_repo"], 201)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", request["github_repo"])
            or type(request["app_root"]) is not str or len(request["app_root"]) > 500
            or any(ord(char) < 32 for char in request["app_root"])
            or "connection_target_id" in request and not _exact(request["connection_target_id"], request["target_id"])):
        raise ValueError
    _tree(request)
    canonical_bytes(request)


def fetch_candidate(request):
    """Read the frozen original parent through the authenticated private route."""
    try:
        _request(request)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError, UnicodeError):
        raise _invalid() from None
    return authenticated_request("POST", CANDIDATE_PATH, data=request)


def validate_proposal(response, *, request, target, baseline_source_sha, supported_verifier):
    """Validate a service proposal against trusted original setup inputs.

    ``target`` is the existing target snapshot, ``baseline_source_sha`` comes
    from the owned original setup request, and ``supported_verifier`` comes
    from the shipped immutable manifest. None is selected by the response.
    The legacy contract digest is checked and preserved, never recomputed.
    """
    try:
        _request(request)
        if (type(response) is not dict or set(response) != {"schema_version", "source_run_id", "scope", "publishing_inputs_v1"}
                or type(response["schema_version"]) is not int or response["schema_version"] != 1
                or not _exact(response["source_run_id"], request["source_run_id"])
                or type(response["scope"]) is not dict or set(response["scope"]) != SCOPE_FIELDS
                or any(not _exact(response["scope"][key], request[key]) for key in SCOPE_FIELDS)
                or type(target) is not dict or target.get("target_id") != request["target_id"]
                or not _exact(target.get("contract_digest"), request["contract_digest"])
                or not _match(SHA, baseline_source_sha)):
            raise ValueError
        _tree(response)
        _tree(target)
        if max(len(canonical_bytes(response)), len(canonical_bytes(target))) > MAX_RESPONSE_BYTES:
            raise ValueError
        contract = target.get("contract", {})
        if type(contract) is not dict or ("contract_digest" in contract and not _exact(contract["contract_digest"], request["contract_digest"])):
            raise ValueError
        # A pending proposal may add a seal, but may not silently replace one.
        if "publishing_inputs_v1" in contract and canonical_bytes(contract["publishing_inputs_v1"]) != canonical_bytes(response["publishing_inputs_v1"]):
            raise ValueError
        proposed = deepcopy(target)
        proposed["contract"] = {**deepcopy(contract), "publishing_inputs_v1": deepcopy(response["publishing_inputs_v1"])}
        candidate = candidate_for_target(proposed)
        verification = candidate["payload"]["verification"]
        execution, oracle = verification["execution"], verification["oracle"]
        routes = (execution["listing_route"], execution["route_template"].replace("{slug}", oracle["seed_slug"]),
            execution["route_template"].replace("{slug}", "mlai-nonexistent-" + oracle["artifact_digest"][:12]))
        if (not _exact(verification["baseline_policy"]["source_sha"], baseline_source_sha)
                or not _verifier_supported(verification["verifier"], supported_verifier)
                or (request["app_root"] or ".") != execution["app_root"]
                or len(set(routes)) != 3 or any("{" in route or "}" in route for route in routes)
                or any(not _exact(request.get(key), value) for key, value in candidate["payload"]["publishing"]["source_selection"].items())):
            raise ValueError
        return candidate
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError, UnicodeError):
        raise _invalid() from None
