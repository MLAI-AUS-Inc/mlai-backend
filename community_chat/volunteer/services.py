"""Transactional recognition, canonical eligibility and rank calculation."""

import hashlib
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from core.actor_ids import actor_ids_for_user, preferred_actor_id_for_user
from roo.models import ChannelFirstPost, Ledger, PointsAccount
from roo.services import PointsService

from .access import (
    VolunteerError,
    capabilities,
    channels,
    community_id,
    flag,
    occurrence,
    public_source,
    require_capability,
)
from .models import (
    VolunteerAttendance,
    VolunteerMemberState,
    VolunteerMilestone,
    VolunteerOpportunity,
    VolunteerRecognition,
    VolunteerSourceReceipt,
)
from .evidence import source_is_invalidated
from .policy import (
    VERSION,
    catalogue,
    levels,
    microroo,
    next_actions,
    period_bounds,
    progress,
    roo,
)

# Canonical content has one contribution classification, regardless of the
# shape of its cap/outcome key. Likes, attendance and purchases are separate.
CONTENT_ACTIONS = {
    "introduce_yourself",
    "monthly_learning_update",
    "monthly_startup_update",
    "share_first_meme",
    "first_channel_contribution",
    "helpful_answer",
    "report_bug",
    "fix_bug",
    "test_product",
    "proofread",
    "event_recap",
    "test_ai_tutorial",
}


def active_policy():
    """Use the established monthly-update amount, including configured changes."""
    return catalogue(int(getattr(settings, "ROO_POINTS_MONTHLY_UPDATE_REWARD", 20)))


def lock_member(user):
    """Serialize canonical-member outcomes before ledger locks, on all devices."""
    current = get_user_model().objects.select_for_update().get(pk=user.pk)
    if not current.is_active:
        raise VolunteerError("member_unavailable", 404)
    return current


@transaction.atomic
def state_for(user):
    """Initialise only provably empty history; ambiguous legacy totals stay null."""
    user = lock_member(user)
    state, created = VolunteerMemberState.objects.get_or_create(
        community=community_id(), user=user
    )
    if created:
        ledger = Ledger.objects.filter(user=user)
        maximum = ledger.aggregate(value=Max("pk"))["value"] or 0
        account = PointsAccount.objects.filter(user=user).first()
        has_history = (
            ledger.exclude(source="purchased_topup")
            .filter(Q(kind="EARN") | Q(kind__isnull=True, delta__gt=0))
            .exists()
        )
        has_history = has_history or bool(
            account and (account.lifetime_earned or account.lifetime_earned_microroo)
        )
        state.historical_ledger_cutoff = maximum
        if not has_history:
            state.historical_microroo = 0
            state.reconciled_at = timezone.now()
            state.reconciliation_note = (
                "No pre-existing earned ledger or lifetime total."
            )
        state.save()
    return state


def contribution_total(user, state=None):
    """Return a reconciled contribution total, or None instead of a false rank."""
    state = state or state_for(user)
    if state.historical_microroo is None:
        return None
    known_ledger = VolunteerRecognition.objects.filter(
        community=community_id(), user=user, ledger__isnull=False
    ).values("ledger_id")
    unknown = (
        Ledger.objects.filter(user=user, pk__gt=state.historical_ledger_cutoff)
        .exclude(source="purchased_topup")
        .exclude(reference_type__in=("VOLUNTEER_LEVEL_BONUS", "VOLUNTEER_CORRECTION"))
        .exclude(pk__in=known_ledger)
        .filter(Q(kind="EARN") | Q(kind__isnull=True, delta__gt=0))
    )
    if unknown.exists():
        return None
    # An audited opening covers all transactions through its cutoff. Later
    # mirrored receipts contribute only when their original ledger is newer.
    total = (
        VolunteerRecognition.objects.filter(
            community=community_id(), user=user, status="approved"
        )
        .filter(
            Q(ledger_id__gt=state.historical_ledger_cutoff) | Q(ledger__isnull=True)
        )
        .aggregate(value=Sum("reward_microroo"))["value"]
        or 0
    )
    reversed_opening = (
        VolunteerRecognition.objects.filter(
            community=community_id(),
            user=user,
            status="reversed",
            ledger_id__lte=state.historical_ledger_cutoff,
        ).aggregate(value=Sum("reward_microroo"))["value"]
        or 0
    )
    return max(0, state.historical_microroo + total - reversed_opening)


def legacy_intro_completed(user):
    """Preserve already-awarded original intro markers without minting again."""
    # A ChannelFirstPost marker alone may be recorded without a paid intro.
    return Ledger.objects.filter(
        user=user, reference_type="FIRST_CHANNEL_POST", kind="EARN", delta__gt=0
    ).exists()


def attendance_verified(user, when=None):
    """Require a real prior check-in rather than a registration or a rank."""
    records = VolunteerAttendance.objects.filter(community=community_id(), user=user)
    return records.filter(checked_in_at__lte=when or timezone.now()).exists()


def enforce_cap(user, action, when, *, exclude=None):
    """Check policy groups while holding the canonical member lock."""
    queryset = VolunteerRecognition.objects.filter(
        community=community_id(),
        user=user,
        status="approved",
        policy_snapshot__cap_group=action["cap_group"],
    )
    if exclude:
        queryset = queryset.exclude(pk=exclude)
    if action["period"] in ("week", "month"):
        start, end = period_bounds(when, action["period"])
        queryset = queryset.filter(occurred_at__gte=start, occurred_at__lt=end)
    elif action["period"] != "once":
        return
    if queryset.count() >= action["cap"]:
        raise VolunteerError("cap_reached", 409)
    if action["key"] == "introduce_yourself" and legacy_intro_completed(user):
        raise VolunteerError("already_recognised", 409)
    if action["cap_group"] == "monthly_update":
        start, _ = period_bounds(when, "month")
        # Existing company/month awards count even before history reconciliation.
        external = Ledger.objects.filter(
            user=user,
            source="STARTUP_UPDATE",
            idempotency_key__endswith=start.strftime(":%Y-%m"),
        )
        external = external.exclude(
            pk__in=VolunteerRecognition.objects.filter(
                community=community_id(), user=user, ledger__isnull=False
            ).values("ledger_id")
        )
        if external.exists():
            raise VolunteerError("cap_reached", 409)


def action_catalogue(user):
    """Decorate templates with verified completion, current caps and destinations."""
    result = []
    now = timezone.now()
    attendance = attendance_verified(user)
    for action in active_policy().values():
        item = dict(action)
        done = (
            VolunteerRecognition.objects.filter(
                community=community_id(),
                user=user,
                action_key=action["key"],
                status="approved",
            )
            .order_by("-occurred_at")
            .first()
        )
        pending = (
            VolunteerRecognition.objects.filter(
                community=community_id(),
                user=user,
                action_key=action["key"],
                status__in=("pending", "needs_update"),
            )
            .order_by("-created_at")
            .first()
        )
        completed = bool(done and action["period"] == "once")
        if action["key"] == "introduce_yourself":
            completed = completed or legacy_intro_completed(user)
        destination = channels().get(action["channel_key"])
        opportunity = (
            VolunteerOpportunity.objects.filter(
                community=community_id(),
                action_key=action["key"],
                status="open",
                audience="community",
                source__channel_id__in=list(channels().values()),
            )
            .filter(Q(ends_at__isnull=True) | Q(ends_at__gt=now))
            .order_by("starts_at", "created_at")
            .first()
        )
        source = (
            public_source(opportunity.source, thread_required=True)
            if opportunity
            else ({"channel_id": destination} if destination else None)
        )
        reason = "completed" if completed else None
        if not reason and pending:
            reason = (
                "needs_update"
                if pending.status == "needs_update"
                else "awaiting_review"
            )
        if not reason and action["requires_attendance"] and not attendance:
            reason = "attendance_required"
        if not reason:
            try:
                enforce_cap(user, action, now)
            except VolunteerError as exc:
                reason = exc.code
        if not reason and not source and action["verification"] not in ("attendance",):
            reason = "not_configured"
        if (
            not reason
            and action["verification"] == "attendance"
            and not getattr(
                settings, "COMMUNITY_CHAT_VOLUNTEER_ATTENDANCE_ENABLED", False
            )
        ):
            reason = "not_configured"
        receipt = pending or done
        item.update(
            completed=completed,
            eligible=reason is None,
            unavailable_reason=reason,
            completion_id=str(receipt.pk) if receipt else None,
            recognition_status=receipt.status if receipt else None,
            source=source,
            opportunity_id=str(opportunity.pk) if opportunity else None,
        )
        result.append(item)
    return result


@transaction.atomic
def journey(user):
    """Return server-owned exact totals and a two-to-three-step eligible checklist."""
    user = lock_member(user)
    member_state = state_for(user)
    total = contribution_total(user, member_state)
    actions = action_catalogue(user)
    state = (
        progress(total)
        if total is not None
        else dict(
            current_level=None, next_level=None, points_to_next=None, progress=None
        )
    )
    paid_levels = set(
        VolunteerMilestone.objects.filter(
            community=community_id(),
            user=user,
            ledger__isnull=False,
        ).values_list("level_key", flat=True)
    )
    public_levels = [
        dict(
            level,
            bonus_awarded=level["key"] in paid_levels,
            bonus_eligible=(
                member_state.historical_microroo is not None
                and microroo(level["threshold_roo"]) > member_state.historical_microroo
            ),
        )
        for level in levels()
    ]
    for key in ("current_level", "next_level"):
        if state[key] is not None:
            state[key] = public_levels[state[key]["level"]]
    suggestions = next_actions(actions)
    # A requested update is a next action, never an earned tick.
    update = (
        VolunteerRecognition.objects.filter(
            community=community_id(), user=user, status="needs_update"
        )
        .order_by("updated_at")
        .first()
    )
    if update:
        item = next(
            (dict(action) for action in actions if action["key"] == update.action_key),
            None,
        )
        if item:
            try:
                update_source = public_source(update.source)
            except VolunteerError:
                update_source = None
            item.update(
                title="Update your recognition request",
                completed=False,
                eligible=True,
                unavailable_reason=None,
                completion_id=str(update.pk),
                source=update_source,
                priority=-1,
            )
            suggestions = [
                item,
                *[action for action in suggestions if action["key"] != item["key"]],
            ][:3]
    return dict(
        account_id=str(user.pk),
        community_id=community_id(),
        relay_url=settings.COMMUNITY_CHAT_RELAY_URL,
        policy_version=VERSION,
        contribution_roo=roo(total) if total is not None else None,
        wallet_balance=roo(PointsService.get_available_microroo(user)),
        levels=public_levels,
        attendance={"verified": attendance_verified(user)},
        suggestions=suggestions,
        actions=actions,
        capabilities=capabilities(user),
        feature_flags={
            "enabled": flag("enabled"),
            "awards_enabled": flag("awards_enabled"),
            "bonuses_enabled": flag("bonuses_enabled"),
        },
        history_reconciled=total is not None,
        updated_at=timezone.now().isoformat(),
        **state,
    )


def outcome_key(action, source, when):
    """Collapse equivalent outcomes and mutually exclusive source classifications."""
    key = action["key"]
    source_id = source.get("source_id") or source.get("message_id")
    if key in (
        "introduce_yourself",
        "attend_first_event",
        "coworking_induction",
        "buy_merch",
    ):
        raw = f"once:{key}"
    elif key == "volunteer_event":
        raw = f"event:{source.get('event_id', '')}"
    elif action["cap_group"] == "monthly_update":
        raw = f"monthly:{period_bounds(when, 'month')[0].date().isoformat()}"
    elif key == "boost_startup":
        raw = f"boost:{source_id}"
    else:
        # Every submitted content/deliverable has one award group by default.
        raw = f"content:{source_id}"
    if not source_id and key not in ("volunteer_event", "attend_first_event"):
        raise VolunteerError("source_required")
    if key == "volunteer_event" and not source.get("event_id"):
        raise VolunteerError("event_required")
    return hashlib.sha256(raw.encode()).hexdigest()


def enforce_source_classification(user, action_key, source):
    """Reject a second category independently of monthly/once reward key shape."""
    source_id = source.get("source_id") or source.get("message_id")
    if action_key not in CONTENT_ACTIONS or not source_id:
        return
    records = VolunteerRecognition.objects.filter(
        community=community_id(),
        user=user,
        action_key__in=CONTENT_ACTIONS,
    ).filter(Q(source__source_id=source_id) | Q(source__message_id=source_id))
    if records.exclude(action_key=action_key).exists():
        raise VolunteerError("source_already_classified", 409)


def verified_source(user, action, source, *, trusted_receipt=None, opportunity=None):
    """Resolve message authorship and occurrence time from authoritative receipts."""
    if trusted_receipt is not None:
        return dict(trusted_receipt.source), trusted_receipt.occurred_at
    source = public_source(source)
    if opportunity:
        if opportunity.kind == "event":
            source = {
                **opportunity.source,
                "event_id": opportunity.event_id,
                "source_id": opportunity.event_id,
            }
        elif not source.get("source_id"):
            raise VolunteerError("source_required")
    message_actions = {
        "introduce_yourself",
        "boost_startup",
        "monthly_learning_update",
        "share_first_meme",
        "first_channel_contribution",
        "helpful_answer",
        "report_bug",
    }
    if action["key"] in message_actions:
        source_id = source.get("source_id") or source.get("message_id")
        kinds = (
            ("reply",)
            if action["key"] == "helpful_answer"
            else ("post", "reply") if action["key"] == "report_bug" else ("post",)
        )
        receipts = VolunteerSourceReceipt.objects.filter(
            community=community_id(),
            actor=user,
            source__source_id=source_id,
            kind__in=kinds,
        )
        receipt = receipts.order_by("occurred_at").first()
        if receipt is None or source_is_invalidated(receipt):
            raise VolunteerError("source_unavailable", 409)
        campaign_start = getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_ACTIVE_FROM", "")
        if (
            action["key"] != "introduce_yourself"
            and receipt.kind == "post"
            and receipt.source.get("channel_id") == channels().get("start_here")
            and all(
                receipt.metadata.get(key) is True
                for key in ("original", "top_level", "has_text")
            )
            and campaign_start
            and receipt.occurred_at >= occurrence(campaign_start)
        ):
            raise VolunteerError("source_reserved_for_introduction", 409)
        expected = channels().get(action["channel_key"])
        if expected and receipt.source.get("channel_id") != expected:
            raise VolunteerError("source_unavailable", 409)
        return public_source(receipt.source), receipt.occurred_at
    # Human reviewed work records the submission time initially. The reviewer
    # confirms actual work, and event recognition is scoped to its event time.
    when = (
        min(timezone.now(), opportunity.ends_at or timezone.now())
        if opportunity
        else timezone.now()
    )
    return source, when


@transaction.atomic
def request_recognition(user, payload, *, actor=None, trusted_receipt=None):
    """Create or resolve one completed-work request, regardless of entry path."""
    if actor is not None:
        require_capability(actor, "can_review")
    if not flag("recognition_enabled") and trusted_receipt is None:
        raise VolunteerError("recognition_disabled", 503)
    user = lock_member(user)
    state_for(user)
    request_key = payload.get("idempotency_key")
    request_receipt = None
    if request_key:
        request_storage_key = (
            f"request:{user.pk}:{hashlib.sha256(request_key.encode()).hexdigest()}"
        )
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()
        request_receipt = (
            VolunteerSourceReceipt.objects.filter(
                community=community_id(), source_key=request_storage_key
            )
            .select_related("recognition")
            .first()
        )
        if request_receipt is not None:
            if request_receipt.metadata.get(
                "fingerprint"
            ) != fingerprint or request_receipt.metadata.get("actor_id") != (
                str(actor.pk) if actor else str(user.pk)
            ):
                raise VolunteerError("conflict", 409)
            return request_receipt.recognition, (
                "already_recognised"
                if request_receipt.recognition.status == "approved"
                else "existing_request"
            )
    action = active_policy().get(payload.get("action_key"))
    if action is None:
        raise VolunteerError("unknown_action")
    if trusted_receipt is None and action["verification"] not in ("human", "merch"):
        raise VolunteerError("trusted_evidence_required", 409)
    opportunity = None
    if payload.get("opportunity_id"):
        opportunity = VolunteerOpportunity.objects.filter(
            pk=payload["opportunity_id"], community=community_id(), audience="community"
        ).first()
        if opportunity is None or opportunity.action_key != action["key"]:
            raise VolunteerError("opportunity_unavailable", 404)
        public_source(opportunity.source, thread_required=True)
    if action["requires_attendance"] and opportunity is None:
        raise VolunteerError("opportunity_required")
    source, when = verified_source(
        user,
        action,
        payload.get("source", {}),
        trusted_receipt=trusted_receipt,
        opportunity=opportunity,
    )
    enforce_source_classification(user, action["key"], source)
    key = outcome_key(action, source, when)
    existing = VolunteerRecognition.objects.filter(
        community=community_id(), user=user, outcome_key=key
    ).first()
    if existing:
        if request_key:
            VolunteerSourceReceipt.objects.create(
                community=community_id(),
                actor=user,
                source_key=request_storage_key,
                origin="member",
                kind="recognition_request",
                source=source,
                metadata={
                    "fingerprint": fingerprint,
                    "actor_id": str(actor.pk) if actor else str(user.pk),
                },
                occurred_at=timezone.now(),
                status="recorded",
                recognition=existing,
            )
        return existing, (
            "already_recognised"
            if existing.status == "approved"
            else "existing_request"
        )
    enforce_cap(user, action, when)
    if action["requires_attendance"] and not attendance_verified(user, when):
        raise VolunteerError("attendance_required", 409)
    snapshot = {**action, "version": VERSION}
    if opportunity:
        snapshot.update(
            title=(
                f"Volunteer at {opportunity.title}"
                if opportunity.kind == "event"
                else opportunity.title
            ),
            description=opportunity.description,
            reward_roo=roo(opportunity.reward_microroo),
            reward_max_roo=roo(opportunity.reward_max_microroo),
            opportunity_version=opportunity.version,
        )
    reviewer_id = (
        opportunity.reviewer_id
        if opportunity
        else getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_REVIEWER_ID", None)
    )
    if trusted_receipt is None and reviewer_id:
        reviewer = (
            get_user_model().objects.filter(pk=reviewer_id, is_active=True).first()
        )
        if reviewer is None or not capabilities(reviewer)["can_review"]:
            fallback_id = getattr(
                settings, "COMMUNITY_CHAT_VOLUNTEER_REVIEWER_ID", None
            )
            fallback = (
                get_user_model().objects.filter(pk=fallback_id, is_active=True).first()
                if fallback_id
                else None
            )
            reviewer_id = (
                fallback.pk
                if fallback and capabilities(fallback)["can_review"]
                else None
            )
    if not reviewer_id and trusted_receipt is None:
        raise VolunteerError("reviewer_unavailable", 409)
    record = VolunteerRecognition.objects.create(
        community=community_id(),
        user=user,
        reviewer_id=reviewer_id,
        opportunity=opportunity,
        outcome_key=key,
        action_key=action["key"],
        source=source,
        policy_snapshot=snapshot,
        occurred_at=when,
        note=payload.get("note", ""),
        evidence=payload.get("evidence", ""),
        reward_microroo=microroo(snapshot["reward_roo"]),
    )
    if request_key:
        receipt_key = (
            f"request:{user.pk}:{hashlib.sha256(request_key.encode()).hexdigest()}"
        )
        VolunteerSourceReceipt.objects.create(
            community=community_id(),
            actor=user,
            source_key=receipt_key,
            origin="member",
            kind="recognition_request",
            source=source,
            metadata={
                "fingerprint": fingerprint,
                "actor_id": str(actor.pk) if actor else str(user.pk),
            },
            occurred_at=timezone.now(),
            status="recorded",
            recognition=record,
        )
    return record, "created"


def award_milestones(user, recognition=None):
    """Issue missing stable bonuses once inside the member's award transaction."""
    if not flag("awards_enabled") or not flag("bonuses_enabled"):
        return
    total = contribution_total(user)
    if total is None:
        return
    historical = state_for(user).historical_microroo or 0
    for level in levels()[1:]:
        if total < microroo(level["threshold_roo"]):
            break
        if microroo(level["threshold_roo"]) <= historical:
            # Historical reconciliation never authorises retroactive bonuses,
            # including indirectly during the member's next new contribution.
            continue
        milestone, created = VolunteerMilestone.objects.get_or_create(
            community=community_id(),
            user=user,
            level_key=level["key"],
            defaults={"reached_at": timezone.now(), "recognition": recognition},
        )
        if milestone.ledger_id is not None:
            continue
        ledger, _ = PointsService.credit_volunteer(
            user,
            microroo(level["bonus_roo"]),
            idempotency_key=f"volunteer:bonus:{community_id()}:{user.pk}:{level['key']}",
            reference_id=str(milestone.pk),
            description=f"{level['name']} — wallet-only level bonus",
            actor_id="volunteer:milestone",
            level_bonus=True,
        )
        milestone.ledger = ledger
        milestone.save(update_fields=("ledger", "updated_at"))


@transaction.atomic
def decision(record, actor, payload, *, automatic=False, existing_ledger=None):
    """Atomically approve/reject/correct a request with optimistic conflict checks."""
    if not automatic:
        require_capability(actor, "can_review")
        if actor.pk == record.user_id:
            raise VolunteerError("self_approval_forbidden", 403)
    user = lock_member(record.user)
    record = VolunteerRecognition.objects.select_for_update().get(
        pk=record.pk, community=community_id()
    )
    requested = payload.get("decision")
    key = payload.get("idempotency_key", "")
    prior = next(
        (
            entry
            for entry in record.review_history
            if key and entry.get("idempotency_key") == key
        ),
        None,
    )
    if prior:
        if (
            prior["decision"] != requested
            or prior["note"] != payload.get("note", "")
            or prior.get("actor_id") != (str(actor.pk) if actor else None)
            or (
                "reward_roo" in payload
                and prior.get("reward_roo") != payload["reward_roo"]
            )
        ):
            raise VolunteerError("conflict", 409)
        return record, (
            "already_recognised" if record.status == "approved" else "existing_decision"
        )
    if payload.get("version") != record.version:
        raise VolunteerError("conflict", 409)
    note = payload.get("note", "").strip()
    if requested not in ("approve", "needs_update", "not_approve", "reverse"):
        raise VolunteerError("invalid_decision")
    if requested == "reverse":
        require_capability(actor, "can_correct")
        if record.status != "approved" or not note:
            raise VolunteerError("invalid_transition", 409)
        if record.ledger:
            PointsService.reverse_volunteer(
                user,
                original=record.ledger,
                actor_id=preferred_actor_id_for_user(actor),
                reason=note,
            )
        record.status = "reversed"
    else:
        if record.status not in ("pending", "needs_update"):
            raise VolunteerError("conflict", 409)
        if requested == "approve":
            if not flag("awards_enabled") and existing_ledger is None:
                raise VolunteerError("awards_disabled", 503)
            first_human = not any(
                any(
                    not entry.get("automatic") and entry.get("decision") == "approve"
                    for entry in item.review_history
                )
                for item in VolunteerRecognition.objects.filter(
                    community=community_id(), user=user, status="approved"
                )
            )
            if not automatic and first_human and not note:
                raise VolunteerError("personal_feedback_required")
            public_source(record.source)
            if not automatic and record.action_key in {
                "monthly_learning_update",
                "share_first_meme",
                "first_channel_contribution",
                "helpful_answer",
                "report_bug",
            }:
                verified_source(user, record.policy_snapshot, record.source)
            enforce_cap(
                user, record.policy_snapshot, record.occurred_at, exclude=record.pk
            )
            if record.policy_snapshot[
                "requires_attendance"
            ] and not attendance_verified(user, record.occurred_at):
                raise VolunteerError("attendance_required", 409)
            amount = microroo(payload.get("reward_roo", roo(record.reward_microroo)))
            low, high = microroo(record.policy_snapshot["reward_roo"]), microroo(
                record.policy_snapshot["reward_max_roo"]
            )
            if (
                record.action_key != "volunteer_event"
                and amount != low
                or not low <= amount <= high
            ):
                raise VolunteerError("invalid_reward")
            record.reward_microroo = amount
            if existing_ledger is not None:
                if (
                    existing_ledger.user_id != user.pk
                    or existing_ledger.delta_microroo != amount
                ):
                    raise VolunteerError("invalid_ledger")
                record.ledger = existing_ledger
            elif amount:
                record.ledger, _ = PointsService.credit_volunteer(
                    user,
                    amount,
                    idempotency_key=f"volunteer:recognition:{record.pk}",
                    reference_id=str(record.pk),
                    description=record.policy_snapshot["title"],
                    actor_id=(
                        preferred_actor_id_for_user(actor)
                        if actor
                        else "volunteer:verified_source"
                    ),
                )
            record.status = "approved"
        else:
            if not note:
                raise VolunteerError("feedback_required")
            record.status = (
                "needs_update" if requested == "needs_update" else "not_approved"
            )
    record.review_history = [
        *record.review_history,
        dict(
            decision=requested,
            note=note,
            actor_id=str(actor.pk) if actor else None,
            at=timezone.now().isoformat(),
            automatic=automatic,
            idempotency_key=key,
            reward_roo=roo(record.reward_microroo),
        ),
    ]
    record.version += 1
    record.save()
    if record.status == "approved":
        award_milestones(user, record)
    return record, "approved" if record.status == "approved" else record.status


@transaction.atomic
def revise_request(record, user, *, version, note=None, evidence=None, withdraw=False):
    """Resubmit or withdraw the same private record, preserving its history."""
    lock_member(user)
    record = VolunteerRecognition.objects.select_for_update().get(
        pk=record.pk, community=community_id(), user=user
    )
    if record.version != version or record.status not in (
        ("pending", "needs_update") if withdraw else ("needs_update",)
    ):
        raise VolunteerError("conflict", 409)
    if not withdraw and not (note or "").strip():
        raise VolunteerError("note_required")
    record.status = "withdrawn" if withdraw else "pending"
    record.note = record.note if note is None else note
    record.evidence = record.evidence if evidence is None else evidence
    record.version += 1
    record.review_history = [
        *record.review_history,
        dict(
            decision="withdrawn" if withdraw else "resubmitted",
            note=note or "",
            actor_id=str(user.pk),
            at=timezone.now().isoformat(),
        ),
    ]
    record.save()
    return record
