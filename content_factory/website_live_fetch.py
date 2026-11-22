"""Bounded HTTPS observation pinned to a validated public origin address."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit, urljoin

from .article_live_evidence import MAX_BYTES
from .website_contract import WebsiteAuthorityError


def public_addresses(url, domain):
    """Validate each same-site HTTPS hop and reject every nonpublic DNS answer."""
    parsed = urlsplit(url)
    apex = domain.lower().rstrip(".").removeprefix("www.")
    if (parsed.scheme != "https" or parsed.hostname not in {apex, f"www.{apex}"} or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.query or parsed.fragment):
        raise WebsiteAuthorityError("deployment_origin_invalid", "Verify the exact HTTPS website origin.")
    addresses = {row[4][0] for row in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(value).is_global for value in addresses):
        raise WebsiteAuthorityError("deployment_origin_invalid", "Website verification requires a public destination.")
    return parsed, sorted(addresses)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to the validated IP, retaining original TLS hostname and Host."""

    def __init__(self, host, address):
        super().__init__(host, port=443, timeout=3, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), timeout=3)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.sock.settimeout(15)
        except Exception:
            raw.close()
            raise


def fetch_live_route(url, domain, *, expected_status=200):
    """Fetch bounded HTML, repinning up to four canonical same-site redirects."""
    if expected_status not in {200, 404}:
        from .website_github_failure import taxonomy_active
        raise WebsiteAuthorityError("deployment_route_contract_invalid" if taxonomy_active() else "deployment_route_unverified", "An explicit public route status is required.")
    original = urlsplit(url)
    if original.username or original.password or original.query or original.fragment:
        raise WebsiteAuthorityError("deployment_origin_invalid", "Verify the exact public website origin.")
    # Upgrade a caller's HTTP origin locally; verification never sends cleartext.
    if original.scheme == "http" and original.port in {None, 80}:
        url = original._replace(scheme="https", netloc=original.hostname or "").geturl()
    original_path = original.path.rstrip("/") or "/"
    for hop in range(5):
        try:
            parsed, addresses = public_addresses(url, domain)
        except OSError as exc:
            from .website_github_failure import taxonomy_active
            raise WebsiteAuthorityError("deployment_route_transient" if taxonomy_active() else "live_route_unavailable", "The public articles route could not be reached.") from exc
        if (parsed.path.rstrip("/") or "/") != original_path:
            raise WebsiteAuthorityError("deployment_origin_invalid", "A redirect changed the public articles route.")
        client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
        response = None
        try:
            client.request("GET", parsed.path or "/", headers={"Host": parsed.hostname, "Connection": "close", "Accept": "text/html", "Accept-Encoding": "identity"})
            response = client.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or hop == 4:
                    raise WebsiteAuthorityError("deployment_origin_invalid", "The public route has an invalid redirect chain.")
                url = urljoin(url, location)
                continue
            if response.status != expected_status:
                code = "live_route_unavailable" if response.status == 404 or response.status >= 500 else "deployment_route_unverified"
                from .website_github_failure import taxonomy_active
                if taxonomy_active():
                    code = "deployment_route_transient" if response.status == 429 or response.status >= 500 else "live_route_unavailable" if response.status == 404 else "deployment_route_contract_invalid"
                raise WebsiteAuthorityError(code, "The exact public articles route has not deployed successfully.")
            size = response.getheader("Content-Length")
            if size and (not size.isdigit() or int(size) > MAX_BYTES):
                raise WebsiteAuthorityError("deployment_body_oversized", "The public route exceeds the verification size limit.")
            body = response.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise WebsiteAuthorityError("deployment_body_oversized", "The public route exceeds the verification size limit.")
            return body, {key.lower(): value for key, value in response.getheaders()}
        except ssl.SSLCertVerificationError as exc:
            from .website_github_failure import taxonomy_active
            raise WebsiteAuthorityError("deployment_tls_invalid" if taxonomy_active() else "live_route_unavailable", "The public articles route has invalid TLS evidence.") from exc
        except (OSError, http.client.HTTPException) as exc:
            from .website_github_failure import taxonomy_active
            raise WebsiteAuthorityError("deployment_route_transient" if taxonomy_active() else "live_route_unavailable", "The public articles route could not be reached.") from exc
        finally:
            if response is not None:
                response.close()
            client.close()


# ---------------------------------------------------------------------------
# Plan E1: the website preflight's homepage probe.
#
# ``fetch_live_route`` above serves verification: it collapses DNS failure,
# connect failure and timeout into one error, raises on any unexpected status
# and refuses a redirect off the apex or www host. The preflight needs to tell
# those apart, so it has its own DNS step and a status-returning variant. The
# guards are the same: every hop resolves to global addresses only, the
# connection is pinned to a validated address with the original TLS name,
# nothing is ever sent in cleartext, and only apex or www hops are followed
# (an off-site redirect is reported, never fetched). The verification path is
# unchanged.

PROBE_USER_AGENT = "MLAI-Website-Check/1.0"
PROBE_MAX_HOPS = 4
PROBE_MAX_BYTES = 512 * 1024
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_NXDOMAIN_ERRNOS = frozenset(value for value in (getattr(socket, "EAI_NONAME", None),
                                                 getattr(socket, "EAI_NODATA", None)) if value is not None)


def _is_global(value) -> bool:
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0]).is_global and "%" not in str(value)
    except ValueError:
        return False


def resolve_public(host):
    """``(state, addresses)``: ``ok``, ``nxdomain``, ``dns_error`` or ``not_public``.

    ``nxdomain`` is only the resolver's "no such name" answer; a temporary
    failure is ``dns_error``. Any non-global answer is ``not_public`` and is
    never connected to.
    """
    try:
        rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return ("nxdomain" if exc.errno in _NXDOMAIN_ERRNOS else "dns_error"), []
    except (UnicodeError, OSError):
        return "dns_error", []
    addresses = sorted({row[4][0] for row in rows})
    if not addresses:
        return "dns_error", []
    if any(not _is_global(value) for value in addresses):
        return "not_public", []
    return "ok", addresses


def probe_homepage(host, *, resolve=resolve_public, connection=PinnedHTTPSConnection):
    """GET ``https://<host>/`` and say what happened; never raises for the site.

    Returns ``{outcome, status, headers, body, finalHost, redirectHost}``.
    ``outcome`` is one of ``ok`` (2xx), ``http_status`` (any other final
    status), ``redirect`` (a redirect off the apex or www host, not followed),
    ``nxdomain``, ``dns_error``, ``not_public``, ``refused``, ``timeout``,
    ``tls_invalid``, ``tls_error`` or ``connect_error``.
    """
    host = str(host or "").lower().rstrip(".")
    apex = host.removeprefix("www.")
    allowed = {apex, f"www.{apex}"}
    result = {"outcome": "connect_error", "status": None, "headers": {}, "body": b"", "finalHost": host,
              "redirectHost": None}
    current, path = host, "/"
    for hop in range(PROBE_MAX_HOPS + 1):
        state, addresses = resolve(current)
        if state != "ok":
            return {**result, "outcome": state, "finalHost": current}
        client = connection(current, addresses[0])
        response = None
        try:
            client.request("GET", path, headers={"Host": current, "Connection": "close", "Accept": "text/html",
                                                 "Accept-Encoding": "identity", "User-Agent": PROBE_USER_AGENT})
            response = client.getresponse()
            headers = {str(key).lower(): str(value)[:500] for key, value in response.getheaders()}
            status = int(response.status)
            if status in _REDIRECTS:
                target = urlsplit(urljoin(f"https://{current}{path}", headers.get("location") or ""))
                same_site = (target.scheme in {"https", "http"} and target.hostname in allowed
                             and not target.username and not target.password and target.port in {None, 80, 443})
                if headers.get("location") and same_site and hop < PROBE_MAX_HOPS:
                    # An http:// hop on the same site is upgraded: never cleartext.
                    current, path = target.hostname, (target.path or "/") + (f"?{target.query}" if target.query else "")
                    continue
                return {**result, "outcome": "redirect" if headers.get("location") else "http_status",
                        "status": status, "headers": headers, "finalHost": current,
                        "redirectHost": (target.hostname or None) if headers.get("location") else None}
            body = response.read(PROBE_MAX_BYTES) if 200 <= status < 300 else b""
            return {**result, "outcome": "ok" if 200 <= status < 300 else "http_status", "status": status,
                    "headers": headers, "body": body, "finalHost": current}
        except ConnectionRefusedError:
            return {**result, "outcome": "refused", "finalHost": current}
        except (socket.timeout, TimeoutError):
            return {**result, "outcome": "timeout", "finalHost": current}
        except ssl.SSLCertVerificationError:
            return {**result, "outcome": "tls_invalid", "finalHost": current}
        except ssl.SSLError:
            return {**result, "outcome": "tls_error", "finalHost": current}
        except (OSError, http.client.HTTPException):
            return {**result, "outcome": "connect_error", "finalHost": current}
        finally:
            if response is not None:
                response.close()
            client.close()
    return {**result, "outcome": "http_status", "finalHost": current}
