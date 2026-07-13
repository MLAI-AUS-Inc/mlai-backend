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
import json
import logging
import math
import secrets
import threading
import time

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from rest_framework.exceptions import APIException
from rest_framework.parsers import JSONParser


logger = logging.getLogger(__name__)

CACHE_PREFIX = "health-hack:ai:v1"
VALID_MODES = {"observe", "enforce"}
_LOCAL_BUDGET_LOCK = threading.Lock()


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


def read_limited_json(upstream, *, max_bytes: int):
    """Read one streamed JSON response without buffering an unbounded body.

    Transport exceptions deliberately propagate to the caller so they can be
    classified as retryable network failures. Malformed or pathologically deep
    JSON is represented by ``None`` and never escapes as an application 500.
    """

    headers = getattr(upstream, "headers", {}) or {}
    declared = headers.get("content-length") or headers.get("Content-Length")
    try:
        if declared and int(declared) > max_bytes:
            return None
    except (TypeError, ValueError):
        pass

    chunks = []
    total = 0
    iterator = upstream.iter_content(chunk_size=8192)
    for chunk in iterator:
        if not chunk:
            continue
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, TypeError, UnicodeDecodeError, RecursionError):
        return None


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


def _redis_client_and_keys(*keys: str):
    """Return the built-in Redis cache client and versioned wire keys.

    Production is required to use Django's shared Redis cache. LocMem remains a
    development/test fallback and is serialized with ``_LOCAL_BUDGET_LOCK``.
    """

    backend_client = getattr(cache, "_cache", None)
    get_client = getattr(backend_client, "get_client", None)
    make_key = getattr(cache, "make_and_validate_key", None)
    if not callable(get_client) or not callable(make_key):
        return None
    redis_keys = [make_key(key) for key in keys]
    return get_client(redis_keys[0], write=True), redis_keys


def _reserve_budget_atomic(
    *,
    call_limit: int,
    token_limit: int,
    token_reservation: int,
    timeout: int,
    enforce: bool,
) -> tuple[bool, int, int]:
    """Atomically reserve a call and its worst-case token cost."""

    calls_key = _daily_key("calls")
    tokens_key = _daily_key("tokens")
    redis_context = _redis_client_and_keys(calls_key, tokens_key)
    if redis_context is not None:
        client, redis_keys = redis_context
        result = client.eval(
            """
            local calls = tonumber(redis.call('get', KEYS[1]) or '0')
            local tokens = tonumber(redis.call('get', KEYS[2]) or '0')
            local next_calls = calls + 1
            local next_tokens = tokens + tonumber(ARGV[3])
            local call_blocked = tonumber(ARGV[1]) > 0 and next_calls > tonumber(ARGV[1])
            local token_blocked = tonumber(ARGV[2]) > 0 and next_tokens > tonumber(ARGV[2])
            if tonumber(ARGV[5]) == 1 and (call_blocked or token_blocked) then
                return {0, calls, tokens}
            end
            calls = redis.call('incr', KEYS[1])
            tokens = redis.call('incrby', KEYS[2], ARGV[3])
            if redis.call('ttl', KEYS[1]) < 0 then redis.call('expire', KEYS[1], ARGV[4]) end
            if redis.call('ttl', KEYS[2]) < 0 then redis.call('expire', KEYS[2], ARGV[4]) end
            return {1, calls, tokens}
            """,
            2,
            *redis_keys,
            max(0, call_limit),
            max(0, token_limit),
            max(0, token_reservation),
            max(1, timeout),
            1 if enforce else 0,
        )
        return bool(int(result[0])), int(result[1]), int(result[2])

    with _LOCAL_BUDGET_LOCK:
        calls = int(cache.get(calls_key, 0) or 0)
        tokens = int(cache.get(tokens_key, 0) or 0)
        next_calls = calls + 1
        next_tokens = tokens + token_reservation
        blocked = (
            (call_limit > 0 and next_calls > call_limit)
            or (token_limit > 0 and next_tokens > token_limit)
        )
        if enforce and blocked:
            return False, calls, tokens
        cache.set(calls_key, next_calls, timeout=timeout)
        cache.set(tokens_key, next_tokens, timeout=timeout)
        return True, next_calls, next_tokens


def _release_budget_atomic(*, calls: int, tokens: int, timeout: int) -> tuple[int, int]:
    """Atomically return an unused reservation without allowing negatives."""

    calls_key = _daily_key("calls")
    tokens_key = _daily_key("tokens")
    redis_context = _redis_client_and_keys(calls_key, tokens_key)
    if redis_context is not None:
        client, redis_keys = redis_context
        result = client.eval(
            """
            local current_calls = tonumber(redis.call('get', KEYS[1]) or '0')
            local current_tokens = tonumber(redis.call('get', KEYS[2]) or '0')
            local next_calls = math.max(0, current_calls - tonumber(ARGV[1]))
            local next_tokens = math.max(0, current_tokens - tonumber(ARGV[2]))
            redis.call('set', KEYS[1], next_calls, 'EX', ARGV[3])
            redis.call('set', KEYS[2], next_tokens, 'EX', ARGV[3])
            return {next_calls, next_tokens}
            """,
            2,
            *redis_keys,
            max(0, calls),
            max(0, tokens),
            max(1, timeout),
        )
        return int(result[0]), int(result[1])

    with _LOCAL_BUDGET_LOCK:
        next_calls = max(0, int(cache.get(calls_key, 0) or 0) - max(0, calls))
        next_tokens = max(0, int(cache.get(tokens_key, 0) or 0) - max(0, tokens))
        cache.set(calls_key, next_calls, timeout=timeout)
        cache.set(tokens_key, next_tokens, timeout=timeout)
        return next_calls, next_tokens


class BudgetReservation:
    """A worst-case token reservation for one admitted upstream attempt."""

    def __init__(self, token_reservation: int, timeout: int):
        self.token_reservation = max(0, int(token_reservation))
        self.timeout = max(1, int(timeout))
        self.active = True

    def cancel(self) -> None:
        """Return the call and token reservation before any upstream attempt."""

        if not self.active:
            return
        _release_budget_atomic(calls=1, tokens=self.token_reservation, timeout=self.timeout)
        self.active = False

    def reconcile(
        self,
        prompt_tokens: int | None,
        completion_tokens: int | None,
    ) -> None:
        """Replace worst-case tokens with trusted usage; missing usage stays worst-case."""

        if not self.active:
            return
        if prompt_tokens is None or completion_tokens is None:
            self.active = False
            return
        actual = max(0, int(prompt_tokens)) + max(0, int(completion_tokens))
        if actual > self.token_reservation:
            # Response projection should make this impossible. Keeping the full
            # reservation is the conservative choice if contracts drift.
            logger.error(
                "Health Hack AI usage exceeded worst-case reservation actual=%s reserved=%s",
                actual,
                self.token_reservation,
            )
            self.active = False
            return
        _release_budget_atomic(
            calls=0,
            tokens=self.token_reservation - actual,
            timeout=self.timeout,
        )
        self.active = False

    def finalize_unknown(self) -> None:
        """Keep the full reservation when an attempted call has no trusted usage."""

        self.active = False


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


def reserve_global_call() -> tuple[BudgetReservation | None, GuardDecision]:
    """Reserve one call plus its worst-case tokens against the daily budget."""

    if _setting_bool("HEALTH_HACK_AI_KILL_SWITCH"):
        return None, GuardDecision(
            allowed=False,
            code="ai_temporarily_disabled",
            retry_after_seconds=60,
        )

    mode = _mode("HEALTH_HACK_AI_BUDGET_MODE")
    call_limit = int(getattr(settings, "HEALTH_HACK_AI_DAILY_CALL_LIMIT", 5000))
    token_limit = int(getattr(settings, "HEALTH_HACK_AI_DAILY_TOKEN_LIMIT", 5_000_000))
    token_reservation = max(
        0,
        int(getattr(settings, "HEALTH_HACK_AI_MAX_PROMPT_TOKENS", 100_000)),
    ) + max(
        0,
        int(getattr(settings, "HEALTH_HACK_AI_MAX_COMPLETION_TOKENS", 8_192)),
    )
    timeout = _daily_timeout()
    try:
        admitted, call_count, current_tokens = _reserve_budget_atomic(
            call_limit=call_limit,
            token_limit=token_limit,
            token_reservation=token_reservation,
            timeout=timeout,
            enforce=mode == "enforce",
        )
        _alert_budget("calls", call_count, call_limit, timeout)
        _alert_budget("tokens", current_tokens, token_limit, timeout)
    except Exception:
        logger.exception("Health Hack AI budget cache unavailable")
        return None, GuardDecision(
            allowed=mode != "enforce",
            code="ai_guard_unavailable",
            retry_after_seconds=2,
            observed=mode != "enforce",
        )

    exceeded = not admitted or (
        (call_limit > 0 and call_count > call_limit)
        or (token_limit > 0 and current_tokens > token_limit)
    )
    if not exceeded:
        return BudgetReservation(token_reservation, timeout), GuardDecision(allowed=True)

    logger.warning(
        "Health Hack AI daily budget exceeded calls=%s/%s tokens=%s/%s mode=%s",
        call_count,
        call_limit,
        current_tokens,
        token_limit,
        mode,
    )
    decision = GuardDecision(
        allowed=mode != "enforce",
        code="ai_budget_exhausted",
        retry_after_seconds=max(60, timeout - 60),
        observed=True,
    )
    reservation = BudgetReservation(token_reservation, timeout) if decision.allowed else None
    return reservation, decision
