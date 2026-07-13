"""Low-friction abuse and cost controls for the Health Hack AI gateway.

The public game remains anonymous. These guards operate on the Worker-minted
participant UUID and a coarse source network supplied by the authenticated
Worker, so ordinary players do not need an account or challenge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta
import hashlib
import io
import ipaddress
import logging
import math
import secrets
import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.parsers import JSONParser


logger = logging.getLogger(__name__)

CACHE_PREFIX = "health-hack:ai:v1"
VALID_MODES = {"observe", "enforce"}


class RequestEntityTooLarge(APIException):
    status_code = 413
    default_detail = "request body is too large"
    default_code = "request_too_large"


class LimitedJSONParser(JSONParser):
    """JSON parser that bounds chunked and Content-Length request bodies."""

    def parse(self, stream, media_type=None, parser_context=None):
        max_bytes = int(getattr(settings, "HEALTH_HACK_AI_BODY_MAX_BYTES", 16 * 1024))
        request = (parser_context or {}).get("request")
        declared = (getattr(request, "META", {}) or {}).get("CONTENT_LENGTH", "")
        try:
            if declared and int(declared) > max_bytes:
                raise RequestEntityTooLarge()
        except (TypeError, ValueError):
            # A malformed Content-Length must not bypass the actual stream cap.
            pass

        raw = stream.read(max_bytes + 1)
        if isinstance(raw, str):
            raw = raw.encode((parser_context or {}).get("encoding") or "utf-8")
        if len(raw) > max_bytes:
            raise RequestEntityTooLarge()
        return super().parse(
            io.BytesIO(raw),
            media_type=media_type,
            parser_context=parser_context,
        )


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    code: str = ""
    retry_after_seconds: int = 0
    observed: bool = False


def _mode(setting_name: str) -> str:
    value = str(getattr(settings, setting_name, "observe") or "observe").lower()
    return value if value in VALID_MODES else "observe"


def _setting_bool(name: str, default: bool = False) -> bool:
    value = getattr(settings, name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _increment(key: str, *, amount: int = 1, timeout: int) -> int:
    """Atomically increment a cache counter on Redis and LocMem backends."""

    if cache.add(key, amount, timeout=timeout):
        return amount
    try:
        return int(cache.incr(key, amount))
    except ValueError:
        # The key may have expired between add() and incr(). Retrying from the
        # initial amount is safe; at worst an expiring window is conservative.
        cache.set(key, amount, timeout=timeout)
        return amount


def _window_counter(scope: str, identity: str, seconds: int) -> tuple[int, int]:
    now = int(time.time())
    window = now // seconds
    retry_after = max(1, seconds - (now % seconds))
    key = f"{CACHE_PREFIX}:quota:{scope}:{identity}:{seconds}:{window}"
    count = _increment(key, timeout=seconds + 2)
    return count, retry_after


def _hashed(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def participant_log_id(value: str) -> str:
    """Return a stable pseudonym suitable for aggregate security telemetry."""

    return _hashed(str(value))


def source_network_key(request) -> str:
    """Return a privacy-preserving /24 (IPv4) or /64 (IPv6) cache identity.

    X-Health-Hack-Source-IP is trusted only because the view first authenticates
    the dedicated Worker credential. CF-Connecting-IP and REMOTE_ADDR are safe
    fallbacks during a rolling Worker deployment.
    """

    meta = getattr(request, "META", {}) or {}
    raw = (
        meta.get("HTTP_X_HEALTH_HACK_SOURCE_IP")
        or meta.get("HTTP_CF_CONNECTING_IP")
        or meta.get("REMOTE_ADDR")
        or "unknown"
    )
    raw = str(raw).split(",", 1)[0].strip()
    try:
        address = ipaddress.ip_address(raw)
        prefix = 24 if address.version == 4 else 64
        network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
        normalized = str(network)
    except ValueError:
        normalized = "unknown"
    return _hashed(normalized)


def consume_rate_limits(participant_id: str, network_key: str) -> GuardDecision:
    """Consume participant and coarse-network fixed-window quotas.

    Counters use Redis in production and the existing LocMem fallback in tests.
    Observe mode records the same counters and warnings without rejecting play.
    """

    mode = _mode("HEALTH_HACK_AI_RATE_LIMIT_MODE")
    participant_limits = (
        (10, int(getattr(settings, "HEALTH_HACK_AI_PARTICIPANT_BURST_LIMIT", 3))),
        (600, int(getattr(settings, "HEALTH_HACK_AI_PARTICIPANT_10M_LIMIT", 40))),
        (3600, int(getattr(settings, "HEALTH_HACK_AI_PARTICIPANT_HOURLY_LIMIT", 100))),
    )
    network_limits = (
        (10, int(getattr(settings, "HEALTH_HACK_AI_NETWORK_BURST_LIMIT", 60))),
        (600, int(getattr(settings, "HEALTH_HACK_AI_NETWORK_10M_LIMIT", 400))),
        (3600, int(getattr(settings, "HEALTH_HACK_AI_NETWORK_HOURLY_LIMIT", 1000))),
    )
    breaches: list[tuple[str, int, int, int]] = []
    retry_after = 0
    try:
        for scope, identity, limits in (
            ("participant", str(participant_id), participant_limits),
            ("network", network_key, network_limits),
        ):
            for seconds, limit in limits:
                if limit <= 0:
                    continue
                count, window_retry = _window_counter(scope, identity, seconds)
                if count > limit:
                    breaches.append((scope, seconds, count, limit))
                    retry_after = max(retry_after, window_retry)
    except Exception:
        logger.exception("Health Hack AI quota cache unavailable")
        return GuardDecision(
            allowed=mode != "enforce",
            code="ai_guard_unavailable",
            retry_after_seconds=2,
            observed=mode != "enforce",
        )

    if not breaches:
        return GuardDecision(allowed=True)

    logger.warning(
        "Health Hack AI quota threshold crossed participant=%s breaches=%s mode=%s",
        participant_log_id(participant_id),
        [(scope, seconds, count, limit) for scope, seconds, count, limit in breaches],
        mode,
    )
    return GuardDecision(
        allowed=mode != "enforce",
        code="ai_rate_limited",
        retry_after_seconds=retry_after,
        observed=True,
    )


class InflightLease:
    """One short-lived upstream request lease per participant across all NPCs."""

    def __init__(self, participant_id: str, role: str):
        # Keep role in the call signature for readable call sites and backwards
        # compatible tests, but deliberately omit it from the key. Multiple
        # tabs talking to different NPCs must not create overlapping model
        # calls for one anonymous participant.
        self.key = f"{CACHE_PREFIX}:inflight:{participant_id}"
        self.owner = secrets.token_urlsafe(18)
        self.ttl = int(getattr(settings, "HEALTH_HACK_AI_INFLIGHT_TTL_SECONDS", 35))
        self.acquired = False

    def acquire(self) -> bool:
        self.acquired = bool(cache.add(self.key, self.owner, timeout=max(1, self.ttl)))
        return self.acquired

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            backend_client = getattr(cache, "_cache", None)
            get_client = getattr(backend_client, "get_client", None)
            serializer = getattr(backend_client, "_serializer", None)
            make_key = getattr(cache, "make_and_validate_key", None)
            if callable(get_client) and serializer is not None and callable(make_key):
                redis_key = make_key(self.key)
                expected = serializer.dumps(self.owner)
                client = get_client(redis_key, write=True)
                client.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] then "
                    "return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    redis_key,
                    expected,
                )
            elif cache.get(self.key) == self.owner:
                # Local/test fallback. Redis uses the atomic compare-delete above.
                cache.delete(self.key)
        except Exception:
            # The short TTL remains the safety net. Never turn a completed player
            # response into an error solely because lock cleanup failed.
            logger.exception("Health Hack AI in-flight lease cleanup failed")
        finally:
            self.acquired = False


def acquire_inflight(participant_id: str, role: str) -> tuple[InflightLease | None, GuardDecision]:
    lease = InflightLease(participant_id, role)
    try:
        if lease.acquire():
            return lease, GuardDecision(allowed=True)
        return None, GuardDecision(
            allowed=False,
            code="ai_request_in_flight",
            retry_after_seconds=2,
        )
    except Exception:
        logger.exception("Health Hack AI in-flight cache unavailable")
        return None, GuardDecision(
            allowed=False,
            code="ai_guard_unavailable",
            retry_after_seconds=2,
        )


def _daily_timeout() -> int:
    now = timezone.now()
    local_now = timezone.localtime(now)
    tomorrow = local_now.date() + timedelta(days=1)
    midnight = timezone.make_aware(
        datetime.combine(tomorrow, datetime_time.min),
        timezone.get_current_timezone(),
    )
    return max(60, math.ceil((midnight - now).total_seconds()) + 60)


def _daily_key(kind: str) -> str:
    return f"{CACHE_PREFIX}:budget:{timezone.localdate().isoformat()}:{kind}"


def _alert_budget(kind: str, count: int, limit: int, timeout: int) -> None:
    if limit <= 0:
        return
    ratio = count / limit
    for threshold in (0.7, 0.9, 1.0):
        if ratio >= threshold:
            marker = f"{_daily_key(kind)}:alert:{int(threshold * 100)}"
            if cache.add(marker, True, timeout=timeout):
                logger.warning(
                    "Health Hack AI daily %s budget reached %s%% (%s/%s)",
                    kind,
                    int(threshold * 100),
                    count,
                    limit,
                )


def reserve_global_call() -> GuardDecision:
    """Reserve one upstream call against the daily circuit breaker."""

    if _setting_bool("HEALTH_HACK_AI_KILL_SWITCH"):
        return GuardDecision(
            allowed=False,
            code="ai_temporarily_disabled",
            retry_after_seconds=60,
        )

    mode = _mode("HEALTH_HACK_AI_BUDGET_MODE")
    call_limit = int(getattr(settings, "HEALTH_HACK_AI_DAILY_CALL_LIMIT", 5000))
    token_limit = int(getattr(settings, "HEALTH_HACK_AI_DAILY_TOKEN_LIMIT", 5_000_000))
    timeout = _daily_timeout()
    try:
        current_tokens = int(cache.get(_daily_key("tokens"), 0) or 0)
        call_count = _increment(_daily_key("calls"), timeout=timeout)
        _alert_budget("calls", call_count, call_limit, timeout)
        _alert_budget("tokens", current_tokens, token_limit, timeout)
    except Exception:
        logger.exception("Health Hack AI budget cache unavailable")
        return GuardDecision(
            allowed=mode != "enforce",
            code="ai_guard_unavailable",
            retry_after_seconds=2,
            observed=mode != "enforce",
        )

    exceeded = (
        (call_limit > 0 and call_count > call_limit)
        or (token_limit > 0 and current_tokens >= token_limit)
    )
    if not exceeded:
        return GuardDecision(allowed=True)

    logger.warning(
        "Health Hack AI daily budget exceeded calls=%s/%s tokens=%s/%s mode=%s",
        call_count,
        call_limit,
        current_tokens,
        token_limit,
        mode,
    )
    return GuardDecision(
        allowed=mode != "enforce",
        code="ai_budget_exhausted",
        retry_after_seconds=max(60, timeout - 60),
        observed=True,
    )


def record_global_tokens(prompt_tokens: int | None, completion_tokens: int | None) -> None:
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    if total <= 0:
        return
    timeout = _daily_timeout()
    limit = int(getattr(settings, "HEALTH_HACK_AI_DAILY_TOKEN_LIMIT", 5_000_000))
    try:
        count = _increment(_daily_key("tokens"), amount=total, timeout=timeout)
        _alert_budget("tokens", count, limit, timeout)
    except Exception:
        # The call ceiling still bounds spend if an upstream response omits usage
        # or a post-response token counter update fails.
        logger.exception("Health Hack AI token budget update failed")
