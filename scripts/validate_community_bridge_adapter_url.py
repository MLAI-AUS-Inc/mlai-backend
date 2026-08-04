#!/usr/bin/env python3
"""Validate the narrowly allowed MLAI Chat bridge adapter base URLs."""

from __future__ import annotations

import ipaddress
import sys
from urllib.parse import urlparse


PUBLIC_BRIDGE_HOST = "chat.mlai.au"
PUBLIC_BRIDGE_PATH = "/_mlai/bridge"


def validate_adapter_url(value: str) -> None:
    parsed = urlparse(value)

    if (
        parsed.username
        or parsed.password
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "BUZZ_BRIDGE_ADAPTER_URL must not contain credentials, parameters, "
            "a query, or a fragment"
        )

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("BUZZ_BRIDGE_ADAPTER_URL contains an invalid port") from exc

    if (
        parsed.scheme == "https"
        and parsed.hostname == PUBLIC_BRIDGE_HOST
        and port in {None, 443}
        and parsed.path == PUBLIC_BRIDGE_PATH
    ):
        return

    if parsed.scheme == "http" and port == 8090 and parsed.path in {"", "/"}:
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise ValueError(
                "BUZZ_BRIDGE_ADAPTER_URL private HTTP mode must use an IP address"
            ) from exc
        if address.is_private or address.is_loopback:
            return

    raise ValueError(
        "BUZZ_BRIDGE_ADAPTER_URL must be either a private/loopback HTTP IP on "
        "port 8090 or https://chat.mlai.au/_mlai/bridge"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: validate_community_bridge_adapter_url.py <adapter-base-url>",
            file=sys.stderr,
        )
        return 2
    try:
        validate_adapter_url(argv[1])
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
