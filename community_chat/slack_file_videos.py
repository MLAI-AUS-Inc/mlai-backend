"""Inline playback of Slack videos visible in MLAI Chat.

Browsers cannot attach the account bearer token to a ``<video>`` element, so
an authenticated request first mints a short-lived signed stream URL bound to
the caller's MLAI Chat session. Every stream request then re-checks that
session and the member's current Slack file authority before reading one
bounded byte range from Slack. Ranges keep each sync worker request short,
and the browser's media stack requests the next range as playback needs it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

import requests
from django.core import signing
from django.core.cache import cache
from django.utils import timezone

from .slack_file_previews import (
    NETWORK_FAILURE_RETRY_SECONDS,
    SLACK_FILE_ID_RE,
    SlackFilePreviewDeferred,
    SlackFilePreviewError,
    _authorized_file,
    _download_response,
)

SLACK_VIDEO_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime"})
SLACK_VIDEO_FILETYPES = frozenset({"mp4", "mov", "webm", "m4v"})
VIDEO_CHUNK_BYTES = 4 * 1024 * 1024
STREAM_TOKEN_MAX_AGE_SECONDS = 6 * 60 * 60
STREAM_TOKEN_SALT = "community-chat.slack-video-stream.v1"
STREAM_REQUESTS_PER_MINUTE = 240
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")


class SlackVideoStreamDenied(Exception):
    """The signed stream URL is no longer valid for its session."""


@dataclass(frozen=True)
class SlackVideoChunk:
    status: int
    content_type: str
    body: bytes
    content_range: str


def _mimetype(file_data) -> str:
    return str(file_data.get("mimetype") or "").split(";", 1)[0].strip().lower()


def is_slack_video(file_data) -> bool:
    """Whether Slack describes the file as a video we can play inline."""
    filetype = str(file_data.get("filetype") or "").strip().lower()
    return bool(
        (_mimetype(file_data).startswith("video/") or filetype in SLACK_VIDEO_FILETYPES)
        and slack_video_source_url(file_data)
    )


def slack_video_source_url(file_data) -> str:
    """Prefer Slack's browser-ready MP4 transcode over the uploaded original."""
    if file_data.get("is_external") or file_data.get("mode") == "external":
        return ""
    for field in ("mp4", "mp4_low"):
        value = str(file_data.get(field) or "").strip()
        if value:
            return value
    if _mimetype(file_data) in SLACK_VIDEO_TYPES:
        return str(
            file_data.get("url_private_download") or file_data.get("url_private") or ""
        ).strip()
    return ""


def slack_video_metadata(file_data) -> dict:
    """Bounded display metadata: duration in seconds and poster dimensions."""
    metadata = {}
    duration_ms = file_data.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and math.isfinite(duration_ms) and duration_ms >= 0:
        metadata["duration"] = round(duration_ms / 1000, 3)
    for source, target in (("thumb_video_w", "width"), ("thumb_video_h", "height")):
        value = file_data.get(source)
        if isinstance(value, int) and 0 < value <= 16384:
            metadata[target] = value
    return metadata


def issue_stream_token(session, file_id: str) -> str:
    return signing.dumps({"s": str(session.pk), "f": file_id}, salt=STREAM_TOKEN_SALT)


def stream_session(token: str):
    """Return ``(session, file_id)`` for a still-valid signed stream URL."""
    from .account_sessions import InvalidAccountSession, _valid_session, _validate_device_owner
    from .models import CommunityChatAccountSession
    from rest_framework.exceptions import PermissionDenied

    from .onboarding import require_community_access

    try:
        payload = signing.loads(
            str(token or ""), salt=STREAM_TOKEN_SALT, max_age=STREAM_TOKEN_MAX_AGE_SECONDS
        )
        session_id, file_id = str(payload["s"]), str(payload["f"])
    except (signing.BadSignature, KeyError, TypeError, ValueError) as exc:
        raise SlackVideoStreamDenied("expired") from exc
    if not SLACK_FILE_ID_RE.fullmatch(file_id):
        raise SlackVideoStreamDenied("invalid")
    session = (
        CommunityChatAccountSession.objects.select_related("user")
        .filter(pk=session_id)
        .first()
    )
    if not _valid_session(session, timezone.now()):
        raise SlackVideoStreamDenied("session")
    try:
        _validate_device_owner(session)
        require_community_access(session.user)
    except InvalidAccountSession as exc:
        raise SlackVideoStreamDenied("session") from exc
    except PermissionDenied as exc:
        raise SlackVideoStreamDenied("membership") from exc
    return session, file_id


def allow_stream_request(session_id) -> bool:
    """A per-session ceiling so a copied stream URL cannot drain Slack budget."""
    key = f"community-chat-slack-video-rate:{session_id}:{int(timezone.now().timestamp() // 60)}"
    cache.add(key, 0, timeout=90)
    try:
        return cache.incr(key) <= STREAM_REQUESTS_PER_MINUTE
    except ValueError:
        return True


def authorize_slack_video(file_id: str, *, user) -> dict:
    """Confirm current access to a playable Slack video and return its metadata."""
    normalized = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized):
        raise SlackFilePreviewError("A valid Slack file is required.")
    with _authorized_file(normalized, user=user) as authorized:
        if not is_slack_video(authorized.data):
            raise SlackFilePreviewError("The Slack file has no playable video.")
        return slack_video_metadata(authorized.data)


def parse_range(header: str) -> tuple[int | None, int | None]:
    """Parse one byte range. ``(None, n)`` is a suffix range of ``n`` bytes."""
    match = _RANGE_RE.fullmatch(str(header or "").strip())
    if not match or match.group(1) == match.group(2) == "":
        return 0, None
    start = int(match.group(1)) if match.group(1) else None
    end = int(match.group(2)) if match.group(2) else None
    if start is not None and end is not None and end < start:
        return 0, None
    return start, end


def fetch_slack_video_range(file_id: str, *, user, range_header: str = "") -> SlackVideoChunk:
    """Read at most one bounded byte range of a Slack video for its member."""
    normalized = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized):
        raise SlackFilePreviewError("A valid Slack file is required.")
    start, end = parse_range(range_header)
    if start is None:
        upstream_range = f"bytes=-{min(end, VIDEO_CHUNK_BYTES)}"
    else:
        last = start + VIDEO_CHUNK_BYTES - 1
        end = last if end is None else min(end, last)
        upstream_range = f"bytes={start}-{end}"
    with _authorized_file(normalized, user=user) as authorized:
        file_data = authorized.data
        source = slack_video_source_url(file_data)
        if not source or not is_slack_video(file_data):
            raise SlackFilePreviewError("The Slack file has no playable video.")
        if not authorized.access_token:
            raise SlackFilePreviewError("Slack video playback is not configured.")
        return _read_range(source, authorized.access_token, upstream_range, start, end,
                           fallback_type=_mimetype(file_data))


def _read_range(source, token, upstream_range, start, end, *, fallback_type):
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = _download_response(
            session,
            source,
            token,
            headers={"Accept": "video/*;q=0.9,*/*;q=0.1", "Range": upstream_range},
        )
        if response.status_code == 416:
            total = str(response.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
            return SlackVideoChunk(416, "", b"", f"bytes */{total if total.isdigit() else '*'}")
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if not content_type.startswith("video/"):
            content_type = fallback_type if fallback_type in SLACK_VIDEO_TYPES else "video/mp4"
        if response.status_code == 206:
            match = _CONTENT_RANGE_RE.fullmatch(str(response.headers.get("Content-Range") or "").strip())
            if not match:
                raise SlackFilePreviewError("Slack returned an unreadable video range.")
            body = _read_body(response, skip=0, limit=VIDEO_CHUNK_BYTES)
            first = int(match.group(1))
            return SlackVideoChunk(
                206, content_type, body,
                f"bytes {first}-{first + len(body) - 1}/{match.group(3)}",
            )
        # The source ignored Range: slice the requested window ourselves.
        try:
            total = int(response.headers.get("Content-Length") or 0) or None
        except (TypeError, ValueError):
            total = None
        if start is None:
            if total is None:
                raise SlackFilePreviewError("Slack returned an unreadable video range.")
            start = max(0, total - end)
            end = start + VIDEO_CHUNK_BYTES - 1
        body = _read_body(response, skip=start, limit=end - start + 1)
        if not body:
            return SlackVideoChunk(416, "", b"", f"bytes */{total or '*'}")
        return SlackVideoChunk(
            206, content_type, body,
            f"bytes {start}-{start + len(body) - 1}/{total or '*'}",
        )
    except requests.RequestException as exc:
        if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
            raise SlackFilePreviewDeferred(NETWORK_FAILURE_RETRY_SECONDS, reason="transport") from exc
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429 or (status is not None and status >= 500):
            raw_retry = exc.response.headers.get("Retry-After", "2")
            raise SlackFilePreviewDeferred(
                int(raw_retry) if str(raw_retry).isdigit() else 2,
                reason="upstream_rate_limit" if status == 429 else "upstream_unavailable",
            ) from exc
        raise SlackFilePreviewError("The Slack video could not be reached.") from exc
    finally:
        if response is not None:
            response.close()
        session.close()


def _read_body(response, *, skip, limit):
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if skip:
            dropped = min(skip, len(chunk))
            chunk = chunk[dropped:]
            skip -= dropped
        if not chunk:
            continue
        chunk = chunk[: limit - size]
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            break
    return b"".join(chunks)
