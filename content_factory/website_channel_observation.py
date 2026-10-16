"""Bounded public homepage hints; never publication or authentication evidence."""

import hashlib
from html.parser import HTMLParser
import http.client
import re
from urllib.parse import urljoin, urlsplit

from .website_contract import WebsiteAuthorityError
from .website_live_fetch import PinnedHTTPSConnection, public_addresses

MAX_PREFIX_BYTES = 65536
RESOURCE_HOSTS = {
    "cdn.shopify.com": "shopify", "fonts.shopifycdn.com": "shopify",
    "cdn.shopifycdn.net": "shopify", "assets.website-files.com": "webflow",
    "cdn.prod.website-files.com": "webflow", "static.parastorage.com": "wix",
    "static.wixstatic.com": "wix", "assets.squarespace.com": "squarespace",
    "static1.squarespace.com": "squarespace",
}
GENERATORS = {
    "wordpress": r"wordpress(?:\s|$)", "ghost": r"ghost(?:\s|$)",
    "webflow": r"webflow(?:\s|$)", "wix": r"wix\.com website builder(?:\s|$)",
    "squarespace": r"squarespace(?:\s|$)", "shopify": r"shopify(?:\s|$)",
}
SIGNALS = frozenset({f"{name}:generator" for name in GENERATORS} |
    {f"{name}:resource" for name in RESOURCE_HOSTS.values()} |
    {"wordpress:api_relation", "webflow:site_attribute"})


def canonical_domain(value):
    """Accept a saved hostname only, never a caller-provided URL or credentials."""
    if not isinstance(value, str) or not 0 < len(value) <= 253:
        raise ValueError("Invalid website hostname")
    domain = value.lower().rstrip(".")
    domain = domain.encode("idna").decode("ascii")
    if (len(domain) > 253 or len(domain.split(".")) < 2 or
            any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                for part in domain.split("."))):
        raise ValueError("Invalid website hostname")
    return domain


class _Markers(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.signals = set()
        self.head = False
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        # Template/noscript contents are not active discovery markup. HTMLParser
        # already treats script/style bodies as data; no data callback is used.
        if tag in {"template", "noscript"}:
            self.ignored.append(tag)
        if self.ignored:
            return
        pairs = [(key.lower(), value or "") for key, value in attrs]
        if len({key for key, _ in pairs}) != len(pairs):
            return
        attrs = dict(pairs)
        if tag == "html" and attrs.get("data-wf-site"):
            self.signals.add("webflow:site_attribute")
        if tag == "head":
            self.head = True
        elif tag == "body":
            self.head = False
        if not self.head:
            return
        if tag == "meta" and attrs.get("name", "").casefold() == "generator":
            value = attrs.get("content", "").strip().casefold()
            for platform, pattern in GENERATORS.items():
                if re.match(pattern, value):
                    self.signals.add(f"{platform}:generator")
        if tag == "link" and "https://api.w.org/" in attrs.get("rel", "").split():
            self.signals.add("wordpress:api_relation")
        if tag in {"script", "link"}:
            try:
                parsed = urlsplit(attrs.get("src" if tag == "script" else "href", ""))
                if parsed.scheme not in {"", "https", "http"} or parsed.username or parsed.password:
                    return
                platform = RESOURCE_HOSTS.get(parsed.hostname or "")
                if platform:
                    self.signals.add(f"{platform}:resource")
            except ValueError:
                pass

    def handle_endtag(self, tag):
        if self.ignored:
            if tag == self.ignored[-1]:
                self.ignored.pop()
            return
        if tag == "head":
            self.head = False


def classify_prefix(body, *, link_header="", truncated=False):
    """Minimize a bounded prefix into fixed hints without retaining remote text."""
    if not isinstance(body, bytes) or len(body) > MAX_PREFIX_BYTES:
        raise ValueError("Homepage prefix exceeds the observation bound")
    parser = _Markers()
    parser.feed(body.decode("utf-8", "replace"))
    if isinstance(link_header, str) and len(link_header) <= 4096:
        # Record only the documented relation, never its advertised URL.
        if re.search(r';\s*rel\s*=\s*(?:"https://api\.w\.org/"|https://api\.w\.org/)(?:\s*[,;]|\s*$)', link_header, re.I):
            parser.signals.add("wordpress:api_relation")
    signals = sorted(parser.signals)
    strong = {signal.split(":")[0] for signal in signals if not signal.endswith(":resource")}
    weak = {signal.split(":")[0] for signal in signals}
    candidates = strong or weak
    platform = next(iter(candidates)) if len(candidates) == 1 else "unknown"
    return {"status": "observed", "platform": platform,
        "confidence": "high" if len(strong) == 1 else "medium" if platform != "unknown" else "unknown",
        "signals": signals, "prefix_sha256": hashlib.sha256(body).hexdigest(),
        "coverage": "prefix" if truncated else "complete"}


def observe_homepage(domain):
    """Read at most 64 KiB of the saved public origin, with pinned TLS and DNS.

    Redirects may change apex/www only. No cookies, authorization, page-advertised
    API URLs, cross-site redirects, compressed bodies or raw headers are kept.
    An unavailable observation returns a fixed unknown result, never a run stop.
    """
    unknown = {"status": "unavailable", "platform": "unknown", "confidence": "unknown",
        "signals": [], "prefix_sha256": None, "coverage": "none"}
    try:
        domain = canonical_domain(domain)
        url = f"https://{domain}/"
        for hop in range(3):
            parsed, addresses = public_addresses(url, domain)
            if parsed.path not in {"", "/"}:
                return unknown
            client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
            response = None
            try:
                client.request("GET", "/", headers={"Host": parsed.hostname,
                    "Connection": "close", "Accept": "text/html", "Accept-Encoding": "identity"})
                response = client.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if not location or hop == 2:
                        return unknown
                    url = urljoin(url, location)
                    continue
                content_type = response.getheader("Content-Type", "").split(";")[0].strip().lower()
                if (response.status != 200 or content_type not in {"text/html", "application/xhtml+xml"}
                        or response.getheader("Content-Encoding", "identity").lower() != "identity"):
                    return unknown
                body = response.read(MAX_PREFIX_BYTES + 1)
                return classify_prefix(body[:MAX_PREFIX_BYTES], link_header=response.getheader("Link", ""),
                    truncated=len(body) > MAX_PREFIX_BYTES)
            finally:
                if response is not None:
                    response.close()
                client.close()
    except (ValueError, UnicodeError, OSError, http.client.HTTPException, WebsiteAuthorityError):
        return unknown
    return unknown
