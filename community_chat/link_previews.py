"""Server-side link preview fetching with public-network and size boundaries."""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from django.core.cache import cache


HTML_LIMIT_BYTES = 512 * 1024
IMAGE_LIMIT_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
REQUEST_TIMEOUT = (3, 5)
USER_AGENT = "MLAI Chat link preview/1.0"


class LinkPreviewError(ValueError):
    """A public-safe link preview validation or retrieval failure."""


@dataclass(frozen=True)
class LinkPreview:
    href: str
    title: str
    description: str
    site_name: str
    image_url: str

    def as_payload(self) -> dict[str, str]:
        return {
            "href": self.href,
            "title": self.title,
            "description": self.description,
            "site_name": self.site_name,
            "image_url": self.image_url,
        }


def fetch_link_preview(raw_url: str) -> LinkPreview:
    normalized_url = _validated_public_url(raw_url)
    cache_key = "community-chat-link-preview:" + hashlib.sha256(
        normalized_url.encode("utf-8")
    ).hexdigest()
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return LinkPreview(**cached)

    final_url, content_type, body = _fetch_limited(
        normalized_url,
        accept="text/html,application/xhtml+xml;q=0.9",
        maximum_bytes=HTML_LIMIT_BYTES,
    )
    if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
        raise LinkPreviewError("The link did not return an HTML page.")

    soup = BeautifulSoup(body, "lxml")
    title = _meta_content(soup, "property", "og:title") or _meta_content(
        soup, "name", "twitter:title"
    )
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    description = _meta_content(soup, "property", "og:description") or _meta_content(
        soup, "name", "description"
    )
    site_name = _meta_content(soup, "property", "og:site_name")
    image_url = _meta_content(soup, "property", "og:image") or _meta_content(
        soup, "name", "twitter:image"
    )
    if image_url:
        try:
            image_url = _validated_public_url(urljoin(final_url, image_url))
        except LinkPreviewError:
            image_url = ""

    host = (urlparse(final_url).hostname or "Link").removeprefix("www.")
    preview = LinkPreview(
        href=final_url,
        title=_bounded_text(title or host, 220),
        description=_bounded_text(description, 360),
        site_name=_bounded_text(site_name or host, 120),
        image_url=image_url,
    )
    cache.set(cache_key, preview.as_payload(), timeout=6 * 60 * 60)
    return preview


def fetch_preview_image(raw_url: str) -> tuple[str, bytes]:
    normalized_url = _validated_public_url(raw_url)
    cache_key = "community-chat-link-preview-image:" + hashlib.sha256(
        normalized_url.encode("utf-8")
    ).hexdigest()
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("body"), bytes):
        return str(cached.get("content_type") or "image/jpeg"), cached["body"]

    _, content_type, body = _fetch_limited(
        normalized_url,
        accept="image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.8",
        maximum_bytes=IMAGE_LIMIT_BYTES,
    )
    normalized_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_type not in {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }:
        raise LinkPreviewError("The preview image used an unsupported content type.")
    cache.set(
        cache_key,
        {"body": body, "content_type": normalized_type},
        timeout=6 * 60 * 60,
    )
    return normalized_type, body


def _fetch_limited(
    raw_url: str,
    *,
    accept: str,
    maximum_bytes: int,
) -> tuple[str, str, bytes]:
    session = requests.Session()
    session.trust_env = False
    current_url = raw_url
    for redirect_count in range(MAX_REDIRECTS + 1):
        current_url = _validated_public_url(current_url)
        try:
            response = session.get(
                current_url,
                allow_redirects=False,
                headers={"Accept": accept, "User-Agent": USER_AGENT},
                stream=True,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise LinkPreviewError("The link could not be reached.") from exc

        if response.is_redirect or response.is_permanent_redirect:
            location = str(response.headers.get("Location") or "").strip()
            response.close()
            if not location or redirect_count >= MAX_REDIRECTS:
                raise LinkPreviewError("The link redirected too many times.")
            current_url = urljoin(current_url, location)
            continue
        try:
            response.raise_for_status()
            declared_length = int(response.headers.get("Content-Length") or 0)
            if declared_length > maximum_bytes:
                raise LinkPreviewError("The preview response was too large.")
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=16 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > maximum_bytes:
                    raise LinkPreviewError("The preview response was too large.")
                chunks.append(chunk)
            return (
                current_url,
                str(response.headers.get("Content-Type") or "").lower(),
                b"".join(chunks),
            )
        except requests.RequestException as exc:
            raise LinkPreviewError("The link returned an unsuccessful response.") from exc
        finally:
            response.close()
    raise LinkPreviewError("The link redirected too many times.")


def _validated_public_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value or len(value) > 2048:
        raise LinkPreviewError("A valid link is required.")
    parsed = urlparse(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise LinkPreviewError("The link used an invalid port.") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise LinkPreviewError("Only public HTTP and HTTPS links are supported.")
    if port not in {None, 80, 443}:
        raise LinkPreviewError("The link used an unsupported port.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise LinkPreviewError("Private network links cannot be previewed.")
    try:
        addresses = socket.getaddrinfo(
            hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise LinkPreviewError("The link host could not be resolved.") from exc
    if not addresses:
        raise LinkPreviewError("The link host could not be resolved.")
    for address in addresses:
        resolved = str(address[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(resolved)
        except ValueError as exc:
            raise LinkPreviewError("The link host returned an invalid address.") from exc
        if not ip.is_global:
            raise LinkPreviewError("Private network links cannot be previewed.")
    return parsed.geturl()


def _meta_content(soup: BeautifulSoup, attribute: str, value: str) -> str:
    tag = soup.find("meta", attrs={attribute: value})
    if tag is None:
        return ""
    return str(tag.get("content") or "").strip()


def _bounded_text(value: object, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]
