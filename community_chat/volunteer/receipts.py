"""Durable trusted receipts, objective awards and existing pipeline adapters."""

from datetime import datetime

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from roo.models import ChannelFirstPost, Ledger
from roo.services import PointsService

from .access import (
    VolunteerError,
    actor_for_key,
    channels,
    community_id,
    flag,
    linked_member,
    occurrence,
    public_source,
    require_capability,
)
from .evidence import source_is_invalidated
from .models import VolunteerAttendance, VolunteerRecognition, VolunteerSourceReceipt
from .policy import MELBOURNE, VERSION, microroo, roo
from .services import (
    active_policy,
    award_milestones,
    decision,
    enforce_cap,
    enforce_source_classification,
    lock_member,
    outcome_key,
    request_recognition,
    state_for,
)

METADATA_FIELDS = {
    "original",
    "top_level",
    "has_text",
    "service_account",
    "invalidated",
    "reaction",
    "target_public_key",
    "company_id",
    "ledger_id",
    "checked_in_at",
    "fulfilled",
    "refunded",
    "deletion_kind",
}


@transaction.atomic
def persist_receipt(payload):
    """Validate and persist a service-origin receipt before any retryable credit."""
    if not isinstance(payload, dict) or set(payload) - {
        "source_key",
        "kind",
        "origin",
        "actor_public_key",
        "actor_id",
        "source",
        "occurred_at",
        "metadata",
    }:
        raise VolunteerError("invalid_receipt")
    kind, origin = payload.get("kind"), payload.get("origin")
    if (
        (
            origin == "relay"
            and kind not in ("post", "reply", "reaction", "invalidation")
        )
        or (origin == "luma" and kind != "attendance")
        or (origin == "startup_updates" and kind != "monthly_update")
        or (origin == "merch" and kind != "merch")
        or origin not in ("relay", "luma", "startup_updates", "merch")
    ):
        raise VolunteerError("invalid_receipt")
    user = (
        actor_for_key(payload.get("actor_public_key"))
        if origin == "relay"
        else linked_member(payload.get("actor_id"))
    )
    lock_member(user)
    # Initialise before creating a qualifying source, preserving legacy totals.
    state_for(user)
    when = occurrence(payload.get("occurred_at"))
    source = public_source(payload.get("source", {}))
    key = payload.get("source_key")
    if not isinstance(key, str) or not 1 <= len(key) <= 240:
        raise VolunteerError("invalid_source_key")
    if origin == "relay" and (
        not source.get("source_id")
        or (kind != "invalidation" and not source.get("channel_id"))
    ):
        raise VolunteerError("source_required")
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict) or set(metadata) - METADATA_FIELDS:
        raise VolunteerError("invalid_receipt_metadata")
    for name, value in metadata.items():
        if name in (
            "original",
            "top_level",
            "has_text",
            "service_account",
            "invalidated",
            "fulfilled",
            "refunded",
        ):
            if not isinstance(value, bool):
                raise VolunteerError("invalid_receipt_metadata")
        elif (
            not isinstance(value, (str, int))
            or isinstance(value, bool)
            or len(str(value)) > 255
        ):
            raise VolunteerError("invalid_receipt_metadata")
    target = (
        actor_for_key(metadata["target_public_key"])
        if metadata.get("target_public_key")
        else None
    )
    ineligible = ""
    if kind == "invalidation":
        deletion_kind = metadata.get("deletion_kind")
        if deletion_kind not in (5, 9005):
            raise VolunteerError("invalid_receipt_metadata")
        if deletion_kind == 9005:
            if not source.get("channel_id"):
                raise VolunteerError("source_required")
            try:
                require_capability(user, "can_review")
            except VolunteerError:
                ineligible = "moderator_not_authorised"
        else:
            if target is not None and target.pk != user.pk:
                ineligible = "deletion_actor_mismatch"
            known = VolunteerSourceReceipt.objects.filter(
                community=community_id(),
                kind__in=("post", "reply"),
                source__source_id=source["source_id"],
            ).first()
            if known is not None and known.actor_id != user.pk:
                ineligible = "deletion_actor_mismatch"
    receipt, created = VolunteerSourceReceipt.objects.get_or_create(
        community=community_id(),
        source_key=f"{origin}:{key}",
        defaults=dict(
            origin=origin,
            kind=kind,
            actor=user,
            target=target,
            source=source,
            metadata=metadata,
            occurred_at=when,
            status="ineligible" if ineligible else "pending",
            error=ineligible,
        ),
    )
    if not created and (
        receipt.actor_id != user.pk
        or receipt.kind != kind
        or receipt.source != source
        or receipt.metadata != metadata
        or receipt.occurred_at != when
    ):
        raise VolunteerError("source_conflict", 409)
    return receipt


def ingest_receipt(payload):
    """Durably record authoritative evidence, then attempt safely retryable credit."""
    receipt = persist_receipt(payload)
    if receipt.kind == "attendance":
        record_attendance_receipt(receipt)
    return process_receipt(receipt)


@transaction.atomic
def record_attendance_receipt(receipt):
    """Persist verification independently of whether reward issuance is enabled."""
    if receipt.community != community_id():
        raise VolunteerError("source_unavailable", 403)
    if (
        receipt.metadata.get("invalidated")
        or receipt.metadata.get("service_account")
        or not receipt.actor.is_active
        or not receipt.source.get("event_id")
        or not receipt.metadata.get("checked_in_at")
    ):
        raise VolunteerError("ineligible_source", 409)
    lock_member(receipt.actor)
    when = occurrence(receipt.metadata["checked_in_at"])
    VolunteerAttendance.objects.get_or_create(
        community=receipt.community,
        user=receipt.actor,
        event_id=receipt.source["event_id"],
        defaults=dict(
            checked_in_at=when,
            source_id=receipt.source_key,
            reason="Verified check-in source",
            audit_history=[
                dict(
                    source_id=receipt.source_key,
                    checked_in_at=when.isoformat(),
                    at=timezone.now().isoformat(),
                )
            ],
        ),
    )


def process_receipt(receipt):
    """Retry a stored receipt; errors leave an auditable, recoverable status."""
    if receipt.community != community_id():
        raise VolunteerError("source_unavailable", 403)
    try:
        return _process_receipt(receipt.pk)
    except VolunteerError as exc:
        terminal = exc.code in (
            "cap_reached",
            "already_recognised",
            "ineligible_source",
            "action_inactive",
        )
        VolunteerSourceReceipt.objects.filter(pk=receipt.pk).update(
            status="ineligible" if terminal else "pending",
            error=exc.code,
            updated_at=timezone.now(),
        )
        receipt.refresh_from_db()
        return receipt


@transaction.atomic
def _process_receipt(receipt_id):
    # Lock member before receipt consistently with source persistence/reviews.
    initial = VolunteerSourceReceipt.objects.select_related("actor").get(pk=receipt_id)
    user = lock_member(initial.actor)
    receipt = VolunteerSourceReceipt.objects.select_for_update().get(pk=receipt_id)
    if receipt.status in ("processed", "ineligible", "recorded"):
        return receipt
    metadata, source = receipt.metadata, receipt.source
    public_source(source)
    if receipt.kind == "invalidation":
        receipt.status, receipt.error = "recorded", ""
        receipt.save(update_fields=("status", "error", "updated_at"))
        return receipt
    if source_is_invalidated(receipt) or not user.is_active:
        raise VolunteerError("ineligible_source", 409)
    action_key = None
    if receipt.kind == "attendance":
        if not source.get("event_id") or not metadata.get("checked_in_at"):
            raise VolunteerError("ineligible_source", 409)
        when = occurrence(metadata["checked_in_at"])
        VolunteerAttendance.objects.get_or_create(
            community=receipt.community,
            user=user,
            event_id=source["event_id"],
            defaults=dict(
                checked_in_at=when,
                source_id=receipt.source_key,
                reason="Verified Luma check-in",
                audit_history=[
                    dict(
                        source_id=receipt.source_key,
                        checked_in_at=when.isoformat(),
                        at=timezone.now().isoformat(),
                    )
                ],
            ),
        )
        action_key = "attend_first_event"
    elif (
        receipt.kind == "post"
        and source.get("channel_id") == channels().get("start_here")
        and metadata.get("original") is True
        and metadata.get("top_level") is True
        and metadata.get("has_text") is True
    ):
        action_key = "introduce_yourself"
    elif receipt.kind == "reaction" and source.get("channel_id") == channels().get(
        "boost_startup"
    ):
        if (
            not receipt.target_id
            or receipt.target_id == user.pk
            or metadata.get("reaction")
            not in getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_LIKE_REACTIONS", [])
        ):
            raise VolunteerError("ineligible_source", 409)
        action_key = "boost_startup"
    elif receipt.kind == "monthly_update":
        return _mirror_monthly_receipt(receipt, user)
    elif receipt.kind == "merch":
        if not metadata.get("fulfilled") or metadata.get("refunded"):
            raise VolunteerError("ineligible_source", 409)
        action_key = "buy_merch"
    if action_key is None:
        receipt.status = "recorded"
        receipt.save(update_fields=("status", "updated_at"))
        return receipt
    if action_key in ("introduce_yourself", "boost_startup"):
        start = getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_ACTIVE_FROM", "")
        if not start or receipt.occurred_at < occurrence(start):
            raise VolunteerError("action_inactive", 409)
    if not flag("awards_enabled"):
        receipt.status, receipt.error = "pending", "awards_disabled"
        receipt.save(update_fields=("status", "error", "updated_at"))
        return receipt
    record, _ = request_recognition(
        user,
        dict(
            action_key=action_key, source=source, note="Recorded from a verified source"
        ),
        trusted_receipt=receipt,
    )
    if record.status != "approved":
        record, _ = decision(
            record,
            None,
            dict(
                decision="approve",
                note="Recorded automatically",
                version=record.version,
                idempotency_key=receipt.source_key,
            ),
            automatic=True,
        )
    receipt.recognition = record
    receipt.status, receipt.error = "processed", ""
    receipt.save(update_fields=("recognition", "status", "error", "updated_at"))
    return receipt


def _mirror_monthly_receipt(receipt, user):
    """Attach the original completed-update ledger; this never pays it again."""
    ledger = Ledger.objects.filter(
        pk=receipt.metadata.get("ledger_id"),
        user=user,
        source="STARTUP_UPDATE",
        kind="EARN",
    ).first()
    if (
        ledger is None
        or not ledger.idempotency_key
        or not ledger.idempotency_key.startswith("monthly_update_reward:")
    ):
        raise VolunteerError("source_unavailable", 409)
    action = active_policy()["monthly_startup_update"]
    enforce_source_classification(user, action["key"], receipt.source)
    key = outcome_key(action, receipt.source, receipt.occurred_at)
    record, created = VolunteerRecognition.objects.get_or_create(
        community=receipt.community,
        user=user,
        outcome_key=key,
        defaults=dict(
            action_key=action["key"],
            source=receipt.source,
            policy_snapshot={
                **action,
                "version": VERSION,
                "reward_roo": roo(ledger.delta_microroo),
                "reward_max_roo": roo(ledger.delta_microroo),
            },
            occurred_at=receipt.occurred_at,
            note="Existing monthly update award",
            status="approved",
            reward_microroo=ledger.delta_microroo,
            ledger=ledger,
            review_history=[
                dict(
                    decision="approve",
                    note="Recorded from the existing monthly update workflow",
                    actor_id=None,
                    automatic=True,
                    at=ledger.created_at.isoformat(),
                )
            ],
        ),
    )
    if not created and record.ledger_id != ledger.pk:
        if record.ledger_id is not None or record.status in ("approved", "reversed"):
            raise VolunteerError("monthly_award_conflict", 409)
        # A pending learning request and a verified startup update share one
        # monthly outcome. Preserve its audit trail while reclassifying before
        # approval, rather than blocking the established startup pipeline.
        record.action_key = action["key"]
        record.source = receipt.source
        record.policy_snapshot = {
            **action,
            "version": VERSION,
            "reward_roo": roo(ledger.delta_microroo),
            "reward_max_roo": roo(ledger.delta_microroo),
        }
        record.status = "approved"
        record.reward_microroo = ledger.delta_microroo
        record.ledger = ledger
        record.version += 1
        record.review_history = [
            *record.review_history,
            dict(
                decision="approve",
                note="Recognised through the existing startup update workflow",
                actor_id=None,
                automatic=True,
                at=ledger.created_at.isoformat(),
            ),
        ]
        record.save()
    receipt.recognition, receipt.status, receipt.error = record, "processed", ""
    receipt.save(update_fields=("recognition", "status", "error", "updated_at"))
    award_milestones(user, record)
    return receipt


@transaction.atomic
def award_startup_update(user, company, month_bucket, draft=None):
    """Extend the existing company/month award with a shared member/month cap.

    Called only behind the award flag. The parent service already verifies the
    company. Locking the company retains its independent global uniqueness.
    """
    user = lock_member(user)
    state_for(user)
    company.__class__.objects.select_for_update().get(pk=company.pk)
    action = active_policy()["monthly_startup_update"]
    when = datetime(month_bucket.year, month_bucket.month, 1, tzinfo=MELBOURNE)
    if when > timezone.now():
        raise VolunteerError("invalid_occurrence")
    key = f"monthly_update_reward:{company.pk}:{month_bucket:%Y-%m}"
    existing = Ledger.objects.filter(idempotency_key=key).first()
    if existing is not None and existing.user_id != user.pk:
        return False
    if existing is None:
        enforce_cap(user, action, when)
    amount = int(getattr(settings, "ROO_POINTS_MONTHLY_UPDATE_REWARD", 20))
    if existing is not None:
        ledger, created = existing, False
    else:
        ledger, created = PointsService.award(
            user=user,
            delta=amount,
            source="STARTUP_UPDATE",
            description=f"Monthly update completed — {month_bucket:%B %Y}",
            created_by_slack_id=user.slack_id or "system",
            idempotency_key=key,
            reference_type="MONTHLY_UPDATE_DRAFT",
            reference_id=str(draft.pk) if draft is not None else None,
        )
    # Direct in-process source, using the canonical account; no Slack requirement.
    receipt, _ = VolunteerSourceReceipt.objects.get_or_create(
        community=community_id(),
        source_key=f"startup_updates:{key}",
        defaults=dict(
            origin="startup_updates",
            kind="monthly_update",
            actor=user,
            source={"source_id": f"company:{company.pk}:{month_bucket:%Y-%m}"},
            metadata={"ledger_id": ledger.pk, "company_id": str(company.pk)},
            occurred_at=when,
        ),
    )
    _mirror_monthly_receipt(receipt, user)
    return created


@transaction.atomic
def award_legacy_intro(user, slack_user_id, channel_id):
    """Coordinate the existing Slack intro and native intro under one user lock."""
    user = lock_member(user)
    state_for(user)
    marker, created = ChannelFirstPost.objects.get_or_create(
        slack_user_id=slack_user_id, channel_id=channel_id
    )
    native = VolunteerRecognition.objects.filter(
        community=community_id(), user=user, action_key="introduce_yourself"
    ).first()
    if native is not None and native.status in ("approved", "reversed"):
        return False, None
    if not created:
        return False, None
    previous = Ledger.objects.filter(
        user=user, reference_type="FIRST_CHANNEL_POST", kind="EARN"
    ).first()
    if previous is not None:
        return False, None
    ledger, awarded = PointsService.award(
        user=user,
        delta=4,
        source="EVENT",
        description="Completed quest: First Contact",
        created_by_slack_id="SYSTEM",
        idempotency_key=f"first_post_award:{slack_user_id}:{channel_id}",
        reference_type="FIRST_CHANNEL_POST",
        reference_id=f"{slack_user_id}:{channel_id}",
    )
    action = active_policy()["introduce_yourself"]
    source = {"source_id": f"legacy-intro:{user.pk}"}
    record = VolunteerRecognition.objects.create(
        community=community_id(),
        user=user,
        action_key="introduce_yourself",
        outcome_key=outcome_key(action, source, ledger.created_at),
        source=source,
        occurred_at=ledger.created_at,
        policy_snapshot={**action, "version": VERSION},
        status="approved",
        reward_microroo=ledger.delta_microroo,
        ledger=ledger,
        note="Existing first-contact award",
        review_history=[
            dict(
                decision="approve",
                note="Recorded from the existing introduction workflow",
                actor_id=None,
                automatic=True,
                at=ledger.created_at.isoformat(),
            )
        ],
    )
    award_milestones(user, record)
    return awarded, PointsService.get_balance(user)["balance"]


def record_luma_guest(*, user, event_id, guest):
    """Map a verified email/account match and a real Luma check-in to evidence.

    The ingestion caller must resolve user by verified account/email link.
    Registration, approval and QR fields are deliberately ignored.
    """
    data = guest.get("guest") if isinstance(guest.get("guest"), dict) else guest
    from integrations.services.luma import _tickets_for_guest

    timestamps = [
        data.get("checked_in_at"),
        *(ticket.get("checked_in_at") for ticket in _tickets_for_guest(data)),
    ]
    verified_times = [occurrence(value) for value in timestamps if value]
    when = min(verified_times) if verified_times else None
    if not when:
        return None
    checked = occurrence(when)
    return ingest_receipt(
        dict(
            origin="luma",
            kind="attendance",
            actor_id=user.pk,
            source_key=f"{event_id}:{data.get('id') or user.pk}",
            source={
                "event_id": str(event_id),
                "source_id": str(data.get("id") or user.pk),
            },
            occurred_at=checked.isoformat(),
            metadata={"checked_in_at": checked.isoformat()},
        )
    )
