"""Strict Volunteer inputs and private/public DTO boundaries."""

from django.db.models import Q
from rest_framework import serializers

from .access import capabilities, community_id, public_source
from .evidence import source_is_invalidated
from .models import VolunteerMilestone, VolunteerSourceReceipt
from .policy import microroo, roo
from .services import active_policy, attendance_verified


def member_dto(user):
    """Expose opted-in account display data, never email or Slack identity."""
    from community_chat.models import CommunityChatDevice

    public_key = (
        CommunityChatDevice.objects.filter(
            user=user, status="verified", revoked_at__isnull=True
        )
        .order_by("created_at", "id")
        .values_list("public_key", flat=True)
        .first()
        if user.is_active
        else None
    )
    return dict(
        id=str(user.pk),
        display_name=user.full_name or "MLAI member",
        avatar_url=user.avatar_url or None,
        public_key=public_key,
    )


def guide_contact(guide, reviewer=None):
    """Resolve a real reachable guide or explicitly configured human fallback."""
    from django.conf import settings
    from django.contrib.auth import get_user_model

    original = member_dto(guide)
    if original["public_key"]:
        return dict(guide=original, guide_available=True, guide_is_fallback=False)
    fallback_id = getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_REVIEWER_ID", None)
    configured = (
        get_user_model().objects.filter(pk=fallback_id, is_active=True).first()
        if fallback_id
        else None
    )
    for candidate in (reviewer, configured):
        if candidate is None or candidate.pk == guide.pk:
            continue
        contact = member_dto(candidate)
        if contact["public_key"] and capabilities(candidate)["can_review"]:
            return dict(guide=contact, guide_available=True, guide_is_fallback=True)
    return dict(guide=original, guide_available=False, guide_is_fallback=False)


def opportunity_dto(record, viewer):
    """Serialize a curated opportunity after server-side visibility validation."""
    action = active_policy()[record.action_key]
    return dict(
        id=str(record.pk),
        kind=record.kind,
        action_key=record.action_key,
        title=record.title,
        purpose=record.purpose,
        description=record.description,
        learning=record.learning,
        **guide_contact(record.guide, record.reviewer),
        reviewer=member_dto(record.reviewer),
        source=public_source(record.source, thread_required=True),
        event_id=record.event_id or None,
        project_id=str(record.project_id) if record.project_id else None,
        starts_at=record.starts_at.isoformat() if record.starts_at else None,
        ends_at=record.ends_at.isoformat() if record.ends_at else None,
        reward_roo=roo(record.reward_microroo),
        reward_max_roo=roo(record.reward_max_microroo),
        recommended_level=record.recommended_level,
        requires_attendance=action["requires_attendance"],
        status=record.status,
        version=record.version,
        can_request=capabilities(viewer)["can_request"]
        and (not action["requires_attendance"] or attendance_verified(viewer)),
    )


def project_dto(record, viewer):
    """Return only a curated brief and its authorised public opportunities."""
    return dict(
        id=str(record.pk),
        title=record.title,
        purpose=record.purpose,
        description=record.description,
        **guide_contact(record.guide),
        source=public_source(record.source, thread_required=True),
        published=record.published,
        version=record.version,
        opportunities=[
            opportunity_dto(item, viewer)
            for item in record.opportunities.filter(audience="community").exclude(
                status="archived"
            )
            if item.source.get("channel_id") in allowed_channels()
        ],
    )


def allowed_channels():
    """Return the current public channel allowlist for query and DTO filtering."""
    from .access import channels

    return set(channels().values())


def contribution_dto(record, viewer):
    """Serialize one receipt only after the caller has authorised its recipient."""
    from django.contrib.auth import get_user_model

    history = []
    for entry in record.review_history:
        actor = (
            get_user_model().objects.filter(pk=entry.get("actor_id")).first()
            if entry.get("actor_id")
            else None
        )
        history.append(
            dict(
                decision=entry["decision"],
                note=entry.get("note", ""),
                actor=member_dto(actor) if actor else None,
                automatic=entry.get("automatic", False),
                at=entry["at"],
            )
        )
    source = (
        record.source
        if record.source.get("channel_id") in allowed_channels()
        or not record.source.get("channel_id")
        else {}
    )
    if source:
        source_ids = {
            value
            for key in ("source_id", "message_id", "thread_root_id")
            if (value := source.get(key))
        }
        evidence_scope = Q(kind__in=("post", "reply"), source__source_id__in=source_ids)
        if source.get("message_id"):
            evidence_scope |= Q(
                kind="reaction", source__message_id=source["message_id"]
            )
        evidence = VolunteerSourceReceipt.objects.filter(
            evidence_scope, community=community_id()
        )
        if any(source_is_invalidated(item) for item in evidence):
            source = {}
    bonus = sum(
        item.ledger.delta_microroo or 0
        for item in VolunteerMilestone.objects.filter(
            recognition=record, ledger__isnull=False
        ).select_related("ledger")
    )
    return dict(
        id=str(record.pk),
        record_type="contribution",
        action_key=record.action_key,
        title=record.policy_snapshot.get("title", record.action_key),
        definition_of_done=record.policy_snapshot.get("description", ""),
        member=member_dto(record.user),
        opportunity_id=str(record.opportunity_id) if record.opportunity_id else None,
        source=source,
        status=record.status,
        credit_status=(
            "reversed"
            if record.status == "reversed"
            else (
                "credited"
                if record.status == "approved" and record.ledger_id
                else "not_awarded"
            )
        ),
        note=record.note,
        evidence=record.evidence,
        reviewer=member_dto(record.reviewer) if record.reviewer else None,
        reward_roo=roo(record.reward_microroo),
        reward_min_roo=record.policy_snapshot.get("reward_roo", "0"),
        reward_max_roo=record.policy_snapshot.get("reward_max_roo", "0"),
        bonus_roo=roo(bonus),
        occurred_at=record.occurred_at.isoformat(),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        version=record.version,
        review_history=history,
        can_resubmit=record.user_id == viewer.pk and record.status == "needs_update",
        can_withdraw=record.user_id == viewer.pk
        and record.status in ("pending", "needs_update"),
        can_review=capabilities(viewer)["can_review"]
        and record.user_id != viewer.pk
        and record.status in ("pending", "needs_update"),
    )


def level_bonus_dto(milestone, viewer):
    """Serialize an authorised standalone paid bonus without review actions."""
    ledger = milestone.ledger
    approval_receipt = (
        VolunteerSourceReceipt.objects.filter(
            community=milestone.community,
            target_id=milestone.user_id,
            kind="historical_bonus_backfill",
            status="processed",
            metadata__result__results__contains=[
                {
                    "milestone_id": str(milestone.pk),
                    "ledger_id": str(ledger.pk),
                    "newly_credited": True,
                }
            ],
        )
        .select_related("actor")
        .order_by("occurred_at")
        .first()
    )
    reviewer = approval_receipt.actor if approval_receipt else None
    note = (
        approval_receipt.metadata.get("approval", {}).get("reason", "")
        if approval_receipt
        else ""
    )
    amount = (
        ledger.delta_microroo
        if ledger.delta_microroo is not None
        else ledger.delta * 1_000_000
    )
    return dict(
        id=str(milestone.pk),
        record_type="level_bonus",
        action_key="level_bonus",
        title=ledger.description or "Level bonus",
        level_key=milestone.level_key,
        definition_of_done="",
        member=member_dto(milestone.user),
        opportunity_id=None,
        source={},
        status="approved",
        credit_status="credited",
        note=note,
        evidence="",
        reviewer=member_dto(reviewer) if reviewer else None,
        reward_roo="0",
        reward_min_roo="0",
        reward_max_roo="0",
        bonus_roo=roo(amount),
        occurred_at=ledger.created_at.isoformat(),
        created_at=ledger.created_at.isoformat(),
        updated_at=milestone.updated_at.isoformat(),
        version=1,
        review_history=(
            [
                dict(
                    decision="approve",
                    note=note,
                    actor=member_dto(reviewer),
                    automatic=False,
                    at=ledger.created_at.isoformat(),
                )
            ]
            if reviewer
            else []
        ),
        can_resubmit=False,
        can_withdraw=False,
        can_review=False,
    )


class StrictSerializer(serializers.Serializer):
    """Reject privilege/amount injection instead of silently ignoring fields."""

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object.")
        if set(data) - set(self.fields):
            raise serializers.ValidationError("Unexpected input fields.")
        return super().to_internal_value(data)


class ExactRooField(serializers.CharField):
    """Reject invalid or over-precision amounts before domain mutation begins."""

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            return roo(microroo(value))
        except ValueError as exc:
            raise serializers.ValidationError("invalid_reward") from exc


class SourceInput(StrictSerializer):
    channel_id = serializers.CharField(
        max_length=255, required=False, allow_null=True, allow_blank=True
    )
    thread_root_id = serializers.CharField(
        max_length=255, required=False, allow_null=True, allow_blank=True
    )
    message_id = serializers.CharField(
        max_length=255, required=False, allow_null=True, allow_blank=True
    )
    source_id = serializers.CharField(
        max_length=255, required=False, allow_null=True, allow_blank=True
    )
    event_id = serializers.CharField(
        max_length=255, required=False, allow_null=True, allow_blank=True
    )
    url = serializers.URLField(
        max_length=2000, required=False, allow_null=True, allow_blank=True
    )


class RequestInput(StrictSerializer):
    action_key = serializers.CharField(max_length=64)
    opportunity_id = serializers.UUIDField(required=False, allow_null=True)
    source = SourceInput(required=False, default=dict)
    note = serializers.CharField(max_length=4000)
    evidence = serializers.CharField(
        max_length=8000, required=False, allow_blank=True, default=""
    )
    idempotency_key = serializers.CharField(max_length=128)


class DirectInput(RequestInput):
    member_id = serializers.IntegerField(min_value=1)
    reward_roo = ExactRooField(max_length=32, required=False)
    feedback = serializers.CharField(max_length=4000)


class DecisionInput(StrictSerializer):
    decision = serializers.ChoiceField(
        choices=("approve", "needs_update", "not_approve", "reverse")
    )
    note = serializers.CharField(max_length=4000, allow_blank=True, default="")
    reward_roo = ExactRooField(max_length=32, required=False)
    version = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.CharField(max_length=128)


class RevisionInput(StrictSerializer):
    version = serializers.IntegerField(min_value=1)
    note = serializers.CharField(max_length=4000, required=False, allow_blank=True)
    evidence = serializers.CharField(max_length=8000, required=False, allow_blank=True)


class OpportunityInput(StrictSerializer):
    title = serializers.CharField(max_length=200)
    purpose = serializers.CharField(max_length=500)
    description = serializers.CharField(max_length=12000)
    learning = serializers.CharField(
        max_length=500, allow_blank=True, required=False, default=""
    )
    kind = serializers.ChoiceField(choices=("event", "project"))
    action_key = serializers.CharField(max_length=64)
    guide_id = serializers.IntegerField(min_value=1)
    reviewer_id = serializers.IntegerField(min_value=1)
    project_id = serializers.UUIDField(required=False, allow_null=True)
    event_id = serializers.CharField(
        max_length=200, required=False, allow_blank=True, allow_null=True
    )
    source = SourceInput()
    starts_at = serializers.DateTimeField(required=False, allow_null=True)
    ends_at = serializers.DateTimeField(required=False, allow_null=True)
    reward_roo = ExactRooField(max_length=32)
    reward_max_roo = ExactRooField(max_length=32)
    recommended_level = serializers.IntegerField(min_value=0, max_value=6, default=0)
    status = serializers.ChoiceField(
        choices=("open", "closed", "archived"), default="open"
    )
    version = serializers.IntegerField(min_value=1, required=False)


class ProjectInput(StrictSerializer):
    title = serializers.CharField(max_length=200)
    purpose = serializers.CharField(max_length=500)
    description = serializers.CharField(max_length=12000)
    guide_id = serializers.IntegerField(min_value=1)
    source = SourceInput()
    published = serializers.BooleanField(default=False)
    version = serializers.IntegerField(min_value=1, required=False)


class BatchItem(StrictSerializer):
    member_id = serializers.IntegerField(min_value=1)
    reward_roo = ExactRooField(max_length=32)
    note = serializers.CharField(max_length=4000)


class BatchInput(StrictSerializer):
    recipients = BatchItem(many=True, allow_empty=False, max_length=50)
    idempotency_key = serializers.CharField(max_length=128)


class AttendanceInput(StrictSerializer):
    member_id = serializers.IntegerField(min_value=1)
    event_id = serializers.CharField(max_length=200)
    checked_in_at = serializers.DateTimeField()
    source_id = serializers.CharField(max_length=255)
    reason = serializers.CharField(max_length=4000)


class ReconciliationInput(StrictSerializer):
    member_id = serializers.IntegerField(min_value=1)
    historical_roo = ExactRooField(max_length=32)
    ledger_cutoff = serializers.IntegerField(min_value=0)
    reason = serializers.CharField(max_length=4000)
