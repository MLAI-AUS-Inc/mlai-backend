"""Roo-backed admission, pricing, settlement, and ticketing for MLAI Coding."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .models import CodingModelCall, CodingPricingVersion, CodingTurn, PointsAccount
from .permissions import IdempotencyConflictError, InsufficientBalanceError
from .services import PointsService


MICROROO_PER_ROO = 1_000_000
MILLION = Decimal("1000000")
CALL_FAILURE_REASONS = {
    "request_invalid",
    "provider_rejected",
    "provider_unavailable",
    "provider_timeout",
    "client_disconnected",
    "dispatch_failed",
    "settlement_unconfirmed",
    "internal_error",
}
USAGE_ENVELOPE_FAILURE_REASON = "usage_outside_admitted_envelope"
RELEASED_SETTLEMENT_AUDIT_MARKER = "late_settlement_report"
RELEASED_FAILURE_AUDIT_PREFIX = "late_failure_report:"
DISPATCH_OWNER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class CodingError(ValueError):
    def __init__(self, code: str, message: str, *, http_status: int = 400, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = dict(extra or {})


@dataclass(frozen=True)
class IssuedTicket:
    token: str
    expires_at: object


def microroo_string(value: int) -> str:
    return str(int(value))


def roo_decimal_string(value_microroo: int) -> str:
    value = Decimal(int(value_microroo)) / Decimal(MICROROO_PER_ROO)
    return f"{value:.6f}"


def _uuid(value, field_name: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CodingError(
            f"invalid_{field_name}",
            f"{field_name} must be a UUID.",
        ) from exc


def _dispatch_owner_hash(value) -> str:
    """Validate and hash the gateway's per-request dispatch owner nonce."""
    if not isinstance(value, str) or not DISPATCH_OWNER_PATTERN.fullmatch(value):
        raise CodingError(
            "invalid_dispatch_owner",
            "dispatch_owner must be a 32 to 128 character URL-safe nonce.",
        )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_dispatch_owner(call: CodingModelCall, owner_hash: str) -> None:
    if not call.dispatch_owner_hash or not hmac.compare_digest(
        call.dispatch_owner_hash,
        owner_hash,
    ):
        raise CodingError(
            "dispatch_owner_mismatch",
            "This call is owned by another dispatch attempt.",
            http_status=409,
        )


def _dispatch_lease_deadline(now):
    return now + timedelta(
        seconds=int(getattr(settings, "MLAI_CODING_DISPATCH_LEASE_SECONDS", 120))
    )


def _nonnegative_int(value, field_name: str, *, maximum: int = 10_000_000) -> int:
    if isinstance(value, bool):
        raise CodingError(f"invalid_{field_name}", f"{field_name} must be an integer.")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise CodingError(f"invalid_{field_name}", f"{field_name} must be an integer.") from exc
    if result < 0 or result > maximum:
        raise CodingError(
            f"invalid_{field_name}",
            f"{field_name} must be between 0 and {maximum}.",
        )
    return result


def _positive_int(value, field_name: str, *, maximum: int = 10_000_000) -> int:
    result = _nonnegative_int(value, field_name, maximum=maximum)
    if result == 0:
        raise CodingError(f"invalid_{field_name}", f"{field_name} must be greater than zero.")
    return result


def _failure_reason_parts(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def _reason_with_marker(value: str, marker: str) -> str | None:
    """Append bounded audit metadata without truncating existing evidence."""
    parts = _failure_reason_parts(value)
    if marker in parts:
        return str(value or "")
    candidate = "; ".join((*parts, marker))
    return candidate if len(candidate) <= 500 else None


def _provider_reference_updates(call, *, provider: str, trace: str) -> dict[str, str]:
    """Return non-destructive provider-reference updates or reject a conflict."""
    updates = {}
    for field_name, reported in (
        ("provider_request_id", provider),
        ("trace_id", trace),
    ):
        stored = getattr(call, field_name)
        if stored and reported and stored != reported:
            raise CodingError(
                "idempotency_conflict",
                "That released call already has different provider audit metadata.",
                http_status=409,
            )
        if not stored and reported:
            updates[field_name] = reported
    return updates


def _record_released_settlement_audit(
    call,
    *,
    prompt: int,
    cached: int,
    output: int,
    provider: str,
    trace: str,
) -> bool:
    """Record a late usage report without ever making a released call billable."""
    parts = _failure_reason_parts(call.failure_reason)
    already_recorded = RELEASED_SETTLEMENT_AUDIT_MARKER in parts
    stored_usage = (call.input_tokens, call.cached_input_tokens, call.output_tokens)
    reported_usage = (prompt, cached, output)
    if (already_recorded or any(stored_usage)) and stored_usage != reported_usage:
        raise CodingError(
            "idempotency_conflict",
            "That released call already has different usage audit metadata.",
            http_status=409,
        )
    updates = _provider_reference_updates(call, provider=provider, trace=trace)
    if already_recorded:
        return False

    marked_reason = _reason_with_marker(
        call.failure_reason,
        RELEASED_SETTLEMENT_AUDIT_MARKER,
    )
    if marked_reason is None:
        # Keeping the original reconciliation evidence is safer than truncating
        # it merely to record a late, explicitly unbilled report.
        return False
    updates.update(
        {
            "failure_reason": marked_reason,
            "input_tokens": prompt,
            "cached_input_tokens": cached,
            "output_tokens": output,
        }
    )
    for field_name, value in updates.items():
        setattr(call, field_name, value)
    call.save(update_fields=(*updates.keys(), "updated_at"))
    return True


def _record_released_failure_audit(
    call,
    *,
    failure_reason: str,
    ambiguous: bool,
    provider: str,
    trace: str,
) -> bool:
    """Record a late failure report while preserving the terminal release."""
    marker = (
        f"{RELEASED_FAILURE_AUDIT_PREFIX}{failure_reason}:"
        f"{'ambiguous' if ambiguous else 'definite'}"
    )
    parts = _failure_reason_parts(call.failure_reason)
    prior_markers = [
        part for part in parts if part.startswith(RELEASED_FAILURE_AUDIT_PREFIX)
    ]
    if prior_markers and marker not in prior_markers:
        raise CodingError(
            "idempotency_conflict",
            "That released call already has different failure audit metadata.",
            http_status=409,
        )
    updates = _provider_reference_updates(call, provider=provider, trace=trace)
    if marker in prior_markers:
        return False

    marked_reason = _reason_with_marker(call.failure_reason, marker)
    if marked_reason is None:
        return False
    updates["failure_reason"] = marked_reason
    for field_name, value in updates.items():
        setattr(call, field_name, value)
    call.save(update_fields=(*updates.keys(), "updated_at"))
    return True


def current_pricing() -> CodingPricingVersion:
    configured = str(
        getattr(settings, "MLAI_CODING_PRICING_VERSION", "do-kimi-k3-2026-08")
    ).strip()
    pricing = CodingPricingVersion.objects.filter(
        version=configured,
        model="kimi-k3",
        is_active=True,
    ).first()
    if pricing is None:
        raise CodingError(
            "pricing_unavailable",
            "The configured Kimi K3 pricing version is unavailable.",
            http_status=503,
        )
    _validate_pricing(pricing)
    return pricing


def _validate_pricing(pricing) -> None:
    rates = (
        pricing.input_usd_per_million,
        pricing.cached_input_usd_per_million,
        pricing.output_usd_per_million,
    )
    # Input/cache discounts may legitimately be free, but the output rate is
    # the divisor used to calculate an affordable completion clamp.  Treating
    # a zero output rate as valid would turn admission into an unhandled
    # Decimal DivisionByZero instead of the intended fail-closed 503.
    if (
        any(Decimal(value) < 0 for value in rates)
        or Decimal(pricing.output_usd_per_million) <= 0
        or any(
            Decimal(value) <= 0
            for value in (
                pricing.usd_aud_rate,
                pricing.margin_multiplier,
                pricing.aud_per_roo,
            )
        )
    ):
        raise CodingError(
            "pricing_unavailable",
            "Kimi K3 pricing contains invalid rates.",
            http_status=503,
        )


def calculate_charge_microroo(
    pricing: CodingPricingVersion,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> int:
    """Apply the published provider cost, FX snapshot, and 30% margin exactly."""
    _validate_pricing(pricing)
    prompt = _nonnegative_int(input_tokens, "input_tokens")
    cached = _nonnegative_int(cached_input_tokens, "cached_input_tokens")
    output = _nonnegative_int(output_tokens, "output_tokens")
    if cached > prompt:
        raise CodingError(
            "invalid_cached_input_tokens",
            "cached_input_tokens cannot exceed input_tokens.",
        )
    uncached = prompt - cached
    provider_usd = (
        Decimal(uncached) * pricing.input_usd_per_million
        + Decimal(cached) * pricing.cached_input_usd_per_million
        + Decimal(output) * pricing.output_usd_per_million
    ) / MILLION
    microroo = (
        provider_usd
        * pricing.usd_aud_rate
        * pricing.margin_multiplier
        / pricing.aud_per_roo
        * Decimal(MICROROO_PER_ROO)
    )
    return int(microroo.to_integral_value(rounding=ROUND_CEILING))


def conservative_call_reservation(
    pricing: CodingPricingVersion,
    *,
    estimated_input_tokens: int,
    requested_output_tokens: int,
    available_microroo: int,
) -> tuple[int, int]:
    """Return `(max_output_tokens, reservation)` without exceeding available Roo."""
    estimated_input = _nonnegative_int(
        estimated_input_tokens,
        "estimated_input_tokens",
    )
    requested_output = _positive_int(
        requested_output_tokens,
        "requested_output_tokens",
    )
    available = max(int(available_microroo), 0)
    input_only = calculate_charge_microroo(
        pricing,
        input_tokens=estimated_input,
        cached_input_tokens=0,
        output_tokens=0,
    )
    if input_only >= available:
        raise CodingError(
            "insufficient_points",
            "The remaining Roo balance cannot cover this request's input.",
            http_status=402,
            extra={"remaining_microroo": microroo_string(available)},
        )

    factor = pricing.usd_aud_rate * pricing.margin_multiplier / pricing.aud_per_roo
    weighted_budget = Decimal(available) / factor
    weighted_input = Decimal(estimated_input) * pricing.input_usd_per_million
    max_by_balance = int(
        max(
            (weighted_budget - weighted_input) / pricing.output_usd_per_million,
            Decimal(0),
        ).to_integral_value(rounding=ROUND_FLOOR)
    )
    max_output = min(requested_output, max_by_balance)
    while max_output > 0:
        reservation = calculate_charge_microroo(
            pricing,
            input_tokens=estimated_input,
            cached_input_tokens=0,
            output_tokens=max_output,
        )
        if reservation <= available:
            return max_output, reservation
        max_output -= 1
    raise CodingError(
        "insufficient_points",
        "The remaining Roo balance cannot cover a model response.",
        http_status=402,
        extra={"remaining_microroo": microroo_string(available)},
    )


def _decode_pem_setting(name: str) -> str:
    value = str(getattr(settings, name, "") or "").strip()
    return value.replace("\\n", "\n")


def _private_signing_key() -> Ed25519PrivateKey:
    raw = _decode_pem_setting("MLAI_CODING_TICKET_PRIVATE_KEY")
    if not raw:
        raise CodingError(
            "ticket_signing_unavailable",
            "Coding ticket signing is not configured.",
            http_status=503,
        )
    try:
        key = load_pem_private_key(raw.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise CodingError(
            "ticket_signing_unavailable",
            "Coding ticket signing is misconfigured.",
            http_status=503,
        ) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise CodingError(
            "ticket_signing_unavailable",
            "Coding tickets require an Ed25519 private key.",
            http_status=503,
        )
    return key


def _public_signing_key() -> Ed25519PublicKey:
    raw = _decode_pem_setting("MLAI_CODING_TICKET_PUBLIC_KEY")
    if raw:
        try:
            key = load_pem_public_key(raw.encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise CodingError(
                "ticket_signing_unavailable",
                "Coding ticket verification is misconfigured.",
                http_status=503,
            ) from exc
        if not isinstance(key, Ed25519PublicKey):
            raise CodingError(
                "ticket_signing_unavailable",
                "Coding tickets require an Ed25519 public key.",
                http_status=503,
            )
        return key
    return _private_signing_key().public_key()


def ticket_jwks() -> dict:
    public = _public_signing_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    encoded = base64.urlsafe_b64encode(public).rstrip(b"=").decode("ascii")
    return {
        "keys": [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "alg": "EdDSA",
                "use": "sig",
                "kid": str(getattr(settings, "MLAI_CODING_TICKET_KEY_ID", "coding-2026-08")),
                "x": encoded,
            }
        ]
    }


def issue_turn_ticket(turn: CodingTurn) -> IssuedTicket:
    now = timezone.now()
    ttl = int(getattr(settings, "MLAI_CODING_TICKET_TTL_SECONDS", 300))
    expires_at = now + timedelta(seconds=max(60, min(ttl, 300)))
    issuer = str(getattr(settings, "MLAI_CODING_TICKET_ISSUER", "api.mlai.au")).strip()
    audience = str(
        getattr(
            settings,
            "MLAI_CODING_TICKET_AUDIENCE",
            "mlai-kimi-inference",
        )
    ).strip()
    if not issuer or not audience:
        raise CodingError(
            "ticket_signing_unavailable",
            "Coding ticket identity is not configured.",
            http_status=503,
        )
    claims = {
        "iss": issuer,
        "aud": audience,
        "sub": str(turn.user.community_chat_profile_id),
        "turn_id": str(turn.id),
        "device_id": str(turn.device_id),
        "model": "kimi-k3",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    key_id = str(getattr(settings, "MLAI_CODING_TICKET_KEY_ID", "coding-2026-08"))
    token = jwt.encode(
        claims,
        _private_signing_key(),
        algorithm="EdDSA",
        headers={"kid": key_id, "typ": "JWT"},
    )
    return IssuedTicket(token=token, expires_at=expires_at)


def user_has_pilot_access(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_ids = {
        str(item).strip().lower()
        for item in getattr(settings, "MLAI_CODING_PILOT_USER_IDS", [])
        if str(item).strip()
    }
    emails = {
        str(item).strip().lower()
        for item in getattr(settings, "MLAI_CODING_PILOT_EMAILS", [])
        if str(item).strip()
    }
    return (
        str(user.community_chat_profile_id).lower() in user_ids
        or str(user.id).lower() in user_ids
        or str(user.email).lower() in emails
    )


def _turn_outstanding_microroo(turn: CodingTurn, *, exclude_call_id=None) -> int:
    calls = turn.model_calls.filter(
        status__in=(CodingModelCall.Status.RESERVED, CodingModelCall.Status.AMBIGUOUS)
    )
    if exclude_call_id is not None:
        calls = calls.exclude(id=exclude_call_id)
    return sum(calls.values_list("reserved_microroo", flat=True))


def turn_remaining_microroo(turn: CodingTurn) -> int:
    return max(
        turn.reserved_microroo
        - turn.settled_microroo
        - _turn_outstanding_microroo(turn),
        0,
    )


def _complete_reconciling_turn_if_ready(turn: CodingTurn, *, now) -> bool:
    if turn.status != CodingTurn.Status.RECONCILING:
        return False
    if turn.model_calls.filter(
        status__in=(CodingModelCall.Status.RESERVED, CodingModelCall.Status.AMBIGUOUS)
    ).exists():
        return False
    turn.status = {
        "completed": CodingTurn.Status.COMPLETED,
        "cancelled": CodingTurn.Status.CANCELLED,
        "failed": CodingTurn.Status.FAILED,
    }.get(turn.finalize_outcome, CodingTurn.Status.FAILED)
    turn.released_microroo = max(turn.reserved_microroo - turn.settled_microroo, 0)
    turn.completed_at = now
    turn.save(
        update_fields=(
            "status",
            "released_microroo",
            "completed_at",
            "updated_at",
        )
    )
    return True


def _ambiguity_deadline(call: CodingModelCall):
    """Bound a provider-uncertain reservation to 24 hours from admission."""
    return call.reserved_at + timedelta(hours=24)


def _mark_reserved_calls_ambiguous(turn: CodingTurn, *, now) -> int:
    """Finalize admitted calls while preserving turn -> call lock ordering.

    A call that never received dispatch-start authority cannot have reached the
    provider, so it is safe to release immediately. Only calls whose provider
    dispatch may have begun need the bounded ambiguity hold.
    """
    calls = list(
        turn.model_calls.select_for_update()
        .filter(status=CodingModelCall.Status.RESERVED)
        .order_by("id")
    )
    for call in calls:
        if call.dispatch_started_at is None:
            call.status = CodingModelCall.Status.RELEASED
            call.failure_reason = "dispatch_not_started"
            call.reconcile_after = None
            call.settled_at = now
        else:
            call.status = CodingModelCall.Status.AMBIGUOUS
            call.failure_reason = "settlement_unconfirmed"
            call.reconcile_after = _ambiguity_deadline(call)
            call.settled_at = None
        call.save(
            update_fields=(
                "status",
                "failure_reason",
                "reconcile_after",
                "settled_at",
                "updated_at",
            )
        )
    return len(calls)


def _expire_active_turn(turn: CodingTurn, *, now) -> bool:
    """Move a stale active turn into reconciliation or release it safely."""
    if turn.status != CodingTurn.Status.ACTIVE or turn.expires_at > now:
        return False
    turn.finalize_outcome = "failed"
    _mark_reserved_calls_ambiguous(turn, now=now)
    if turn.model_calls.filter(status=CodingModelCall.Status.AMBIGUOUS).exists():
        turn.status = CodingTurn.Status.RECONCILING
        turn.save(update_fields=("status", "finalize_outcome", "updated_at"))
    else:
        turn.status = CodingTurn.Status.RECONCILING
        turn.save(update_fields=("status", "finalize_outcome", "updated_at"))
        _complete_reconciling_turn_if_ready(turn, now=now)
    return True


@transaction.atomic
def create_turn(*, user, account_session, idempotency_key, local_session_id, model) -> tuple[CodingTurn, bool]:
    if not user_has_pilot_access(user):
        raise CodingError(
            "pilot_access_required",
            "MLAI Coding is currently available to pilot members only.",
            http_status=403,
        )
    if model != "kimi-k3":
        raise CodingError("unsupported_model", "Only kimi-k3 is available during the pilot.")
    idem = _uuid(idempotency_key, "idempotency_key")
    local_id = _uuid(local_session_id, "local_session_id")
    if account_session is None or account_session.user_id != user.id:
        raise CodingError(
            "community_chat_session_required",
            "A device-bound Community Chat session is required.",
            http_status=401,
        )

    now = timezone.now()
    for stale_turn in CodingTurn.objects.select_for_update().filter(
        user=user,
        status=CodingTurn.Status.ACTIVE,
        expires_at__lte=now,
    ):
        _expire_active_turn(stale_turn, now=now)

    existing = CodingTurn.objects.select_for_update().filter(
        user=user,
        idempotency_key=idem,
    ).first()
    if existing:
        if (
            existing.local_session_id != local_id
            or existing.model != model
            or existing.device_id != account_session.installation_id
        ):
            raise CodingError(
                "idempotency_conflict",
                "That idempotency key was already used for another turn.",
                http_status=409,
            )
        if existing.status != CodingTurn.Status.ACTIVE:
            raise CodingError(
                "turn_not_active",
                "That idempotent turn is no longer active.",
                http_status=409,
            )
        return existing, False

    active = CodingTurn.objects.select_for_update().filter(
        user=user,
        status__in=(CodingTurn.Status.ACTIVE, CodingTurn.Status.RECONCILING),
    ).first()
    if active:
        raise CodingError(
            "active_turn_exists",
            "Finish or cancel the active coding turn before starting another.",
            http_status=409,
            extra={"active_turn_id": str(active.id)},
        )

    account = PointsAccount.objects.select_for_update().filter(user=user).first()
    if account is None:
        account = PointsAccount.objects.create(user=user)
        account = PointsAccount.objects.select_for_update().get(user=user)
    PointsService._ensure_microroo_account(account)
    available = account.balance_microroo
    if available <= 0:
        raise CodingError(
            "insufficient_points",
            "Add Roo Points before starting a coding turn.",
            http_status=402,
            extra={
                "balance_microroo": "0",
                "balance_roo": "0.000000",
            },
        )
    try:
        # Keep an insert-race IntegrityError inside a savepoint so the outer
        # transaction remains usable by callers and error middleware.
        with transaction.atomic():
            turn = CodingTurn.objects.create(
                user=user,
                account_session=account_session,
                device_id=account_session.installation_id,
                local_session_id=local_id,
                idempotency_key=idem,
                model=model,
                pricing_version=current_pricing(),
                reserved_microroo=available,
            )
    except IntegrityError as exc:
        raise CodingError(
            "active_turn_exists",
            "Finish or cancel the active coding turn before starting another.",
            http_status=409,
        ) from exc
    return turn, True


@transaction.atomic
def admit_call(
    *,
    turn_id,
    call_id,
    subject,
    device_id,
    estimated_input_tokens,
    requested_output_tokens,
    dispatch_owner,
) -> tuple[CodingModelCall, bool, int]:
    turn_uuid = _uuid(turn_id, "turn_id")
    call_uuid = _uuid(call_id, "call_id")
    subject_uuid = _uuid(subject, "subject")
    device_uuid = _uuid(device_id, "device_id")
    estimated_input = _nonnegative_int(estimated_input_tokens, "estimated_input_tokens")
    requested_output = _positive_int(requested_output_tokens, "requested_output_tokens")
    owner_hash = _dispatch_owner_hash(dispatch_owner)
    now = timezone.now()
    try:
        turn = CodingTurn.objects.select_for_update().select_related("pricing_version", "user").get(id=turn_uuid)
    except CodingTurn.DoesNotExist as exc:
        raise CodingError("turn_not_found", "Coding turn was not found.", http_status=404) from exc
    if turn.user.community_chat_profile_id != subject_uuid or turn.device_id != device_uuid:
        raise CodingError(
            "ticket_scope_mismatch",
            "The inference ticket is not valid for this coding turn and device.",
            http_status=403,
        )
    if turn.status == CodingTurn.Status.ACTIVE and turn.expires_at <= now:
        _expire_active_turn(turn, now=now)
    if turn.status != CodingTurn.Status.ACTIVE:
        raise CodingError("turn_not_active", "Coding turn is not active.", http_status=409)

    existing = CodingModelCall.objects.select_for_update().filter(
        turn=turn,
        call_id=call_uuid,
    ).first()
    if existing:
        if (
            existing.estimated_input_tokens != estimated_input
            or existing.requested_output_tokens != requested_output
        ):
            raise CodingError(
                "idempotency_conflict",
                "That call ID was already used with different token limits.",
                http_status=409,
            )
        if existing.status != CodingModelCall.Status.RESERVED:
            raise CodingError(
                "call_already_finalized",
                "That call has already been finalized.",
                http_status=409,
            )
        if existing.dispatch_started_at is not None:
            raise CodingError(
                "call_already_dispatched",
                "That call ID has already started provider dispatch.",
                http_status=409,
            )
        same_owner = hmac.compare_digest(existing.dispatch_owner_hash, owner_hash)
        if not same_owner and existing.dispatch_lease_expires_at > now:
            raise CodingError(
                "dispatch_lease_owned",
                "That call is temporarily owned by another dispatch attempt.",
                http_status=409,
                extra={"dispatch_lease_expires_at": existing.dispatch_lease_expires_at},
            )
        # Replaying with the same owner recovers a lost admission response.
        # A different owner may recover only after the old, unstarted lease
        # expires; the stale owner then loses all mutation authority.
        existing.dispatch_owner_hash = owner_hash
        existing.dispatch_lease_expires_at = _dispatch_lease_deadline(now)
        existing.save(
            update_fields=(
                "dispatch_owner_hash",
                "dispatch_lease_expires_at",
                "updated_at",
            )
        )
        return existing, False, turn_remaining_microroo(turn)

    remaining = turn_remaining_microroo(turn)
    max_output, reservation = conservative_call_reservation(
        turn.pricing_version,
        estimated_input_tokens=estimated_input,
        requested_output_tokens=requested_output,
        available_microroo=remaining,
    )
    call = CodingModelCall.objects.create(
        turn=turn,
        call_id=call_uuid,
        estimated_input_tokens=estimated_input,
        requested_output_tokens=requested_output,
        max_output_tokens=max_output,
        reserved_microroo=reservation,
        pricing_version_snapshot=turn.pricing_version.version,
        input_usd_per_million=turn.pricing_version.input_usd_per_million,
        cached_input_usd_per_million=turn.pricing_version.cached_input_usd_per_million,
        output_usd_per_million=turn.pricing_version.output_usd_per_million,
        usd_aud_rate=turn.pricing_version.usd_aud_rate,
        margin_multiplier=turn.pricing_version.margin_multiplier,
        aud_per_roo=turn.pricing_version.aud_per_roo,
        dispatch_owner_hash=owner_hash,
        dispatch_lease_expires_at=_dispatch_lease_deadline(now),
    )
    return call, True, turn_remaining_microroo(turn)


@transaction.atomic
def start_call_dispatch(
    *,
    turn_id,
    call_id,
    reservation_id,
    dispatch_owner,
) -> tuple[CodingModelCall, bool, int]:
    """Grant exactly one provider-dispatch start to the current lease owner."""
    turn_uuid = _uuid(turn_id, "turn_id")
    call_uuid = _uuid(call_id, "call_id")
    reservation_uuid = _uuid(reservation_id, "reservation_id")
    owner_hash = _dispatch_owner_hash(dispatch_owner)
    now = timezone.now()
    try:
        # All mutating paths preserve turn -> call lock ordering.
        turn = CodingTurn.objects.select_for_update().get(id=turn_uuid)
        if turn.status == CodingTurn.Status.ACTIVE and turn.expires_at <= now:
            _expire_active_turn(turn, now=now)
        call = CodingModelCall.objects.select_for_update().get(
            id=reservation_uuid,
            turn=turn,
            call_id=call_uuid,
        )
    except (CodingTurn.DoesNotExist, CodingModelCall.DoesNotExist) as exc:
        raise CodingError(
            "reservation_not_found",
            "Call reservation was not found.",
            http_status=404,
        ) from exc

    _require_dispatch_owner(call, owner_hash)
    if turn.status != CodingTurn.Status.ACTIVE:
        raise CodingError("turn_not_active", "Coding turn is not active.", http_status=409)
    if call.status != CodingModelCall.Status.RESERVED:
        raise CodingError(
            "call_already_finalized",
            "That call has already been finalized.",
            http_status=409,
        )
    if call.dispatch_started_at is not None:
        # An acknowledgement may have been lost. Returning false is the safe
        # exactly-once result: the caller must not dispatch the provider again.
        return call, False, turn_remaining_microroo(turn)
    if call.dispatch_lease_expires_at <= now:
        raise CodingError(
            "dispatch_lease_expired",
            "The dispatch lease expired; renew admission before dispatching.",
            http_status=409,
        )
    call.dispatch_started_at = now
    call.save(update_fields=("dispatch_started_at", "updated_at"))
    return call, True, turn_remaining_microroo(turn)


@transaction.atomic
def settle_call(
    *,
    turn_id,
    call_id,
    reservation_id,
    input_tokens,
    cached_input_tokens,
    output_tokens,
    dispatch_owner,
    provider_request_id="",
    trace_id="",
) -> tuple[CodingModelCall, bool, int, int]:
    turn_uuid = _uuid(turn_id, "turn_id")
    call_uuid = _uuid(call_id, "call_id")
    reservation_uuid = _uuid(reservation_id, "reservation_id")
    prompt = _nonnegative_int(input_tokens, "input_tokens")
    cached = _nonnegative_int(cached_input_tokens, "cached_input_tokens")
    output = _nonnegative_int(output_tokens, "output_tokens")
    owner_hash = _dispatch_owner_hash(dispatch_owner)
    provider = str(provider_request_id or "").strip()
    trace = str(trace_id or "").strip()
    if len(provider) > 255 or len(trace) > 255:
        raise CodingError("invalid_provider_reference", "Provider references must be 255 characters or fewer.")

    try:
        turn = CodingTurn.objects.select_for_update().select_related("pricing_version", "user").get(id=turn_uuid)
        call = CodingModelCall.objects.select_for_update().get(
            id=reservation_uuid,
            turn=turn,
            call_id=call_uuid,
        )
    except (CodingTurn.DoesNotExist, CodingModelCall.DoesNotExist) as exc:
        raise CodingError("reservation_not_found", "Call reservation was not found.", http_status=404) from exc

    _require_dispatch_owner(call, owner_hash)
    if call.dispatch_started_at is None:
        raise CodingError(
            "dispatch_not_started",
            "Provider dispatch was never started for this reservation.",
            http_status=409,
        )

    if cached > prompt:
        raise CodingError("invalid_cached_input_tokens", "cached_input_tokens cannot exceed input_tokens.")

    if call.status == CodingModelCall.Status.SETTLED:
        if (
            call.input_tokens != prompt
            or call.cached_input_tokens != cached
            or call.output_tokens != output
            or call.provider_request_id != provider
            or call.trace_id != trace
        ):
            raise CodingError(
                "idempotency_conflict",
                "That call was already settled with different usage.",
                http_status=409,
            )
        balance = PointsService.get_balance(turn.user)["balance_microroo"]
        return call, False, balance, turn_remaining_microroo(turn)
    if call.status == CodingModelCall.Status.RELEASED:
        _record_released_settlement_audit(
            call,
            prompt=prompt,
            cached=cached,
            output=output,
            provider=provider,
            trace=trace,
        )
        # The 24-hour release is authoritative. A delayed durable outbox
        # report is acknowledged for audit only and can never charge, settle,
        # or reopen this call.
        balance = PointsService.get_balance(turn.user)["balance_microroo"]
        return call, False, balance, turn_remaining_microroo(turn)
    same_rejected_report = (
        call.status == CodingModelCall.Status.AMBIGUOUS
        and call.failure_reason == USAGE_ENVELOPE_FAILURE_REASON
        and call.input_tokens == prompt
        and call.cached_input_tokens == cached
        and call.output_tokens == output
        and call.provider_request_id == provider
        and call.trace_id == trace
    )
    if same_rejected_report:
        # Reconciliation may have released the hold before a delayed gateway
        # outbox replay arrives. The identical rejected report is still a
        # terminal acknowledgement and must not become an immortal outbox job.
        balance = PointsService.get_balance(turn.user)["balance_microroo"]
        return call, False, balance, turn_remaining_microroo(turn)
    # The gateway admits a padded estimate of the normalized request, including
    # chat/tool framing and image overhead, and clamps output against both Roo
    # credit and the K3 context ceiling before dispatch. Usage outside either
    # admitted bound is not a trustworthy bill: retain the reservation for
    # reconciliation and persist bounded provider references, but never create
    # a ledger entry.
    outside_admitted_envelope = (
        prompt > call.estimated_input_tokens
        or output > call.max_output_tokens
    )
    if outside_admitted_envelope:
        if (
            call.status == CodingModelCall.Status.AMBIGUOUS
            and call.failure_reason == USAGE_ENVELOPE_FAILURE_REASON
        ):
            raise CodingError(
                "idempotency_conflict",
                "That rejected usage report was replayed with different details.",
                http_status=409,
            )

        call.status = CodingModelCall.Status.AMBIGUOUS
        call.failure_reason = USAGE_ENVELOPE_FAILURE_REASON
        call.input_tokens = prompt
        call.cached_input_tokens = cached
        call.output_tokens = output
        call.provider_request_id = provider
        call.trace_id = trace
        call.settled_at = None
        call.reconcile_after = _ambiguity_deadline(call)
        call.save(
            update_fields=(
                "status",
                "failure_reason",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "provider_request_id",
                "trace_id",
                "settled_at",
                "reconcile_after",
                "updated_at",
            )
        )
        balance = PointsService.get_balance(turn.user)["balance_microroo"]
        return call, True, balance, turn_remaining_microroo(turn)

    calculated = calculate_charge_microroo(
        call,
        input_tokens=prompt,
        cached_input_tokens=cached,
        output_tokens=output,
    )
    account = PointsAccount.objects.select_for_update().get(user=turn.user)
    PointsService._ensure_microroo_account(account)
    capacity = max(
        turn.reserved_microroo
        - turn.settled_microroo
        - _turn_outstanding_microroo(turn, exclude_call_id=call.id),
        0,
    )
    charge = min(calculated, capacity, account.balance_microroo)
    if charge <= 0 and calculated > 0:
        raise CodingError(
            "reservation_exhausted",
            "The turn reservation is exhausted.",
            http_status=409,
        )
    ledger = None
    if charge:
        try:
            ledger, _ = PointsService.spend_microroo(
                user=turn.user,
                delta_microroo=charge,
                source="TOOLS",
                description="Kimi K3 coding model call",
                created_by_slack_id="MLAI_CODING_GATEWAY",
                idempotency_key=f"kimi_call:{turn.id}:{call.call_id}",
                reference_type="KIMI_MODEL_CALL",
                reference_id=str(call.id),
                allow_reserved_turn_id=turn.id,
            )
        except InsufficientBalanceError as exc:
            raise CodingError(
                "reservation_exhausted",
                "The turn reservation is exhausted.",
                http_status=409,
            ) from exc
        except IdempotencyConflictError as exc:
            raise CodingError(
                "idempotency_conflict",
                "That call's ledger key was already used for another operation.",
                http_status=409,
            ) from exc

    now = timezone.now()
    call.status = CodingModelCall.Status.SETTLED
    call.charged_microroo = charge
    call.calculated_microroo = calculated
    call.input_tokens = prompt
    call.cached_input_tokens = cached
    call.output_tokens = output
    call.provider_request_id = provider
    call.trace_id = trace
    call.ledger_entry = ledger
    call.settled_at = now
    call.reconcile_after = None
    call.failure_reason = ""
    call.save()
    turn.settled_microroo += charge
    turn.save(update_fields=("settled_microroo", "updated_at"))
    _complete_reconciling_turn_if_ready(turn, now=now)
    account.refresh_from_db()
    return call, True, account.balance_microroo, turn_remaining_microroo(turn)


@transaction.atomic
def fail_call(
    *,
    turn_id,
    call_id,
    reservation_id,
    reason,
    ambiguous,
    dispatch_owner,
    provider_request_id="",
    trace_id="",
) -> tuple[CodingModelCall, bool, int]:
    turn_uuid = _uuid(turn_id, "turn_id")
    call_uuid = _uuid(call_id, "call_id")
    reservation_uuid = _uuid(reservation_id, "reservation_id")
    failure_reason = str(reason or "").strip()
    provider = str(provider_request_id or "").strip()
    trace = str(trace_id or "").strip()
    owner_hash = _dispatch_owner_hash(dispatch_owner)
    if failure_reason not in CALL_FAILURE_REASONS:
        raise CodingError(
            "invalid_reason",
            "reason must be one of: " + ", ".join(sorted(CALL_FAILURE_REASONS)) + ".",
        )
    if not isinstance(ambiguous, bool):
        raise CodingError("invalid_ambiguous", "ambiguous must be a boolean.")
    if failure_reason == "settlement_unconfirmed" and not ambiguous:
        raise CodingError(
            "invalid_ambiguous",
            "settlement_unconfirmed must be marked ambiguous.",
        )
    if len(provider) > 255 or len(trace) > 255:
        raise CodingError("invalid_provider_reference", "Provider references must be 255 characters or fewer.")
    try:
        turn = CodingTurn.objects.select_for_update().get(id=turn_uuid)
        call = CodingModelCall.objects.select_for_update().get(
            id=reservation_uuid,
            turn=turn,
            call_id=call_uuid,
        )
    except (CodingTurn.DoesNotExist, CodingModelCall.DoesNotExist) as exc:
        raise CodingError("reservation_not_found", "Call reservation was not found.", http_status=404) from exc
    _require_dispatch_owner(call, owner_hash)
    # Before dispatch-start, even a timeout is definitively pre-provider and
    # must release rather than create a 24-hour ambiguity hold.
    if call.dispatch_started_at is None:
        ambiguous = False
    if call.status == CodingModelCall.Status.RELEASED:
        _record_released_failure_audit(
            call,
            failure_reason=failure_reason,
            ambiguous=ambiguous,
            provider=provider,
            trace=trace,
        )
        # This return reports no lifecycle change even when bounded audit
        # metadata was added. RELEASED remains terminal and unbilled.
        return call, False, turn_remaining_microroo(turn)
    target = CodingModelCall.Status.AMBIGUOUS if ambiguous else CodingModelCall.Status.RELEASED
    if call.status == CodingModelCall.Status.AMBIGUOUS and target == CodingModelCall.Status.AMBIGUOUS:
        if (
            call.failure_reason != failure_reason
            or call.provider_request_id != provider
            or call.trace_id != trace
        ):
            raise CodingError(
                "idempotency_conflict",
                "That failure was already recorded with different details.",
                http_status=409,
            )
        return call, False, turn_remaining_microroo(turn)
    if call.status == CodingModelCall.Status.SETTLED:
        if call.status != target:
            raise CodingError("call_already_finalized", "That call is already finalized.", http_status=409)
        if (
            call.failure_reason != failure_reason
            or call.provider_request_id != provider
            or call.trace_id != trace
        ):
            raise CodingError(
                "idempotency_conflict",
                "That failure was already recorded with different details.",
                http_status=409,
            )
        return call, False, turn_remaining_microroo(turn)
    call.status = target
    call.failure_reason = failure_reason
    call.provider_request_id = provider
    call.trace_id = trace
    call.settled_at = timezone.now() if not ambiguous else None
    call.reconcile_after = (
        _ambiguity_deadline(call) if ambiguous else None
    )
    call.save()
    _complete_reconciling_turn_if_ready(turn, now=timezone.now())
    return call, True, turn_remaining_microroo(turn)


@transaction.atomic
def finalize_turn(*, turn: CodingTurn, outcome: str) -> tuple[CodingTurn, bool]:
    if outcome not in {"completed", "cancelled", "failed"}:
        raise CodingError("invalid_outcome", "outcome must be completed, cancelled, or failed.")
    turn = CodingTurn.objects.select_for_update().get(id=turn.id, user=turn.user)
    if turn.finalize_outcome and turn.finalize_outcome != outcome:
        raise CodingError(
            "idempotency_conflict",
            "That turn was already finalized with a different outcome.",
            http_status=409,
        )
    if turn.status not in (CodingTurn.Status.ACTIVE, CodingTurn.Status.RECONCILING):
        return turn, False
    now = timezone.now()
    turn.finalize_outcome = outcome
    _mark_reserved_calls_ambiguous(turn, now=now)
    if turn.model_calls.filter(status=CodingModelCall.Status.AMBIGUOUS).exists():
        turn.status = CodingTurn.Status.RECONCILING
        turn.save(update_fields=("status", "finalize_outcome", "updated_at"))
        return turn, True
    turn.status = {
        "completed": CodingTurn.Status.COMPLETED,
        "cancelled": CodingTurn.Status.CANCELLED,
        "failed": CodingTurn.Status.FAILED,
    }[outcome]
    turn.released_microroo = max(turn.reserved_microroo - turn.settled_microroo, 0)
    turn.completed_at = now
    turn.save(update_fields=("status", "released_microroo", "finalize_outcome", "completed_at", "updated_at"))
    return turn, True


def _scope_turns(queryset, *, user=None, turn_id=None):
    if user is not None:
        queryset = queryset.filter(user=user)
    if turn_id is not None:
        queryset = queryset.filter(id=turn_id)
    return queryset


def _scope_calls(queryset, *, user=None, turn_id=None):
    if user is not None:
        queryset = queryset.filter(turn__user=user)
    if turn_id is not None:
        queryset = queryset.filter(turn_id=turn_id)
    return queryset


@transaction.atomic
def reconcile_coding_reservations(*, now=None, user=None, turn_id=None) -> dict:
    """Reconcile reservations globally or within an explicit account scope."""
    now = now or timezone.now()
    expired_turns = 0
    expired_queryset = CodingTurn.objects.select_for_update().filter(
        status=CodingTurn.Status.ACTIVE, expires_at__lte=now
    )
    for turn in _scope_turns(
        expired_queryset,
        user=user,
        turn_id=turn_id,
    ):
        if _expire_active_turn(turn, now=now):
            expired_turns += 1

    expired_unstarted = _scope_calls(
        CodingModelCall.objects.filter(
            status=CodingModelCall.Status.RESERVED,
            dispatch_started_at__isnull=True,
            dispatch_lease_expires_at__lte=now,
        ),
        user=user,
        turn_id=turn_id,
    )
    expired_ambiguous = _scope_calls(
        CodingModelCall.objects.filter(
            status=CodingModelCall.Status.AMBIGUOUS,
            reconcile_after__isnull=False,
            reconcile_after__lte=now,
        ),
        user=user,
        turn_id=turn_id,
    )
    eligible_turn_ids = sorted(
        set(expired_unstarted.values_list("turn_id", flat=True))
        | set(expired_ambiguous.values_list("turn_id", flat=True)),
        key=str,
    )
    # Every mutating path locks a turn before its calls. Keeping that ordering
    # here prevents settlement/reconciliation deadlocks under PostgreSQL.
    locked_turns = list(
        CodingTurn.objects.select_for_update()
        .filter(id__in=eligible_turn_ids)
        .order_by("id")
    )
    unstarted_calls = list(
        CodingModelCall.objects.select_for_update()
        .filter(
            turn_id__in=[turn.id for turn in locked_turns],
            status=CodingModelCall.Status.RESERVED,
            dispatch_started_at__isnull=True,
            dispatch_lease_expires_at__lte=now,
        )
        .order_by("turn_id", "id")
    )
    for call in unstarted_calls:
        call.status = CodingModelCall.Status.RELEASED
        call.failure_reason = "dispatch_lease_expired"
        call.settled_at = now
        call.reconcile_after = None
        call.save(
            update_fields=(
                "status",
                "failure_reason",
                "settled_at",
                "reconcile_after",
                "updated_at",
            )
        )

    ambiguous_calls = list(
        CodingModelCall.objects.select_for_update()
        .filter(
            turn_id__in=[turn.id for turn in locked_turns],
            status=CodingModelCall.Status.AMBIGUOUS,
            reconcile_after__isnull=False,
            reconcile_after__lte=now,
        )
        .order_by("turn_id", "id")
    )
    for call in ambiguous_calls:
        call.status = CodingModelCall.Status.RELEASED
        call.failure_reason = f"{call.failure_reason}; reconciliation_timeout".strip("; ")[:500]
        call.settled_at = now
        call.reconcile_after = None
        call.save(update_fields=("status", "failure_reason", "settled_at", "reconcile_after", "updated_at"))
    for turn in locked_turns:
        if turn.status != CodingTurn.Status.RECONCILING:
            continue
        if not turn.model_calls.filter(status=CodingModelCall.Status.AMBIGUOUS).exists():
            _complete_reconciling_turn_if_ready(turn, now=now)
    released_unstarted = len(unstarted_calls)
    released_ambiguous = len(ambiguous_calls)
    return {
        "expired_turns": expired_turns,
        "released_calls": released_unstarted + released_ambiguous,
        "released_unstarted_calls": released_unstarted,
        "released_ambiguous_calls": released_ambiguous,
    }


def release_stale_ambiguous_calls(*, now=None, user=None, turn_id=None) -> int:
    """Backward-compatible narrow result for callers interested in call releases."""
    return reconcile_coding_reservations(
        now=now,
        user=user,
        turn_id=turn_id,
    )["released_ambiguous_calls"]


def pricing_payload(pricing: CodingPricingVersion) -> dict:
    return {
        "version": pricing.version,
        "input_usd_per_million": str(pricing.input_usd_per_million),
        "cached_input_usd_per_million": str(pricing.cached_input_usd_per_million),
        "output_usd_per_million": str(pricing.output_usd_per_million),
        "usd_aud_rate": str(pricing.usd_aud_rate),
        "margin_multiplier": str(pricing.margin_multiplier),
        "aud_per_roo": str(pricing.aud_per_roo),
    }
