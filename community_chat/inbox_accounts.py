"""Opaque relay account bindings; called under the existing user authority lock."""

import hashlib
import hmac
import uuid

from django.conf import settings

from . import adapter

MEMBER_ACCOUNTS_PROTOCOL = "member_accounts_v1"


def account_key(community_id, user_id):
    """Derive the 32-byte tenant-scoped key without exposing a backend user ID."""
    secret = settings.MLAI_CHAT_ACCOUNT_KEY_SECRET
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise adapter.MembershipAdapterUnavailable("inbox_account_secret_unavailable")
    try:
        community = str(uuid.UUID(str(community_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise adapter.MembershipAdapterUnavailable("adapter_invalid_response") from exc
    if user_id is None or isinstance(user_id, bool) or not str(user_id):
        raise ValueError("A durable user ID is required")
    return hmac.new(
        secret.encode("utf-8"), f"{community}:{user_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def bind_verified_device(user_id, public_key):
    """Bind before verification commits; fail closed when opt-in is misconfigured.

    The caller must hold the same user/device locks used for verification and
    revocation. The relay generation CAS fences requests that race a DELETE.
    """
    if not settings.COMMUNITY_CHAT_MEMBER_ACCOUNTS_ENABLED:
        return False
    capabilities, _ = adapter._request("GET", "/v1/capabilities")
    protocols = capabilities.get("member_account_protocols")
    if not isinstance(protocols, list) or MEMBER_ACCOUNTS_PROTOCOL not in protocols:
        raise adapter.MembershipAdapterUnavailable("adapter_protocol_unavailable")
    key = account_key(capabilities.get("community_id"), user_id)
    intent, _ = adapter._request(
        "POST", "/v2/member-invite-intents", json_body={"public_key": public_key}
    )
    generation = intent.get("generation")
    if (
        intent.get("public_key") != public_key
        or isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 <= generation <= adapter.MAX_MEMBER_INVITE_GENERATION
    ):
        raise adapter.MembershipAdapterUnavailable("adapter_invalid_response")
    result, _ = adapter._request(
        "PUT",
        f"/v2/member-accounts/{public_key}",
        json_body={"account_key": key, "generation": generation},
    )
    if (
        result.get("public_key") != public_key
        or isinstance(result.get("generation"), bool)
        or result.get("generation") != generation
        or result.get("account_key") != key
        or result.get("status") not in {"bound", "unchanged"}
    ):
        raise adapter.MembershipAdapterUnavailable("adapter_invalid_response")
    return result["status"] == "bound"
