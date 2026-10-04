#!/usr/bin/env python3
"""Validate repository rollout and exact migration approval before deployment."""

import ipaddress
import os
import re
import sys


MODE = "WEBSITE_CONNECTION_WRITE_MODE"
CANARIES = "WEBSITE_CONNECTION_CANARY_DOMAINS"
APPROVAL = "APPROVED_MIGRATION_PLAN_SHA256"
DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_single(key, value):
    """Accept canonical single-line values; empty optional keys clear old state."""
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a single-line value")
    if key == MODE:
        if value not in {"disabled", "canary", "enabled"}:
            raise ValueError(f"{MODE} must be disabled, canary or enabled")
    elif key == CANARIES:
        if not value:
            return
        domains = value.split(",")
        if len(domains) > 50 or len(value) > 4096 or len(set(domains)) != len(domains):
            raise ValueError(f"{CANARIES} must be a bounded unique domain list")
        for domain in domains:
            if not DOMAIN.fullmatch(domain):
                raise ValueError(f"{CANARIES} requires exact lowercase domains, without URLs or wildcards")
            try:
                ipaddress.ip_address(domain)
            except ValueError:
                continue
            raise ValueError(f"{CANARIES} must contain domains, not IP addresses")
    elif key == APPROVAL:
        if value and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError(f"{APPROVAL} must be an exact lowercase SHA-256")
    else:
        raise ValueError("Unsupported website deployment key")


def validate(environment):
    """Default to paused repository writes and no migration approval."""
    mode = environment.get(MODE, "disabled")
    canaries = environment.get(CANARIES, "")
    validate_single(MODE, mode)
    validate_single(CANARIES, canaries)
    validate_single(APPROVAL, environment.get(APPROVAL, ""))
    if mode == "canary" and not canaries:
        raise ValueError(f"{MODE}=canary requires an exact domain allowlist")


def canonical_migration_plan(raw):
    """Keep every planned operation, excluding environment-specific import logs."""
    lines = raw.splitlines()
    headers = [index for index, line in enumerate(lines) if line == "Planned operations:"]
    if len(headers) != 1:
        raise ValueError("Migration output must contain one exact planned-operations header")
    return "\n".join(lines[headers[0]:]).rstrip("\n")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin":
            validate_single(sys.argv[2], sys.stdin.read())
        elif sys.argv[1:] == ["--migration-plan"]:
            sys.stdout.write(canonical_migration_plan(sys.stdin.read()))
        elif len(sys.argv) == 1:
            validate(dict(os.environ))
        else:
            raise ValueError("Use no arguments, --migration-plan, or --stdin with a configuration key")
    except ValueError as error:
        raise SystemExit(str(error)) from error
