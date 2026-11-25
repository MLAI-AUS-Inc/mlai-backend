"""Share a signed Chat voice attachment through Slack's hosted-file API."""

import hashlib
import re
from datetime import datetime
from urllib.parse import urlsplit

import requests
from django.conf import settings

from integrations.services.message_sync.delivery import guard_delivery

from .buzz import BuzzBridgeClient

META = "buzz_voice_attachment"
MAX_BYTES = 44 + 300 * 32_000
DIGEST = re.compile(r"[0-9a-f]{64}")
FILE_ID = re.compile(r"F[A-Z0-9]{1,99}")
ALLOCATE = "files_getUploadURLExternal"
COMPLETE = "files_completeUploadExternal"
METHODS = frozenset({ALLOCATE, COMPLETE})


def from_callback(event, raw):
    """Accept the voice metadata only from the adapter's verified create tags."""
    if (
        raw.get("kind") != 9
        or event.get("delivery_type") != "create"
        or raw.get("event_id") != event.get("source_message_id")
        or raw.get("pubkey") != event.get("source_author_id")
    ):
        return None
    attachments = []
    for tag in raw.get("tags") or []:
        if not isinstance(tag, list) or not tag or tag[0] != "imeta":
            continue
        parts = {}
        for part in tag[1:]:
            if not isinstance(part, str):
                continue
            key, _, value = part.partition(" ")
            if key in parts:
                parts = {}
                break
            parts[key] = value
        if parts.get("m") != "audio/wav":
            continue
        digest = parts.get("x", "")
        size = parts.get("size", "")
        url = parts.get("url", "")
        try:
            path = urlsplit(url).path
        except ValueError:
            continue
        if (
            DIGEST.fullmatch(digest)
            and re.fullmatch(r"[0-9]{1,8}", size)
            and 46 <= int(size) <= MAX_BYTES
            and parts.get("duration")
            and path == f"/media/{digest}.wav"
            and url in str(event.get("text") or "")
        ):
            attachments.append({"sha256": digest, "size": int(size), "url": url})
    return attachments[0] if len(attachments) == 1 else None


def prepare(row, channel, client, wire):
    """Freeze the provider's file ID before the worker arms the channel share."""
    enabled = {value.strip() for value in getattr(settings, "SLACK_VOICE_UPLOAD_CHANNELS", "").split(",")}
    voice = ((row.payload or {}).get("metadata") or {}).get(META)
    if (
        row.source_platform != "buzz"
        or row.delivery_type != "create"
        or channel.slack_channel_id not in enabled
        or not voice
    ):
        return wire

    from .member_delivery import MemberDeliveryRetry, text_hash

    guard_delivery(row)
    audio = BuzzBridgeClient.read_voice(
        channel_id=row.source_channel_id,
        slack_workspace_id=channel.slack_workspace_id,
        slack_channel_id=channel.slack_channel_id,
        event_id=row.source_message_id,
        author_id=(row.payload or {}).get("source_author_id"),
        sha256=voice["sha256"],
        size=voice["size"],
    )
    if len(audio) != voice["size"] or hashlib.sha256(audio).hexdigest() != voice["sha256"]:
        raise MemberDeliveryRetry("voice_audio_integrity_failed")
    ticket = client.voice_call(ALLOCATE, filename="Voice message.wav", length=len(audio))
    file_id, upload_url = ticket.get("file_id"), ticket.get("upload_url")
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id) or not isinstance(upload_url, str):
        raise MemberDeliveryRetry("invalid_voice_upload_ticket")
    destination = urlsplit(upload_url)
    if (
        destination.netloc != "files.slack.com"
        or destination.scheme != "https"
        or not destination.path.startswith("/upload/")
        or destination.fragment
    ):
        raise MemberDeliveryRetry("invalid_voice_upload_destination")
    guard_delivery(row)
    try:
        with requests.post(
            upload_url,
            data=audio,
            headers={"Content-Type": "application/octet-stream"},
            allow_redirects=False,
            stream=True,
            timeout=(4, 12),
        ) as response:
            if response.status_code != 200:
                raise MemberDeliveryRetry("voice_byte_upload_failed")
    except requests.RequestException:
        raise MemberDeliveryRetry("voice_byte_upload_failed") from None

    guard_delivery(row)
    kwargs = dict(wire["kwargs"])
    url = voice["url"]
    own_lines = {url, f"[Voice message]({url})"}
    kwargs["text"] = "\n".join(
        line for line in kwargs["text"].split("\n") if line.strip() not in own_lines
    ).strip()
    return {**wire, "kwargs": kwargs, "text_hash": text_hash(kwargs["text"]), "voice_file_id": file_id}


def message_has_file(message, file_id):
    return file_id in file_ids(message)


def file_ids(message):
    if not isinstance(message, dict) or not isinstance(message.get("files"), list):
        return []
    return [
        item["id"] for item in message["files"]
        if isinstance(item, dict) and isinstance(item.get("id"), str) and FILE_ID.fullmatch(item["id"])
    ][:20]


def is_audio_share(event):
    from community_chat.slack_file_streams import SLACK_AUDIO_FILETYPES

    files = event.get("files")
    return isinstance(files, list) and any(
        isinstance(item, dict)
        and (
            str(item.get("mimetype") or "").lower().startswith("audio/")
            or str(item.get("filetype") or "").lower() in SLACK_AUDIO_FILETYPES
            or item.get("media_display_type") == "audio"
        )
        for item in files
    )


def share(client, wire):
    options = {
        "channel_id": wire["kwargs"]["channel"],
        "files": [{"id": wire["voice_file_id"], "title": "Voice message.wav"}],
    }
    if wire["kwargs"]["text"]:
        options["initial_comment"] = wire["kwargs"]["text"]
    if wire["parent"]:
        options["thread_ts"] = wire["parent"]
    client.voice_call(COMPLETE, **options)


def reconcile(client, channel, actor, wire, attempt):
    """Require an exact posted file, author and thread; never resend on absence."""
    from .member_attempts import _post_read_info, actor_proven
    from .member_delivery import MemberDeliveryDeferred, MemberDeliveryUnavailable, TS

    try:
        started = datetime.fromisoformat(attempt["started_at"])
        if started.tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise MemberDeliveryUnavailable("invalid_voice_attempt") from None

    options = {
        "channel": channel.slack_channel_id,
        "oldest": str(max(0, started.timestamp() - 10)),
        "limit": 100,
    }
    parent = wire["parent"]
    method = "conversations_replies" if parent else "conversations_history"
    if parent:
        options["ts"] = parent
    response = client.call(method, **options)
    messages = response.get("messages")
    if response.get("ok") is not True or not isinstance(messages, list):
        raise MemberDeliveryDeferred(60)
    response_channel = response.get("channel", channel.slack_channel_id)
    if isinstance(response_channel, dict):
        response_channel = response_channel.get("id")
    if response_channel != channel.slack_channel_id:
        raise MemberDeliveryDeferred(60)
    _post_read_info(client)
    matches = [
        item for item in messages
        if message_has_file(item, wire["voice_file_id"])
        and actor_proven(item, actor)
        and TS.fullmatch(str(item.get("ts") or ""))
        and (item.get("thread_ts") or "") == parent
        and item["ts"] != parent
    ]
    if len(matches) != 1:
        raise MemberDeliveryDeferred(60, reason="voice_share_not_yet_observed")
    return {
        "ok": True, "channel": channel.slack_channel_id, "ts": matches[0]["ts"],
        "message": matches[0],
    }
