import hashlib
import hmac
import re
import time
import uuid
from typing import List, Optional
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings


EVENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")


class BuzzBridgeError(RuntimeError):
    permanent = False


class BuzzBridgePermanentError(BuzzBridgeError):
    permanent = True


class BuzzBridgeClient:
    _private_batch_disabled_until = 0.0
    _private_batch_fallback_seconds = 300

    @classmethod
    def is_configured(cls) -> bool:
        return bool(cls._adapter_url() and cls._api_token() and cls._callback_secret())

    @classmethod
    def validate_callback_signature(cls, body: bytes, timestamp: str, signature: str) -> bool:
        secret = cls._callback_secret()
        if not secret or not timestamp or not signature:
            return False
        try:
            timestamp_value = int(str(timestamp).strip())
        except (TypeError, ValueError):
            return False
        max_age = max(1, int(getattr(settings, "BUZZ_BRIDGE_CALLBACK_MAX_AGE_SECONDS", 300)))
        if abs(int(time.time()) - timestamp_value) > max_age:
            return False
        signed_body = str(timestamp_value).encode("ascii") + b"." + body
        computed = "v1=" + hmac.new(
            secret.encode("utf-8"),
            signed_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, str(signature).strip())

    @classmethod
    def deliver(
        cls,
        *,
        delivery_id: str,
        created_at: int,
        operation: str,
        channel_id: str,
        text: str,
        target_message_id: str = "",
        parent_message_id: str = "",
        source_workspace_id: str = "",
        source_channel_id: str = "",
        source_message_id: str = "",
        source_author_id: str = "",
        source_author_display_name: str = "",
        source_author_avatar_url: str = "",
        linked_pubkey: str = "",
        linked_profile_id: str = "",
        source_created_at: int = 0,
        broadcast: bool = False,
    ) -> dict:
        adapter_url = cls._validated_adapter_url()
        api_token = cls._api_token()
        if not api_token:
            raise BuzzBridgePermanentError("BUZZ_BRIDGE_ADAPTER_TOKEN is not configured")
        payload = {
            "delivery_id": str(delivery_id),
            "created_at": int(created_at),
            "operation": str(operation),
            "channel_id": str(channel_id),
            "text": str(text or ""),
            "target_message_id": str(target_message_id or "") or None,
            "parent_message_id": str(parent_message_id or "") or None,
            "source_workspace_id": str(source_workspace_id or ""),
            "source_channel_id": str(source_channel_id or ""),
            "source_message_id": str(source_message_id or ""),
            "source_author_id": str(source_author_id or ""),
            "source_author_display_name": str(source_author_display_name or "") or None,
            "source_author_avatar_url": str(source_author_avatar_url or "") or None,
            "linked_pubkey": str(linked_pubkey or "") or None,
            "linked_profile_id": str(linked_profile_id or "") or None,
            "source_created_at": int(source_created_at or 0) or None,
            "broadcast": bool(broadcast),
        }
        timeout = max(1, min(int(getattr(settings, "BUZZ_BRIDGE_ADAPTER_TIMEOUT_SECONDS", 15)), 60))
        try:
            response = requests.post(
                urljoin(adapter_url, "v1/deliveries"),
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise BuzzBridgeError(f"MLAI Chat adapter request failed: {exc.__class__.__name__}") from exc
        if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
            raise BuzzBridgePermanentError(
                f"MLAI Chat adapter rejected delivery with HTTP {response.status_code}"
            )
        if not response.ok:
            raise BuzzBridgeError(f"MLAI Chat adapter returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise BuzzBridgeError("MLAI Chat adapter returned invalid JSON") from exc
        destination_channel_id = str(result.get("channel_id") or "").strip()
        message_id = str(result.get("message_id") or "").strip().lower()
        if destination_channel_id != str(channel_id).strip():
            raise BuzzBridgeError("MLAI Chat adapter returned the wrong channel")
        if not EVENT_ID_RE.fullmatch(message_id):
            raise BuzzBridgeError("MLAI Chat adapter returned an invalid event ID")
        return {
            "channel_id": destination_channel_id,
            "message_id": message_id,
            "parent_message_id": str(result.get("parent_message_id") or "").strip(),
        }

    @classmethod
    def provision_private_conversation(
        cls,
        participant_pubkeys: list[str],
        *,
        callback_author_pubkeys: list[str],
        conversation_name: str = "",
    ) -> dict:
        """Idempotently provision an exact-participant private MLAI DM."""
        pubkeys = sorted({str(value or "").strip().lower() for value in participant_pubkeys})
        if len(pubkeys) < 2 or len(pubkeys) > 9 or any(not EVENT_ID_RE.fullmatch(value) for value in pubkeys):
            raise BuzzBridgePermanentError("Private conversations require 2-9 valid public keys")
        callback_pubkeys = sorted(
            {
                str(value or "").strip().lower()
                for value in callback_author_pubkeys
            }
        )
        if (
            not callback_pubkeys
            or len(callback_pubkeys) > 8
            or any(value not in pubkeys for value in callback_pubkeys)
        ):
            raise BuzzBridgePermanentError(
                "Private callback authors must be participant public keys"
            )
        name = str(conversation_name or "").strip()
        if len(name) > 255 or any(character.isprintable() is False for character in name):
            raise BuzzBridgePermanentError("Private conversation name is invalid")
        result = cls._post_adapter(
            "v1/private-conversations",
            {
                "participant_pubkeys": pubkeys,
                "callback_author_pubkeys": callback_pubkeys,
                "conversation_name": name or None,
            },
        )
        channel_id = str(result.get("channel_id") or "").strip()
        try:
            uuid.UUID(channel_id)
        except (ValueError, TypeError) as exc:
            raise BuzzBridgeError("MLAI Chat adapter returned an invalid private channel") from exc
        returned = sorted(str(value or "").strip().lower() for value in result.get("participant_pubkeys") or [])
        if returned != pubkeys:
            raise BuzzBridgeError("MLAI Chat adapter returned the wrong private participants")
        returned_callback_pubkeys = sorted(
            str(value or "").strip().lower()
            for value in result.get("callback_author_pubkeys") or []
        )
        if returned_callback_pubkeys != callback_pubkeys:
            raise BuzzBridgeError(
                "MLAI Chat adapter returned the wrong private callback authors"
            )
        return {
            "channel_id": channel_id,
            "participant_pubkeys": returned,
            "callback_author_pubkeys": returned_callback_pubkeys,
        }

    @classmethod
    def unregister_private_conversation(cls, channel_id: str) -> None:
        """Idempotently remove one private-channel adapter registration."""

        try:
            normalized_channel_id = str(uuid.UUID(str(channel_id or "").strip()))
        except (ValueError, TypeError, AttributeError) as exc:
            raise BuzzBridgePermanentError(
                "Private conversation channel must be a valid UUID"
            ) from exc
        cls._delete_adapter(
            f"v1/private-conversations/{normalized_channel_id}"
        )

    @classmethod
    def deliver_private(
        cls,
        *,
        delivery_id: str,
        created_at: int,
        operation: str,
        channel_id: str,
        participant_pubkeys: list[str],
        text: str,
        source_workspace_id: str,
        source_channel_id: str,
        source_message_id: str,
        source_author_id: str,
        source_author_display_name: str,
        source_author_avatar_url: str,
        linked_pubkey: str,
        target_message_id: str = "",
        parent_message_id: str = "",
    ) -> dict:
        result = cls._post_adapter(
            "v1/private-deliveries",
            {
                "delivery_id": str(delivery_id),
                "created_at": int(created_at),
                "operation": str(operation),
                "channel_id": str(channel_id),
                "participant_pubkeys": sorted(participant_pubkeys),
                "text": str(text or ""),
                "source_workspace_id": str(source_workspace_id),
                "source_channel_id": str(source_channel_id),
                "source_message_id": str(source_message_id),
                "source_author_id": str(source_author_id),
                "source_author_display_name": str(source_author_display_name or "") or None,
                "source_author_avatar_url": str(source_author_avatar_url or "") or None,
                "linked_pubkey": str(linked_pubkey),
                "target_message_id": str(target_message_id or "") or None,
                "parent_message_id": str(parent_message_id or "") or None,
            },
        )
        return cls._validated_private_delivery_result(
            result,
            channel_id=str(channel_id),
        )

    @classmethod
    def _validated_private_delivery_result(
        cls,
        result: dict,
        *,
        channel_id: str,
    ) -> dict:
        returned_channel = str(result.get("channel_id") or "").strip()
        message_id = str(result.get("message_id") or "").strip().lower()
        returned_parent_message_id = str(result.get("parent_message_id") or "").strip().lower()
        if returned_channel != channel_id or not EVENT_ID_RE.fullmatch(message_id):
            raise BuzzBridgeError("MLAI Chat adapter returned an invalid private delivery")
        if returned_parent_message_id and not EVENT_ID_RE.fullmatch(returned_parent_message_id):
            raise BuzzBridgeError("MLAI Chat adapter returned an invalid private parent message")
        return {
            "channel_id": returned_channel,
            "message_id": message_id,
            "parent_message_id": returned_parent_message_id,
        }

    @classmethod
    def deliver_private_batch(cls, deliveries: list[dict]) -> list[dict]:
        """Deliver an ordered, single-conversation private batch."""

        if not 1 <= len(deliveries) <= 20:
            raise BuzzBridgePermanentError(
                "Private delivery batches require 1-20 deliveries"
            )
        channel_ids = {
            str(delivery.get("channel_id") or "").strip()
            for delivery in deliveries
        }
        if len(channel_ids) != 1:
            raise BuzzBridgePermanentError(
                "Private delivery batches must target one channel"
            )
        if time.monotonic() < cls._private_batch_disabled_until:
            return cls._deliver_private_one_at_a_time(deliveries)
        try:
            result = cls._post_adapter(
                "v1/private-deliveries/batch",
                {"deliveries": deliveries},
            )
        except BuzzBridgePermanentError as exc:
            if "HTTP 404" not in str(exc) and "HTTP 405" not in str(exc):
                raise
            # Rolling deploy compatibility: an older adapter can safely accept
            # the same deterministic deliveries one at a time until its batch
            # route is available.
            cls._private_batch_disabled_until = (
                time.monotonic() + cls._private_batch_fallback_seconds
            )
            return cls._deliver_private_one_at_a_time(deliveries)
        except BuzzBridgeError:
            # A relay batch can cross an upstream timeout after storing only a
            # prefix of its events. Every event is deterministic, and the
            # adapter treats an already-stored event as a successful retry, so
            # degrading to individual requests is both safe and substantially
            # faster than retrying the same failing batch forever.
            cls._private_batch_disabled_until = (
                time.monotonic() + cls._private_batch_fallback_seconds
            )
            return cls._deliver_private_one_at_a_time(deliveries)
        raw_results = result.get("deliveries")
        if not isinstance(raw_results, list) or len(raw_results) != len(deliveries):
            raise BuzzBridgeError("MLAI Chat adapter returned an invalid private batch")
        validated = []
        for delivery, raw_result in zip(deliveries, raw_results):
            if not isinstance(raw_result, dict):
                raise BuzzBridgeError(
                    "MLAI Chat adapter returned an invalid private batch"
                )
            returned_channel = str(raw_result.get("channel_id") or "").strip()
            message_id = str(raw_result.get("message_id") or "").strip().lower()
            parent_message_id = str(
                raw_result.get("parent_message_id") or ""
            ).strip().lower()
            if (
                returned_channel != str(delivery.get("channel_id") or "")
                or not EVENT_ID_RE.fullmatch(message_id)
                or (
                    parent_message_id
                    and not EVENT_ID_RE.fullmatch(parent_message_id)
                )
            ):
                raise BuzzBridgeError(
                    "MLAI Chat adapter returned an invalid private batch delivery"
                )
            validated.append(
                {
                    "channel_id": returned_channel,
                    "message_id": message_id,
                    "parent_message_id": parent_message_id,
                }
            )
        return validated

    @classmethod
    def _deliver_private_one_at_a_time(cls, deliveries: list[dict]) -> list[dict]:
        return [
            cls._validated_private_delivery_result(
                cls._post_adapter("v1/private-deliveries", delivery),
                channel_id=str(delivery.get("channel_id") or ""),
            )
            for delivery in deliveries
        ]

    @classmethod
    def _post_adapter(cls, path: str, payload: dict) -> dict:
        adapter_url = cls._validated_adapter_url()
        api_token = cls._api_token()
        if not api_token:
            raise BuzzBridgePermanentError("BUZZ_BRIDGE_ADAPTER_TOKEN is not configured")
        timeout = max(1, min(int(getattr(settings, "BUZZ_BRIDGE_ADAPTER_TIMEOUT_SECONDS", 15)), 60))
        try:
            response = requests.post(
                urljoin(adapter_url, path),
                headers={"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise BuzzBridgeError(f"MLAI Chat adapter request failed: {exc.__class__.__name__}") from exc
        if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
            raise BuzzBridgePermanentError(
                f"MLAI Chat adapter rejected request with HTTP {response.status_code}"
            )
        if not response.ok:
            raise BuzzBridgeError(f"MLAI Chat adapter returned HTTP {response.status_code}")
        try:
            result = response.json()
        except ValueError as exc:
            raise BuzzBridgeError("MLAI Chat adapter returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise BuzzBridgeError("MLAI Chat adapter returned invalid JSON")
        return result

    @classmethod
    def _delete_adapter(cls, path: str) -> None:
        adapter_url = cls._validated_adapter_url()
        api_token = cls._api_token()
        if not api_token:
            raise BuzzBridgePermanentError(
                "BUZZ_BRIDGE_ADAPTER_TOKEN is not configured"
            )
        timeout = max(
            1,
            min(
                int(
                    getattr(
                        settings,
                        "BUZZ_BRIDGE_ADAPTER_TIMEOUT_SECONDS",
                        15,
                    )
                ),
                60,
            ),
        )
        try:
            response = requests.delete(
                urljoin(adapter_url, path),
                headers={"Authorization": f"Bearer {api_token}"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise BuzzBridgeError(
                "MLAI Chat adapter unregister request failed: "
                f"{exc.__class__.__name__}"
            ) from exc
        if 400 <= response.status_code < 500 and response.status_code not in {
            408,
            429,
        }:
            raise BuzzBridgePermanentError(
                "MLAI Chat adapter rejected unregister request with HTTP "
                f"{response.status_code}"
            )
        if not response.ok:
            raise BuzzBridgeError(
                "MLAI Chat adapter unregister returned HTTP "
                f"{response.status_code}"
            )

    @classmethod
    def lookup_messages(
        cls,
        *,
        channel_id: str,
        source_workspace_id: str,
        source_channel_id: str,
        source_message_ids: List[str],
        destination_message_ids: Optional[List[str]] = None,
    ) -> list[dict]:
        """Return active trusted bridge events for reconciliation only."""

        requested_sources = {
            str(message_id or "").strip() for message_id in source_message_ids
        }
        requested_destinations = {
            str(message_id or "").strip().lower()
            for message_id in (destination_message_ids or [])
            if str(message_id or "").strip()
        }
        if not requested_sources and not requested_destinations:
            return []
        if len(requested_sources) + len(requested_destinations) > 200:
            raise BuzzBridgePermanentError("MLAI Chat lookup is limited to 200 identifiers")
        adapter_url = cls._validated_adapter_url()
        api_token = cls._api_token()
        if not api_token:
            raise BuzzBridgePermanentError("BUZZ_BRIDGE_ADAPTER_TOKEN is not configured")
        timeout = max(
            1,
            min(
                int(getattr(settings, "BUZZ_BRIDGE_ADAPTER_TIMEOUT_SECONDS", 15)),
                60,
            ),
        )
        try:
            response = requests.post(
                urljoin(adapter_url, "v1/lookups"),
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "channel_id": str(channel_id),
                    "source_workspace_id": str(source_workspace_id),
                    "source_channel_id": str(source_channel_id),
                    "source_message_ids": sorted(requested_sources),
                    "destination_message_ids": sorted(requested_destinations),
                },
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise BuzzBridgeError(
                f"MLAI Chat adapter lookup failed: {exc.__class__.__name__}"
            ) from exc
        if 400 <= response.status_code < 500 and response.status_code not in {408, 429}:
            raise BuzzBridgePermanentError(
                f"MLAI Chat adapter rejected lookup with HTTP {response.status_code}"
            )
        if not response.ok:
            raise BuzzBridgeError(
                f"MLAI Chat adapter lookup returned HTTP {response.status_code}"
            )
        try:
            result = response.json()
        except ValueError as exc:
            raise BuzzBridgeError("MLAI Chat adapter lookup returned invalid JSON") from exc
        if str(result.get("channel_id") or "").strip() != str(channel_id).strip():
            raise BuzzBridgeError("MLAI Chat adapter lookup returned the wrong channel")
        matches = result.get("matches") or []
        if not isinstance(matches, list) or len(matches) > 500:
            raise BuzzBridgeError("MLAI Chat adapter lookup returned invalid matches")
        validated = []
        for match in matches:
            if not isinstance(match, dict):
                raise BuzzBridgeError("MLAI Chat adapter lookup returned an invalid match")
            source_message_id = str(match.get("source_message_id") or "").strip()
            destination_message_id = str(
                match.get("destination_message_id") or ""
            ).strip().lower()
            parent_message_id = str(match.get("parent_message_id") or "").strip().lower()
            if source_message_id not in requested_sources:
                raise BuzzBridgeError("MLAI Chat adapter lookup returned an unknown source message")
            if not EVENT_ID_RE.fullmatch(destination_message_id):
                raise BuzzBridgeError("MLAI Chat adapter lookup returned an invalid event ID")
            if parent_message_id and not EVENT_ID_RE.fullmatch(parent_message_id):
                raise BuzzBridgeError("MLAI Chat adapter lookup returned an invalid parent ID")
            try:
                created_at = int(match.get("created_at") or 0)
            except (TypeError, ValueError) as exc:
                raise BuzzBridgeError(
                    "MLAI Chat adapter lookup returned an invalid timestamp"
                ) from exc
            if created_at <= 0:
                raise BuzzBridgeError("MLAI Chat adapter lookup returned an invalid timestamp")
            validated.append(
                {
                    "source_message_id": source_message_id,
                    "destination_message_id": destination_message_id,
                    "parent_message_id": parent_message_id,
                    "broadcast": bool(match.get("broadcast")),
                    "created_at": created_at,
                }
            )
        return validated

    @staticmethod
    def _adapter_url() -> str:
        return str(getattr(settings, "BUZZ_BRIDGE_ADAPTER_URL", "") or "").strip()

    @staticmethod
    def _api_token() -> str:
        return str(getattr(settings, "BUZZ_BRIDGE_ADAPTER_TOKEN", "") or "").strip()

    @staticmethod
    def _callback_secret() -> str:
        return str(getattr(settings, "BUZZ_BRIDGE_CALLBACK_SECRET", "") or "").strip()

    @classmethod
    def _validated_adapter_url(cls) -> str:
        value = cls._adapter_url()
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BuzzBridgePermanentError("BUZZ_BRIDGE_ADAPTER_URL must be an absolute HTTP URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise BuzzBridgePermanentError(
                "BUZZ_BRIDGE_ADAPTER_URL must not contain credentials, a query, or a fragment"
            )
        return value.rstrip("/") + "/"
