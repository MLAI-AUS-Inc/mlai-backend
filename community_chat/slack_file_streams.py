"""Inline playback and download of Slack files visible in MLAI Chat.

Browsers cannot attach the account bearer token to a ``<video>`` or
``<audio>`` element, so an authenticated request first mints a short-lived
signed stream URL bound to the caller's MLAI Chat session. Every stream
request then re-checks that session and the member's current Slack file
authority before reading one bounded byte range from Slack. Ranges keep each
sync worker request short; media elements and downloads request the next
range as they need it.

This module also classifies Slack files so the app can render each kind the
way Slack does: media inline, audio as a player, snippets as code, and every
other file as a typed file card.
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
    ALLOWED_IMAGE_TYPES,
    NETWORK_FAILURE_RETRY_SECONDS,
    SLACK_FILE_ID_RE,
    SlackFilePreviewDeferred,
    SlackFilePreviewError,
    _authorized_file,
    _download_response,
)

SLACK_VIDEO_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime"})
SLACK_VIDEO_FILETYPES = frozenset({"mp4", "mov", "webm", "m4v"})
SLACK_AUDIO_TYPES = frozenset({
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac",
    "audio/wav", "audio/x-wav", "audio/wave", "audio/ogg", "audio/webm", "audio/flac",
})
SLACK_AUDIO_FILETYPES = frozenset({"mp3", "m4a", "aac", "wav", "ogg", "oga", "flac", "opus", "weba"})
SNIPPET_FILETYPES = frozenset({
    "text", "markdown", "md", "python", "javascript", "typescript", "json", "yaml", "xml",
    "html", "css", "shell", "bash", "sql", "go", "rust", "java", "kotlin", "swift", "c",
    "cpp", "csharp", "ruby", "php", "diff", "dockerfile", "toml", "ini", "log", "jsx", "tsx",
    "scala", "r", "perl", "lua", "haskell", "elixir", "erlang", "clojure", "dart", "groovy",
    "objc", "powershell", "vb", "matlab", "latex", "plain", "vbscript", "apex", "post",
})
FILE_KIND_BY_FILETYPE = {
    "pdf": "pdf",
    "email": "email",
    **dict.fromkeys(("doc", "docx", "gdoc", "odt", "rtf", "pages", "dotx"), "document"),
    **dict.fromkeys(("xls", "xlsx", "gsheet", "ods", "numbers", "csv", "tsv"), "spreadsheet"),
    **dict.fromkeys(("ppt", "pptx", "gpres", "odp", "key"), "presentation"),
    **dict.fromkeys(("zip", "gzip", "gz", "tar", "tgz", "rar", "7z", "bz2", "xz"), "archive"),
    **dict.fromkeys(("quip", "canvas", "space", "list"), "canvas"),
}
PLAYABLE_KINDS = frozenset({"video", "audio"})
STREAM_CHUNK_BYTES = 4 * 1024 * 1024
STREAM_TOKEN_MAX_AGE_SECONDS = 6 * 60 * 60
STREAM_TOKEN_SALT = "community-chat.slack-file-stream.v1"
STREAM_REQUESTS_PER_MINUTE = 240
PREVIEW_TEXT_MAX_CHARS = 4000
PREVIEW_TEXT_MAX_LINES = 60
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+|\*)$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SlackFileStreamDenied(Exception):
    """The signed stream URL is no longer valid for its session."""


@dataclass(frozen=True)
class SlackFileChunk:
    status: int
    content_type: str
    body: bytes
    content_range: str


def _mimetype(file_data) -> str:
    return str(file_data.get("mimetype") or "").split(";", 1)[0].strip().lower()


def _filetype(file_data) -> str:
    return str(file_data.get("filetype") or "").strip().lower()


def _is_external(file_data) -> bool:
    return bool(file_data.get("is_external") or file_data.get("mode") == "external")


def _private_url(file_data) -> str:
    return str(file_data.get("url_private_download") or file_data.get("url_private") or "").strip()


def slack_video_source_url(file_data) -> str:
    """Prefer Slack's browser-ready MP4 transcode over the uploaded original."""
    if _is_external(file_data):
        return ""
    for field in ("mp4", "mp4_low"):
        value = str(file_data.get(field) or "").strip()
        if value:
            return value
    return _private_url(file_data) if _mimetype(file_data) in SLACK_VIDEO_TYPES else ""


def slack_audio_source_url(file_data) -> str:
    """Prefer Slack's AAC transcode (voice clips) over the uploaded original."""
    if _is_external(file_data):
        return ""
    value = str(file_data.get("aac") or "").strip()
    if value:
        return value
    if _mimetype(file_data) in SLACK_AUDIO_TYPES or _filetype(file_data) in SLACK_AUDIO_FILETYPES:
        return _private_url(file_data)
    return ""


def is_slack_video(file_data) -> bool:
    """Whether Slack describes the file as a video we can play inline."""
    return bool(
        (_mimetype(file_data).startswith("video/") or _filetype(file_data) in SLACK_VIDEO_FILETYPES)
        and slack_video_source_url(file_data)
    )


def is_slack_audio(file_data) -> bool:
    return bool(
        (_mimetype(file_data).startswith("audio/") or _filetype(file_data) in SLACK_AUDIO_FILETYPES
         or file_data.get("media_display_type") == "audio")
        and slack_audio_source_url(file_data)
    )


def slack_file_kind(file_data) -> str:
    """Classify a Slack file into the presentation Slack uses for it."""
    if _is_external(file_data):
        return "external"
    mimetype, filetype = _mimetype(file_data), _filetype(file_data)
    if mimetype == "image/gif" or filetype == "gif":
        return "gif"
    if mimetype in ALLOWED_IMAGE_TYPES or mimetype.startswith("image/"):
        return "image"
    if is_slack_video(file_data):
        return "video"
    if is_slack_audio(file_data):
        return "audio"
    if filetype in FILE_KIND_BY_FILETYPE:
        return FILE_KIND_BY_FILETYPE[filetype]
    if file_data.get("mode") in {"snippet", "post"} or filetype in SNIPPET_FILETYPES or mimetype.startswith("text/"):
        return "snippet"
    if file_data.get("mode") in {"space", "docs", "canvas", "quip"}:
        return "canvas"
    return "file"


def _preview_text(file_data) -> tuple[str, int | None]:
    raw = file_data.get("preview_plain_text") or file_data.get("preview") or file_data.get("plain_text") or ""
    if not isinstance(raw, str) or not raw.strip():
        return "", None
    text = _CONTROL_RE.sub("", raw.replace("\r\n", "\n"))
    lines = text.split("\n")[:PREVIEW_TEXT_MAX_LINES]
    text = "\n".join(lines)[:PREVIEW_TEXT_MAX_CHARS].rstrip()
    total = file_data.get("lines")
    if isinstance(total, int) and total > 0:
        total_lines = total
    else:
        total_lines = len(lines)
    return text, total_lines


def _waveform(file_data) -> list[int]:
    samples = file_data.get("audio_wave_samples")
    if not isinstance(samples, list):
        return []
    values = [int(value) for value in samples[:200] if isinstance(value, (int, float)) and math.isfinite(value)]
    return [max(0, min(100, value)) for value in values]


def slack_file_metadata(file_data) -> dict:
    """Bounded, display-only facts about a Slack file. Never URLs or tokens."""
    kind = slack_file_kind(file_data)
    metadata: dict = {"file_kind": kind}
    filetype = _filetype(file_data)
    if re.fullmatch(r"[a-z0-9_+.-]{1,24}", filetype or ""):
        metadata["filetype"] = filetype
    pretty = str(file_data.get("pretty_type") or "").strip()
    if pretty:
        metadata["pretty_type"] = pretty[:60]
    size = file_data.get("size")
    if isinstance(size, int) and size >= 0:
        metadata["size"] = size
    duration_ms = file_data.get("duration_ms")
    if kind in PLAYABLE_KINDS and isinstance(duration_ms, (int, float)) and math.isfinite(duration_ms) and duration_ms >= 0:
        metadata["duration"] = round(duration_ms / 1000, 3)
    if kind == "video":
        for source, target in (("thumb_video_w", "width"), ("thumb_video_h", "height")):
            value = file_data.get(source)
            if isinstance(value, int) and 0 < value <= 16384:
                metadata[target] = value
    elif kind in {"image", "gif"}:
        for source, target in (("original_w", "width"), ("original_h", "height")):
            value = file_data.get(source)
            if isinstance(value, int) and 0 < value <= 65536:
                metadata[target] = value
    if kind == "audio":
        waveform = _waveform(file_data)
        if waveform:
            metadata["waveform"] = waveform
    if kind in {"snippet", "spreadsheet", "email"}:
        text, lines = _preview_text(file_data)
        if text:
            metadata["preview_text"] = text
            metadata["preview_lines"] = lines
        if kind == "snippet" and file_data.get("lines_more"):
            metadata["preview_lines"] = max(metadata.get("preview_lines") or 0,
                len(text.split("\n")) + int(file_data.get("lines_more") or 0))
    if kind == "email":
        subject = str(file_data.get("subject") or "").strip()
        if subject:
            metadata["email_subject"] = subject[:220]
        sender = file_data.get("from")
        if isinstance(sender, list) and sender and isinstance(sender[0], dict):
            name = str(sender[0].get("name") or sender[0].get("address") or "").strip()
            if name:
                metadata["email_from"] = name[:120]
    return metadata


def slack_stream_source(file_data) -> tuple[str, str]:
    """The private URL and served media type for playing or downloading a file.

    Only video and audio keep their media type; every other file is served as
    an opaque attachment so its bytes can never render on the API origin.
    """
    kind = slack_file_kind(file_data)
    if kind == "video":
        return slack_video_source_url(file_data), "video"
    if kind == "audio":
        return slack_audio_source_url(file_data), "audio"
    if kind == "external":
        return "", ""
    return _private_url(file_data), ""


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
        raise SlackFileStreamDenied("expired") from exc
    if not SLACK_FILE_ID_RE.fullmatch(file_id):
        raise SlackFileStreamDenied("invalid")
    session = (
        CommunityChatAccountSession.objects.select_related("user")
        .filter(pk=session_id)
        .first()
    )
    if not _valid_session(session, timezone.now()):
        raise SlackFileStreamDenied("session")
    try:
        _validate_device_owner(session)
        require_community_access(session.user)
    except InvalidAccountSession as exc:
        raise SlackFileStreamDenied("session") from exc
    except PermissionDenied as exc:
        raise SlackFileStreamDenied("membership") from exc
    return session, file_id


def allow_stream_request(session_id) -> bool:
    """A per-session ceiling so a copied stream URL cannot drain Slack budget."""
    key = f"community-chat-slack-file-rate:{session_id}:{int(timezone.now().timestamp() // 60)}"
    cache.add(key, 0, timeout=90)
    try:
        return cache.incr(key) <= STREAM_REQUESTS_PER_MINUTE
    except ValueError:
        return True


def authorize_slack_file(file_id: str, *, user) -> dict:
    """Confirm current access to a streamable Slack file and return its metadata."""
    normalized = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized):
        raise SlackFilePreviewError("A valid Slack file is required.")
    with _authorized_file(normalized, user=user) as authorized:
        source, _media = slack_stream_source(authorized.data)
        if not source:
            raise SlackFilePreviewError("This Slack file can only be opened in Slack.")
        return slack_file_metadata(authorized.data)


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


def fetch_slack_file_range(file_id: str, *, user, range_header: str = "") -> SlackFileChunk:
    """Read at most one bounded byte range of a Slack file for its member."""
    normalized = str(file_id or "").strip().upper()
    if not SLACK_FILE_ID_RE.fullmatch(normalized):
        raise SlackFilePreviewError("A valid Slack file is required.")
    start, end = parse_range(range_header)
    if start is None:
        upstream_range = f"bytes=-{min(end, STREAM_CHUNK_BYTES)}"
    else:
        last = start + STREAM_CHUNK_BYTES - 1
        end = last if end is None else min(end, last)
        upstream_range = f"bytes={start}-{end}"
    with _authorized_file(normalized, user=user) as authorized:
        file_data = authorized.data
        source, media = slack_stream_source(file_data)
        if not source:
            raise SlackFilePreviewError("This Slack file can only be opened in Slack.")
        if not authorized.access_token:
            raise SlackFilePreviewError("Slack file access is not configured.")
        return _read_range(source, authorized.access_token, upstream_range, start, end,
                           media=media, fallback_type=_mimetype(file_data))


def _served_type(upstream_type, *, media, fallback_type):
    if not media:
        return "application/octet-stream"
    allowed = SLACK_VIDEO_TYPES if media == "video" else SLACK_AUDIO_TYPES
    for candidate in (upstream_type, fallback_type):
        if candidate in allowed:
            return candidate
    return "video/mp4" if media == "video" else "audio/mp4"


def _read_range(source, token, upstream_range, start, end, *, media, fallback_type):
    session = requests.Session()
    session.trust_env = False
    response = None
    try:
        response = _download_response(
            session,
            source,
            token,
            headers={"Accept": f"{media or '*'}/*;q=0.9,*/*;q=0.1", "Range": upstream_range},
        )
        if response.status_code == 416:
            total = str(response.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
            return SlackFileChunk(416, "", b"", f"bytes */{total if total.isdigit() else '*'}")
        response.raise_for_status()
        content_type = _served_type(
            str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower(),
            media=media, fallback_type=fallback_type,
        )
        if response.status_code == 206:
            match = _CONTENT_RANGE_RE.fullmatch(str(response.headers.get("Content-Range") or "").strip())
            if not match:
                raise SlackFilePreviewError("Slack returned an unreadable file range.")
            body = _read_body(response, skip=0, limit=STREAM_CHUNK_BYTES)
            first = int(match.group(1))
            return SlackFileChunk(
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
                raise SlackFilePreviewError("Slack returned an unreadable file range.")
            start = max(0, total - end)
            end = start + STREAM_CHUNK_BYTES - 1
        body = _read_body(response, skip=start, limit=end - start + 1)
        if not body:
            return SlackFileChunk(416, "", b"", f"bytes */{total or '*'}")
        return SlackFileChunk(
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
        raise SlackFilePreviewError("The Slack file could not be reached.") from exc
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
