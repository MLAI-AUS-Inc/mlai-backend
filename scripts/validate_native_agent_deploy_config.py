"""Validate the bounded native pilot without disclosing configuration values."""

import json
import os
import re
import sys
from urllib.parse import urlsplit
from uuid import UUID

FLAGS = {"MLAI_MCP_NATIVE_AGENTS_ENABLED", "AGENT_RUNTIME_ENABLED", "AGENT_RUNTIME_NATIVE_ENABLED",
         "AGENT_RUNTIME_NATIVE_REPLIES_ENABLED", "AGENT_RUNTIME_MCP_EVENTS_ENABLED"}
# These remain independently switchable even while the pilot is enabled.
OPTIONAL_FLAGS = {"AGENT_RUNTIME_COMMUNITY_ENABLED", "AGENT_RUNTIME_NATIVE_ALWAYS_ADMIT", "AGENT_RUNTIME_NATIVE_BACKLOG_REPLAY",
                  "AGENT_RUNTIME_NATIVE_DURABLE_WATCH", "AGENT_RUNTIME_NATIVE_LAPSE_ALERTS",
                  "AGENT_RUNTIME_NATIVE_RENEWAL_ENABLED", "MLAI_MCP_AGENT_GRANT_EXTENSION_ENABLED"}
BOUNDED_INTEGERS = {
    "AGENT_RUNTIME_NATIVE_QUEUE_TTL_MINUTES": (15, 10080),
    "AGENT_RUNTIME_NATIVE_ADMISSION_DAILY_CAP": (1, 1000),
    "AGENT_RUNTIME_NATIVE_DENIAL_GRACE_SECONDS": (60, 86400),
    "AGENT_RUNTIME_NATIVE_UNIFIED_GRACE_SECONDS": (600, 604800),
    "MLAI_MCP_REFRESH_REPLAY_GRACE_SECONDS": (0, 300),
}
ENUMS = {"AGENT_RUNTIME_MCP_EVENTS_LEASE_POLICY": {"24h", "grant"}}
TIMEZONE_KEY = "AGENT_RUNTIME_NATIVE_LIMIT_TIMEZONE"
REQUIRED_VALUES = {"AGENT_RUNTIME_NATIVE_COMMUNITIES", "AGENT_RUNTIME_DOT_SETUP_URL", "AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT"}
VALUES = FLAGS | REQUIRED_VALUES | OPTIONAL_FLAGS | set(BOUNDED_INTEGERS) | set(ENUMS) | {TIMEZONE_KEY}
SECRETS = {"AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY", "AGENT_RUNTIME_NATIVE_REGISTRY_KEY", "COMMUNITY_CHAT_ROLE_SERVICE_TOKEN"}
ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def canonical_uuid(value):
    return isinstance(value, str) and str(UUID(value)) == value


def https_url(value, *, origin=False):
    try:
        parsed = urlsplit(value)
        parsed.port
    except (ValueError, TypeError):
        return False
    return bool(parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None
        and not parsed.query and not parsed.fragment and (not origin or parsed.path in ("", "/"))
        and not any(character.isspace() for character in value))


def validate_single(key, value):
    """Reject malformed stdin payloads before they can replace a host setting."""
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{key} must be a non-empty single-line value")
    if key in FLAGS | OPTIONAL_FLAGS:
        if value not in {"true", "false"}:
            raise ValueError(f"{key} must be true or false")
    elif key in BOUNDED_INTEGERS:
        low, high = BOUNDED_INTEGERS[key]
        if not re.fullmatch(r"0|[1-9][0-9]{0,6}", value) or not low <= int(value) <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    elif key in ENUMS:
        if value not in ENUMS[key]:
            raise ValueError(f"{key} has an unsupported policy")
    elif key == TIMEZONE_KEY:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*", value):
            raise ValueError(f"{key} must be an IANA timezone name")
    elif key == "AGENT_RUNTIME_NATIVE_PERSONAL_LIMIT":
        if value != "1":
            raise ValueError("The native pilot requires one personal installation per community")
    elif key == "AGENT_RUNTIME_DOT_SETUP_URL":
        if not https_url(value):
            raise ValueError(f"{key} requires the actual HTTPS provider setup URL")
    elif key == "AGENT_RUNTIME_NATIVE_COMMUNITIES":
        try:
            communities = json.loads(value)
            if not isinstance(communities, dict) or len(communities) > 1:
                raise ValueError()
            for community, config in communities.items():
                if (not canonical_uuid(community) or not isinstance(config, dict)
                        or set(config) != {"relay_url", "channel_ids"} or not https_url(config["relay_url"], origin=True)
                        or not isinstance(config["channel_ids"], list) or len(config["channel_ids"]) != 1
                        or not canonical_uuid(config["channel_ids"][0])):
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("The native pilot requires one canonical community, HTTPS relay and public channel") from exc
    elif key == "AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY":
        if not re.fullmatch("[0-9a-f]{64}", value) or not 0 < int(value, 16) < ORDER:
            raise ValueError(f"{key} requires a dedicated valid secp256k1 private key")
    elif key in {"AGENT_RUNTIME_NATIVE_REGISTRY_KEY", "COMMUNITY_CHAT_ROLE_SERVICE_TOKEN"}:
        if len(value.encode()) < 32 or any(character.isspace() for character in value):
            raise ValueError(f"{key} requires at least 32 bytes without whitespace")
    else:
        raise ValueError("Unsupported native pilot configuration key")


def validate(environment, *, check_provider=False):
    """Keep dormant settings optional and require complete explicit activation."""
    for key in FLAGS | OPTIONAL_FLAGS:
        validate_single(key, environment.get(key, "false"))
    for key in (VALUES | SECRETS) - FLAGS:
        if environment.get(key):
            validate_single(key, environment[key])
    community = environment.get("AGENT_RUNTIME_COMMUNITY_ENABLED") == "true"
    if community and any(environment.get(key) != "true" for key in FLAGS):
        raise ValueError("Community replies require the complete native gateway")
    # Model credentials stay in the host's secret environment. The local deploy
    # preflight cannot see them; the remote preflight validates readiness below.
    if community and check_provider:
        if (not https_url(environment.get("AGENT_RUNTIME_MODEL_BASE_URL", ""))
                or not environment.get("AGENT_RUNTIME_MODEL_NAME")
                or not environment.get("AGENT_RUNTIME_MODEL_API_KEY")
                or any(c in environment["AGENT_RUNTIME_MODEL_API_KEY"] for c in "\r\n")):
            raise ValueError("Community replies require configured HTTPS inference")
        for key in ("AGENT_RUNTIME_MODEL_INPUT_MICROUNITS_PER_MILLION", "AGENT_RUNTIME_MODEL_OUTPUT_MICROUNITS_PER_MILLION"):
            value = environment.get(key, "0")
            if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 1000000000:
                raise ValueError("Community inference requires valid nonzero pricing")
    if environment.get("MLAI_MCP_NATIVE_AGENTS_ENABLED", "false") != "true":
        return
    if any(environment.get(key) != "true" for key in FLAGS | {"MLAI_MCP_ENABLED"}):
        raise ValueError("The native pilot requires its complete runtime and unified MCP gates")
    if environment.get("COMMUNITY_CHAT_STARTUP_UPDATES_ENABLED") == "false":
        raise ValueError("The native pilot requires the existing startup connection gate")
    for key in REQUIRED_VALUES | SECRETS:
        if not environment.get(key):
            raise ValueError(f"The enabled native pilot requires {key}")
    if not json.loads(environment["AGENT_RUNTIME_NATIVE_COMMUNITIES"]):
        raise ValueError("The enabled native pilot requires its single approved public channel")
    role = environment["COMMUNITY_CHAT_ROLE_SERVICE_TOKEN"]
    if role in {environment["AGENT_RUNTIME_NATIVE_REGISTRY_KEY"], environment["AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY"],
            *(environment.get(key) for key in ("ROO_API_KEY", "INTERNAL_API_KEY", "COMMUNITY_CHAT_ADAPTER_TOKEN"))}:
        raise ValueError("The relay role service requires an independent trust-domain credential")
    if environment["AGENT_RUNTIME_NATIVE_REGISTRY_KEY"] in {
            environment["AGENT_RUNTIME_NATIVE_SERVICE_PRIVATE_KEY"],
            *(environment.get(key) for key in ("ROO_API_KEY", "INTERNAL_API_KEY", "COMMUNITY_CHAT_ADAPTER_TOKEN"))}:
        raise ValueError("The native registry requires an independent trust-domain credential")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--stdin":
            validate_single(sys.argv[2], sys.stdin.read())
        elif len(sys.argv) == 1 or sys.argv[1:] == ["--provider"]:
            validate(dict(os.environ), check_provider=sys.argv[1:] == ["--provider"])
        else:
            raise ValueError("Use no arguments, or --stdin with one native configuration key")
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
