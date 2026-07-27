from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_UP

from django.conf import settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from .models import (
    MemoryCostReservation,
    MemoryCostReservationStatus,
    MemoryDailyCostLedger,
    MemoryWorkTaskType,
)
from .scheduling import reconciliation_window


METERED_TASK_TYPES = frozenset(
    {
        MemoryWorkTaskType.EMBED,
        MemoryWorkTaskType.EXTRACT,
        MemoryWorkTaskType.CONSOLIDATE,
    }
)
MONEY_QUANTUM = Decimal("0.000001")


class MemoryCostConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class CostReservationDecision:
    allowed: bool
    metered: bool
    estimated_tokens: int = 0
    estimated_cost_aud: Decimal = Decimal("0")
    reason: str = ""
    retry_at: object = None


def _decimal_setting(name: str, default="0") -> Decimal:
    try:
        value = Decimal(str(getattr(settings, name, default) or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MemoryCostConfigurationError(f"{name} must be a decimal number.") from exc
    if value < 0:
        raise MemoryCostConfigurationError(f"{name} cannot be negative.")
    return value


def _tokens_for_work(work_item) -> tuple[int, Decimal]:
    if work_item.task_type == MemoryWorkTaskType.EMBED:
        chunk = work_item.source_version.chunks.filter(
            pk=(work_item.payload or {}).get("chunk_id")
        ).only("token_count").first()
        tokens = max(int(getattr(chunk, "token_count", 0) or 0), 1)
        rate = _decimal_setting(
            "ORG_MEMORY_EMBEDDING_COST_AUD_PER_MILLION_TOKENS"
        )
        return tokens, rate

    if work_item.task_type == MemoryWorkTaskType.EXTRACT:
        tokens = int(
            work_item.source_version.chunks.filter(active_for_retrieval=True).aggregate(
                total=Sum("token_count")
            )["total"]
            or 0
        )
        maximum = max(
            int(getattr(settings, "ORG_MEMORY_EXTRACTION_MAX_INPUT_CHARS", 60000)) // 4,
            1,
        )
        input_tokens = max(min(tokens, maximum), 1)
        output_tokens = max(
            int(getattr(settings, "ORG_MEMORY_EXTRACTION_MAX_OUTPUT_TOKENS", 6000)),
            0,
        )
    else:
        input_tokens = max(
            int(
                getattr(
                    settings,
                    "ORG_MEMORY_CONSOLIDATION_ESTIMATED_INPUT_TOKENS",
                    4000,
                )
            ),
            1,
        )
        output_tokens = max(
            int(getattr(settings, "ORG_MEMORY_CONSOLIDATION_MAX_OUTPUT_TOKENS", 1200)),
            0,
        )
    input_rate = _decimal_setting(
        "ORG_MEMORY_MODEL_INPUT_COST_AUD_PER_MILLION_TOKENS"
    )
    output_rate = _decimal_setting(
        "ORG_MEMORY_MODEL_OUTPUT_COST_AUD_PER_MILLION_TOKENS"
    )
    total_tokens = input_tokens + output_tokens
    blended_cost = (
        Decimal(input_tokens) * input_rate + Decimal(output_tokens) * output_rate
    ) / Decimal(1_000_000)
    effective_rate = (
        blended_cost * Decimal(1_000_000) / Decimal(total_tokens)
        if total_tokens
        else Decimal("0")
    )
    return total_tokens, effective_rate


def estimate_memory_work_cost(work_item) -> tuple[int, Decimal]:
    if work_item.task_type not in METERED_TASK_TYPES:
        return 0, Decimal("0")
    tokens, rate = _tokens_for_work(work_item)
    cost = (Decimal(tokens) * rate / Decimal(1_000_000)).quantize(
        MONEY_QUANTUM,
        rounding=ROUND_UP,
    )
    return tokens, cost


@transaction.atomic
def reserve_memory_work_cost(work_item, *, now=None) -> CostReservationDecision:
    if work_item.task_type not in METERED_TASK_TYPES:
        return CostReservationDecision(allowed=True, metered=False)
    now = now or timezone.now()
    window = reconciliation_window(now)
    ceiling = _decimal_setting("ORG_MEMORY_DAILY_MODEL_COST_CEILING_AUD")
    tokens, estimate = estimate_memory_work_cost(work_item)
    if ceiling <= 0:
        return CostReservationDecision(
            allowed=True,
            metered=True,
            estimated_tokens=tokens,
            estimated_cost_aud=estimate,
            reason="ceiling_disabled",
        )
    if estimate <= 0:
        return CostReservationDecision(
            allowed=False,
            metered=True,
            estimated_tokens=tokens,
            estimated_cost_aud=estimate,
            reason="pricing_not_configured",
            retry_at=window["next_window_at"],
        )

    existing = MemoryCostReservation.objects.select_for_update().filter(
        work_item=work_item
    ).select_related("ledger").first()
    if existing and existing.status == MemoryCostReservationStatus.RESERVED:
        if existing.ledger.budget_date == window["report_date"]:
            return CostReservationDecision(
                allowed=True,
                metered=True,
                estimated_tokens=existing.estimated_tokens,
                estimated_cost_aud=existing.estimated_cost_aud,
                reason="already_reserved",
            )
        release_memory_work_cost(work_item, now=now)
        existing.refresh_from_db()
    if existing and existing.status == MemoryCostReservationStatus.CONSUMED:
        return CostReservationDecision(
            allowed=True,
            metered=True,
            estimated_tokens=existing.estimated_tokens,
            estimated_cost_aud=existing.estimated_cost_aud,
            reason="already_consumed",
        )

    ledger, _created = MemoryDailyCostLedger.objects.get_or_create(
        organization=work_item.organization,
        budget_date=window["report_date"],
        defaults={"ceiling_aud": ceiling},
    )
    ledger = MemoryDailyCostLedger.objects.select_for_update().get(pk=ledger.pk)
    ledger.ceiling_aud = ceiling
    if ledger.reserved_aud + ledger.consumed_aud + estimate > ceiling:
        ledger.save(update_fields=("ceiling_aud", "updated_at"))
        return CostReservationDecision(
            allowed=False,
            metered=True,
            estimated_tokens=tokens,
            estimated_cost_aud=estimate,
            reason="daily_cost_ceiling_reached",
            retry_at=window["next_window_at"],
        )
    reservation = existing or MemoryCostReservation(work_item=work_item)
    reservation.ledger = ledger
    reservation.organization = work_item.organization
    reservation.task_type = work_item.task_type
    reservation.estimated_tokens = tokens
    reservation.estimated_cost_aud = estimate
    reservation.actual_cost_aud = None
    reservation.status = MemoryCostReservationStatus.RESERVED
    reservation.reserved_at = now
    reservation.completed_at = None
    reservation.full_clean()
    reservation.save()
    ledger.reserved_aud = F("reserved_aud") + estimate
    ledger.save(update_fields=("ceiling_aud", "reserved_aud", "updated_at"))
    return CostReservationDecision(
        allowed=True,
        metered=True,
        estimated_tokens=tokens,
        estimated_cost_aud=estimate,
        reason="reserved",
    )


@transaction.atomic
def consume_memory_work_cost(work_item, *, now=None) -> None:
    now = now or timezone.now()
    reservation = MemoryCostReservation.objects.select_for_update().filter(
        work_item=work_item,
        status=MemoryCostReservationStatus.RESERVED,
    ).select_related("ledger").first()
    if reservation is None:
        return
    ledger = MemoryDailyCostLedger.objects.select_for_update().get(
        pk=reservation.ledger_id
    )
    ledger.reserved_aud = max(
        Decimal(ledger.reserved_aud) - reservation.estimated_cost_aud,
        Decimal("0"),
    )
    ledger.consumed_aud = Decimal(ledger.consumed_aud) + reservation.estimated_cost_aud
    ledger.save(update_fields=("reserved_aud", "consumed_aud", "updated_at"))
    reservation.status = MemoryCostReservationStatus.CONSUMED
    reservation.actual_cost_aud = reservation.estimated_cost_aud
    reservation.completed_at = now
    reservation.save(
        update_fields=("status", "actual_cost_aud", "completed_at", "updated_at")
    )


@transaction.atomic
def release_memory_work_cost(work_item, *, now=None) -> None:
    now = now or timezone.now()
    reservation = MemoryCostReservation.objects.select_for_update().filter(
        work_item=work_item,
        status=MemoryCostReservationStatus.RESERVED,
    ).select_related("ledger").first()
    if reservation is None:
        return
    ledger = MemoryDailyCostLedger.objects.select_for_update().get(
        pk=reservation.ledger_id
    )
    ledger.reserved_aud = max(
        Decimal(ledger.reserved_aud) - reservation.estimated_cost_aud,
        Decimal("0"),
    )
    ledger.save(update_fields=("reserved_aud", "updated_at"))
    reservation.status = MemoryCostReservationStatus.RELEASED
    reservation.completed_at = now
    reservation.save(update_fields=("status", "completed_at", "updated_at"))


def release_cost_reservations(work_item_ids, *, now=None) -> int:
    released = 0
    for reservation in MemoryCostReservation.objects.filter(
        work_item_id__in=tuple(work_item_ids),
        status=MemoryCostReservationStatus.RESERVED,
    ).select_related("work_item"):
        release_memory_work_cost(reservation.work_item, now=now)
        released += 1
    return released
