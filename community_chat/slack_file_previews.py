"""Authenticated previews for files shared from mapped public Slack channels."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from slack_sdk.errors import SlackApiError

from integrations.models import CommunityBridgeChannel, CommunityBridgePlatform
from integrations.services.community_bridge.slack import SlackBridgeClient


SLACK_FILE_ID_RE = re.compile(r"^F[A-Z0-9]+$")
SLACK_IMAGE_LIMIT_BYTES = 10 * 1024 * 1024
SLACK_REQUEST_TIMEOUT = (3, 10)
ALLOWED_IMAGE_TYPES = frozenset(
    {
        "image/avif",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/webp",
    }
)


class SlackFilePreviewError(ValueError):
    """A public-safe Slack file validation or retrieval failure."""


@dataclass(frozen=True)
class SlackFilePreview:
    file_id: str
    href: str
    title: str
    description: str
    site_name: str
    content_type: str

    @property
    def is_image(self) -> bool:
        return self.content_type in ALLOWED_IMAGE_TYPES

    def as_payload(self) -> dict[str, str]:
        return {
            "href": self.href,
            "title": self.title,
            "description": self.description,
            "site_name": self.site_name,
            "image_url": "",
        }


def slack_file_id_from_url(raw_url: str) -> str:
    """Return a Slack file ID for a canonical workspace file permalink."""

    try:
        parsed = urlparse(str(raw_url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host.endswith(".slack.com"):
        return ""
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) < 3 or segments[0] != "files":
        return ""
    file_id = segments[2].upper()
    return file_id if SLACK_FILE_ID_RE.fullmatch(file_id) else ""


def fetch_slack_file_preview(raw_url: str) -> SlackFilePreview | None:
    """Resolve a Slack permalink, or return ``None`` for a non-Slack URL."""

    file_id = slack_file_id_from_url(raw_url)
    if not file_id:
        return None
    file_data = _authorized_file(file_id)
    title = str(
        file_data.get("title") or file_data.get("name") or "Slack file"
    ).strip()
    content_type = (
        str(file_data.get("mimetype") or "").split(";", 1)[0].strip().lower()
    )
    href = str(file_data.get("permalink") or raw_url).strip()
    filetype = "image" if content_type in ALLOWED_IMAGE_TYPES else "file"
    return SlackFilePreview(
        file_id=file_id,
        href=href,
        title=title[:220],
        description=f"{filetype.capitalize()} shared in MLAI Slack",
        site_name="MLAI Slack",
        content_type=content_type,
    )


def fetch_slack_file_image(file_id: str) -> tuple[str, bytes]:
    """Download one authorized Slack image without exposing the bot token."""

    normalized_file_id = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized_file_id):
        raise SlackFilePreviewError("A valid Slack file is required.")
    file_data = _authorized_file(normalized_file_id)
    content_type = (
        str(file_data.get("mimetype") or "").split(";", 1)[0].strip().lower()
    )
    if content_type not in ALLOWED_IMAGE_TYPES:
        raise SlackFilePreviewError("The Slack file is not a supported image.")

    cache_key = "community-chat-slack-file-image:" + hashlib.sha256(
        normalized_file_id.encode("utf-8")
    ).hexdigest()
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("body"), bytes):
        return str(cached.get("content_type") or content_type), cached["body"]

    private_url = str(
        file_data.get("url_private_download") or file_data.get("url_private") or ""
    ).strip()
    private_host = (urlparse(private_url).hostname or "").lower().rstrip(".")
    if not private_url.startswith("https://") or not private_host.endswith(".slack.com"):
        raise SlackFilePreviewError("The Slack image URL was unavailable.")

    token = str(getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", "") or "").strip()
    if not token:
        raise SlackFilePreviewError("Slack image previews are not configured.")
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            private_url,
            allow_redirects=True,
            headers={
                "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.8",
                "Authorization": f"Bearer {token}",
            },
            stream=True,
            timeout=SLACK_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        try:
            declared_length = int(response.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            declared_length = 0
        if declared_length > SLACK_IMAGE_LIMIT_BYTES:
            raise SlackFilePreviewError("The Slack image was too large to preview.")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(chunk_size=16 * 1024):
            size += len(chunk)
            if size > SLACK_IMAGE_LIMIT_BYTES:
                raise SlackFilePreviewError("The Slack image was too large to preview.")
            chunks.append(chunk)
    except requests.RequestException as exc:
        raise SlackFilePreviewError("The Slack image could not be reached.") from exc
    finally:
        if "response" in locals():
            response.close()

    response_type = str(response.headers.get("Content-Type") or content_type)
    response_type = response_type.split(";", 1)[0].strip().lower()
    if response_type not in ALLOWED_IMAGE_TYPES:
        raise SlackFilePreviewError("The Slack file is not a supported image.")
    body = b"".join(chunks)
    cache.set(
        cache_key,
        {"body": body, "content_type": response_type},
        timeout=6 * 60 * 60,
    )
    return response_type, body


def _authorized_file(file_id: str) -> dict:
    file_data = _slack_file_info(file_id)
    shared_channel_ids = {
        str(channel_id or "").strip()
        for channel_id in (file_data.get("channels") or [])
        if str(channel_id or "").strip()
    }
    public_shares = (file_data.get("shares") or {}).get("public") or {}
    shared_channel_ids.update(str(channel_id).strip() for channel_id in public_shares)
    if not shared_channel_ids:
        raise SlackFilePreviewError("The Slack file is not shared in a public channel.")
    is_mapped = CommunityBridgeChannel.objects.filter(
        enabled=True,
        destination_platform=CommunityBridgePlatform.BUZZ,
        slack_channel_id__in=shared_channel_ids,
    ).exists()
    if not is_mapped:
        raise SlackFilePreviewError(
            "The Slack file is not shared in an MLAI Chat channel."
        )
    return file_data


def _slack_file_info(file_id: str) -> dict:
    cache_key = "community-chat-slack-file-info:" + hashlib.sha256(
        file_id.encode("utf-8")
    ).hexdigest()
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    if not SlackBridgeClient.is_configured():
        raise SlackFilePreviewError("Slack image previews are not configured.")
    try:
        response = SlackBridgeClient.get_client().files_info(file=file_id)
    except SlackApiError as exc:
        raise SlackFilePreviewError("The Slack file could not be loaded.") from exc
    except Exception as exc:
        raise SlackFilePreviewError("The Slack file could not be loaded.") from exc
    if not response.get("ok") or not isinstance(response.get("file"), dict):
        raise SlackFilePreviewError("The Slack file could not be loaded.")
    file_data = dict(response["file"])
    cache.set(cache_key, file_data, timeout=60 * 60)
    return file_data
