from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .actions import (
    AgentActionError,
    approve_action_proposal,
    create_action_proposal,
    execute_action_proposal,
    reject_action_proposal,
    reverse_action_proposal,
)
from .authentication import OrgMemoryActorAuthentication
from .models import AgentActionProposal, AgentActionStatus, AgentActionType
from .permissions import HasOrgMemoryCapability, HasOrgMemoryServiceScope


def _action_payload(proposal, *, include_content=False, include_events=False):
    payload = {
        "id": str(proposal.pk),
        "action_type": proposal.action_type,
        "target_system": proposal.target_system,
        "configuration_id": (
            str(proposal.configuration_id) if proposal.configuration_id else None
        ),
        "risk_level": proposal.risk_level,
        "requires_approval": proposal.requires_approval,
        "status": proposal.status,
        "input_hash": proposal.input_hash,
        "evidence_claim_ids": proposal.evidence_claim_ids,
        "evidence_source_ids": proposal.evidence_source_ids,
        "precondition_hash": proposal.precondition_hash,
        "preconditions_refreshed_at": proposal.preconditions_refreshed_at,
        "requested_by_id": proposal.requested_by_id,
        "requested_by_slack_id": proposal.requested_by_slack_id,
        "approved_by_id": proposal.approved_by_id,
        "approved_at": proposal.approved_at,
        "rejected_by_id": proposal.rejected_by_id,
        "rejected_at": proposal.rejected_at,
        "rejection_reason": proposal.rejection_reason,
        "executed_by_id": proposal.executed_by_id,
        "execution_attempts": proposal.execution_attempts,
        "executed_at": proposal.executed_at,
        "reversal_supported": proposal.reversal_supported,
        "reversed_by_id": proposal.reversed_by_id,
        "reversed_at": proposal.reversed_at,
        "ingestion_action_request_id": (
            str(proposal.ingestion_action_request_id)
            if proposal.ingestion_action_request_id
            else None
        ),
        "error_text": proposal.error_text,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
        "approval": {
            "required": proposal.requires_approval,
            "pending": proposal.status
            in {
                AgentActionStatus.AWAITING_APPROVAL,
                AgentActionStatus.STALE,
            },
            "approve_endpoint": f"/api/v1/org-memory/actions/{proposal.pk}/approve",
            "reject_endpoint": f"/api/v1/org-memory/actions/{proposal.pk}/reject",
        },
    }
    if include_content:
        payload.update(
            {
                "input_payload": proposal.input_payload,
                "precondition_snapshot": proposal.precondition_snapshot,
                "result_payload": proposal.result_payload,
            }
        )
    if include_events:
        payload["events"] = [
            {
                "id": str(event.pk),
                "event_type": event.event_type,
                "actor_user_id": event.actor_user_id,
                "request_id": event.request_id,
                "payload_hash": event.payload_hash,
                "metadata": event.metadata,
                "created_at": event.created_at,
            }
            for event in proposal.events.all()
        ]
    return payload


class OrgMemoryActionView(APIView):
    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (HasOrgMemoryServiceScope, HasOrgMemoryCapability)
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "org_memory_actions"
    required_service_scope = "org_memory.actions"
    required_actor_capability = "view_general_memory"

    def handle_exception(self, exc):
        if isinstance(exc, AgentActionError):
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().handle_exception(exc)

    def _guard(self, request):
        if not getattr(settings, "ORG_MEMORY_ACTIONS_ENABLED", False):
            return Response(
                {"detail": "The controlled action gateway is not enabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if not request.auth.principal.allows_surface("admin_roo"):
            return Response(
                {"detail": "Controlled actions are restricted to Admin Roo."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return None

    def _can_approve(self, request) -> bool:
        return request.org_memory_authorization.has_capability("approve_actions")

    def _get_action(self, request, proposal_id):
        proposal = (
            AgentActionProposal.objects.filter(
                pk=proposal_id,
                organization=request.org_memory_actor.organization,
            )
            .select_related(
                "configuration",
                "configuration__external_connection",
                "configuration__google_connection",
                "requested_by",
                "approved_by",
                "rejected_by",
                "executed_by",
                "reversed_by",
                "ingestion_action_request",
            )
            .first()
        )
        if proposal is None:
            return None
        if (
            proposal.requested_by_id != request.org_memory_actor.user.pk
            and not self._can_approve(request)
        ):
            return None
        return proposal

    def _approval_required(self, request):
        if self._can_approve(request):
            return None
        return Response(
            {"detail": "The acting user cannot approve controlled actions."},
            status=status.HTTP_403_FORBIDDEN,
        )


class OrgMemoryActionListCreateView(OrgMemoryActionView):
    def get(self, request):
        guarded = self._guard(request)
        if guarded:
            return guarded
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= 200:
            return Response(
                {"detail": "limit must be between 1 and 200."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows = AgentActionProposal.objects.filter(
            organization=request.org_memory_actor.organization,
        )
        if not self._can_approve(request):
            rows = rows.filter(requested_by=request.org_memory_actor.user)
        requested_status = str(request.query_params.get("status") or "").strip()
        if requested_status:
            if requested_status not in AgentActionStatus.values:
                return Response(
                    {"detail": "status is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(status=requested_status)
        return Response(
            {"actions": [_action_payload(row) for row in rows[:limit]]}
        )

    def post(self, request):
        guarded = self._guard(request)
        if guarded:
            return guarded
        data = request.data if isinstance(request.data, dict) else {}
        action_type = str(data.get("action_type") or "").strip()
        if action_type not in AgentActionType.values:
            raise AgentActionError("action_type is invalid or unsupported.")
        proposal, created = create_action_proposal(
            organization=request.org_memory_actor.organization,
            authorization=request.org_memory_authorization,
            actor=request.org_memory_actor,
            action_type=action_type,
            input_payload=data.get("input_payload"),
            idempotency_key=request.headers.get("Idempotency-Key"),
            configuration_id=data.get("configuration_id"),
            evidence_claim_ids=data.get("evidence_claim_ids") or [],
            evidence_source_ids=data.get("evidence_source_ids") or [],
            request_id=request.org_memory_actor.request_id,
        )
        return Response(
            {
                **_action_payload(proposal, include_content=True),
                "created": created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class OrgMemoryActionDetailView(OrgMemoryActionView):
    def get(self, request, proposal_id):
        guarded = self._guard(request)
        if guarded:
            return guarded
        proposal = self._get_action(request, proposal_id)
        if proposal is None:
            return Response(
                {"detail": "Action proposal was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            _action_payload(
                proposal,
                include_content=True,
                include_events=True,
            )
        )


class OrgMemoryActionApproveView(OrgMemoryActionView):
    def post(self, request, proposal_id):
        guarded = self._guard(request)
        if guarded:
            return guarded
        denied = self._approval_required(request)
        if denied:
            return denied
        proposal = self._get_action(request, proposal_id)
        if proposal is None:
            return Response(
                {"detail": "Action proposal was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        proposal, changed = approve_action_proposal(
            proposal=proposal,
            authorization=request.org_memory_authorization,
            actor=request.org_memory_actor,
            idempotency_key=request.headers.get("Idempotency-Key"),
            request_id=request.org_memory_actor.request_id,
        )
        return Response(
            {
                **_action_payload(proposal, include_content=True),
                "changed": changed,
            }
        )


class OrgMemoryActionRejectView(OrgMemoryActionView):
    def post(self, request, proposal_id):
        guarded = self._guard(request)
        if guarded:
            return guarded
        denied = self._approval_required(request)
        if denied:
            return denied
        proposal = self._get_action(request, proposal_id)
        if proposal is None:
            return Response(
                {"detail": "Action proposal was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        data = request.data if isinstance(request.data, dict) else {}
        proposal, changed = reject_action_proposal(
            proposal=proposal,
            actor=request.org_memory_actor,
            reason=data.get("reason"),
            idempotency_key=request.headers.get("Idempotency-Key"),
            request_id=request.org_memory_actor.request_id,
        )
        return Response(
            {
                **_action_payload(proposal, include_content=True),
                "changed": changed,
            }
        )


class OrgMemoryActionExecuteView(OrgMemoryActionView):
    def post(self, request, proposal_id):
        guarded = self._guard(request)
        if guarded:
            return guarded
        proposal = self._get_action(request, proposal_id)
        if proposal is None:
            return Response(
                {"detail": "Action proposal was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        proposal, changed = execute_action_proposal(
            proposal=proposal,
            authorization=request.org_memory_authorization,
            actor=request.org_memory_actor,
            idempotency_key=request.headers.get("Idempotency-Key"),
            request_id=request.org_memory_actor.request_id,
        )
        return Response(
            {
                **_action_payload(proposal, include_content=True),
                "changed": changed,
            }
        )


class OrgMemoryActionReverseView(OrgMemoryActionView):
    def post(self, request, proposal_id):
        guarded = self._guard(request)
        if guarded:
            return guarded
        denied = self._approval_required(request)
        if denied:
            return denied
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("confirm") is not True:
            raise AgentActionError("Action reversal requires confirm=true.")
        proposal = self._get_action(request, proposal_id)
        if proposal is None:
            return Response(
                {"detail": "Action proposal was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        proposal, changed = reverse_action_proposal(
            proposal=proposal,
            actor=request.org_memory_actor,
            idempotency_key=request.headers.get("Idempotency-Key"),
            request_id=request.org_memory_actor.request_id,
        )
        return Response(
            {
                **_action_payload(proposal, include_content=True),
                "changed": changed,
            }
        )
