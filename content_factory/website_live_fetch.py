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
