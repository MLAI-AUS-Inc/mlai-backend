"""Fail-closed delegation policy. Identity always comes from persisted grants."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


SCOPES = frozenset({
    "tasks:read", "tasks:write", "responsibilities:read", "responsibilities:write",
    "events:read", "messages:read", "messages:write", "messages:reply", "artifacts:read",
    "artifacts:write", "actions:propose", "web:access",
})
DEFAULT_LIMITS = {
    "max_seconds": 300, "max_model_calls": 20, "max_model_tokens": 50000,
    "max_cost_microunits": 100000, "max_artifact_bytes": 10485760,
}
HARD_LIMITS = {
    "max_seconds": 1800, "max_model_calls": 100, "max_model_tokens": 500000,
    "max_cost_microunits": 1000000, "max_artifact_bytes": 104857600,
}


class AgentPermissionDenied(ValueError):
    def __init__(self, code="agent_permission_denied"):
        self.code = code
        super().__init__(code)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def bounded_json(value, *, max_bytes=65536):
    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise AgentPermissionDenied("invalid_payload") from exc
    if len(serialized.encode()) > max_bytes:
        raise AgentPermissionDenied("payload_too_large")
    return json.loads(serialized)


def validate_scopes(scopes):
    if not isinstance(scopes, list) or not scopes or any(not isinstance(s, str) or s not in SCOPES for s in scopes):
        raise AgentPermissionDenied("invalid_scopes")
    return sorted(set(scopes))


def validate_resource(resource):
    if not isinstance(resource, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:/_.@-]{0,299}", resource):
        raise AgentPermissionDenied("invalid_resource")
    if ".." in resource or "://" in resource:
        raise AgentPermissionDenied("invalid_resource")
    return resource


def validate_resources(resources):
    if not isinstance(resources, list) or len(resources) > 100:
        raise AgentPermissionDenied("invalid_resources")
    return sorted({validate_resource(resource) for resource in resources})


def feature_enabled():
    from django.conf import settings
    return bool(getattr(settings, "AGENT_RUNTIME_ENABLED", False))


def validate_grant_base(grant, *, expected_generation=None, now=None):
    now = now or datetime.now(timezone.utc)
    if not feature_enabled():
        raise AgentPermissionDenied("agent_runtime_disabled")
    installation = grant.installation
    if not installation.is_active or not installation.owner.is_active:
        raise AgentPermissionDenied("installation_inactive")
    if getattr(installation, "community_id", None) is not None and getattr(installation, "state", "") != "active":
        raise AgentPermissionDenied("installation_inactive")
    if grant.revoked_at is not None or grant.expires_at <= now:
        raise AgentPermissionDenied("grant_inactive")
    if expected_generation is not None and grant.generation != expected_generation:
        raise AgentPermissionDenied("grant_changed")
    validate_scopes(grant.scopes)
    validate_resources(grant.resources)
    return grant


def authorize(grant, scope, resource=None, *, expected_generation=None, access_checker=None, now=None):
    """Intersect current consent, runtime scopes, and live source-account ACL.

    The resource checker MUST return True (not merely a truthy object); failures
    and absent adapters deny access. Callers never choose an owner independently
    of the authenticated grant. Non-resource control APIs still constrain their
    querysets to that grant/owner; this function does not authorize arbitrary IDs.
    """
    validate_grant_base(grant, expected_generation=expected_generation, now=now)
    if scope not in SCOPES or scope not in grant.scopes:
        raise AgentPermissionDenied("scope_denied")
    if resource is not None:
        validate_resource(resource)
        if resource not in grant.resources:
            raise AgentPermissionDenied("resource_denied")
        try:
            allowed = access_checker is not None and access_checker(grant.installation.owner, scope, resource) is True
        except Exception as exc:
            if getattr(exc, "status_code", None) in {401, 403, 404}:
                raise AgentPermissionDenied("current_access_denied") from exc
            raise AgentPermissionDenied("resource_unavailable") from exc
        if not allowed:
            raise AgentPermissionDenied("current_access_denied")


def normalize_limits(limits=None, *, max_seconds=None):
    if limits is not None and not isinstance(limits, dict):
        raise AgentPermissionDenied("invalid_limits")
    values = dict(DEFAULT_LIMITS)
    if set(limits or {}) - set(values):
        raise AgentPermissionDenied("invalid_limits")
    values.update(limits or {})
    for key, value in values.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > HARD_LIMITS[key]:
            raise AgentPermissionDenied("invalid_limits")
    if max_seconds is not None and values["max_seconds"] > max_seconds:
        raise AgentPermissionDenied("task_duration_exceeds_grant")
    return values


def instruction_text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 16000 or "\x00" in value:
        raise AgentPermissionDenied("invalid_instruction")
    return value.strip()


def idempotency_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._/@-]{0,159}", value):
        raise AgentPermissionDenied("invalid_idempotency_key")
    return value


def artifact_name(value):
    """Validate display-only relative paths; storage keys stay server generated."""
    if (not isinstance(value, str) or not 1 <= len(value) <= 160
            or "\\" in value or ":" in value
            or any(not character.isprintable() for character in value)
            or any(part in {"", ".", ".."} or not part.strip() for part in value.split("/"))):
        raise AgentPermissionDenied("invalid_artifact_name")
    return value


def after_quiet_hours(instant, *, timezone_name, quiet_start=None, quiet_end=None):
    """Return a real UTC instant outside the specified local quiet interval.

    Minute stepping resolves daylight-saving gaps/folds without constructing a
    nonexistent wall time. Seconds are retained and no wake is moved earlier.
    """
    if (quiet_start is None) != (quiet_end is None):
        raise AgentPermissionDenied("invalid_quiet_hours")
    try:
        zone = ZoneInfo(timezone_name)
    except (ValueError, KeyError) as exc:
        raise AgentPermissionDenied("invalid_timezone") from exc
    if instant.tzinfo is None:
        raise AgentPermissionDenied("naive_timestamp")
    if quiet_start is None or quiet_start == quiet_end:
        return instant
    if not isinstance(quiet_start, time) or not isinstance(quiet_end, time):
        raise AgentPermissionDenied("invalid_quiet_hours")
    result = instant.astimezone(timezone.utc)
    for _ in range(26 * 60 + 1):
        wall = result.astimezone(zone).time().replace(tzinfo=None)
        quiet = quiet_start <= wall < quiet_end if quiet_start < quiet_end else wall >= quiet_start or wall < quiet_end
        if not quiet:
            return result
        result += timedelta(minutes=1)
    raise AgentPermissionDenied("invalid_quiet_hours")
