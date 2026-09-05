"""Account-session Volunteer endpoints; permissions remain backend-owned."""

import hmac

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import Exists, Max, OuterRef, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from hospital.authentication import CustomJWTAuthentication
from community_chat.authentication import CommunityChatAccountAuthentication
from community_chat.models import CommunityChatDevice
from community_chat.throttles import CommunityChatScopedThrottle

from .access import (
    VolunteerError,
    capabilities,
    community_id,
    flag,
    linked_member,
    occurrence,
    public_source,
    require_capability,
)
from .models import (
    VolunteerAttendance,
    VolunteerMilestone,
    VolunteerOpportunity,
    VolunteerProject,
    VolunteerRecognition,
    VolunteerSourceReceipt,
)
from .policy import VERSION, levels, microroo, roo
from .serializers import (
    AttendanceInput,
    BatchInput,
    DecisionInput,
    DirectInput,
    OpportunityInput,
    ProjectInput,
    ReconciliationInput,
    RequestInput,
    RevisionInput,
    allowed_channels,
    contribution_dto,
    level_bonus_dto,
    member_dto,
    opportunity_dto,
    project_dto,
)
from .services import (
    active_policy,
    award_milestones,
    decision,
    journey,
    lock_member,
    request_recognition,
    revise_request,
    state_for,
)


def validated(serializer_class, request, *, partial=False):
    serializer = serializer_class(data=request.data, partial=partial)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def pagination_window(request):
    """Validate a bounded page while keeping its offset explicit."""
    try:
        offset = max(0, int(request.query_params.get("offset", 0)))
        limit = min(50, max(1, int(request.query_params.get("limit", 20))))
    except (TypeError, ValueError) as exc:
        raise VolunteerError("invalid_pagination") from exc
    return offset, limit


def page_response(request, rows, offset, limit):
    """Keep selected filters in same-path continuation URLs."""
    query = request.query_params.copy()
    query["offset"], query["limit"] = str(offset + limit), str(limit)
    return dict(
        results=rows[:limit],
        next=f"?{query.urlencode()}" if len(rows) > limit else None,
    )


def paginated(request, queryset, convert):
    offset, limit = pagination_window(request)
    rows = [convert(item) for item in queryset[offset : offset + limit + 1]]
    return page_response(request, rows, offset, limit)


def standalone_bonuses(viewer):
    """At most six paid level receipts not already attached to a contribution."""
    return VolunteerMilestone.objects.filter(
        community=community_id(),
        user=viewer,
        ledger__isnull=False,
        recognition__isnull=True,
        level_key__in=[level["key"] for level in levels()[1:]],
    ).select_related("user", "ledger")


def recognised_page(request, records):
    """Merge a bounded recognition window with at most six standalone bonuses."""
    offset, limit = pagination_window(request)
    bonuses = list(standalone_bonuses(request.user))
    start = max(0, offset - len(bonuses))
    window = list(records.order_by("-created_at", "-pk")[start : offset + limit + 1])
    combined = [(row.created_at, str(row.pk), row, contribution_dto) for row in window]
    combined.extend(
        (row.ledger.created_at, str(row.pk), row, level_bonus_dto) for row in bonuses
    )
    combined.sort(key=lambda row: (row[0], row[1]), reverse=True)
    selected = combined[offset - start : offset - start + limit + 1]
    return page_response(
        request,
        [convert(row, request.user) for _, _, row, convert in selected],
        offset,
        limit,
    )


def opportunities(*, include_archived=False):
    records = VolunteerOpportunity.objects.filter(
        community=community_id(),
        audience="community",
        source__channel_id__in=allowed_channels(),
    ).select_related("guide", "reviewer", "project")
    return records if include_archived else records.exclude(status="archived")


def recognition(record_id, viewer, *, manage=False):
    records = VolunteerRecognition.objects.filter(
        community=community_id()
    ).select_related("user", "reviewer", "opportunity", "ledger")
    if not manage:
        records = records.filter(user=viewer)
    else:
        require_capability(viewer, "can_review")
    return get_object_or_404(records, pk=record_id)


def contribution_detail(record_id, viewer, *, manage=False):
    """Read owned receipt detail, with an explicit reviewer permission boundary."""
    if manage:
        require_capability(viewer, "can_review")
    records = VolunteerRecognition.objects.filter(community=community_id())
    if not manage:
        records = records.filter(user=viewer)
    record = (
        records.select_related("user", "reviewer", "opportunity", "ledger")
        .filter(pk=record_id)
        .first()
    )
    if record is not None:
        return contribution_dto(record, viewer)
    bonuses = VolunteerMilestone.objects.filter(
        community=community_id(),
        ledger__isnull=False,
        recognition__isnull=True,
        level_key__in=[level["key"] for level in levels()[1:]],
    ).select_related("user", "ledger")
    if not manage:
        bonuses = bonuses.filter(user=viewer)
    return level_bonus_dto(get_object_or_404(bonuses, pk=record_id), viewer)


class VolunteerView(APIView):
    """Use existing account-session and JWT origin/authentication contracts."""

    authentication_classes = (
        CommunityChatAccountAuthentication,
        CustomJWTAuthentication,
    )
    permission_classes = (IsAuthenticated,)
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        if not request.user.is_active:
            raise VolunteerError("not_authorised", 403)
        if not flag("enabled"):
            raise VolunteerError("volunteer_disabled", 404)

    def handle_exception(self, exc):
        if isinstance(exc, VolunteerError):
            return Response({"error": exc.code, "outcome": exc.code}, status=exc.status)
        if isinstance(exc, IntegrityError):
            return Response({"error": "conflict", "outcome": "conflict"}, status=409)
        return super().handle_exception(exc)


class JourneyView(VolunteerView):
    def get(self, request):
        return Response(journey(request.user))


class PolicyView(VolunteerView):
    def get(self, request):
        return Response(
            dict(
                version=VERSION, levels=levels(), actions=list(active_policy().values())
            )
        )


class OpportunitiesView(VolunteerView):
    def get(self, request, opportunity_id=None):
        records = opportunities(include_archived=opportunity_id is not None)
        if opportunity_id:
            return Response(
                opportunity_dto(
                    get_object_or_404(records, pk=opportunity_id), request.user
                )
            )
        records = records.filter(status="open").filter(
            Q(ends_at__isnull=True) | Q(ends_at__gt=timezone.now())
        )
        if request.query_params.get("kind") in ("event", "project"):
            records = records.filter(kind=request.query_params["kind"])
        if request.query_params.get("project_id"):
            records = records.filter(project_id=request.query_params["project_id"])
        return Response(
            paginated(
                request,
                records.order_by("starts_at", "created_at"),
                lambda item: opportunity_dto(item, request.user),
            )
        )


class ProjectsView(VolunteerView):
    def get(self, request, project_id=None):
        records = VolunteerProject.objects.filter(
            community=community_id(),
            published=True,
            source__channel_id__in=allowed_channels(),
        ).select_related("guide")
        if project_id:
            return Response(
                project_dto(get_object_or_404(records, pk=project_id), request.user)
            )
        return Response(
            paginated(
                request,
                records.order_by("title"),
                lambda item: project_dto(item, request.user),
            )
        )


class ContributionsView(VolunteerView):
    def get(self, request, contribution_id=None):
        if contribution_id:
            return Response(contribution_detail(contribution_id, request.user))
        selected = request.query_params.get("filter", "recognised")
        records = VolunteerRecognition.objects.filter(
            community=community_id(), user=request.user
        ).select_related("user", "reviewer")
        if selected == "conversations":
            # Only persisted authoritative replies, grouped by published root.
            roots = {
                (item.source.get("channel_id"), item.source.get("thread_root_id")): item
                for item in opportunities(include_archived=True)
            }
            if not roots:
                return Response(dict(results=[], next=None))
            scope = Q()
            for channel, root in roots:
                scope |= Q(source__channel_id=channel, source__thread_root_id=root)
            receipts = (
                VolunteerSourceReceipt.objects.filter(
                    scope,
                    community=community_id(),
                    actor=request.user,
                    kind="reply",
                )
                .exclude(metadata__invalidated=True)
                .exclude(metadata__service_account=True)
            )
            invalidations = (
                VolunteerSourceReceipt.objects.filter(
                    community=community_id(),
                    kind="invalidation",
                    source__source_id=OuterRef("source__source_id"),
                )
                .exclude(status="ineligible")
                .filter(
                    Q(actor=request.user, metadata__deletion_kind=5)
                    | Q(
                        metadata__deletion_kind=9005,
                        source__channel_id=OuterRef("source__channel_id"),
                    )
                )
            )
            receipts = receipts.annotate(invalidated=Exists(invalidations)).filter(
                invalidated=False
            )
            conversations = (
                receipts.values("source__channel_id", "source__thread_root_id")
                .annotate(latest=Max("occurred_at"))
                .order_by("-latest", "source__thread_root_id")
            )

            def conversation_dto(group):
                key = (group["source__channel_id"], group["source__thread_root_id"])
                opportunity = roots[key]
                receipt = (
                    receipts.filter(
                        source__channel_id=key[0], source__thread_root_id=key[1]
                    )
                    .order_by("-occurred_at", "-created_at")
                    .first()
                )
                return dict(
                    id=str(receipt.pk),
                    action_key=opportunity.action_key,
                    title=opportunity.title,
                    member=member_dto(request.user),
                    opportunity_id=str(opportunity.pk),
                    source=receipt.source,
                    status="conversation",
                    note="You joined the conversation",
                    evidence="",
                    reviewer=member_dto(opportunity.reviewer),
                    reward_roo="0",
                    credit_status="not_awarded",
                    reward_min_roo="0",
                    reward_max_roo="0",
                    definition_of_done="",
                    bonus_roo="0",
                    occurred_at=receipt.occurred_at.isoformat(),
                    created_at=receipt.created_at.isoformat(),
                    updated_at=receipt.updated_at.isoformat(),
                    version=1,
                    review_history=[],
                    can_resubmit=False,
                    can_withdraw=False,
                    can_review=False,
                )

            return Response(paginated(request, conversations, conversation_dto))
        if selected == "awaiting_review":
            records = records.filter(status__in=("pending", "needs_update"))
        elif selected == "recognised":
            records = records.filter(
                status__in=("approved", "reversed", "not_approved", "withdrawn")
            )
            return Response(recognised_page(request, records))
        else:
            raise VolunteerError("invalid_filter")
        return Response(
            paginated(
                request,
                records.order_by("-created_at"),
                lambda item: contribution_dto(item, request.user),
            )
        )


class RequestsView(VolunteerView):
    def post(self, request):
        record, outcome = request_recognition(
            request.user, validated(RequestInput, request)
        )
        return Response(
            dict(outcome=outcome, contribution=contribution_dto(record, request.user)),
            status=201 if outcome == "created" else 200,
        )


class ReviseView(VolunteerView):
    def post(self, request, contribution_id, operation):
        if operation not in ("resubmit", "withdraw"):
            raise VolunteerError("not_found", 404)
        payload = validated(RevisionInput, request)
        record = recognition(contribution_id, request.user)
        record = revise_request(
            record, request.user, withdraw=operation == "withdraw", **payload
        )
        return Response(
            dict(
                outcome=record.status,
                contribution=contribution_dto(record, request.user),
            )
        )


class ReviewsView(VolunteerView):
    def get(self, request):
        require_capability(request.user, "can_review")
        records = VolunteerRecognition.objects.filter(
            community=community_id(), status__in=("pending", "needs_update")
        ).select_related("user", "reviewer")
        if request.query_params.get("scope", "mine") != "all":
            records = records.filter(
                Q(reviewer=request.user)
                | Q(reviewer__isnull=True)
                | Q(reviewer__is_active=False)
            )
        return Response(
            paginated(
                request,
                records.order_by("created_at"),
                lambda item: contribution_dto(item, request.user),
            )
        )


class DecisionView(VolunteerView):
    def get(self, request, contribution_id):
        return Response(contribution_detail(contribution_id, request.user, manage=True))

    def post(self, request, contribution_id):
        record = recognition(contribution_id, request.user, manage=True)
        record, outcome = decision(
            record, request.user, validated(DecisionInput, request)
        )
        return Response(
            dict(outcome=outcome, contribution=contribution_dto(record, request.user))
        )


class DirectRecognitionView(VolunteerView):
    def post(self, request):
        require_capability(request.user, "can_review")
        payload = validated(DirectInput, request)
        user = linked_member(payload.pop("member_id"))
        if user.pk == request.user.pk:
            raise VolunteerError("self_approval_forbidden", 403)
        with transaction.atomic():
            record, outcome = request_recognition(user, payload, actor=request.user)
            if record.status != "approved":
                review = dict(
                    decision="approve",
                    note=payload["feedback"],
                    version=record.version,
                    idempotency_key=payload["idempotency_key"],
                )
                if "reward_roo" in payload:
                    review["reward_roo"] = payload["reward_roo"]
                record, outcome = decision(record, request.user, review)
        return Response(
            dict(outcome=outcome, contribution=contribution_dto(record, request.user))
        )


class EventRecognitionsView(VolunteerView):
    def post(self, request, event_id):
        require_capability(request.user, "can_review")
        payload = validated(BatchInput, request)
        opportunity = get_object_or_404(
            opportunities(include_archived=True), event_id=event_id, kind="event"
        )
        result = []
        if len({row["member_id"] for row in payload["recipients"]}) != len(
            payload["recipients"]
        ):
            raise VolunteerError("duplicate_recipient")
        for recipient in payload["recipients"]:
            try:
                with transaction.atomic():
                    user = linked_member(recipient["member_id"])
                    record, outcome = request_recognition(
                        user,
                        dict(
                            action_key="volunteer_event",
                            opportunity_id=opportunity.pk,
                            source=opportunity.source,
                            note=recipient["note"],
                            reward_roo=recipient["reward_roo"],
                            idempotency_key=f"batch:{payload['idempotency_key']}:{user.pk}",
                        ),
                        actor=request.user,
                    )
                    if record.status != "approved":
                        record, outcome = decision(
                            record,
                            request.user,
                            dict(
                                decision="approve",
                                note=recipient["note"],
                                reward_roo=recipient["reward_roo"],
                                version=record.version,
                                idempotency_key=f"{payload['idempotency_key']}:{user.pk}",
                            ),
                        )
                    result.append(
                        dict(
                            member_id=str(user.pk),
                            outcome=outcome,
                            contribution=contribution_dto(record, request.user),
                        )
                    )
            except VolunteerError as exc:
                result.append(
                    dict(
                        member_id=str(recipient["member_id"]),
                        outcome=exc.code,
                        error=exc.code,
                    )
                )
        return Response(dict(results=result))


class MembersView(VolunteerView):
    def get(self, request):
        require_capability(request.user, "can_review")
        if request.query_params.get("public_key"):
            device = (
                CommunityChatDevice.objects.select_related("user")
                .filter(
                    public_key=request.query_params["public_key"],
                    status="verified",
                    user__is_active=True,
                )
                .first()
            )
            return Response(
                dict(results=[member_dto(device.user)] if device else [], next=None)
            )
        query = str(request.query_params.get("q", "")).strip()
        if len(query) < 2:
            return Response(dict(results=[], next=None))
        users = (
            get_user_model()
            .objects.filter(
                is_active=True,
                pk__in=CommunityChatDevice.objects.filter(status="verified").values(
                    "user_id"
                ),
            )
            .filter(Q(first_name__icontains=query) | Q(last_name__icontains=query))
            .order_by("first_name", "last_name")[:20]
        )
        return Response(dict(results=[member_dto(user) for user in users], next=None))


class ManageOpportunitiesView(VolunteerView):
    def get(self, request, opportunity_id=None):
        require_capability(request.user, "can_publish")
        records = opportunities(include_archived=True)
        if opportunity_id:
            return Response(
                opportunity_dto(
                    get_object_or_404(records, pk=opportunity_id), request.user
                )
            )
        return Response(
            paginated(
                request,
                records.order_by("-created_at"),
                lambda item: opportunity_dto(item, request.user),
            )
        )

    @transaction.atomic
    def post(self, request):
        require_capability(request.user, "can_publish")
        payload = validated(OpportunityInput, request)
        record = self._save(request, payload)
        return Response(opportunity_dto(record, request.user), status=201)

    @transaction.atomic
    def patch(self, request, opportunity_id):
        require_capability(request.user, "can_publish")
        payload = validated(OpportunityInput, request, partial=True)
        record = get_object_or_404(
            VolunteerOpportunity.objects.select_for_update(),
            community=community_id(),
            pk=opportunity_id,
        )
        if payload.pop("version", None) != record.version:
            raise VolunteerError("conflict", 409)
        record = self._save(request, payload, record)
        return Response(opportunity_dto(record, request.user))

    def _save(self, request, payload, record=None):
        payload.pop("version", None)
        record = record or VolunteerOpportunity(community=community_id())
        if "reward_roo" in payload:
            record.reward_microroo = microroo(payload.pop("reward_roo"))
        if "reward_max_roo" in payload:
            record.reward_max_microroo = microroo(payload.pop("reward_max_roo"))
        for key, value in payload.items():
            setattr(record, key, value if key != "event_id" else value or "")
        record.guide = linked_member(record.guide_id)
        record.reviewer = linked_member(record.reviewer_id)
        require_capability(record.reviewer, "can_review")
        record.source = public_source(record.source, thread_required=True)
        action = active_policy().get(record.action_key)
        if action is None or action["verification"] != "human":
            raise VolunteerError("invalid_action")
        lower = (
            6_000_000
            if record.action_key == "fix_bug"
            else microroo(action["reward_roo"])
        )
        if (
            not lower
            <= record.reward_microroo
            <= record.reward_max_microroo
            <= microroo(action["reward_max_roo"])
        ):
            raise VolunteerError("invalid_reward")
        if record.kind == "event":
            if (
                record.action_key != "volunteer_event"
                or not record.event_id
                or not record.starts_at
                or not record.ends_at
                or record.ends_at <= record.starts_at
            ):
                raise VolunteerError("invalid_event")
        elif (
            record.action_key == "volunteer_event"
            or record.event_id
            or record.reward_microroo != record.reward_max_microroo
        ):
            raise VolunteerError("invalid_project_opportunity")
        if (
            record.project_id
            and not VolunteerProject.objects.filter(
                pk=record.project_id, community=community_id(), published=True
            ).exists()
        ):
            raise VolunteerError("project_unavailable", 404)
        if not record._state.adding:
            record.version += 1
        record.save()
        return record


class ManageProjectsView(VolunteerView):
    def get(self, request, project_id=None):
        require_capability(request.user, "can_publish")
        records = VolunteerProject.objects.filter(
            community=community_id(), source__channel_id__in=allowed_channels()
        ).select_related("guide")
        if project_id:
            return Response(
                project_dto(get_object_or_404(records, pk=project_id), request.user)
            )
        return Response(
            paginated(
                request,
                records.order_by("title"),
                lambda item: project_dto(item, request.user),
            )
        )

    @transaction.atomic
    def post(self, request):
        require_capability(request.user, "can_publish")
        record = self._save(validated(ProjectInput, request))
        return Response(project_dto(record, request.user), status=201)

    @transaction.atomic
    def patch(self, request, project_id):
        require_capability(request.user, "can_publish")
        payload = validated(ProjectInput, request, partial=True)
        record = get_object_or_404(
            VolunteerProject.objects.select_for_update(),
            pk=project_id,
            community=community_id(),
        )
        if payload.pop("version", None) != record.version:
            raise VolunteerError("conflict", 409)
        return Response(project_dto(self._save(payload, record), request.user))

    def _save(self, payload, record=None):
        payload.pop("version", None)
        record = record or VolunteerProject(community=community_id())
        for key, value in payload.items():
            setattr(record, key, value)
        record.guide = linked_member(record.guide_id)
        record.source = public_source(record.source, thread_required=True)
        if not record._state.adding:
            record.version += 1
        record.save()
        return record


class AttendanceView(VolunteerView):
    @transaction.atomic
    def post(self, request):
        require_capability(request.user, "can_correct")
        payload = validated(AttendanceInput, request)
        user = lock_member(linked_member(payload["member_id"]))
        if user.pk == request.user.pk:
            raise VolunteerError("self_approval_forbidden", 403)
        when = occurrence(payload["checked_in_at"])
        audit = dict(
            actor_id=str(request.user.pk),
            source_id=payload["source_id"],
            checked_in_at=when.isoformat(),
            reason=payload["reason"],
            at=timezone.now().isoformat(),
        )
        record, created = VolunteerAttendance.objects.get_or_create(
            community=community_id(),
            user=user,
            event_id=payload["event_id"],
            defaults=dict(
                checked_in_at=when,
                source_id=payload["source_id"],
                verifier=request.user,
                reason=payload["reason"],
                audit_history=[audit],
            ),
        )
        if not created:
            record.checked_in_at, record.source_id, record.verifier, record.reason = (
                when,
                payload["source_id"],
                request.user,
                payload["reason"],
            )
            record.audit_history = [*record.audit_history, audit]
            record.save()
        from .receipts import process_receipt

        # An accountable organiser correction is trusted evidence, with the
        # same once-per-member outcome and award flags as a Luma check-in.
        receipt, _ = VolunteerSourceReceipt.objects.get_or_create(
            community=community_id(),
            source_key=f"attendance_correction:{record.pk}:{when.isoformat()}",
            defaults=dict(
                origin="organiser",
                kind="attendance",
                actor=user,
                source={"event_id": record.event_id, "source_id": record.source_id},
                metadata={"checked_in_at": when.isoformat()},
                occurred_at=when,
            ),
        )
        process_receipt(receipt)
        return Response(
            dict(
                outcome="verified",
                id=str(record.pk),
                member_id=str(user.pk),
                event_id=record.event_id,
                checked_in_at=when.isoformat(),
            )
        )


class ReconciliationView(VolunteerView):
    @transaction.atomic
    def post(self, request):
        require_capability(request.user, "can_correct")
        payload = validated(ReconciliationInput, request)
        user = lock_member(linked_member(payload["member_id"]))
        if user.pk == request.user.pk:
            raise VolunteerError("self_approval_forbidden", 403)
        state = state_for(user)
        if state.reconciled_by_id or state.historical_microroo not in (None, 0):
            raise VolunteerError("already_reconciled", 409)
        from roo.models import Ledger

        cutoff = payload["ledger_cutoff"]
        if cutoff < state.historical_ledger_cutoff or (
            cutoff and not Ledger.objects.filter(user=user, pk=cutoff).exists()
        ):
            raise VolunteerError("invalid_ledger_cutoff")
        state.historical_microroo = microroo(payload["historical_roo"])
        state.historical_ledger_cutoff = cutoff
        state.reconciled_by = request.user
        state.reconciliation_note = payload["reason"]
        state.reconciled_at = timezone.now()
        state.save()
        # Reconciliation never silently creates historical bonuses.
        return Response(dict(outcome="reconciled", journey=journey(user)))


class ReceiptView(APIView):
    """A separate service credential, never accepted as a member session."""

    authentication_classes = ()
    permission_classes = ()
    throttle_classes = (CommunityChatScopedThrottle,)
    community_chat_throttle_scope = "community_chat_home"

    def post(self, request):
        token = getattr(settings, "COMMUNITY_CHAT_VOLUNTEER_RECEIPT_TOKEN", "")
        supplied = request.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(supplied, f"Bearer {token}"):
            return Response({"error": "not_authorised"}, status=403)
        if not flag("enabled"):
            return Response({"error": "volunteer_disabled"}, status=503)
        from .receipts import ingest_receipt

        try:
            receipt = ingest_receipt(request.data)
        except VolunteerError as exc:
            if exc.code == "member_unavailable":
                return Response(
                    dict(status="ignored", outcome="member_unavailable"), status=202
                )
            return Response({"error": exc.code}, status=exc.status)
        return Response(
            dict(
                id=str(receipt.pk),
                status=receipt.status,
                outcome=receipt.error or receipt.status,
            )
        )
