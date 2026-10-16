"""Shared operation vocabulary for backend-owned article publishing channels."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

OPERATIONS = frozenset({"inspect", "capture_existing", "prepare", "verify", "publish", "unpublish", "disconnect"})
CHANNELS = frozenset({"git", "cms", "hosted", "portable"})


@dataclass(frozen=True)
class ChannelRequest:
    """Trusted controller context; user payload never supplies handler functions.

    Domain handlers still perform their existing company, consent, source,
    billing and provider checks. This context cannot act as an authority lease.
    Omit private objects from repr so routine diagnostics cannot expose content.
    """

    context: Any = field(repr=False)
    config: Any = field(repr=False)
    user: Any = field(default=None, repr=False)
    operation: Any = field(default=None, repr=False)
    run: Any = field(default=None, repr=False)
    data: dict = field(default_factory=dict, repr=False)
    step: str = ""
    now: Any = None


class PublishingChannel(ABC):
    """One explicit surface; unavailable effects must never report completion."""

    name: str
    effect_capabilities: frozenset[str] = frozenset()

    @abstractmethod
    def inspect(self, request: ChannelRequest):
        """Read saved channel facts without starting provider work."""

    @abstractmethod
    def capture_existing(self, request: ChannelRequest):
        """Capture existing content within the current provider binding."""

    @abstractmethod
    def prepare(self, request: ChannelRequest):
        """Prepare through the original guarded and idempotent controls."""

    @abstractmethod
    def verify(self, request: ChannelRequest):
        """Require channel-specific evidence; an export is never live proof."""

    @abstractmethod
    def publish(self, request: ChannelRequest):
        """Generate/deliver an article through its current original consent."""

    @abstractmethod
    def unpublish(self, request: ChannelRequest):
        """Remove only specifically authorized content owned by this channel."""

    @abstractmethod
    def disconnect(self, request: ChannelRequest):
        """Revoke future effects while preserving published content by default."""
