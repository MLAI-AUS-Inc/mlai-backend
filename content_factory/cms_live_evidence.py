"""Exact public CMS article observation, separate from provider acknowledgement."""

from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from .channel_artifact_contract import digest
from .cms_api_transport import CmsApiError
from .cms_post_contract import validate_intent, validate_post_state
from .website_contract import WebsiteAuthorityError
from .website_live_fetch import public_addresses, PinnedHTTPSConnection

MAX_HTML_BYTES = 2 * 1024 * 1024
EVIDENCE_FIELDS = {"schema_version", "kind", "status", "platform", "post_operation_id", "source_run_id", "run_generation",
    "scope_sha256", "intent_digest", "provider_id", "public_url", "body_source_sha256", "title", "text_sha256", "headings_sha256",
    "links_sha256", "images_sha256", "checked_at", "digest"}


def fetch_public_article(intent, state):
    """Perform one pinned credential-free request to the acknowledged post URL.

    The caller must validate original paid approval and current CMS authority
    immediately before fetching and fence the result afterward. This observation
    cannot grant or extend that authority. Redirects never count as live proof.
    """
    validate_post_state(state, intent)
    if state["phase"] != "provider_confirmed" or state["provider_status"] != "published":
        raise CmsApiError("public_article_unverified")
    parsed = urlsplit(state["public_url"])
    # WordPress's documented plain ?p=ID URL is already checked by the post
    # contract. Resolve DNS against the query-free origin, then send only that
    # exact validated query, with no cookie, provider key, redirect or referer.
    bare = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    try:
        _origin, addresses = public_addresses(bare, urlsplit(intent["binding"]["public_origin"]).hostname)
    except (WebsiteAuthorityError, ValueError, TypeError, OSError):
        raise CmsApiError("public_article_unavailable") from None
    client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
    response = None
    try:
        client.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={
            "Host": parsed.hostname, "Connection": "close", "Accept": "text/html", "Accept-Encoding": "identity"})
        response = client.getresponse()
        if response.status != 200:
            raise CmsApiError("public_article_unavailable")
        if (response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() not in {"text/html", "application/xhtml+xml"} or
                response.getheader("Content-Encoding", "identity").lower() != "identity"):
            raise CmsApiError("public_article_unverified")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isascii() or not length.isdigit() or len(length) > 10 or int(length) > MAX_HTML_BYTES):
            raise CmsApiError("public_article_unverified")
        body = response.read(MAX_HTML_BYTES + 1)
        if len(body) > MAX_HTML_BYTES:
            raise CmsApiError("public_article_unverified")
        return body.decode("utf-8")
    except CmsApiError:
        raise
    except (OSError, http.client.HTTPException):
        raise CmsApiError("public_article_unavailable") from None
    except (ValueError, TypeError, UnicodeError):
        raise CmsApiError("public_article_unverified") from None
    finally:
        if response is not None:
            response.close()
        client.close()


def _text(value):
    return re.sub(r"\s+", " ", value).strip()


def _url(value, base):
    if not isinstance(value, str) or not value or "\\" in value or any(ord(c) < 33 or 127 <= ord(c) < 160 for c in value):
        raise CmsApiError("public_article_unverified")
    result = urljoin(base, value)
    parsed = urlsplit(result)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.port not in {None, 443}:
        raise CmsApiError("public_article_unverified")
    return result


def _body(html, public_url):
    if not isinstance(html, str) or not 0 < len(html.encode()) <= MAX_HTML_BYTES:
        raise CmsApiError("public_article_unverified")
    soup = BeautifulSoup(html, "html.parser")
    candidates = soup.select("[data-cf-article-body]")
    if len(candidates) != 1:
        raise CmsApiError("public_article_unverified")
    body = candidates[0]
    if body.name != "article" or len(body.find_all("h1")) != 1:
        raise CmsApiError("public_article_unverified")
    if body.find(["script", "style", "template", "iframe", "object", "embed", "form", "input", "button", "select", "textarea", "base", "meta", "link"]):
        raise CmsApiError("public_article_unverified")
    for element in [body, *body.parents, *body.find_all(True)]:
        if (element.name in {"head", "template", "noscript"} or element.has_attr("hidden") or element.has_attr("inert") or
                str(element.get("aria-hidden", "")).lower() == "true" or
                any(str(key).lower().startswith("on") for key in element.attrs) or
                re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", str(element.get("style", "")), re.I)):
            raise CmsApiError("public_article_unverified")
    visible = _text(body.get_text(" ", strip=True))
    if len(visible.split()) < 30 or len(visible) > 200000:
        raise CmsApiError("public_article_unverified")
    headings = [(element.name, _text(element.get_text(" ", strip=True))) for element in body.find_all(re.compile(r"^h[1-6]$"))]
    links = [(_text(element.get_text(" ", strip=True)), _url(element.get("href"), public_url)) for element in body.find_all("a")]
    images = [(_url(element.get("src"), public_url), _text(element.get("alt", ""))) for element in body.find_all("img")]
    # Native themes may wrap markup or add classes/srcset. They cannot replace
    # approved copy, heading order, destinations, image sources or alternatives.
    return {"title": headings[0][1] if headings and headings[0][0] == "h1" else "", "text_sha256": hashlib.sha256(visible.encode()).hexdigest(),
        "headings_sha256": digest(headings), "links_sha256": digest(links), "images_sha256": digest(images)}, soup


def observe_public_article(intent, state, public_html, *, checked_at=None):
    """Require exact current approved semantics and one matching canonical URL.

    Only a private backend intent and current confirmed provider state may be
    used by the caller. A worker's proof/approval booleans are never inputs.
    This function performs no mutation and fabricates no Git merge/build proof.
    """
    validate_intent(intent)
    validate_post_state(state, intent)
    if state["phase"] != "provider_confirmed" or intent["status"] != "published":
        raise CmsApiError("public_article_unverified")
    try:
        expected, _ = _body(intent["html"], state["public_url"])
        actual, soup = _body(public_html, state["public_url"])
        head = soup.find("head")
        canonicals = [element for element in soup.find_all("link") if any(str(token).lower() == "canonical" for token in element.get("rel", []))]
        if (expected["title"] != intent["title"] or actual != expected or head is None or len(canonicals) != 1 or
                canonicals[0].parent is not head or canonicals[0].get("href") != state["public_url"]):
            raise CmsApiError("public_article_unverified")
        now = checked_at or datetime.now(timezone.utc)
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise CmsApiError("public_article_unverified")
        evidence = {"schema_version": 1, "kind": "cms_public_article_observation", "status": "body_verified", "platform": intent["binding"]["platform"],
            "post_operation_id": intent["operation_id"], "source_run_id": intent["source_run_id"], "run_generation": intent["run_generation"],
            "scope_sha256": digest(intent["binding"]), "intent_digest": intent["digest"], "provider_id": state["provider_id"],
            "public_url": state["public_url"], "body_source_sha256": intent["body_sha256"], **actual,
            "checked_at": now.astimezone(timezone.utc).isoformat()}
        return {**evidence, "digest": digest(evidence)}
    except CmsApiError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise CmsApiError("public_article_unverified") from None


def validate_public_observation(value, *, intent, state, now=None):
    """Check a current private receipt, never promote incoming worker evidence.

    The publishing operation must own this saved receipt and separately prove
    exact paid artifact approval/current consent. A valid hash is no authority.
    """
    validate_post_state(state, intent)
    if state["phase"] != "provider_confirmed" or state["provider_status"] != "published":
        raise CmsApiError("public_article_unverified")
    try:
        if not isinstance(value, dict) or set(value) != EVIDENCE_FIELDS:
            raise CmsApiError("public_article_unverified")
        expected_body, _ = _body(intent["html"], state["public_url"])
        expected = {"schema_version": 1, "kind": "cms_public_article_observation", "status": "body_verified",
            "platform": intent["binding"]["platform"], "post_operation_id": intent["operation_id"],
            "source_run_id": intent["source_run_id"], "run_generation": intent["run_generation"],
            "scope_sha256": digest(intent["binding"]), "intent_digest": intent["digest"], "provider_id": state["provider_id"],
            "public_url": state["public_url"], "body_source_sha256": intent["body_sha256"], **expected_body}
        if (any(value[key] != item or type(value[key]) is not type(item) for key, item in expected.items()) or
                value["digest"] != digest({key: item for key, item in value.items() if key != "digest"})):
            raise CmsApiError("public_article_unverified")
        observed = datetime.fromisoformat(value["checked_at"])
        now = now or datetime.now(timezone.utc)
        if (observed.tzinfo is None or now.tzinfo is None or observed.isoformat() != value["checked_at"] or
                not now - timedelta(minutes=15) <= observed <= now + timedelta(seconds=30)):
            raise CmsApiError("public_article_unverified")
        return value
    except CmsApiError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError, OverflowError):
        raise CmsApiError("public_article_unverified") from None
