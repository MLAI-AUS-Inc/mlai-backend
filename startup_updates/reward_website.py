"""Bounded public website evidence for the optional Startup Pulse bonus."""

import ipaddress
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
import urllib3


MAX_BYTES = 512 * 1024
_DNS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pulse-website-dns")


def website_evidence(domain, *, allow_host=None, user_agent="MLAI Startup Pulse verification/1.0"):
    """Read public homepage text without cookies, private IPs or unbounded bodies.

    Connect to the validated IP, preserving TLS hostname verification. Every
    redirect is checked independently so DNS changes cannot reach local services.
    allow_host, when given, must accept every hop's host before its lookup.
    """
    url = str(domain or "").strip()
    if "://" not in url:
        url = "https://" + url
    deadline = time.monotonic() + 8
    for _ in range(4):
        if time.monotonic() >= deadline:
            raise TimeoutError("Website evidence deadline exceeded.")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        if (parsed.scheme not in {"http", "https"} or not host
                or parsed.username or parsed.password or parsed.port not in {None, 80, 443}
                or host == "localhost" or host.endswith((".localhost", ".local"))):
            raise ValueError("Website must be public HTTP or HTTPS.")
        if allow_host is not None and not allow_host(host):
            raise ValueError("Website is outside the allowed site.")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        lookup = _DNS_POOL.submit(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM)
        try:
            addresses = lookup.result(timeout=min(2, max(0, deadline - time.monotonic())))
        finally:
            lookup.cancel()
        ips = [str(address[4][0]).split("%", 1)[0] for address in addresses]
        if not ips or not all(ipaddress.ip_address(ip).is_global for ip in ips):
            raise ValueError("Website must resolve to public addresses.")
        pool_class = urllib3.HTTPSConnectionPool if parsed.scheme == "https" else urllib3.HTTPConnectionPool
        tls = {"assert_hostname": host, "server_hostname": host, "cert_reqs": "CERT_REQUIRED"} if parsed.scheme == "https" else {}
        pool = pool_class(ips[0], port=port, timeout=urllib3.Timeout(connect=2, read=3, total=5), **tls)
        response = None
        try:
            response = pool.request(
                "GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                headers={"Host": parsed.netloc, "Accept": "text/html,application/xhtml+xml", "User-Agent": user_agent},
                redirect=False, retries=False, preload_content=False,
            )
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("Website redirect has no destination.")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError("Website is temporarily unavailable.")
            content_type = response.headers.get("Content-Type", "").lower()
            if not any(value in content_type for value in ("text/html", "application/xhtml+xml")):
                raise ValueError("Website did not return a web page.")
            # read1 performs at most one underlying buffered read. A trickling
            # response cannot extend an inter-byte socket timeout indefinitely.
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Website evidence deadline exceeded.")
                chunk = response.read1(min(16 * 1024, MAX_BYTES + 1 - size), decode_content=True)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("Website page is too large.")
                chunks.append(chunk)
            body = b"".join(chunks)
            soup = BeautifulSoup(body, "html.parser")
            metadata = " ".join(str(tag.get("content") or "") for tag in soup.select(
                'meta[name="description"], meta[property="og:description"], meta[property="og:site_name"], meta[property="og:title"]'
            ))
            for tag in soup.select("script, style, template, noscript"):
                tag.decompose()
            return f"{metadata} {soup.get_text(' ', strip=True)}"[:100_000]
        finally:
            if response is not None:
                response.close()
            pool.close()
    raise ValueError("Website redirected too many times.")
