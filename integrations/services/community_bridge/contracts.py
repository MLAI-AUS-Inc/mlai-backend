"""Provider-neutral contracts for the MLAI community bridge.

Adapters translate provider payloads at the edges. The store and delivery
pipeline exchange only these canonical, content-minimal structures.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence


BRIDGE_PLATFORMS = frozenset({"slack", "discord", "buzz"})
DELIVERY_TYPES = frozenset({"create", "edit", "delete"})


def _required_text(value: object, field_name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return normalized


def _optional_text(value: object, field_name: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if len(normalized) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return normalized


@dataclass(frozen=True)
class BridgeAttachment:
    """A safe link to provider-hosted media; bridge workers do not fetch it."""

    title: str
    url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _required_text(self.title, "attachment.title", 255))
        normalized_url = _required_text(self.url, "attachment.url", 2048)
        if not normalized_url.startswith(("https://", "http://")):
            raise ValueError("attachment.url must use http or https")
        object.__setattr__(self, "url", normalized_url)

    def as_payload(self) -> dict:
        return {"title": self.title, "url": self.url}


@dataclass(frozen=True)
class CanonicalBridgeEvent:
    """One live provider event after signature verification and normalization."""

    receipt_key: str
    source_platform: str
    source_channel_id: str
    source_message_id: str
    delivery_type: str
    source_parent_message_id: str = ""
    source_author_id: str = ""
    source_author_display_name: str = ""
    text: str = ""
    attachments: Sequence[BridgeAttachment] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "receipt_key", _required_text(self.receipt_key, "receipt_key", 255))
        platform = _required_text(self.source_platform, "source_platform", 20)
        if platform not in BRIDGE_PLATFORMS:
            raise ValueError("source_platform is unsupported")
        object.__setattr__(self, "source_platform", platform)
        object.__setattr__(
            self,
            "source_channel_id",
            _required_text(self.source_channel_id, "source_channel_id", 100),
        )
        object.__setattr__(
            self,
            "source_message_id",
            _required_text(self.source_message_id, "source_message_id", 100),
        )
        delivery_type = _required_text(self.delivery_type, "delivery_type", 20)
        if delivery_type not in DELIVERY_TYPES:
            raise ValueError("delivery_type is unsupported")
        object.__setattr__(self, "delivery_type", delivery_type)
        object.__setattr__(self, "attachments", tuple(self.attachments or ()))
        object.__setattr__(
            self,
            "source_parent_message_id",
            _optional_text(self.source_parent_message_id, "source_parent_message_id", 100),
        )
        object.__setattr__(
            self,
            "source_author_id",
            _optional_text(self.source_author_id, "source_author_id", 100),
        )
        object.__setattr__(
            self,
            "source_author_display_name",
            _optional_text(
                self.source_author_display_name,
                "source_author_display_name",
                255,
            ),
        )
        if delivery_type == "delete" and (self.text or self.attachments):
            raise ValueError("delete events must not retain message content")

    def normalized_payload(self) -> dict:
        return {
            "delivery_type": self.delivery_type,
            "source_channel_id": self.source_channel_id,
            "source_message_id": self.source_message_id,
            "source_parent_message_id": str(self.source_parent_message_id or "").strip(),
            "source_author_id": str(self.source_author_id or "").strip(),
            "source_author_display_name": str(self.source_author_display_name or "").strip(),
            "text": str(self.text or ""),
            "attachments": [attachment.as_payload() for attachment in self.attachments],
            "metadata": dict(self.metadata or {}),
        }


@dataclass(frozen=True)
class BridgeDeliveryResult:
    """Provider identifier returned after an idempotent adapter delivery."""

    destination_channel_id: str
    destination_message_id: str
    destination_parent_message_id: str = ""
    provider_payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "destination_channel_id",
            _required_text(self.destination_channel_id, "destination_channel_id", 100),
        )
        object.__setattr__(
            self,
            "destination_message_id",
            _required_text(self.destination_message_id, "destination_message_id", 100),
        )


class BridgeAdapter(Protocol):
    """Transport boundary implemented by Slack, Discord, and Buzz adapters."""

    platform: str

    async def deliver(self, delivery: Mapping[str, Any]) -> BridgeDeliveryResult:
        """Create, edit, or delete a provider message for one claimed outbox row."""

        ...
