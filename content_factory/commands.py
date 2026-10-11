"""Explicit actor and input for domain operations shared across transports."""
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ContentCommand:
    """An authenticated domain command, with no HTTP or credential dispatch."""

    user: Any
    data: dict
