"""Authenticated previews for Slack files visible in MLAI Chat."""

from __future__ import annotations

import hashlib
import math
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse, urljoin
from urllib.error import URLError

import requests
from django.core.cache import cache
from slack_sdk.errors import SlackApiError, SlackRequestError

from contextlib import contextmanager
from integrations.services.message_sync.scheduler import BudgetDeferred
from integrations.services.slack_dm_mirror import SlackDmMirrorRateLimited


SLACK_FILE_ID_RE = re.compile(r"^F[A-Z0-9]+$")
SLACK_IMAGE_LIMIT_BYTES = 10 * 1024 * 1024
SLACK_REQUEST_TIMEOUT = (3, 10)
FAILURE_CACHE_SECONDS = 60
NETWORK_FAILURE_RETRY_SECONDS = 15
PREVIEW_WAIT_REASONS = frozenset({
    "metadata_pending", "single_flight", "shared_budget", "upstream_rate_limit",
    "upstream_unavailable", "transport",
})
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

    def __init__(self, retry_after=1, *, reason="metadata_pending"):
        super().__init__("Slack is preparing this preview. It will retry shortly.")
        self.retry_after = max(1, int(retry_after))
        self.reason = reason if isinstance(reason, str) and reason in PREVIEW_WAIT_REASONS else "metadata_pending"


def _raise_cached_failure(cache_key: str) -> None:
    failure = cache.get(cache_key + ":failure")
    if not isinstance(failure, dict):
        return
    if failure.get("retry_at"):
        raise SlackFilePreviewDeferred(
            math.ceil(failure["retry_at"] - time.time()),
            reason=failure.get("reason", "metadata_pending"),
        )
    raise SlackFilePreviewError("The Slack preview is temporarily unavailable.")


def _remember_failure(cache_key: str, error: SlackFilePreviewError) -> None:
    """Cache only safe failure state in the same scope as the source read."""
    retry_after = getattr(error, "retry_after", None)
    cache.set(
        cache_key + ":failure",
        {
            "retry_at": time.time() + retry_after if retry_after else None,
            "reason": error.reason if isinstance(error, SlackFilePreviewDeferred) else None,
        },
        timeout=min(FAILURE_CACHE_SECONDS, retry_after or FAILURE_CACHE_SECONDS),
    )


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
    with _authorized_file(file_id, user=user) as authorized:
        return _preview(file_id, raw_url, authorized.data)


def _preview(file_id, raw_url, file_data):
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
    """Download one image while retaining its current member authority."""

    normalized_file_id = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized_file_id):
        raise SlackFilePreviewError("A valid Slack file is required.")
    with _authorized_file(normalized_file_id, user=user) as authorized:
        return _image(normalized_file_id, authorized, original=original)


def _image(normalized_file_id, authorized, *, original=False):
    file_data = authorized.data
    if file_data.get("is_external") or file_data.get("mode") == "external":
        raise SlackFilePreviewError("External Slack files have no supported image preview.")
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
    # Authorization above still runs on every request, including cache hits.
    _raise_cached_failure(cache_key)

    private_url = slack_image_download_url(file_data, original=original)
    _validate_download_url(private_url)

    token = authorized.access_token
    if not token:
        raise SlackFilePreviewError("Slack image previews are not configured.")
    session = requests.Session()
    session.trust_env = False
    try:
        response = _download_response(session, private_url, token)
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
            error = SlackFilePreviewDeferred(NETWORK_FAILURE_RETRY_SECONDS, reason="transport")
        elif (response_status := getattr(exc.response, "status_code", None)) == 429 or (
            response_status is not None and response_status >= 500
        ):
            raw_retry = exc.response.headers.get("Retry-After", "2")
            error = SlackFilePreviewDeferred(
                int(raw_retry) if str(raw_retry).isdigit() else 2,
                reason="upstream_rate_limit" if response_status == 429 else "upstream_unavailable",
            )
        else:
            error = SlackFilePreviewError("The Slack image could not be reached.")
        _remember_failure(cache_key, error)
        raise error from exc
    except SlackFilePreviewError as exc:
        _remember_failure(cache_key, exc)
        raise
    finally:
        if "response" in locals():
            response.close()
        session.close()

    response_type = str(response.headers.get("Content-Type") or content_type)
    response_type = response_type.split(";", 1)[0].strip().lower()
    if response_type not in ALLOWED_IMAGE_TYPES:
        error = SlackFilePreviewError("The Slack file is not a supported image.")
        _remember_failure(cache_key, error)
        raise error
    body = b"".join(chunks)
    cache.set(
        cache_key,
        {"body": body, "content_type": response_type},
        timeout=6 * 60 * 60,
    )
    return response_type, body


def _validate_download_url(url):
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "https" and parsed.hostname == "files.slack.com"
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise SlackFilePreviewError("The Slack image URL was unavailable.")


def _download_response(session, url, token):
    # Slack remote-file URLs are app supplied. Validate every hop before
    # attaching the member token; never follow an external or HTTP redirect.
    for _ in range(3):
        _validate_download_url(url)
        response = session.get(url, allow_redirects=False, stream=True, timeout=SLACK_REQUEST_TIMEOUT,
            headers={"Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif;q=0.8",
                     "Authorization": f"Bearer {token}"})
        if not (response.is_redirect or response.is_permanent_redirect):
            return response
        destination = response.headers.get("Location", "")
        response.close()
        if not destination:
            break
        url = urljoin(url, destination)
    raise SlackFilePreviewError("The Slack image URL was unavailable.")


@contextmanager
def _authorized_file(file_id: str, *, user=None):
    from integrations.services.slack_file_authority import authorized_file

    with authorized_file(file_id, user=user, load=_slack_file_info) as (data, token, scope):
        yield _AuthorizedSlackFile(data, token, scope)


def _slack_file_info(
    file_id: str,
    *,
    read,
    cache_scope: str,
    fresh: bool = False,
) -> dict:
    cache_key = (
        "community-chat-slack-file-info-v3:"
        + hashlib.sha256(f"{cache_scope}:{file_id}".encode("utf-8")).hexdigest()
    )
    cached = cache.get(cache_key)
    if not fresh and isinstance(cached, dict):
        return cached
    _raise_cached_failure(cache_key)
    # Concurrent rows/devices requesting the same file share one source read.
    # Authorization still runs for every caller after this metadata lookup.
    lock_key = cache_key + ":loading"
    if not cache.add(lock_key, True, timeout=30):
        raise SlackFilePreviewDeferred(1, reason="single_flight")
    try:
        return _fetch_slack_file_info(file_id, read=read, cache_key=cache_key)
    finally:
        cache.delete(lock_key)


def _fetch_slack_file_info(file_id, *, read, cache_key):
    try:
        response = read(file=file_id)
    except BudgetDeferred as exc:
        raise SlackFilePreviewDeferred(exc.retry_after, reason="shared_budget") from exc
    except SlackDmMirrorRateLimited as exc:
        raise SlackFilePreviewDeferred(2, reason="upstream_rate_limit") from exc
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
            error = SlackFilePreviewDeferred(
                int(raw_retry) if str(raw_retry).isdigit() else 2,
                reason="upstream_rate_limit" if exc.response.get("error") == "ratelimited" else "upstream_unavailable",
            )
        else:
            error = SlackFilePreviewError("The Slack file could not be loaded.")
        _remember_failure(cache_key, error)
        raise error from exc
    except (TimeoutError, ConnectionError, URLError, SlackRequestError) as exc:
        error = SlackFilePreviewDeferred(NETWORK_FAILURE_RETRY_SECONDS, reason="transport")
        _remember_failure(cache_key, error)
        raise error from exc
    if (not response.get("ok") or not isinstance(response.get("file"), dict)
            or response["file"].get("id") != file_id):
        error = SlackFilePreviewError("The Slack file could not be loaded.")
        _remember_failure(cache_key, error)
        raise error
    file_data = dict(response["file"])
    cache.delete(cache_key + ":failure")
    cache.set(cache_key, file_data, timeout=60 * 60)
    return file_data
