"""Bounded HTTPS observation pinned to a validated public origin address."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

from .article_live_evidence import MAX_BYTES
from .website_contract import WebsiteAuthorityError


def public_addresses(url, domain):
    """Reject nonexact origins, redirects and every nonpublic DNS answer."""
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != domain.lower().rstrip(".") or parsed.username or parsed.password
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
    """Read at most MAX_BYTES + 1 without proxies, redirects or DNS re-resolution."""
    parsed, addresses = public_addresses(url, domain)
    client = PinnedHTTPSConnection(parsed.hostname, addresses[0])
    response = None
    try:
        client.request("GET", parsed.path or "/", headers={"Host": parsed.hostname, "Connection": "close", "Accept": "text/html", "Accept-Encoding": "identity"})
        response = client.getresponse()
        if expected_status not in {200, 404} or response.status != expected_status:
            raise WebsiteAuthorityError("deployment_route_unverified", "The exact public articles route has not deployed successfully.")
        size = response.getheader("Content-Length")
        if size and (not size.isdigit() or int(size) > MAX_BYTES):
            raise WebsiteAuthorityError("deployment_body_oversized", "The public route exceeds the verification size limit.")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise WebsiteAuthorityError("deployment_body_oversized", "The public route exceeds the verification size limit.")
        return body, {key.lower(): value for key, value in response.getheaders()}
    finally:
        if response is not None:
            response.close()
        client.close()
