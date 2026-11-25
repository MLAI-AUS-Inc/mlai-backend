"""Publish an authorized, revisioned room projection through the community relay."""

import json
import os
import re
from uuid import UUID

from django.utils import timezone
import requests

from agent_runtime.native_crypto import canonical_json, sign_event
from roo.native_points_requests import RooRelay, tags

METADATA_KIND = 39000
MEMBERS_KIND = 39002
CREATE_KIND = 9007
ADD_MEMBER_KIND = 9000
REMOVE_MEMBER_KIND = 9001
MANAGED_KIND = 44300


def _read(relay, channel_id, kind):
    events = relay.query(None, [kind], **{"#d": [channel_id], "limit": 2})
    matching = [event for event in events if tags(event, "d") == [["d", channel_id]]]
    if len(matching) > 1:
        raise ValueError("Ambiguous relay group state")
    return next(iter(matching), None)


def _write(relay, kind, event_tags, content=""):
    event = sign_event(
        relay.key,
        kind=kind,
        created_at=timezone.now().timestamp(),
        tags=event_tags,
        content=content,
    )
    result = relay.post("/events", event)
    if result.get("accepted") is not True or result.get("event_id") != event["id"]:
        raise ValueError("Managed room write was not accepted by the relay")


def _verify_target(relay, channel_id, metadata, visibility, adopted, handle):
    expected_tag = "private" if visibility == "private" else "public"
    if ([expected_tag] not in metadata.get("tags", [])
            or tags(metadata, "t") not in ([["t", "stream"]], [["t", "forum"]])):
        raise ValueError("Managed room has an incompatible audience or channel type")
    if not adopted:
        created = relay.query(channel_id, [CREATE_KIND], authors=[relay.pubkey], limit=2)
        if (len(created) != 1 or tags(created[0], "h") != [["h", channel_id]]
                or tags(created[0], "name") != [["name", handle]]
                or tags(created[0], "visibility") != [["visibility", visibility]]):
            raise ValueError("Managed room creation could not be verified")


def _verify_members(relay, channel_id, allowed, adopt):
    def roster():
        state = _read(relay, channel_id, MEMBERS_KIND)
        if state is None:
            raise ValueError("Managed private room roster unavailable")
        return {tag[1] for tag in tags(state, "p")}

    actual = roster()
    if adopt and actual != allowed:
        raise ValueError("Managed private room roster differs from the verified roster")
    for pubkey in sorted(actual - allowed):
        _write(relay, REMOVE_MEMBER_KIND, [["h", channel_id], ["p", pubkey]])
    for pubkey in sorted(allowed - actual):
        _write(relay, ADD_MEMBER_KIND, [["h", channel_id], ["p", pubkey], ["role", "member"]])
    if roster() != allowed:
        raise ValueError("Updated private room roster could not be verified")


def _projection_matches(projection, desired):
    if projection is None:
        return False
    fields = {
        "revision": "managed_revision",
        "section": "channel_section",
        "title": "managed_title",
        "status": "source_status",
        "url": "source_url",
        "start_at": "source_start",
        "end_at": "source_end",
        "timezone": "source_timezone",
    }
    for name, tag in fields.items():
        value = desired.get(name)
        if tags(projection, tag) != ([[tag, str(value)]] if value else []):
            return False
    links = [["source_link", name, url] for name, url in sorted(desired.get("links", {}).items())]
    if sorted(tags(projection, "source_link")) != links:
        return False
    update = desired.get("latest_project_update") or {}
    observed = tags(projection, "source_project_update")
    if not update:
        return not observed
    if len(observed) != 1:
        return False
    try:
        value = json.loads(observed[0][1])
    except (IndexError, ValueError, TypeError):
        return False
    return isinstance(value, dict) and all(value.get(key) == data for key, data in update.items())


def apply_room(config, room, payload):
    """Create or verify one stable room and apply its latest visible source revision."""
    community = str(UUID(str(config["community"])))
    key = os.environ.get("MANAGED_CHANNEL_RELAY_SIGNING_KEY", "")
    expected_community = os.environ.get("MANAGED_CHANNEL_RELAY_COMMUNITY_ID", "")
    if not key or community != expected_community:
        raise ValueError("Managed room signer is not configured for this community")
    if room.visibility not in {"public", "private"}:
        raise ValueError("Unknown managed room audience")
    channel_id = str(UUID(str(room.channel_id)))
    visibility = "private" if room.visibility == "private" else "open"
    relay = RooRelay(key)
    public_keys = set(room.members)
    if visibility == "private" and (
        not public_keys or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in public_keys)
    ):
        raise ValueError("Managed private room requires a verified roster")

    # Keep provider identifiers and titles in revisioned metadata. The opaque
    # transport name is safe if the source later becomes restricted.
    handle = f"{payload['section']}-{UUID(channel_id).hex}"
    metadata = _read(relay, channel_id, METADATA_KIND)
    if metadata is None and not room.adopted:
        if payload["status"] in {"removed", "restricted", "archived"}:
            raise ValueError("Inactive managed room does not exist")
        try:
            _write(relay, CREATE_KIND, [
                ["h", channel_id], ["name", handle], ["visibility", visibility],
                ["channel_type", "stream"],
            ])
        except (requests.RequestException, ValueError):
            # The response can be lost after a successful create. Verify the
            # existing UUID and creation signer before treating it as ours.
            metadata = _read(relay, channel_id, METADATA_KIND)
            if metadata is None:
                raise
        else:
            metadata = _read(relay, channel_id, METADATA_KIND)
    if metadata is None:
        raise ValueError("Managed room metadata unavailable")
    _verify_target(relay, channel_id, metadata, visibility, room.adopted, handle)
    body = {**payload, "visibility": visibility}
    if payload["status"] == "restricted":
        for name in ("title", "url", "start_at", "end_at", "timezone", "links", "latest_project_update"):
            body.pop(name, None)
    elif visibility == "private":
        allowed = public_keys if room.adopted else public_keys | {relay.pubkey}
        _verify_members(relay, channel_id, allowed, room.adopted)

    try:
        _write(relay, MANAGED_KIND, [["h", channel_id]], canonical_json(body))
    except (requests.RequestException, ValueError):
        if not _projection_matches(_read(relay, channel_id, METADATA_KIND), body):
            raise
    else:
        if not _projection_matches(_read(relay, channel_id, METADATA_KIND), body):
            raise ValueError("Managed room projection could not be verified")
