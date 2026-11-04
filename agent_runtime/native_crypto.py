"""Server-only native signing and explicit client-owned enrollment proofs."""

import base64
import hashlib
import json
from types import SimpleNamespace

from coincurve import PrivateKey, PublicKeyXOnly
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from rest_framework.exceptions import PermissionDenied, ValidationError

from community_chat.nostr import verify_device_proof, InvalidDeviceProof, PUBLIC_KEY_RE


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def signing_key(value):
    if not isinstance(value, str) or not PUBLIC_KEY_RE.fullmatch(value):
        raise PermissionDenied("Native signing is not configured.")
    try:
        return PrivateKey(bytes.fromhex(value))
    except ValueError as exc:
        raise PermissionDenied("Native signing is not configured.") from exc


def public_key(key):
    return key.public_key_xonly.format().hex()


def seal_key(raw):
    return _cipher().encrypt(raw.encode("ascii")).decode("ascii")


def unseal_key(sealed):
    try:
        raw = _cipher().decrypt(sealed.encode("ascii")).decode("ascii")
        return signing_key(raw)
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise PermissionDenied("Agent signing key is unavailable.") from exc


def _cipher():
    digest = hashlib.sha256(("mlai-native-agent-key-v1:" + settings.SECRET_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def sign_event(key, *, kind, created_at, tags, content):
    event = {"pubkey": public_key(key), "created_at": int(created_at), "kind": kind,
             "tags": tags, "content": content}
    material = json.dumps([0, event["pubkey"], event["created_at"], kind, tags, content],
                          ensure_ascii=False, separators=(",", ":")).encode()
    event["id"] = hashlib.sha256(material).hexdigest()
    event["sig"] = key.sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def enrollment_challenge(enrollment, *, relay_url, audience, now):
    installation = enrollment.installation
    owner = installation.owner_device.public_key
    payload = {"action": "agent.install", "audience": audience, "enrollment_id": str(enrollment.pk),
        "installation_id": str(installation.pk), "agent_pubkey": installation.public_key,
        "community_id": str(installation.community_id), "resource": enrollment.resource,
        "consent_version": enrollment.consent_version, "nonce": enrollment.nonce, "owner_pubkey": owner}
    tags = [["challenge", str(enrollment.pk)], ["nonce", enrollment.nonce], ["action", "agent.install"],
            ["aud", audience], ["origin", relay_url], ["agent", installation.public_key],
            ["installation", str(installation.pk)], ["community", str(installation.community_id)],
            ["consent", enrollment.consent_version]]
    return {"kind": 27235, "created_at": int(now.timestamp()), "content": canonical_json(payload), "tags": tags}


def verify_enrollment(event, enrollment, *, relay_url, audience, now):
    if enrollment.expires_at <= now:
        raise PermissionDenied("Enrollment has expired. Start setup again.")
    challenge = enrollment_challenge(enrollment, relay_url=relay_url, audience=audience, now=now)
    expected = SimpleNamespace(public_key=enrollment.installation.owner_device.public_key,
                               content=challenge["content"], tags=challenge["tags"])
    try:
        created_at = verify_device_proof(event, expected)
    except InvalidDeviceProof as exc:
        raise PermissionDenied("Enrollment signature is invalid.") from exc
    if abs(created_at - now.timestamp()) > 300:
        raise PermissionDenied("Enrollment signature has expired.")


def verify_owner_attestation(value, installation, conditions):
    try:
        tag = json.loads(value) if isinstance(value, str) else value
        owner = installation.owner_device.public_key
        if (not isinstance(tag, list) or len(tag) != 4 or tag[:3] != ["auth", owner, conditions]
                or not isinstance(tag[3], str) or len(tag[3]) != 128 or tag[3] != tag[3].lower()):
            raise ValueError()
        digest = hashlib.sha256(f"nostr:agent-auth:{installation.public_key}:{conditions}".encode()).digest()
        if not PublicKeyXOnly(bytes.fromhex(owner)).verify(bytes.fromhex(tag[3]), digest):
            raise ValueError()
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError("Owner attestation is invalid.") from exc
    return tag
