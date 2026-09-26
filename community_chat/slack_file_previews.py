"""Authenticated previews for Slack files visible in MLAI Chat."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.error import URLError

import requests
from django.conf import settings
from django.core.cache import cache
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackRequestError

from integrations.models import (
    CommunityBridgeChannel,
    CommunityBridgePlatform,
    ExternalServiceConnectionStatus,
    SlackDmMirrorConversation,
    SlackDmMirrorConversationStatus,
    SlackDmMirrorGrant,
    SlackDmMirrorGrantStatus,
)
from integrations.services.community_bridge.slack import SlackBridgeClient
from integrations.services.message_sync.configuration import user_app_id
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.message_sync.slack_client import budgeted_client


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


class SlackFilePreviewDeferred(SlackFilePreviewError):
    """A temporary source delay that clients can retry without reconnecting."""

    def __init__(self, retry_after=1):
        super().__init__("Slack is preparing this preview. It will retry shortly.")
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class SlackFilePreview:
    file_id: str
    href: str
    title: str
    description: str
    site_name: str
    content_type: str
    has_thumbnail: bool = False

    @property
    def is_image(self) -> bool:
        return self.content_type in ALLOWED_IMAGE_TYPES or self.has_thumbnail

    def as_payload(self) -> dict[str, str | bool]:
        return {
            "href": self.href,
            "title": self.title,
            "description": self.description,
            "site_name": self.site_name,
            "image_url": "",
            "image_is_thumbnail": self.content_type not in ALLOWED_IMAGE_TYPES,
        }


@dataclass(frozen=True)
class _AuthorizedSlackFile:
    data: dict
    access_token: str
    cache_scope: str


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


def fetch_slack_file_preview(raw_url: str, *, user=None) -> SlackFilePreview | None:
    """Resolve a Slack permalink, or return ``None`` for a non-Slack URL."""

    file_id = slack_file_id_from_url(raw_url)
    if not file_id:
        return None
    file_data = _authorized_file(file_id, user=user).data
    title = str(file_data.get("title") or file_data.get("name") or "Slack file").strip()
    content_type = str(file_data.get("mimetype") or "").split(";", 1)[0].strip().lower()
    href = str(file_data.get("permalink") or raw_url).strip()
    filetype = "image" if content_type in ALLOWED_IMAGE_TYPES else "file"
    return SlackFilePreview(
        file_id=file_id,
        href=href,
        title=title[:220],
        description=f"{filetype.capitalize()} shared in MLAI Slack",
        site_name="MLAI Slack",
        content_type=content_type,
        has_thumbnail=bool(_slack_thumbnail_url(file_data)),
    )


def _slack_thumbnail_url(file_data):
    for field in (
        "thumb_1024",
        "thumb_960",
        "thumb_800",
        "thumb_720",
        "thumb_pdf",
        "thumb_480",
        "thumb_360",
    ):
        if file_data.get(field):
            return str(file_data[field]).strip()
    return ""


def slack_image_download_url(file_data, *, original=False):
    """Use Slack's uncropped display rendition, preserving animated originals."""
    if not original and file_data.get("mimetype") != "image/gif":
        thumbnail = _slack_thumbnail_url(file_data)
        if thumbnail:
            return thumbnail
    return str(
        file_data.get("url_private_download") or file_data.get("url_private") or ""
    ).strip()


def fetch_slack_file_image(
    file_id: str, *, user=None, original=False
) -> tuple[str, bytes]:
    """Download one authorized Slack image without exposing the bot token."""

    normalized_file_id = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized_file_id):
        raise SlackFilePreviewError("A valid Slack file is required.")
    authorized = _authorized_file(normalized_file_id, user=user)
    file_data = authorized.data
    content_type = str(file_data.get("mimetype") or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_TYPES and (
        original or not _slack_thumbnail_url(file_data)
    ):
        raise SlackFilePreviewError("The Slack file has no supported image preview.")

    cache_key = (
        "community-chat-slack-file-image-v2:"
        + hashlib.sha256(
            f"{authorized.cache_scope}:{normalized_file_id}:{original}".encode("utf-8")
        ).hexdigest()
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict) and isinstance(cached.get("body"), bytes):
        return str(cached.get("content_type") or content_type), cached["body"]

    private_url = slack_image_download_url(file_data, original=original)
    private_host = (urlparse(private_url).hostname or "").lower().rstrip(".")
    if not private_url.startswith("https://") or not private_host.endswith(
        ".slack.com"
    ):
        raise SlackFilePreviewError("The Slack image URL was unavailable.")

    token = authorized.access_token
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
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            raise SlackFilePreviewDeferred(2) from exc
        response_status = getattr(exc.response, "status_code", None)
        if response_status == 429 or (
            response_status is not None and response_status >= 500
        ):
            raw_retry = exc.response.headers.get("Retry-After", "2")
            raise SlackFilePreviewDeferred(
                int(raw_retry) if str(raw_retry).isdigit() else 2
            ) from exc
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


def _authorized_file(file_id: str, *, user=None) -> _AuthorizedSlackFile:
    bot_token = str(getattr(settings, "SLACK_BRIDGE_BOT_TOKEN", "") or "").strip()
    file_data = None
    if bot_token:
        try:
            file_data = _slack_file_info(file_id)
        except SlackFilePreviewDeferred:
            # A shared method budget cannot be bypassed using another token.
            raise
        except SlackFilePreviewError:
            file_data = None
    if file_data is not None and _file_is_in_mapped_public_channel(file_data):
        return _AuthorizedSlackFile(
            data=file_data,
            access_token=bot_token,
            cache_scope="bot",
        )

    private_file = _authorized_private_file(file_id, user=user)
    if private_file is not None:
        return private_file
    raise SlackFilePreviewError(
        "The Slack file is not shared in an MLAI Chat conversation."
    )


def _file_is_in_mapped_public_channel(file_data: dict) -> bool:
    shared_channel_ids = {
        str(channel_id or "").strip()
        for channel_id in (file_data.get("channels") or [])
        if str(channel_id or "").strip()
    }
    public_shares = (file_data.get("shares") or {}).get("public") or {}
    shared_channel_ids.update(str(channel_id).strip() for channel_id in public_shares)
    return (
        bool(shared_channel_ids)
        and CommunityBridgeChannel.objects.filter(
            enabled=True,
            destination_platform=CommunityBridgePlatform.BUZZ,
            slack_channel_id__in=shared_channel_ids,
        ).exists()
    )


def _authorized_private_file(file_id: str, *, user=None) -> _AuthorizedSlackFile | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    grant = (
        SlackDmMirrorGrant.objects.select_related("connection")
        .filter(
            user=user,
            status__in=(
                SlackDmMirrorGrantStatus.ACTIVE,
                SlackDmMirrorGrantStatus.PAUSED,
            ),
            revoked_at__isnull=True,
            connection__status__in=(
                ExternalServiceConnectionStatus.CONNECTED,
                ExternalServiceConnectionStatus.SYNCING,
            ),
        )
        .order_by("-updated_at")
        .first()
    )
    if grant is None or "files:read" not in set(grant.connection.scopes or []):
        return None
    token = str(grant.connection.access_token or "").strip()
    if not token:
        return None
    private_cache_scope = f"user:{user.pk}:workspace:{grant.slack_workspace_id}"
    file_data = _slack_file_info(
        file_id,
        access_token=token,
        cache_scope=private_cache_scope,
        workspace_id=grant.slack_workspace_id,
    )
    file_workspace_id = str(
        file_data.get("team_id") or file_data.get("user_team") or ""
    ).strip()
    if file_workspace_id and file_workspace_id != grant.slack_workspace_id:
        return None
    private_channel_ids = {
        str(channel_id or "").strip()
        for field in ("ims", "groups")
        for channel_id in (file_data.get(field) or [])
        if str(channel_id or "").strip()
    }
    private_shares = (file_data.get("shares") or {}).get("private") or {}
    private_channel_ids.update(
        str(channel_id or "").strip()
        for channel_id in private_shares
        if str(channel_id or "").strip()
    )
    if not private_channel_ids:
        return None
    visible = SlackDmMirrorConversation.objects.filter(
        grant=grant,
        slack_conversation_id__in=private_channel_ids,
        status__in=(
            SlackDmMirrorConversationStatus.LIVE,
            SlackDmMirrorConversationStatus.PAUSED,
        ),
    ).exists()
    if not visible:
        return None
    return _AuthorizedSlackFile(
        data=file_data,
        access_token=token,
        cache_scope=private_cache_scope,
    )


def _slack_file_info(
    file_id: str,
    *,
    access_token: str = "",
    cache_scope: str = "bot",
    workspace_id: str = "",
) -> dict:
    cache_key = (
        "community-chat-slack-file-info:"
        + hashlib.sha256(f"{cache_scope}:{file_id}".encode("utf-8")).hexdigest()
    )
    cached = cache.get(cache_key)
    if isinstance(cached, dict):
        return cached
    # Concurrent rows/devices requesting the same file share one source read.
    # Authorization still runs for every caller after this metadata lookup.
    lock_key = cache_key + ":loading"
    if not cache.add(lock_key, True, timeout=30):
        raise SlackFilePreviewDeferred(1)
    try:
        return _fetch_slack_file_info(file_id, access_token=access_token,
                                      workspace_id=workspace_id, cache_key=cache_key)
    finally:
        cache.delete(lock_key)


def _fetch_slack_file_info(file_id, *, access_token, workspace_id, cache_key):
    if not access_token and not SlackBridgeClient.is_configured():
        raise SlackFilePreviewError("Slack image previews are not configured.")
    try:
        client = (
            budgeted_client(
                WebClient(token=access_token, timeout=SLACK_REQUEST_TIMEOUT[1]),
                workspace_id=workspace_id,
                app_id=user_app_id(),
            )
            if access_token
            else SlackBridgeClient.get_client()
        )
        response = client.files_info(file=file_id)
    except BudgetDeferred as exc:
        raise SlackFilePreviewDeferred(exc.retry_after) from exc
    except SlackApiError as exc:
        if exc.response.get("error") in {
            "ratelimited",
            "internal_error",
            "service_unavailable",
            "request_timeout",
        }:
            raw_retry = (getattr(exc.response, "headers", {}) or {}).get(
                "Retry-After", "2"
            )
            raise SlackFilePreviewDeferred(
                int(raw_retry) if str(raw_retry).isdigit() else 2
            ) from exc
        raise SlackFilePreviewError("The Slack file could not be loaded.") from exc
    except (TimeoutError, ConnectionError, URLError, SlackRequestError) as exc:
        raise SlackFilePreviewDeferred(2) from exc
    except Exception as exc:
        raise SlackFilePreviewError("The Slack file could not be loaded.") from exc
    if not response.get("ok") or not isinstance(response.get("file"), dict):
        raise SlackFilePreviewError("The Slack file could not be loaded.")
    file_data = dict(response["file"])
    cache.set(cache_key, file_data, timeout=60 * 60)
    return file_data
