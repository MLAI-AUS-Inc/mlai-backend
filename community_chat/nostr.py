"""Strict verification for the one-purpose Nostr device-control proof."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass

from coincurve import PublicKeyXOnly


PUBLIC_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
DEVICE_PROOF_KIND = 27235


class InvalidDeviceProof(ValueError):
    pass


@dataclass(frozen=True)
class DeviceProofExpectation:
    challenge_id: str
    public_key: str
    nonce: str
    action: str
    audience: str
    origin: str

    @property
    def tags(self):
        return [
            ["challenge", self.challenge_id],
            ["nonce", self.nonce],
            ["action", self.action],
            ["aud", self.audience],
            ["origin", self.origin],
        ]

    @property
    def content(self):
        return json.dumps(
            {
                "action": self.action,
                "audience": self.audience,
                "challenge_id": self.challenge_id,
                "nonce": self.nonce,
                "origin": self.origin,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def unsigned_event(self):
        return {
            "kind": DEVICE_PROOF_KIND,
            "content": self.content,
            "tags": self.tags,
        }


def normalize_public_key(value):
    normalized = str(value or "").strip().lower()
    if not PUBLIC_KEY_RE.fullmatch(normalized):
        raise InvalidDeviceProof("invalid_public_key")
    try:
        PublicKeyXOnly(bytes.fromhex(normalized))
    except (ValueError, TypeError):
        raise InvalidDeviceProof("invalid_public_key") from None
    return normalized


def _strict_event(event):
    if not isinstance(event, dict):
        raise InvalidDeviceProof("invalid_event")
    required = {"id", "pubkey", "created_at", "kind", "tags", "content", "sig"}
    if set(event) != required:
        raise InvalidDeviceProof("invalid_event_fields")
    if not isinstance(event["created_at"], int) or isinstance(event["created_at"], bool):
        raise InvalidDeviceProof("invalid_created_at")
    if event["kind"] != DEVICE_PROOF_KIND:
        raise InvalidDeviceProof("invalid_kind")
    if not isinstance(event["content"], str) or not isinstance(event["tags"], list):
        raise InvalidDeviceProof("invalid_event")
    if any(
        not isinstance(tag, list)
        or len(tag) != 2
        or any(not isinstance(part, str) for part in tag)
        for tag in event["tags"]
    ):
        raise InvalidDeviceProof("invalid_tags")


def verify_device_proof(event, expected):
    _strict_event(event)
    public_key = normalize_public_key(event["pubkey"])
    if public_key != expected.public_key:
        raise InvalidDeviceProof("public_key_mismatch")
    if event["content"] != expected.content or event["tags"] != expected.tags:
        raise InvalidDeviceProof("challenge_mismatch")

    event_id = str(event["id"] or "").lower()
    signature = str(event["sig"] or "").lower()
    if not EVENT_ID_RE.fullmatch(event_id) or not SIGNATURE_RE.fullmatch(signature):
        raise InvalidDeviceProof("invalid_signature_encoding")

    serialized = json.dumps(
        [0, public_key, event["created_at"], event["kind"], event["tags"], event["content"]],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    computed_id = hashlib.sha256(serialized).hexdigest()
    if not hmac.compare_digest(computed_id, event_id):
        raise InvalidDeviceProof("event_id_mismatch")

    try:
        valid = PublicKeyXOnly(bytes.fromhex(public_key)).verify(
            bytes.fromhex(signature),
            bytes.fromhex(event_id),
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise InvalidDeviceProof("invalid_signature")
    return event["created_at"]
