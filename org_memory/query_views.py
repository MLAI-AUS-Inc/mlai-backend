from __future__ import annotations

import hashlib

from django.conf import settings
from django.db import transaction
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .answering import (
    GroundedAnswerProviderError,
    answer_memory_query,
    search_memory_query,
)
from .authentication import OrgMemoryActorAuthentication
from .consolidation import entity_timeline, propose_correction
from .models import (
    MemoryChunk,
    MemoryClaim,
    MemoryClaimStatus,
    MemoryEntity,
    MemoryFeedback,
    MemoryFeedbackType,
    MemoryQueryLog,
    MemorySourceLifecycle,
    MemorySourceVersion,
)
from .permissions import (
    HasActiveOrgMemoryPilotAccess,
    HasCommitteePointsAdminClass,
    HasOrgMemoryCapability,
    HasOrgMemoryServiceScope,
)
from .retrieval import allowed_memory_classifications


class OrgMemoryQueryView(APIView):
    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (
        HasOrgMemoryServiceScope,
        HasOrgMemoryCapability,
        HasCommitteePointsAdminClass,
        HasActiveOrgMemoryPilotAccess,
    )
    required_service_scope = "org_memory.read"
    required_actor_capability = "view_general_memory"

    def _disabled(self):
        if settings.ORG_MEMORY_QUERY_API_ENABLED:
            return None
        return Response(
            {"detail": "The Admin Roo memory query API is not enabled."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    def _bound_request(self, request):
        actor = request.org_memory_actor
        data = request.data if isinstance(request.data, dict) else {}
        domain = str(data.get("organization_domain") or "").strip().casefold()
        if domain and domain != actor.organization.domain.casefold():
            return None, Response(
                {"detail": "organization_domain does not match the verified actor."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        channel_id = str(data.get("channel_id") or "").strip()
        thread_ts = str(data.get("thread_ts") or "").strip()
        if channel_id and channel_id != actor.slack_channel_id:
            return None, Response(
                {"detail": "channel_id does not match the verified request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if thread_ts and thread_ts != actor.slack_thread_ts:
            return None, Response(
                {"detail": "thread_ts does not match the verified request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        query = str(data.get("query") or "").strip()
        if not query or len(query) > 2000:
            return None, Response(
                {"detail": "query must contain between 1 and 2,000 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        as_of, error = _optional_datetime(data.get("as_of"), "as_of")
        if error:
            return None, error
        time_range = data.get("time_range") or {}
        if not isinstance(time_range, dict):
            return None, Response(
                {"detail": "time_range must be an object."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        time_start, error = _optional_datetime(time_range.get("start"), "time_range.start")
        if error:
            return None, error
        time_end, error = _optional_datetime(time_range.get("end"), "time_range.end")
        if error:
            return None, error
        if time_start and time_end and time_end <= time_start:
            return None, Response(
                {"detail": "time_range.end must be after time_range.start."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        answer_mode = str(data.get("answer_mode") or "auto").casefold()
        if answer_mode not in {"auto", "current", "historical", "timeline", "evidence"}:
            return None, Response(
                {"detail": "answer_mode is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            context_budget = int(
                data.get("max_context_tokens")
                or settings.ORG_MEMORY_ANSWER_MAX_CONTEXT_TOKENS
            )
        except (TypeError, ValueError):
            context_budget = 0
        if not 1000 <= context_budget <= 12000:
            return None, Response(
                {"detail": "max_context_tokens must be between 1,000 and 12,000."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return {
            "organization": actor.organization,
            "authorization": request.org_memory_authorization,
            "actor": actor,
            "query": query,
            "as_of": as_of,
            "time_start": time_start,
            "time_end": time_end,
            "answer_mode": answer_mode,
            "context_token_budget": context_budget,
        }, None


def _optional_datetime(value, field_name):
    if value in (None, ""):
        return None, None
    parsed = parse_datetime(str(value))
    if parsed is None or parsed.tzinfo is None:
        return None, Response(
            {"detail": f"{field_name} must be an ISO-8601 date-time with timezone."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return parsed, None


def _selection_payload(selection):
    return [
        {
            **item.payload,
            "score": round(item.candidate.score, 8),
            "citations": list(item.citations),
        }
        for item in selection.selected
    ]


def _query_log_is_currently_authorized(query_log, authorization) -> bool:
    classifications = allowed_memory_classifications(authorization)
    claim_ids = {str(value) for value in query_log.selected_claim_ids or ()}
    chunk_ids = {str(value) for value in query_log.selected_chunk_ids or ()}
    allowed_claim_ids = {
        str(value)
        for value in MemoryClaim.objects.filter(
            pk__in=claim_ids,
            organization=query_log.organization,
            classification__in=classifications,
            evidence__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
            evidence__source__access_revoked_at__isnull=True,
            evidence__source_version__tombstoned_at__isnull=True,
            evidence__source_version__acl_snapshot__is_accessible=True,
            evidence__source_version__acl_snapshot__revoked_at__isnull=True,
        ).values_list("pk", flat=True)
    }
    allowed_chunk_ids = {
        str(value)
        for value in MemoryChunk.objects.filter(
            pk__in=chunk_ids,
            source_version__source__organization=query_log.organization,
            classification__in=classifications,
            source_version__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
            source_version__source__access_revoked_at__isnull=True,
            source_version__tombstoned_at__isnull=True,
            source_version__acl_snapshot__is_accessible=True,
            source_version__acl_snapshot__revoked_at__isnull=True,
        ).values_list("pk", flat=True)
    }
    citation_pairs = {
        (str(item.get("source_id") or ""), str(item.get("source_version_id") or ""))
        for item in query_log.citation_data or ()
        if item.get("source_id") and item.get("source_version_id")
    }
    allowed_citation_pairs = {
        (str(source_id), str(version_id))
        for source_id, version_id in MemorySourceVersion.objects.filter(
            pk__in={version_id for _source_id, version_id in citation_pairs},
            source__organization=query_log.organization,
            source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
            source__access_revoked_at__isnull=True,
            tombstoned_at__isnull=True,
            acl_snapshot__is_accessible=True,
            acl_snapshot__revoked_at__isnull=True,
        ).values_list("source_id", "pk")
    }
    return (
        claim_ids == allowed_claim_ids
        and chunk_ids == allowed_chunk_ids
        and citation_pairs == allowed_citation_pairs
    )


class OrgMemoryAnswerView(OrgMemoryQueryView):
    def post(self, request):
        disabled = self._disabled()
        if disabled:
            return disabled
        values, error = self._bound_request(request)
        if error:
            return error
        try:
            query_log, selection, answer = answer_memory_query(**values)
        except GroundedAnswerProviderError as exc:
            return Response(
                {
                    "detail": "Grounded answer generation is temporarily unavailable.",
                    "query_id": exc.query_id or None,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(
            {
                "query_id": str(query_log.pk),
                "audience": query_log.audience,
                "intent": selection.plan.to_dict(),
                "answer": answer["answer"],
                "confidence": answer["confidence"],
                "evidence_sufficiency": selection.sufficiency,
                "freshness": {
                    "as_of": (selection.plan.as_of or query_log.created_at).isoformat(),
                    "latest_evidence_at": answer.get("latest_evidence_at"),
                    "contains_stale_memory": "stale_memory" in selection.warnings,
                },
                "citations": answer["citations"],
                "warnings": list(selection.warnings),
                "suggested_follow_up": answer.get("suggested_follow_up"),
            }
        )


class OrgMemorySearchView(OrgMemoryQueryView):
    def post(self, request):
        disabled = self._disabled()
        if disabled:
            return disabled
        values, error = self._bound_request(request)
        if error:
            return error
        query_log, selection = search_memory_query(**values)
        return Response(
            {
                "query_id": str(query_log.pk),
                "audience": query_log.audience,
                "intent": selection.plan.to_dict(),
                "evidence_sufficiency": selection.sufficiency,
                "confidence": selection.confidence,
                "memories": _selection_payload(selection),
                "warnings": list(selection.warnings),
            }
        )


class OrgMemoryTimelineView(OrgMemoryQueryView):
    def get(self, request, entity_id):
        disabled = self._disabled()
        if disabled:
            return disabled
        actor = request.org_memory_actor
        authorization = request.org_memory_authorization
        classifications = allowed_memory_classifications(authorization)
        entity = MemoryEntity.objects.filter(
            pk=entity_id,
            organization=actor.organization,
            classification__in=classifications,
        ).first()
        if entity is None:
            return Response(
                {"detail": "Entity was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        from_at, error = _optional_datetime(request.query_params.get("from"), "from")
        if error:
            return error
        to_at, error = _optional_datetime(request.query_params.get("to"), "to")
        if error:
            return error
        if from_at and to_at and to_at <= from_at:
            return Response(
                {"detail": "to must be after from."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        kinds = tuple(
            value.strip()
            for value in str(request.query_params.get("kinds") or "").split(",")
            if value.strip()
        )
        include_superseded = str(
            request.query_params.get("include_superseded") or ""
        ).casefold() in {"1", "true", "yes"}
        allowed_statuses = [
            MemoryClaimStatus.ACTIVE,
            MemoryClaimStatus.STALE,
            MemoryClaimStatus.CONTRADICTED,
        ]
        if include_superseded:
            allowed_statuses.append(MemoryClaimStatus.SUPERSEDED)
        claims = entity_timeline(
            entity,
            from_at=from_at,
            to_at=to_at,
            kinds=kinds or None,
            include_superseded=include_superseded,
        ).filter(
            status__in=allowed_statuses,
            classification__in=classifications,
            evidence__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
            evidence__source__access_revoked_at__isnull=True,
            evidence__source_version__tombstoned_at__isnull=True,
            evidence__source_version__acl_snapshot__is_accessible=True,
            evidence__source_version__acl_snapshot__revoked_at__isnull=True,
        ).distinct()[:200]
        rows = []
        for claim in claims:
            evidence = claim.evidence.filter(
                source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
                source__access_revoked_at__isnull=True,
                source_version__tombstoned_at__isnull=True,
                source_version__acl_snapshot__is_accessible=True,
                source_version__acl_snapshot__revoked_at__isnull=True,
            ).select_related("source", "source_version")[:5]
            rows.append(
                {
                    "claim_id": str(claim.pk),
                    "kind": claim.kind,
                    "status": claim.status,
                    "statement": claim.statement,
                    "observed_at": claim.observed_at.isoformat() if claim.observed_at else None,
                    "valid_from": claim.valid_from.isoformat() if claim.valid_from else None,
                    "valid_until": claim.valid_until.isoformat() if claim.valid_until else None,
                    "citations": [
                        {
                            "provider": item.source.provider,
                            "label": item.source.title or item.source.source_type,
                            "source_url": item.source.canonical_url,
                            "occurred_at": (
                                item.source_version.occurred_at
                                or item.source_version.captured_at
                            ).isoformat(),
                            "locator": item.source_locator,
                        }
                        for item in evidence
                    ],
                }
            )
        return Response(
            {
                "entity": {
                    "id": str(entity.pk),
                    "name": entity.canonical_name,
                    "type": entity.entity_type,
                },
                "timeline": rows,
            }
        )


class OrgMemoryQueryTraceView(OrgMemoryQueryView):
    def get(self, request, query_id):
        disabled = self._disabled()
        if disabled:
            return disabled
        actor = request.org_memory_actor
        authorization = request.org_memory_authorization
        query_log = MemoryQueryLog.objects.filter(
            pk=query_id,
            organization=actor.organization,
        ).first()
        if query_log is None:
            return Response(
                {"detail": "Query trace was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if (
            query_log.requester_user_id != actor.user.pk
            and not authorization.has_capability("review_claims")
        ):
            return Response(
                {"detail": "Query trace access is denied."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not _query_log_is_currently_authorized(query_log, authorization):
            return Response(
                {"detail": "Query trace evidence is no longer authorised."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(
            {
                "query_id": str(query_log.pk),
                "created_at": query_log.created_at.isoformat(),
                "query": query_log.query,
                "query_plan": query_log.query_plan,
                "candidate_trace": query_log.candidate_trace,
                "selected_claim_ids": query_log.selected_claim_ids,
                "selected_chunk_ids": query_log.selected_chunk_ids,
                "answer": query_log.answer,
                "citations": query_log.citation_data,
                "warnings": query_log.warnings,
                "status": query_log.status,
                "evidence_sufficiency": query_log.evidence_sufficiency,
                "confidence": float(query_log.confidence),
                "versions": {
                    "selector": query_log.selector_version,
                    "embedding_model": query_log.embedding_model,
                    "embedding_version": query_log.embedding_version,
                    "answer_model": query_log.model_name,
                    "answerer": query_log.answerer_version,
                    "prompt": query_log.prompt_version,
                    "schema": query_log.schema_version,
                },
                "usage": {
                    "latency_ms": query_log.latency_ms,
                    "input_tokens": query_log.input_tokens,
                    "output_tokens": query_log.output_tokens,
                },
            }
        )


class OrgMemoryFeedbackView(OrgMemoryQueryView):
    @transaction.atomic
    def post(self, request):
        disabled = self._disabled()
        if disabled:
            return disabled
        actor = request.org_memory_actor
        data = request.data if isinstance(request.data, dict) else {}
        query_log = MemoryQueryLog.objects.filter(
            pk=data.get("query_id"),
            organization=actor.organization,
        ).first()
        if query_log is None:
            return Response(
                {"detail": "query_id was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if (
            query_log.requester_user_id != actor.user.pk
            and not request.org_memory_authorization.has_capability("review_claims")
        ):
            return Response(
                {"detail": "Feedback access is denied."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not _query_log_is_currently_authorized(
            query_log,
            request.org_memory_authorization,
        ):
            return Response(
                {"detail": "Query evidence is no longer authorised."},
                status=status.HTTP_403_FORBIDDEN,
            )
        feedback_type = str(data.get("feedback_type") or "").casefold()
        if feedback_type not in MemoryFeedbackType.values:
            return Response(
                {"detail": "feedback_type is invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        claim = None
        claim_id = str(data.get("claim_id") or "").strip()
        if claim_id:
            if claim_id not in query_log.selected_claim_ids:
                return Response(
                    {"detail": "claim_id was not selected for this query."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            claim = MemoryClaim.objects.filter(
                pk=claim_id,
                organization=actor.organization,
            ).first()
            if claim is None:
                return Response(
                    {"detail": "claim_id was not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )
        correction_text = str(data.get("correction_text") or "").strip()
        if len(correction_text) > 4000:
            return Response(
                {"detail": "correction_text cannot exceed 4,000 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if feedback_type == MemoryFeedbackType.INCORRECT and (
            claim is None or not correction_text
        ):
            return Response(
                {"detail": "Incorrect feedback requires a selected claim and correction_text."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        idempotency_key = hashlib.sha256(
            "|".join(
                (
                    str(actor.organization.pk),
                    actor.request_id,
                    str(query_log.pk),
                    str(claim.pk) if claim else "",
                    feedback_type,
                )
            ).encode("utf-8")
        ).hexdigest()
        existing = MemoryFeedback.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return Response(_feedback_payload(existing))
        correction = None
        if claim and correction_text:
            correction = propose_correction(
                original_claim=claim,
                correction_text=correction_text,
                requested_by=actor.user,
            )
        feedback = MemoryFeedback.objects.create(
            organization=actor.organization,
            query_log=query_log,
            claim=claim,
            user=actor.user,
            feedback_type=feedback_type,
            correction_text=correction_text,
            correction_proposal=correction,
            request_id=actor.request_id,
            idempotency_key=idempotency_key,
        )
        return Response(_feedback_payload(feedback), status=status.HTTP_201_CREATED)


def _feedback_payload(feedback):
    return {
        "feedback_id": str(feedback.pk),
        "status": "recorded",
        "correction_proposal_id": (
            str(feedback.correction_proposal_id)
            if feedback.correction_proposal_id
            else None
        ),
        "review_item_id": (
            str(feedback.correction_proposal.review_item_id)
            if feedback.correction_proposal_id
            else None
        ),
    }
