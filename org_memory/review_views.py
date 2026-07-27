from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import OrgMemoryActorAuthentication
from .consolidation import (
    ConsolidationInvariantError,
    apply_correction,
    approve_consolidation,
    merge_entities,
    reject_consolidation,
    reject_correction,
    split_entity,
    transition_claim,
)
from .control_plane import SourceControlError, request_runtime_action
from .models import (
    MemoryActionType,
    MemoryClaim,
    MemoryConsolidationRun,
    MemoryCorrectionProposal,
    MemoryCurrentState,
    MemoryDerivedArtifactStatus,
    MemoryDigest,
    MemoryDigestItemEvidence,
    MemoryEntity,
    MemoryEvidence,
    MemoryExtractionRun,
    MemoryPublication,
    MemoryReviewItem,
    MemoryReviewSeverity,
    MemoryReviewStatus,
    MemoryReviewType,
    MemorySource,
    MemorySourceLifecycle,
    MemorySummary,
    MemorySummaryType,
)
from .permissions import (
    HasActiveOrgMemoryPilotAccess,
    HasOrgMemoryCapability,
    HasOrgMemoryServiceScope,
)
from .publication import (
    PublicationError,
    approve_publication,
    publication_source_claims,
    reject_publication,
)
from .retrieval import allowed_memory_classifications
from .review_summaries import (
    OPEN_REVIEW_STATUSES,
    REVIEW_QUEUE_TYPES,
    review_dashboard_snapshot,
)
from .source_views import IDEMPOTENCY_PATTERN, OrgMemorySourceControlView


def _bounded_limit(request, *, default=50, maximum=200):
    try:
        value = int(request.query_params.get("limit") or default)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= maximum else None


def _disabled_response():
    if settings.ORG_MEMORY_QUERY_API_ENABLED:
        return None
    return Response(
        {"detail": "The Admin Roo memory review API is not enabled."},
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


class OrgMemoryReviewView(APIView):
    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (
        HasOrgMemoryServiceScope,
        HasOrgMemoryCapability,
        HasActiveOrgMemoryPilotAccess,
    )
    required_service_scope = "org_memory.read"
    required_actor_capability = "review_claims"

    def handle_exception(self, exc):
        if isinstance(
            exc,
            (
                ConsolidationInvariantError,
                PublicationError,
                ReviewResolutionError,
            ),
        ):
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().handle_exception(exc)


class ReviewResolutionError(ValueError):
    pass


def _review_queryset(organization):
    return MemoryReviewItem.objects.filter(organization=organization).select_related(
        "target_content_type",
        "assigned_to",
        "resolved_by",
    )


def _review_payload(review, *, reason=None):
    payload = {
        "id": str(review.pk),
        "queue": next(
            (
                queue
                for queue, review_type in REVIEW_QUEUE_TYPES.items()
                if review_type == review.review_type
            ),
            "other",
        ),
        "review_type": review.review_type,
        "severity": review.severity,
        "status": review.status,
        "target_type": review.target_content_type.model,
        "target_id": review.target_object_id,
        "assigned_to_id": review.assigned_to_id,
        "due_at": review.due_at,
        "resolution": review.resolution,
        "resolved_by_id": review.resolved_by_id,
        "resolved_at": review.resolved_at,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


def _claims_for_target(target):
    if isinstance(target, MemoryClaim):
        return MemoryClaim.objects.filter(pk=target.pk)
    if isinstance(target, MemoryCurrentState):
        return MemoryClaim.objects.filter(pk=target.claim_id)
    if isinstance(target, MemoryConsolidationRun):
        return MemoryClaim.objects.filter(
            Q(pk=target.candidate_claim_id) | Q(pk=target.matched_claim_id)
        )
    if isinstance(target, MemoryCorrectionProposal):
        return MemoryClaim.objects.filter(
            Q(pk=target.original_claim_id) | Q(pk=target.replacement_claim_id)
        )
    if isinstance(target, MemoryExtractionRun):
        return target.claims.all()
    if isinstance(target, MemoryEntity):
        return MemoryClaim.objects.filter(
            Q(subject_entity=target) | Q(object_entity=target)
        )
    if isinstance(target, MemoryPublication):
        return publication_source_claims(target.source)
    return MemoryClaim.objects.none()


def _authorized_review_evidence(review, authorization):
    classifications = allowed_memory_classifications(authorization)
    target = review.target
    claims = _claims_for_target(target).filter(
        organization=review.organization,
        classification__in=classifications,
    )
    return (
        MemoryEvidence.objects.filter(
            claim__in=claims,
            source_version__classification__in=classifications,
            chunk__classification__in=classifications,
            source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
            source__access_revoked_at__isnull=True,
            source_version__tombstoned_at__isnull=True,
            source_version__acl_snapshot__is_accessible=True,
            source_version__acl_snapshot__revoked_at__isnull=True,
        )
        .select_related(
            "claim",
            "source",
            "source__configuration",
            "source__source_scope",
            "source_version",
            "chunk",
        )
        .order_by("claim_id", "source_id", "created_at")
    )


def _authorized_extraction_chunks(review, authorization):
    target = review.target
    if not isinstance(target, MemoryExtractionRun):
        return []
    version = target.source_version
    classifications = allowed_memory_classifications(authorization)
    if (
        version.classification not in classifications
        or version.tombstoned_at is not None
        or version.source.lifecycle_state != MemorySourceLifecycle.ACTIVE
        or version.source.access_revoked_at is not None
    ):
        return []
    acl = getattr(version, "acl_snapshot", None)
    if acl is None or not acl.is_accessible or acl.revoked_at is not None:
        return []
    return list(
        version.chunks.select_related(
            "source_version__source",
            "source_version__source__configuration",
            "source_version__source__source_scope",
        ).order_by("ordinal")[:50]
    )


def _authorized_review_sources(review, authorization):
    sources = {
        evidence.source_id: evidence.source
        for evidence in _authorized_review_evidence(review, authorization)
    }
    for chunk in _authorized_extraction_chunks(review, authorization):
        source = chunk.source_version.source
        sources[source.pk] = source
    return sources


def _target_payload(review, authorization):
    target = review.target
    classifications = allowed_memory_classifications(authorization)
    if target is None:
        return {"type": review.target_content_type.model, "id": review.target_object_id}
    payload = {
        "type": review.target_content_type.model,
        "id": str(target.pk),
    }
    if isinstance(target, MemoryClaim) and target.classification in classifications:
        payload.update(
            {
                "kind": target.kind,
                "statement": target.statement,
                "claim_status": target.status,
                "classification": target.classification,
            }
        )
    elif isinstance(target, MemoryConsolidationRun):
        payload.update(
            {
                "operation": target.operation,
                "consolidation_status": target.status,
            }
        )
        target_claims = list(_claims_for_target(target))
        if target_claims and all(
            claim.classification in classifications for claim in target_claims
        ):
            payload.update(
                {
                    "candidate_claim_id": str(target.candidate_claim_id),
                    "matched_claim_id": (
                        str(target.matched_claim_id)
                        if target.matched_claim_id
                        else None
                    ),
                }
            )
    elif isinstance(target, MemoryCorrectionProposal):
        payload["correction_status"] = target.status
        target_claims = list(_claims_for_target(target))
        if target_claims and all(
            claim.classification in classifications for claim in target_claims
        ):
            payload.update(
                {
                    "original_claim_id": str(target.original_claim_id),
                    "replacement_claim_id": (
                        str(target.replacement_claim_id)
                        if target.replacement_claim_id
                        else None
                    ),
                }
            )
    elif isinstance(target, MemoryExtractionRun):
        payload["extraction_status"] = target.status
        if target.source_version.classification in classifications:
            payload.update(
                {
                    "safety_flags": target.safety_flags,
                    "source_version_id": str(target.source_version_id),
                }
            )
    elif isinstance(target, MemoryEntity) and target.classification in classifications:
        payload.update(
            {
                "entity_type": target.entity_type,
                "canonical_name": target.canonical_name,
                "classification": target.classification,
            }
        )
    elif isinstance(target, MemoryPublication):
        target_claims = list(_claims_for_target(target))
        if target_claims and all(
            claim.classification in classifications for claim in target_claims
        ):
            payload.update(
                {
                    "publication_status": target.status,
                    "public_key": target.public_key,
                    "public_title": target.proposed_title,
                    "public_body": target.proposed_body,
                    "tags": target.proposed_tags,
                    "sensitivity_findings": target.sensitivity_findings,
                    "redaction_notes": target.redaction_notes,
                    "proposal_hash": target.proposal_hash,
                }
            )
    return payload


def _evidence_payload(evidence):
    return {
        "evidence_id": str(evidence.pk),
        "claim_id": str(evidence.claim_id),
        "claim_kind": evidence.claim.kind,
        "claim_status": evidence.claim.status,
        "statement": evidence.claim.statement,
        "evidence_role": evidence.evidence_role,
        "quote": evidence.quote,
        "source": {
            "id": str(evidence.source_id),
            "provider": evidence.source.provider,
            "source_type": evidence.source.source_type,
            "title": evidence.source.title,
            "canonical_url": evidence.source.canonical_url,
            "configuration_id": (
                str(evidence.source.configuration_id)
                if evidence.source.configuration_id
                else None
            ),
            "scope_external_id": (
                evidence.source.source_scope.external_id
                if evidence.source.source_scope_id
                else None
            ),
        },
        "source_version_id": str(evidence.source_version_id),
        "source_occurred_at": (
            evidence.source_version.occurred_at
            or evidence.source_version.captured_at
        ),
        "chunk_id": str(evidence.chunk_id),
        "locator": evidence.source_locator,
    }


def _chunk_evidence_payload(chunk):
    source = chunk.source_version.source
    return {
        "evidence_id": None,
        "claim_id": None,
        "claim_kind": None,
        "claim_status": None,
        "statement": "",
        "evidence_role": "quarantined_source_context",
        "quote": chunk.text[:2000],
        "source": {
            "id": str(source.pk),
            "provider": source.provider,
            "source_type": source.source_type,
            "title": source.title,
            "canonical_url": source.canonical_url,
            "configuration_id": (
                str(source.configuration_id) if source.configuration_id else None
            ),
            "scope_external_id": (
                source.source_scope.external_id if source.source_scope_id else None
            ),
        },
        "source_version_id": str(chunk.source_version_id),
        "source_occurred_at": (
            chunk.source_version.occurred_at
            or chunk.source_version.captured_at
        ),
        "chunk_id": str(chunk.pk),
        "locator": chunk.source_locator,
    }


class OrgMemoryReviewDashboardView(OrgMemoryReviewView):
    def get(self, request):
        disabled = _disabled_response()
        if disabled:
            return disabled
        return Response(
            review_dashboard_snapshot(
                organization=request.org_memory_actor.organization,
            )
        )


class OrgMemoryReviewListView(OrgMemoryReviewView):
    def get(self, request):
        disabled = _disabled_response()
        if disabled:
            return disabled
        limit = _bounded_limit(request)
        if limit is None:
            return Response(
                {"detail": "limit must be between 1 and 200."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows = _review_queryset(request.org_memory_actor.organization)
        queue_name = str(request.query_params.get("queue") or "").strip()
        if queue_name:
            review_type = REVIEW_QUEUE_TYPES.get(queue_name)
            if review_type is None:
                return Response(
                    {"detail": "queue is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(review_type=review_type)
        review_status = str(request.query_params.get("status") or "").strip()
        if review_status:
            if review_status not in MemoryReviewStatus.values:
                return Response(
                    {"detail": "status is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(status=review_status)
        else:
            rows = rows.filter(status__in=OPEN_REVIEW_STATUSES)
        severity = str(request.query_params.get("severity") or "").strip()
        if severity:
            if severity not in MemoryReviewSeverity.values:
                return Response(
                    {"detail": "severity is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(severity=severity)
        rows = rows.annotate(
            queue_priority=Case(
                When(severity="critical", then=Value(4)),
                When(severity="high", then=Value(3)),
                When(severity="normal", then=Value(2)),
                When(severity="low", then=Value(1)),
                default=Value(0),
                output_field=IntegerField(),
            )
        ).order_by("-queue_priority", "due_at", "created_at")[:limit]
        return Response({"reviews": [_review_payload(row) for row in rows]})


class OrgMemoryReviewDetailView(OrgMemoryReviewView):
    def get(self, request, review_id):
        disabled = _disabled_response()
        if disabled:
            return disabled
        review = _review_queryset(request.org_memory_actor.organization).filter(
            pk=review_id
        ).first()
        if review is None:
            return Response(
                {"detail": "Review item was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        evidence = list(
            _authorized_review_evidence(
                review,
                request.org_memory_authorization,
            )[:200]
        )
        chunk_evidence = _authorized_extraction_chunks(
            review,
            request.org_memory_authorization,
        )
        sources = _authorized_review_sources(
            review,
            request.org_memory_authorization,
        )
        return Response(
            {
                **_review_payload(
                    review,
                    reason=(
                        review.reason
                        if evidence
                        or chunk_evidence
                        or (
                            isinstance(review.target, MemoryEntity)
                            and review.target.classification
                            in allowed_memory_classifications(
                                request.org_memory_authorization
                            )
                        )
                        else "Restricted review context."
                    ),
                ),
                "target": _target_payload(
                    review,
                    request.org_memory_authorization,
                ),
                "evidence": [
                    *[_evidence_payload(item) for item in evidence],
                    *[_chunk_evidence_payload(item) for item in chunk_evidence],
                ],
                "reprocessable_sources": list(
                    {
                        source_id: {
                            "id": str(source_id),
                            "provider": source.provider,
                            "source_type": source.source_type,
                            "title": source.title,
                            "canonical_url": source.canonical_url,
                            "configuration_id": str(source.configuration_id),
                            "scope_external_id": source.source_scope.external_id,
                        }
                        for source_id, source in sources.items()
                        if source.configuration_id and source.source_scope_id
                    }.values()
                ),
            }
        )


class OrgMemoryReviewEvidenceView(OrgMemoryReviewView):
    def get(self, request, review_id):
        disabled = _disabled_response()
        if disabled:
            return disabled
        review = _review_queryset(request.org_memory_actor.organization).filter(
            pk=review_id
        ).first()
        if review is None:
            return Response(
                {"detail": "Review item was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        limit = _bounded_limit(request, default=100)
        if limit is None:
            return Response(
                {"detail": "limit must be between 1 and 200."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        evidence = _authorized_review_evidence(
            review,
            request.org_memory_authorization,
        )
        payloads = [_evidence_payload(item) for item in evidence[:limit]]
        if len(payloads) < limit:
            payloads.extend(
                _chunk_evidence_payload(item)
                for item in _authorized_extraction_chunks(
                    review,
                    request.org_memory_authorization,
                )[: limit - len(payloads)]
            )
        return Response(
            {
                "review_id": str(review.pk),
                "evidence": payloads,
            }
        )


def _resolution_claim_access(review, authorization) -> bool:
    allowed = set(allowed_memory_classifications(authorization))
    target = review.target
    claims = list(_claims_for_target(target))
    if claims and any(claim.classification not in allowed for claim in claims):
        return False
    if claims and MemoryEvidence.objects.filter(claim__in=claims).exclude(
        source_version__classification__in=allowed,
        chunk__classification__in=allowed,
    ).exists():
        return False
    if isinstance(target, MemoryEntity) and target.classification not in allowed:
        return False
    if (
        isinstance(target, MemoryExtractionRun)
        and target.source_version.classification not in allowed
    ):
        return False
    return True


def _terminal_resolution(review, *, idempotency_key):
    if review.status in OPEN_REVIEW_STATUSES:
        return None
    if (review.resolution or {}).get("idempotency_key") == idempotency_key:
        return review
    raise ReviewResolutionError("This review item has already been resolved.")


def _record_resolution(
    review,
    *,
    status_value,
    actor,
    idempotency_key,
    resolution,
):
    review.status = status_value
    review.resolved_by = actor
    review.resolved_at = timezone.now()
    review.resolution = {
        **dict(review.resolution or {}),
        **dict(resolution or {}),
        "idempotency_key": idempotency_key,
    }
    review.save(
        update_fields=(
            "status",
            "resolved_by",
            "resolved_at",
            "resolution",
            "updated_at",
        )
    )
    return review


class OrgMemoryReviewResolveView(OrgMemoryReviewView):
    @transaction.atomic
    def post(self, request, review_id):
        if request.data.get("confirm") is not True:
            raise ReviewResolutionError("Review resolution requires confirm=true.")
        idempotency_key = str(
            request.headers.get("Idempotency-Key") or ""
        ).strip()
        if not idempotency_key or not IDEMPOTENCY_PATTERN.fullmatch(
            idempotency_key
        ):
            raise ReviewResolutionError(
                "A valid Idempotency-Key header is required."
            )
        review = (
            _review_queryset(request.org_memory_actor.organization)
            .select_for_update()
            .filter(pk=review_id)
            .first()
        )
        if review is None:
            return Response(
                {"detail": "Review item was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        replay = _terminal_resolution(
            review,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            return Response(
                {**_review_payload(replay), "created": False}
            )
        if not _resolution_claim_access(
            review,
            request.org_memory_authorization,
        ):
            return Response(
                {"detail": "Review resolution access is denied."},
                status=status.HTTP_403_FORBIDDEN,
            )

        data = request.data if isinstance(request.data, dict) else {}
        decision = str(data.get("decision") or "").strip().casefold()
        reason = str(data.get("reason") or "").strip()
        if len(reason) > 1000:
            raise ReviewResolutionError("reason cannot exceed 1,000 characters.")
        actor = request.org_memory_actor.user
        target = review.target

        if review.review_type in {
            MemoryReviewType.CONTRADICTION,
            MemoryReviewType.CLAIM_ACTIVATION,
        }:
            run = (
                target
                if isinstance(target, MemoryConsolidationRun)
                else review.consolidation_runs.filter(
                    status="review_required"
                ).first()
            )
            if run is None:
                raise ReviewResolutionError(
                    "This review has no pending consolidation operation."
                )
            if decision == "approve":
                winner = None
                winner_id = str(data.get("winner_claim_id") or "").strip()
                if winner_id:
                    winner = MemoryClaim.objects.filter(
                        pk=winner_id,
                        organization=review.organization,
                        classification__in=allowed_memory_classifications(
                            request.org_memory_authorization
                        ),
                    ).first()
                    if winner is None:
                        raise ReviewResolutionError(
                            "winner_claim_id was not found or is not authorised."
                        )
                approve_consolidation(
                    run=run,
                    actor=actor,
                    winner_claim=winner,
                )
            elif decision == "reject":
                reject_consolidation(
                    run=run,
                    actor=actor,
                    reason=reason,
                )
            else:
                raise ReviewResolutionError(
                    "Consolidation decision must be approve or reject."
                )
            review.refresh_from_db()
            _record_resolution(
                review,
                status_value=review.status,
                actor=actor,
                idempotency_key=idempotency_key,
                resolution={
                    "decision": decision,
                    "reason": reason,
                    "consolidation_run_id": str(run.pk),
                    "winner_claim_id": str(
                        data.get("winner_claim_id") or ""
                    ),
                },
            )

        elif review.review_type == MemoryReviewType.CORRECTION:
            proposal = (
                target
                if isinstance(target, MemoryCorrectionProposal)
                else review.correction_proposals.first()
            )
            if proposal is None:
                raise ReviewResolutionError(
                    "This review has no correction proposal."
                )
            if decision == "approve":
                replacement_id = str(
                    data.get("replacement_claim_id") or ""
                ).strip()
                if replacement_id:
                    replacement = MemoryClaim.objects.filter(
                        pk=replacement_id,
                        organization=review.organization,
                        classification__in=allowed_memory_classifications(
                            request.org_memory_authorization
                        ),
                        evidence__isnull=False,
                    ).distinct().first()
                    if replacement is None:
                        raise ReviewResolutionError(
                            "replacement_claim_id was not found, authorised, and evidenced."
                        )
                    if MemoryEvidence.objects.filter(claim=replacement).exclude(
                        source_version__classification__in=allowed_memory_classifications(
                            request.org_memory_authorization
                        ),
                        chunk__classification__in=allowed_memory_classifications(
                            request.org_memory_authorization
                        ),
                    ).exists():
                        raise ReviewResolutionError(
                            "Replacement evidence classification is not authorised."
                        )
                    if replacement.pk == proposal.original_claim_id:
                        raise ReviewResolutionError(
                            "A correction must use a different replacement claim."
                        )
                    if (
                        proposal.replacement_claim_id
                        and proposal.replacement_claim_id != replacement.pk
                    ):
                        raise ReviewResolutionError(
                            "The correction already references another replacement claim."
                        )
                    proposal.replacement_claim = replacement
                    proposal.save(
                        update_fields=("replacement_claim", "updated_at")
                    )
                apply_correction(proposal=proposal, actor=actor)
            elif decision == "reject":
                reject_correction(
                    proposal=proposal,
                    actor=actor,
                    reason=reason,
                )
            else:
                raise ReviewResolutionError(
                    "Correction decision must be approve or reject."
                )
            review.refresh_from_db()
            _record_resolution(
                review,
                status_value=review.status,
                actor=actor,
                idempotency_key=idempotency_key,
                resolution={
                    "decision": decision,
                    "reason": reason,
                    "correction_proposal_id": str(proposal.pk),
                    "replacement_claim_id": str(
                        proposal.replacement_claim_id or ""
                    ),
                },
            )

        elif review.review_type == MemoryReviewType.ENTITY_MERGE:
            if decision == "merge":
                primary = MemoryEntity.objects.filter(
                    pk=data.get("primary_entity_id"),
                    organization=review.organization,
                    classification__in=allowed_memory_classifications(
                        request.org_memory_authorization
                    ),
                ).first()
                duplicate = MemoryEntity.objects.filter(
                    pk=data.get("duplicate_entity_id"),
                    organization=review.organization,
                    classification__in=allowed_memory_classifications(
                        request.org_memory_authorization
                    ),
                ).first()
                if primary is None or duplicate is None:
                    raise ReviewResolutionError(
                        "Both authorised entity IDs are required for a merge."
                    )
                _record_resolution(
                    review,
                    status_value=MemoryReviewStatus.APPROVED,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    resolution={"decision": decision, "reason": reason},
                )
                event = merge_entities(
                    primary=primary,
                    duplicate=duplicate,
                    actor=actor,
                    review_item=review,
                    reason=reason or "reviewed_entity_merge",
                )
            elif decision == "split":
                entity = MemoryEntity.objects.filter(
                    pk=data.get("entity_id"),
                    organization=review.organization,
                    classification__in=allowed_memory_classifications(
                        request.org_memory_authorization
                    ),
                ).first()
                if entity is None:
                    raise ReviewResolutionError(
                        "An authorised entity_id is required for a split."
                    )
                _record_resolution(
                    review,
                    status_value=MemoryReviewStatus.APPROVED,
                    actor=actor,
                    idempotency_key=idempotency_key,
                    resolution={"decision": decision, "reason": reason},
                )
                event = split_entity(
                    entity=entity,
                    actor=actor,
                    review_item=review,
                    reason=reason or "reviewed_entity_split",
                )
            else:
                raise ReviewResolutionError(
                    "Entity decision must be merge or split."
                )
            _record_resolution(
                review,
                status_value=MemoryReviewStatus.RESOLVED,
                actor=actor,
                idempotency_key=idempotency_key,
                resolution={
                    "decision": decision,
                    "reason": reason,
                    "entity_resolution_event_id": str(event.pk),
                },
            )

        elif review.review_type == MemoryReviewType.PUBLICATION:
            if not getattr(
                settings,
                "ORG_MEMORY_PUBLICATION_ENABLED",
                False,
            ):
                return Response(
                    {
                        "detail": (
                            "The public knowledge publication workflow is not enabled."
                        )
                    },
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            if (
                not request.org_memory_authorization.has_capability(
                    "publish_knowledge"
                )
                or not request.auth.principal.has_scope(
                    "org_memory.publish"
                )
            ):
                return Response(
                    {"detail": "Publication approval access is denied."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not isinstance(target, MemoryPublication):
                raise ReviewResolutionError(
                    "Publication review target is invalid."
                )
            if decision == "approve":
                item = approve_publication(
                    publication=target,
                    authorization=request.org_memory_authorization,
                    actor=actor,
                )
                resolution = {
                    "decision": decision,
                    "reason": reason,
                    "publication_id": str(target.pk),
                    "public_item_id": str(item.pk),
                    "public_revision": item.revision,
                }
                status_value = MemoryReviewStatus.APPROVED
            elif decision == "reject":
                reject_publication(
                    publication=target,
                    actor=actor,
                    reason=reason,
                )
                resolution = {
                    "decision": decision,
                    "reason": reason,
                    "publication_id": str(target.pk),
                }
                status_value = MemoryReviewStatus.REJECTED
            else:
                raise ReviewResolutionError(
                    "Publication decision must be approve or reject."
                )
            _record_resolution(
                review,
                status_value=status_value,
                actor=actor,
                idempotency_key=idempotency_key,
                resolution=resolution,
            )

        elif review.review_type == MemoryReviewType.STALE:
            if not isinstance(target, MemoryClaim):
                raise ReviewResolutionError(
                    "Stale review target is not a claim."
                )
            if decision == "retract":
                if target.status != "stale":
                    raise ReviewResolutionError(
                        "Only a currently stale claim can be retracted here."
                    )
                transition_claim(
                    claim=target,
                    to_status="retracted",
                    reason=reason or "stale_claim_retracted",
                    actor=actor,
                    review_item=review,
                )
            elif decision not in {"acknowledge", "resolve"}:
                raise ReviewResolutionError(
                    "Stale decision must be acknowledge or retract."
                )
            _record_resolution(
                review,
                status_value=MemoryReviewStatus.RESOLVED,
                actor=actor,
                idempotency_key=idempotency_key,
                resolution={"decision": decision, "reason": reason},
            )

        else:
            if decision not in {"resolve", "reject"}:
                raise ReviewResolutionError(
                    "Review decision must be resolve or reject."
                )
            _record_resolution(
                review,
                status_value=(
                    MemoryReviewStatus.RESOLVED
                    if decision == "resolve"
                    else MemoryReviewStatus.REJECTED
                ),
                actor=actor,
                idempotency_key=idempotency_key,
                resolution={"decision": decision, "reason": reason},
            )

        review.refresh_from_db()
        return Response({**_review_payload(review), "created": True})


class OrgMemoryReviewReprocessView(OrgMemorySourceControlView):
    def post(self, request, review_id):
        if request.data.get("confirm") is not True:
            raise SourceControlError("Review reprocessing requires confirm=true.")
        review = _review_queryset(request.org_memory_actor.organization).filter(
            pk=review_id
        ).first()
        if review is None:
            raise SourceControlError("Review item was not found.", code="not_found")
        sources = _authorized_review_sources(
            review,
            request.org_memory_authorization,
        )
        source_ids = set(sources)
        requested_source_id = str(request.data.get("source_id") or "").strip()
        if requested_source_id:
            source = MemorySource.objects.filter(
                pk=requested_source_id,
                organization=request.org_memory_actor.organization,
            ).first()
            if source is None or source.pk not in source_ids:
                raise SourceControlError(
                    "source_id is not authorised evidence for this review."
                )
        else:
            if len(source_ids) != 1:
                raise SourceControlError(
                    "source_id is required when a review has multiple evidence sources."
                )
            source = sources[next(iter(source_ids))]
        if source.configuration_id is None or source.source_scope_id is None:
            raise SourceControlError(
                "This source is not attached to a reprocessable selected scope."
            )
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        if key and not IDEMPOTENCY_PATTERN.fullmatch(key):
            raise SourceControlError("Idempotency-Key is invalid.")
        key = key or f"review:{review.pk}:source:{source.pk}"[:128]
        action, created = request_runtime_action(
            source.configuration,
            action=MemoryActionType.REPROCESS,
            actor=request.org_memory_actor,
            authorization=request.org_memory_authorization,
            request_id=request.org_memory_actor.request_id,
            idempotency_key=key,
            scope_external_ids=[source.source_scope.external_id],
        )
        return Response(
            {
                "id": str(action.pk),
                "action": action.action,
                "status": action.status,
                "created": created,
                "review_id": str(review.pk),
                "source_id": str(source.pk),
                "requested_at": action.requested_at,
            },
            status=status.HTTP_202_ACCEPTED,
        )


class OrgMemoryDerivedArtifactView(APIView):
    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (
        HasOrgMemoryServiceScope,
        HasOrgMemoryCapability,
        HasActiveOrgMemoryPilotAccess,
    )
    required_service_scope = "org_memory.read"
    required_actor_capability = "view_general_memory"

    def _classifications(self, request):
        return set(allowed_memory_classifications(request.org_memory_authorization))

    def _authorized(self, request, artifact):
        return set(artifact.required_classifications or ()).issubset(
            self._classifications(request)
        )


def _summary_payload(summary, *, include_lineage=False):
    payload = {
        "id": str(summary.pk),
        "summary_type": summary.summary_type,
        "subject_key": summary.subject_key,
        "title": summary.title,
        "body": summary.body,
        "status": summary.status,
        "window_start": summary.window_start,
        "window_end": summary.window_end,
        "required_classifications": summary.required_classifications,
        "source_report_id": str(summary.source_report_id),
        "parent_id": str(summary.parent_id) if summary.parent_id else None,
        "fingerprint": summary.fingerprint,
        "structured_data": summary.structured_data,
        "generated_at": summary.generated_at,
    }
    if include_lineage:
        claims = {
            link.claim_id: {
                "claim_id": str(link.claim_id),
                "ordinal": link.ordinal,
                "kind": link.claim.kind,
                "status": link.claim.status,
                "statement": link.claim.statement,
                "classification": link.claim.classification,
                "evidence": [],
            }
            for link in summary.claim_links.select_related("claim").order_by("ordinal")
        }
        for link in summary.evidence_links.select_related(
            "evidence__claim",
            "evidence__source",
            "evidence__source_version",
            "evidence__chunk",
        ):
            if link.evidence.claim_id in claims:
                claims[link.evidence.claim_id]["evidence"].append(
                    _evidence_payload(link.evidence)
                )
        payload["lineage"] = list(claims.values())
    return payload


def _summary_currently_authorized(summary):
    return not summary.claim_links.exclude(
        claim__status="active",
    ).exists() and not summary.evidence_links.exclude(
        evidence__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        evidence__source__access_revoked_at__isnull=True,
        evidence__source_version__tombstoned_at__isnull=True,
        evidence__source_version__acl_snapshot__is_accessible=True,
        evidence__source_version__acl_snapshot__revoked_at__isnull=True,
    ).exists()


class OrgMemorySummaryListView(OrgMemoryDerivedArtifactView):
    def get(self, request):
        disabled = _disabled_response()
        if disabled:
            return disabled
        limit = _bounded_limit(request)
        if limit is None:
            return Response(
                {"detail": "limit must be between 1 and 200."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows = MemorySummary.objects.filter(
            organization=request.org_memory_actor.organization,
            is_current=True,
            status__in=(
                MemoryDerivedArtifactStatus.READY,
                MemoryDerivedArtifactStatus.EMPTY,
            ),
        ).select_related("source_report", "parent")
        summary_type = str(request.query_params.get("type") or "").strip()
        if summary_type:
            if summary_type not in MemorySummaryType.values:
                return Response(
                    {"detail": "type is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(summary_type=summary_type)
        visible = []
        for row in rows.iterator(chunk_size=200):
            if self._authorized(request, row) and _summary_currently_authorized(row):
                visible.append(row)
            if len(visible) == limit:
                break
        return Response(
            {"summaries": [_summary_payload(row) for row in visible]}
        )


class OrgMemorySummaryDetailView(OrgMemoryDerivedArtifactView):
    def get(self, request, summary_id):
        disabled = _disabled_response()
        if disabled:
            return disabled
        summary = (
            MemorySummary.objects.filter(
                pk=summary_id,
                organization=request.org_memory_actor.organization,
                is_current=True,
                status__in=(
                    MemoryDerivedArtifactStatus.READY,
                    MemoryDerivedArtifactStatus.EMPTY,
                ),
            )
            .select_related("source_report", "parent")
            .first()
        )
        if (
            summary is None
            or not self._authorized(request, summary)
            or not _summary_currently_authorized(summary)
        ):
            return Response(
                {"detail": "Summary was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(_summary_payload(summary, include_lineage=True))


def _digest_payload(digest, *, include_lineage=False):
    payload = {
        "id": str(digest.pk),
        "digest_type": digest.digest_type,
        "digest_date": digest.digest_date,
        "title": digest.title,
        "body": digest.body,
        "status": digest.status,
        "warnings": digest.warnings,
        "window_start": digest.window_start,
        "window_end": digest.window_end,
        "required_classifications": digest.required_classifications,
        "source_report_id": str(digest.source_report_id),
        "generated_at": digest.generated_at,
    }
    if include_lineage:
        items = []
        for item in digest.items.select_related("claim", "summary").order_by("ordinal"):
            items.append(
                {
                    "ordinal": item.ordinal,
                    "text": item.text,
                    "claim_id": str(item.claim_id),
                    "claim_kind": item.claim.kind,
                    "claim_status": item.claim.status,
                    "summary_id": str(item.summary_id) if item.summary_id else None,
                    "evidence": [
                        _evidence_payload(link.evidence)
                        for link in item.evidence_links.select_related(
                            "evidence__claim",
                            "evidence__source",
                            "evidence__source_version",
                            "evidence__chunk",
                        )
                    ],
                }
            )
        payload["items"] = items
    return payload


def _digest_currently_authorized(digest):
    return not digest.items.exclude(
        claim__status="active",
    ).exists() and not MemoryDigestItemEvidence.objects.filter(
        item__digest=digest,
    ).exclude(
        evidence__source__lifecycle_state=MemorySourceLifecycle.ACTIVE,
        evidence__source__access_revoked_at__isnull=True,
        evidence__source_version__tombstoned_at__isnull=True,
        evidence__source_version__acl_snapshot__is_accessible=True,
        evidence__source_version__acl_snapshot__revoked_at__isnull=True,
    ).exists()


class OrgMemoryDigestListView(OrgMemoryDerivedArtifactView):
    def get(self, request):
        disabled = _disabled_response()
        if disabled:
            return disabled
        limit = _bounded_limit(request)
        if limit is None:
            return Response(
                {"detail": "limit must be between 1 and 200."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows = MemoryDigest.objects.filter(
            organization=request.org_memory_actor.organization,
        ).select_related("source_report")
        visible = []
        for row in rows.iterator(chunk_size=200):
            if row.status == MemoryDerivedArtifactStatus.BLOCKED:
                visible.append(row)
            elif self._authorized(request, row) and _digest_currently_authorized(row):
                visible.append(row)
            if len(visible) == limit:
                break
        return Response({"digests": [_digest_payload(row) for row in visible]})


class OrgMemoryDigestDetailView(OrgMemoryDerivedArtifactView):
    def get(self, request, digest_id):
        disabled = _disabled_response()
        if disabled:
            return disabled
        digest = (
            MemoryDigest.objects.filter(
                pk=digest_id,
                organization=request.org_memory_actor.organization,
            )
            .select_related("source_report")
            .first()
        )
        if digest is None:
            return Response(
                {"detail": "Digest was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if digest.status != MemoryDerivedArtifactStatus.BLOCKED and (
            not self._authorized(request, digest)
            or not _digest_currently_authorized(digest)
        ):
            return Response(
                {"detail": "Digest was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            _digest_payload(
                digest,
                include_lineage=digest.status
                != MemoryDerivedArtifactStatus.BLOCKED,
            )
        )
