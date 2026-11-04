"""Bounded HTTPS delivery with DNS pinning and no redirect authority."""

from dataclasses import dataclass
import http.client
import ipaddress
import queue
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit


class CallbackEndpointError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class WebhookResponse:
    status: int
    body: bytes


_DNS_SLOTS = threading.BoundedSemaphore(8)


def _bounded_resolve(hostname, resolver, *, timeout):
    # getaddrinfo has no portable timeout. A bounded pool of daemon calls lets
    # the request stop waiting without allowing slow DNS to exhaust threads.
    if not _DNS_SLOTS.acquire(blocking=False):
        raise CallbackEndpointError("dns_busy")
    answer = queue.Queue(maxsize=1)
    def resolve():
        try:
            answer.put((True, resolver(hostname, 443, type=socket.SOCK_STREAM)))
        except Exception:
            answer.put((False, None))
        finally:
            _DNS_SLOTS.release()
    thread = threading.Thread(target=resolve, name="agent-callback-dns", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        _DNS_SLOTS.release()
        raise CallbackEndpointError("dns_busy") from None
    try:
        succeeded, addresses = answer.get(timeout=timeout)
    except queue.Empty:
        raise CallbackEndpointError("dns_timeout") from None
    if not succeeded:
        raise CallbackEndpointError("invalid_destination")
    return addresses


def resolve_callback(url, *, resolver=socket.getaddrinfo, dns_timeout=3):
    if (not isinstance(url, str) or len(url) > 2048
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url)):
        raise CallbackEndpointError("invalid_url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or parsed.port not in {None, 443}):
            raise CallbackEndpointError("invalid_url")
        hostname = parsed.hostname.encode("idna").decode("ascii")
        addresses = _bounded_resolve(hostname, resolver, timeout=min(3, max(0.001, dns_timeout)))
    except CallbackEndpointError:
        raise
    except (ValueError, UnicodeError, OSError):
        raise CallbackEndpointError("invalid_destination") from None
    ips = sorted({item[4][0] for item in addresses})
    if not ips:
        raise CallbackEndpointError("invalid_destination")
    for raw in ips:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise CallbackEndpointError("invalid_destination") from None
        if (not address.is_global or address.is_multicast or address.is_unspecified
                or getattr(address, "ipv4_mapped", None) is not None
                or (isinstance(address, ipaddress.IPv6Address) and any(address in ipaddress.ip_network(network)
                    for network in ("64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32")))):
            raise CallbackEndpointError("private_destination")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    # HTTP request targets are ASCII; preserve existing %-encoding while
    # encoding Unicode paths instead of leaking a UnicodeEncodeError as a 500.
    target = quote(target, safe="/%?=&!$'()*+,-.:;@_~")
    return hostname, ips[0], target


def post_webhook(url, body, headers, *, timeout=8, resolver=socket.getaddrinfo, max_response_bytes=65536):
    """POST exact bytes under one total deadline and a bounded response size.

    Event callbacks retain the 64 KiB default. The configured native relay may
    request up to 256 KiB for bounded thread context through this same transport.
    """
    if not isinstance(body, bytes) or len(body) > 262144:
        raise CallbackEndpointError("payload_too_large")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 15:
        raise CallbackEndpointError("invalid_timeout")
    if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 262144:
        raise CallbackEndpointError("invalid_response_bound")
    deadline = time.monotonic() + timeout
    hostname, address, target = resolve_callback(url, resolver=resolver, dns_timeout=min(3, timeout))
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CallbackEndpointError("timeout")
    # HTTPSConnection still uses the original host for TLS SNI/certificate
    # validation; only the TCP destination is replaced by the validated IP.
    connection = http.client.HTTPSConnection(hostname, 443, timeout=remaining, context=ssl.create_default_context())
    def pinned_connection(_destination, socket_timeout, source_address=None):
        available = deadline - time.monotonic()
        if available <= 0:
            raise TimeoutError()
        sock = socket.create_connection((address, 443), min(socket_timeout, available), source_address)
        available = deadline - time.monotonic()
        if available <= 0:
            sock.close()
            raise TimeoutError()
        sock.settimeout(available)
        return sock
    connection._create_connection = pinned_connection
    active_socket = None
    def expire():
        active = active_socket or connection.sock
        if active:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
    # A peer that trickles headers/body must not reset the per-read timeout
    # forever. Socket shutdown interrupts HTTPResponse's buffered socket too.
    timer = threading.Timer(max(0.001, deadline - time.monotonic()), expire)
    timer.daemon = True
    timer.start()
    try:
        connection.request("POST", target, body=body, headers={
            **headers, "Host": f"[{hostname}]" if ":" in hostname else hostname,
            "Content-Type": "application/json", "Content-Length": str(len(body)),
        })
        active_socket = connection.sock
        response = connection.getresponse()
        content = response.read(max_response_bytes + 1)
        if len(content) > max_response_bytes:
            raise CallbackEndpointError("response_too_large")
        if 300 <= response.status < 400:
            raise CallbackEndpointError("redirect_rejected")
        return WebhookResponse(response.status, content)
    except (socket.timeout, TimeoutError):
        raise CallbackEndpointError("timeout") from None
    except (OSError, http.client.HTTPException):
        raise CallbackEndpointError("connection_failed") from None
    finally:
        timer.cancel()
        connection.close()
