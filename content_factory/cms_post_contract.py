"""Private immutable post intent and lost-create recovery; no authority or HTTP.

The backend must derive this intent from its original paid generation, saved
editorial policy and exact artifact approval. JSON from an owner, repository or
worker is never itself proof of that approval. Provider callers must separately
load/recheck current CMS authority around every effect and fence every result.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from html.parser import HTMLParser
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from .cms_api_transport import CmsApiError
from .cms_connection_contract import validate_binding, CmsContractError
from .cms_inventory_contract import _identifier, _public_url, MAX_CONTENT_BYTES

DIGEST = re.compile(r"[a-f0-9]{64}")
INTENT_FIELDS = {"schema_version", "binding", "operation_id", "source_run_id", "run_generation", "artifact_sha256",
    "editorial_sha256", "approval_sha256", "title", "slug_seed", "slug", "html", "body_sha256", "status", "native_fields", "digest"}
STATE_FIELDS = {"schema_version", "intent_digest", "phase", "provider_id", "provider_status", "public_url",
    "create_count", "probe_count", "outcome_unknown", "observation_digest"}
PHASES = {"reserved", "create_ready", "create_issued", "provider_confirmed", "reconcile_exhausted"}


@dataclass(frozen=True)
class PostEffect:
    """A planned fixed provider request; this object never grants permission."""

    method: str
    resource: str = "posts"
    identifier: object = None
    query: dict = field(default_factory=dict, repr=False)
    payload: dict | None = field(default=None, repr=False)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class _PostHtml(HTMLParser):
    allowed = frozenset("article aside section div span p h1 h2 h3 h4 h5 h6 a img figure figcaption strong em b i u s del ins br hr ul ol li blockquote pre code table thead tbody tfoot tr th td caption sup sub small details summary time dl dt dd".split())

    def handle_starttag(self, tag, attrs):
        if tag not in self.allowed:
            raise CmsApiError("unsafe_artifact")
        seen = set()
        for key, value in attrs:
            safe_attributes = {"id", "class", "title", "alt", "href", "src", "width", "height", "loading", "decoding",
                "rel", "target", "role", "scope", "datetime", "colspan", "rowspan", "open", "start", "reversed"}
            if (key in seen or key.startswith("on") or
                    key not in safe_attributes and not key.startswith(("aria-", "data-"))):
                raise CmsApiError("unsafe_artifact")
            seen.add(key)
            if value is not None and any(ord(c) < 32 for c in value):
                raise CmsApiError("unsafe_artifact")
            if key in {"href", "src"}:
                if not isinstance(value, str) or not value or "\\" in value or value.startswith("//"):
                    raise CmsApiError("unsafe_artifact")
                parsed = urlsplit(value)
                if (parsed.username is not None or parsed.password is not None or parsed.scheme and (parsed.scheme != "https" or not parsed.hostname) or
                        not parsed.scheme and not value.startswith(("/", "#")) and ":" in value):
                    raise CmsApiError("unsafe_artifact")

    handle_startendtag = handle_starttag

    def handle_endtag(self, tag):
        if tag not in self.allowed:
            raise CmsApiError("unsafe_artifact")

    def handle_comment(self, data):
        if "mlai-post:" in data or "kg-card-" in data:
            raise CmsApiError("unsafe_artifact")

    def handle_decl(self, decl):
        raise CmsApiError("unsafe_artifact")


def _native_fields(value, platform, binding):
    if not isinstance(value, dict):
        raise CmsApiError("invalid_request")
    if platform == "wordpress":
        if set(value) != {"author", "categories", "tags", "featured_media"}:
            raise CmsApiError("invalid_request")
        _identifier(value["author"], platform)
        for name in ("categories", "tags"):
            if not isinstance(value[name], list) or len(value[name]) > 100 or len(set(value[name])) != len(value[name]):
                raise CmsApiError("invalid_request")
            for identifier in value[name]: _identifier(identifier, platform)
        if type(value["featured_media"]) is not int or not 0 <= value["featured_media"] < 10**19:
            raise CmsApiError("invalid_request")
    else:
        if set(value) not in ({"authors", "tags"}, {"authors", "tags", "feature_image"}):
            raise CmsApiError("invalid_request")
        for name in ("authors", "tags"):
            if (not isinstance(value[name], list) or len(value[name]) > 100 or
                    name == "authors" and not value[name] or len(set(value[name])) != len(value[name])):
                raise CmsApiError("invalid_request")
            for identifier in value[name]: _identifier(identifier, platform)
        if "feature_image" in value and value["feature_image"] is not None:
            from .cms_media_transport import _media_url
            _media_url(value["feature_image"], binding)
    return value


def validate_intent(value):
    """Require a closed exact artifact/context; it is still not approval authority."""
    try:
        if not isinstance(value, dict) or set(value) != INTENT_FIELDS or type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise CmsApiError("invalid_request")
        validate_binding(value["binding"])
        if (not isinstance(value["operation_id"], str) or str(UUID(value["operation_id"])) != value["operation_id"] or
                not isinstance(value["source_run_id"], str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}", value["source_run_id"]) is None or
                type(value["run_generation"]) is not int or not 0 <= value["run_generation"] < 10**12 or
                any(not isinstance(value[key], str) or DIGEST.fullmatch(value[key]) is None for key in
                    ("artifact_sha256", "editorial_sha256", "approval_sha256", "body_sha256", "digest")) or
                not isinstance(value["status"], str) or value["status"] not in {"draft", "published"} or
                not isinstance(value["title"], str) or not 0 < len(value["title"]) <= 200 or
                any(ord(c) < 32 or c in "<>" for c in value["title"]) or
                not isinstance(value["slug_seed"], str) or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value["slug_seed"]) is None or
                not 0 < len(value["slug_seed"]) <= 150 or
                value["slug"] != value["slug_seed"] + "-" + UUID(value["operation_id"]).hex or
                not isinstance(value["html"], str) or not value["html"].strip() or len(value["html"].encode()) > MAX_CONTENT_BYTES or
                any(ord(c) < 32 and c not in "\t\n\r" for c in value["html"]) or
                hashlib.sha256(value["html"].encode()).hexdigest() != value["body_sha256"] or
                _digest({key: item for key, item in value.items() if key != "digest"}) != value["digest"]):
            raise CmsApiError("invalid_request")
        _native_fields(value["native_fields"], value["binding"]["platform"], value["binding"])
        _PostHtml(convert_charrefs=True).feed(value["html"])
        return value
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None


def _make_intent(*, binding, operation_id, source_run_id, run_generation, artifact_sha256, editorial_sha256,
                approval_sha256, title, slug_seed, html, status, native_fields):
    """Seal an already authorized backend artifact, before any provider mutation."""
    value = {"schema_version": 1, "binding": validate_binding(binding), "operation_id": operation_id,
        "source_run_id": source_run_id, "run_generation": run_generation, "artifact_sha256": artifact_sha256,
        "editorial_sha256": editorial_sha256, "approval_sha256": approval_sha256, "title": title,
        "slug_seed": slug_seed, "slug": slug_seed + "-" + UUID(operation_id).hex, "html": html,
        "body_sha256": hashlib.sha256(html.encode()).hexdigest(), "status": status, "native_fields": deepcopy(native_fields)}
    value["digest"] = _digest(value)
    return deepcopy(validate_intent(value))


def make_intent(**fields):
    """Seal a typed private backend intent without exposing malformed content."""
    try:
        return _make_intent(**fields)
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_request") from None


def new_post_state(intent):
    """Start without a provider ID; no URL, merge or live proof is fabricated."""
    validate_intent(intent)
    return {"schema_version": 1, "intent_digest": intent["digest"], "phase": "reserved", "provider_id": None,
        "provider_status": None, "public_url": None, "create_count": 0, "probe_count": 0,
        "outcome_unknown": False, "observation_digest": None}


def validate_post_state(state, intent):
    """Deny another artifact, generation, owner, content or returned resource."""
    validate_intent(intent)
    try:
        if (not isinstance(state, dict) or set(state) != STATE_FIELDS or type(state["schema_version"]) is not int or state["schema_version"] != 1 or
                state["intent_digest"] != intent["digest"] or state["phase"] not in PHASES or
                type(state["create_count"]) is not int or state["create_count"] not in {0, 1} or
                type(state["probe_count"]) is not int or not 0 <= state["probe_count"] <= 8 or type(state["outcome_unknown"]) is not bool):
            raise CmsApiError("invalid_request")
        confirmed = state["phase"] == "provider_confirmed"
        issued = state["phase"] in {"create_issued", "reconcile_exhausted"}
        if ((state["phase"] in {"reserved", "create_ready"}) != (state["create_count"] == 0) or
                state["outcome_unknown"] != issued or
                confirmed != (state["provider_id"] is not None) or
                confirmed != (state["observation_digest"] is not None) or
                not confirmed and (state["provider_status"] is not None or state["public_url"] is not None)):
            raise CmsApiError("invalid_request")
        if ((state["phase"] == "reconcile_exhausted") != (state["probe_count"] == 8) or
                state["phase"] in {"reserved", "create_ready"} and state["probe_count"] != 0):
            raise CmsApiError("invalid_request")
        if confirmed:
            _identifier(state["provider_id"], intent["binding"]["platform"])
            if state["provider_status"] != intent["status"] or DIGEST.fullmatch(state["observation_digest"]) is None:
                raise CmsApiError("invalid_request")
            if intent["status"] == "published": _public_url(state["public_url"], intent["binding"], state["provider_id"])
            elif state["public_url"] is not None: raise CmsApiError("invalid_request")
            evidence = {"provider_id": state["provider_id"], "status": state["provider_status"], "slug": intent["slug"],
                "body_sha256": intent["body_sha256"], "public_url": state["public_url"]}
            if state["observation_digest"] != _digest(evidence):
                raise CmsApiError("invalid_request")
        return state
    except CmsApiError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError):
        raise CmsApiError("invalid_request") from None


def marked_html(intent):
    """Carry an opaque ownership boundary around the exact approved HTML bytes."""
    validate_intent(intent)
    start = f"<!--mlai-post:{intent['operation_id']}:begin:{intent['body_sha256']}-->"
    end = f"<!--mlai-post:{intent['operation_id']}:end-->"
    return start + intent["html"] + end


def lookup_effect(intent, state):
    """Recover by the operation-unique slug, including after an uncertain POST."""
    validate_post_state(state, intent)
    if state["phase"] not in {"reserved", "create_issued"}:
        raise CmsApiError("invalid_request")
    if intent["binding"]["platform"] == "wordpress":
        return PostEffect("GET", query={"slug": intent["slug"], "context": "edit", "status": "any", "per_page": 2})
    return PostEffect("GET", query={"filter": "slug:" + intent["slug"], "formats": "html", "limit": 2})


def issue_create(intent, state):
    """Return the sole create intent; persist this state before sending HTTP.

    Resuming create_issued can only look up the result. Never call POST again
    just because a reply was lost or a probe has not found the resource yet.
    """
    validate_post_state(state, intent)
    if state["phase"] != "create_ready":
        raise CmsApiError("invalid_request")
    if intent["binding"]["platform"] == "wordpress":
        payload = {"title": intent["title"], "slug": intent["slug"], "content": marked_html(intent),
            "status": "publish" if intent["status"] == "published" else "draft", **deepcopy(intent["native_fields"])}
        effect = PostEffect("POST", query={"context": "edit"}, payload=payload)
    else:
        native = {key: [{"id": item} for item in intent["native_fields"][key]] for key in ("authors", "tags")}
        if "feature_image" in intent["native_fields"]:
            native["feature_image"] = intent["native_fields"]["feature_image"]
        payload = {"posts": [{"title": intent["title"], "slug": intent["slug"], "status": intent["status"],
            "html": "<!--kg-card-begin: html-->" + marked_html(intent) + "<!--kg-card-end: html-->", **native}]}
        effect = PostEffect("POST", query={"source": "html", "formats": "html"}, payload=payload)
    updated = {**deepcopy(state), "phase": "create_issued", "create_count": 1, "outcome_unknown": True}
    return updated, effect


def _owned_record(row, intent):
    if not isinstance(row, dict): raise CmsApiError("invalid_response")
    platform = intent["binding"]["platform"]
    identifier = _identifier(row.get("id"), platform)
    if row.get("slug") != intent["slug"]: raise CmsApiError("post_ownership_conflict")
    if platform == "wordpress":
        content, title = row.get("content"), row.get("title")
        if not isinstance(content, dict) or not isinstance(title, dict): raise CmsApiError("invalid_response")
        body, title = content.get("raw"), title.get("raw")
        status = "published" if row.get("status") == "publish" else row.get("status")
        if (row.get("password") or (content.get("protected") is not None and content.get("protected") is not False) or row.get("type", "post") != "post" or
                _identifier(row.get("author"), platform) != intent["native_fields"]["author"] or
                type(row.get("featured_media")) is not int or row["featured_media"] != intent["native_fields"]["featured_media"]):
            raise CmsApiError("post_ownership_conflict")
        for name in ("categories", "tags"):
            actual = row.get(name)
            if not isinstance(actual, list) or len(actual) > 100 or len(set(actual)) != len(actual):
                raise CmsApiError("post_ownership_conflict")
            for item in actual: _identifier(item, platform)
            if sorted(actual) != sorted(intent["native_fields"][name]): raise CmsApiError("post_ownership_conflict")
        url = row.get("link")
    else:
        body, title, status, url = row.get("html"), row.get("title"), row.get("status"), row.get("url")
        if row.get("visibility") != "public": raise CmsApiError("post_ownership_conflict")
        if "feature_image" in intent["native_fields"] and ("feature_image" not in row or row["feature_image"] != intent["native_fields"]["feature_image"]):
            raise CmsApiError("post_ownership_conflict")
        for name in ("authors", "tags"):
            actual = row.get(name)
            if not isinstance(actual, list) or len(actual) > 100:
                raise CmsApiError("post_ownership_conflict")
            identifiers = [_identifier(item.get("id") if isinstance(item, dict) else None, platform) for item in actual]
            if identifiers != intent["native_fields"][name]: raise CmsApiError("post_ownership_conflict")
    if not isinstance(body, str) or len(body.encode()) > MAX_CONTENT_BYTES + 4096 or title != intent["title"] or status != intent["status"]:
        raise CmsApiError("post_ownership_conflict")
    marker = marked_html(intent)
    if body.count(marker) != 1 or body.count("<!--mlai-post:") != 2:
        raise CmsApiError("post_ownership_conflict")
    accepted = {marker}
    if platform == "ghost":
        accepted.update({"<!--kg-card-begin: html-->" + marker + "<!--kg-card-end: html-->",
            '<div class="kg-card kg-html-card">' + marker + '</div>'})
    if body.strip() not in accepted:
        raise CmsApiError("post_ownership_conflict")
    if intent["status"] == "published": url = _public_url(url, intent["binding"], identifier)
    else: url = None
    evidence = {"provider_id": identifier, "status": status, "slug": intent["slug"], "body_sha256": intent["body_sha256"], "public_url": url}
    return evidence


def _observe_posts(intent, state, response, *, lookup=True):
    """Validate a read/ack without ever authorizing another create or a live proof."""
    validate_post_state(state, intent)
    if state["phase"] not in {"reserved", "create_issued"}:
        raise CmsApiError("invalid_request")
    platform = intent["binding"]["platform"]
    if platform == "wordpress": rows = response if lookup else [response]
    else: rows = response.get("posts") if isinstance(response, dict) else None
    if not isinstance(rows, list) or len(rows) > 1:
        raise CmsApiError("post_ownership_conflict")
    result = deepcopy(state)
    if not rows:
        if not lookup: raise CmsApiError("invalid_response")
        if state["phase"] == "reserved": result["phase"] = "create_ready"
        else:
            result["probe_count"] += 1
            if result["probe_count"] >= 8: result["phase"] = "reconcile_exhausted"
        return result
    if state["phase"] == "reserved":
        # An owned previous response belongs only to a journaled issued effect.
        # Unissued reservations never adopt a page or create over an occupied URL.
        raise CmsApiError("post_ownership_conflict")
    evidence = _owned_record(rows[0], intent)
    result.update(phase="provider_confirmed", provider_id=evidence["provider_id"], provider_status=evidence["status"],
        public_url=evidence["public_url"], outcome_unknown=False, observation_digest=_digest(evidence))
    return result


def observe_posts(intent, state, response, *, lookup=True):
    """Decode a provider observation without leaking malformed response fields."""
    try:
        return _observe_posts(intent, state, response, lookup=lookup)
    except CmsApiError:
        raise
    except (CmsContractError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise CmsApiError("invalid_response") from None
