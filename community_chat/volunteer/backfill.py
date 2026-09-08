"""Explicitly reviewed historical bonuses, separate from automatic new awards."""

import hashlib
import json

from django.db import transaction
from django.utils import timezone

from core.actor_ids import preferred_actor_id_for_user
from roo.services import PointsService

from .access import VolunteerError, community_id, flag, require_capability
from .models import VolunteerMemberState, VolunteerMilestone, VolunteerSourceReceipt
from .policy import VERSION, levels, microroo, roo
from .services import contribution_total, lock_member


def bonus_liability(user, state, total):
    """Read unpaid historical potential and prospective liability separately."""
    if total is None:
        return dict(historical_potential_roo=None, prospective_pending_roo=None)
    opening = state.historical_microroo or 0 if state else 0
    paid = set(
        VolunteerMilestone.objects.filter(
            community=community_id(),
            user=user,
            ledger__isnull=False,
        ).values_list("level_key", flat=True)
    )
    unpaid = [
        level
        for level in levels()[1:]
        if level["key"] not in paid and microroo(level["threshold_roo"]) <= total
    ]
    historical = [
        level for level in unpaid if microroo(level["threshold_roo"]) <= opening
    ]
    prospective = [
        level for level in unpaid if microroo(level["threshold_roo"]) > opening
    ]
    return dict(
        historical_potential_roo=roo(
            sum(microroo(level["bonus_roo"]) for level in historical)
        ),
        prospective_pending_roo=roo(
            sum(microroo(level["bonus_roo"]) for level in prospective)
        ),
    )


def _reviewed_state(user, state):
    if (
        state is None
        or state.historical_microroo is None
        or not state.reconciled_by_id
        or state.reconciled_at is None
    ):
        raise VolunteerError("history_not_reviewed", 409)
    total = contribution_total(user, state)
    if total is None:
        raise VolunteerError("history_unreconciled", 409)
    snapshot = dict(
        community_id=community_id(),
        member_id=str(user.pk),
        state_id=str(state.pk),
        state_updated_at=state.updated_at.isoformat(),
        opening_roo=roo(state.historical_microroo),
        ledger_cutoff=state.historical_ledger_cutoff,
        reconciled_by=str(state.reconciled_by_id),
        reconciled_at=state.reconciled_at.isoformat(),
        contribution_roo=roo(total),
        policy_version=VERSION,
        levels=levels(),
    )
    token = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    eligible = [
        level
        for level in levels()[1:]
        if microroo(level["threshold_roo"]) <= min(state.historical_microroo, total)
    ]
    return snapshot, token, eligible


def historical_bonus_preview(user, reviewer):
    """Read an auditable proposal without creating state, ledger or audit rows."""
    require_capability(reviewer, "can_correct")
    if reviewer.pk == user.pk:
        raise VolunteerError("self_approval_forbidden", 403)
    state = VolunteerMemberState.objects.filter(
        community=community_id(), user=user
    ).first()
    snapshot, token, eligible = _reviewed_state(user, state)
    paid = set(
        VolunteerMilestone.objects.filter(
            community=community_id(),
            user=user,
            ledger__isnull=False,
        ).values_list("level_key", flat=True)
    )
    return dict(
        outcome="preview",
        reviewed_state=snapshot,
        reviewed_state_token=token,
        levels=[
            dict(level, already_awarded=level["key"] in paid) for level in eligible
        ],
        liability=bonus_liability(user, state, microroo(snapshot["contribution_roo"])),
        issuance_enabled=flag("enabled")
        and flag("awards_enabled")
        and flag("bonuses_enabled"),
    )


@transaction.atomic
def award_historical_bonuses(
    user,
    reviewer,
    *,
    expected_opening_roo,
    expected_ledger_cutoff,
    expected_state_token,
    approved_level_keys,
    reason,
):
    """Apply one specifically reviewed historical bonus approval exactly once.

    Membership state, approved thresholds and policy must still match the dry
    run. Wallet credits use the same lifetime-stable keys as ordinary bonuses;
    neither qualifying contribution nor gross lifetime earned is increased.
    """
    require_capability(reviewer, "can_correct")
    if reviewer.pk == user.pk:
        raise VolunteerError("self_approval_forbidden", 403)
    if not flag("enabled") or not flag("awards_enabled") or not flag("bonuses_enabled"):
        raise VolunteerError("awards_disabled", 503)
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise VolunteerError("reason_required")
    if (
        not isinstance(approved_level_keys, (tuple, list))
        or not approved_level_keys
        or any(not isinstance(key, str) for key in approved_level_keys)
    ):
        raise VolunteerError("approved_levels_required")
    if len(set(approved_level_keys)) != len(approved_level_keys):
        raise VolunteerError("duplicate_level")
    user = lock_member(user)
    state = (
        VolunteerMemberState.objects.select_for_update()
        .filter(community=community_id(), user=user)
        .first()
    )
    snapshot, token, eligible = _reviewed_state(user, state)
    try:
        opening = microroo(expected_opening_roo)
    except ValueError as exc:
        raise VolunteerError("invalid_reward") from exc
    if (
        opening != state.historical_microroo
        or not isinstance(expected_ledger_cutoff, int)
        or isinstance(expected_ledger_cutoff, bool)
        or expected_ledger_cutoff != state.historical_ledger_cutoff
        or expected_state_token != token
    ):
        raise VolunteerError("reviewed_state_changed", 409)
    approved = sorted(approved_level_keys)
    available = {level["key"]: level for level in eligible}
    if not set(approved).issubset(available):
        raise VolunteerError("level_not_eligible", 409)
    approval = dict(
        reviewed_state=snapshot,
        reviewed_state_token=token,
        approved_level_keys=approved,
        reviewer_id=str(reviewer.pk),
        reason=reason.strip(),
    )
    identity = hashlib.sha256(
        json.dumps(dict(token=token, levels=approved), sort_keys=True).encode()
    ).hexdigest()
    receipt, created = VolunteerSourceReceipt.objects.get_or_create(
        community=community_id(),
        source_key=f"historical_bonus:{user.pk}:{identity}",
        defaults=dict(
            origin="committee",
            kind="historical_bonus_backfill",
            actor=reviewer,
            target=user,
            source={"source_id": str(state.pk)},
            metadata={"approval": approval},
            occurred_at=timezone.now(),
        ),
    )
    if not created:
        if receipt.metadata.get("approval") != approval:
            raise VolunteerError("conflict", 409)
        return {**receipt.metadata["result"], "outcome": "already_applied"}
    results = []
    for key in approved:
        level = available[key]
        milestone, _ = VolunteerMilestone.objects.get_or_create(
            community=community_id(),
            user=user,
            level_key=key,
            defaults={"reached_at": state.reconciled_at},
        )
        credited = False
        if milestone.ledger_id is None:
            milestone.ledger, credited = PointsService.credit_volunteer(
                user,
                microroo(level["bonus_roo"]),
                idempotency_key=f"volunteer:bonus:{community_id()}:{user.pk}:{key}",
                reference_id=str(milestone.pk),
                description=f"{level['name']} — reviewed historical wallet-only bonus",
                actor_id=preferred_actor_id_for_user(reviewer),
                level_bonus=True,
            )
            milestone.save(update_fields=("ledger", "updated_at"))
        results.append(
            dict(
                level_key=key,
                milestone_id=str(milestone.pk),
                ledger_id=str(milestone.ledger_id),
                bonus_roo=level["bonus_roo"],
                newly_credited=credited,
            )
        )
    result = dict(
        outcome="applied",
        audit_receipt_id=str(receipt.pk),
        member_id=str(user.pk),
        results=results,
        credited_roo=roo(
            sum(microroo(row["bonus_roo"]) for row in results if row["newly_credited"])
        ),
    )
    receipt.metadata = {"approval": approval, "result": result}
    receipt.status = "processed"
    receipt.save(update_fields=("metadata", "status", "updated_at"))
    return result
