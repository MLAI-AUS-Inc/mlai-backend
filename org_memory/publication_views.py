from __future__ import annotations

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import OrgMemoryActorAuthentication
from .models import MemoryPublication, MemoryPublicationStatus
from .permissions import HasOrgMemoryCapability, HasOrgMemoryServiceScope
from .publication import (
    PublicationError,
    create_publication_candidate,
    resolve_publication_source,
    revoke_publication,
    submit_publication_for_review,
    update_publication_candidate,
)


def _publication_payload(publication, *, include_events=False):
    payload = {
        "id": str(publication.pk),
        "status": publication.status,
        "source_type": publication.source_content_type.model,
        "source_id": publication.source_object_id,
        "source_fingerprint": publication.source_fingerprint,
        "public_key": publication.public_key,
        "public_title": publication.proposed_title,
        "public_body": publication.proposed_body,
        "tags": publication.proposed_tags,
        "proposal_hash": publication.proposal_hash,
        "sensitivity_findings": publication.sensitivity_findings,
        "redaction_notes": publication.redaction_notes,
        "review_id": str(publication.review_item_id) if publication.review_item_id else None,
        "public_item": (
            {
                "item_id": str(publication.published_item_id),
                "revision": publication.published_item.revision,
                "status": publication.published_item.status,
                "published_at": publication.published_item.published_at,
            }
            if publication.published_item_id
            else None
        ),
        "proposed_by_id": publication.proposed_by_id,
        "redaction_confirmed_by_id": publication.redaction_confirmed_by_id,
        "redaction_confirmed_at": publication.redaction_confirmed_at,
        "approved_by_id": publication.approved_by_id,
        "approved_at": publication.approved_at,
        "revoked_by_id": publication.revoked_by_id,
        "revoked_at": publication.revoked_at,
        "revocation_reason": publication.revocation_reason,
        "created_at": publication.created_at,
        "updated_at": publication.updated_at,
    }
    if include_events:
        payload["events"] = [
            {
                "id": str(event.pk),
                "event_type": event.event_type,
                "actor_user_id": event.actor_user_id,
                "payload_hash": event.payload_hash,
                "metadata": event.metadata,
                "created_at": event.created_at,
            }
            for event in publication.events.all()
        ]
    return payload


class OrgMemoryPublicationView(APIView):
    authentication_classes = (OrgMemoryActorAuthentication,)
    permission_classes = (HasOrgMemoryServiceScope, HasOrgMemoryCapability)
    required_service_scope = "org_memory.publish"
    required_actor_capability = "publish_knowledge"

    def handle_exception(self, exc):
        if isinstance(exc, PublicationError):
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().handle_exception(exc)

    def _disabled(self):
        if getattr(settings, "ORG_MEMORY_PUBLICATION_ENABLED", False):
            return None
        return Response(
            {"detail": "The public knowledge publication workflow is not enabled."},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    def _get_publication(self, request, publication_id):
        return (
            MemoryPublication.objects.filter(
                pk=publication_id,
                organization=request.org_memory_actor.organization,
            )
            .select_related(
                "source_content_type",
                "review_item",
                "published_item",
                "proposed_by",
                "redaction_confirmed_by",
                "approved_by",
                "revoked_by",
            )
            .first()
        )


class OrgMemoryPublicationListCreateView(OrgMemoryPublicationView):
    def get(self, request):
        disabled = self._disabled()
        if disabled:
            return disabled
        try:
            limit = int(request.query_params.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 0
        if not 1 <= limit <= 200:
            return Response(
                {"detail": "limit must be between 1 and 200."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        rows = MemoryPublication.objects.filter(
            organization=request.org_memory_actor.organization,
        ).select_related(
            "source_content_type",
            "review_item",
            "published_item",
        )
        requested_status = str(request.query_params.get("status") or "").strip()
        if requested_status:
            if requested_status not in MemoryPublicationStatus.values:
                return Response(
                    {"detail": "status is invalid."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            rows = rows.filter(status=requested_status)
        return Response(
            {
                "publications": [
                    _publication_payload(row)
                    for row in rows[:limit]
                ]
            }
        )

    def post(self, request):
        disabled = self._disabled()
        if disabled:
            return disabled
        data = request.data if isinstance(request.data, dict) else {}
        source = resolve_publication_source(
            organization=request.org_memory_actor.organization,
            source_type=data.get("source_type"),
            source_id=data.get("source_id"),
        )
        publication, created = create_publication_candidate(
            organization=request.org_memory_actor.organization,
            source=source,
            authorization=request.org_memory_authorization,
            actor=request.org_memory_actor.user,
            idempotency_key=request.headers.get("Idempotency-Key"),
            public_key=data.get("public_key"),
            public_title=(
                data.get("public_title")
                if "public_title" in data
                else None
            ),
            public_body=(
                data.get("public_body")
                if "public_body" in data
                else None
            ),
            tags=data.get("tags") or [],
            redaction_notes=data.get("redaction_notes") or "",
        )
        review_created = False
        if data.get("submit_for_review") is True:
            publication, review_created = submit_publication_for_review(
                publication=publication,
                authorization=request.org_memory_authorization,
                actor=request.org_memory_actor.user,
                confirm_redacted=data.get("confirm_redacted") is True,
            )
        publication.refresh_from_db()
        return Response(
            {
                **_publication_payload(publication),
                "created": created,
                "review_created": review_created,
            },
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class OrgMemoryPublicationDetailView(OrgMemoryPublicationView):
    def get(self, request, publication_id):
        disabled = self._disabled()
        if disabled:
            return disabled
        publication = self._get_publication(request, publication_id)
        if publication is None:
            return Response(
                {"detail": "Publication was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(_publication_payload(publication, include_events=True))

    def patch(self, request, publication_id):
        disabled = self._disabled()
        if disabled:
            return disabled
        publication = self._get_publication(request, publication_id)
        if publication is None:
            return Response(
                {"detail": "Publication was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        data = request.data if isinstance(request.data, dict) else {}
        publication = update_publication_candidate(
            publication=publication,
            actor=request.org_memory_actor.user,
            public_title=data.get("public_title", publication.proposed_title),
            public_body=data.get("public_body", publication.proposed_body),
            tags=data.get("tags", publication.proposed_tags),
            redaction_notes=data.get(
                "redaction_notes",
                publication.redaction_notes,
            ),
        )
        return Response(_publication_payload(publication))


class OrgMemoryPublicationSubmitView(OrgMemoryPublicationView):
    def post(self, request, publication_id):
        disabled = self._disabled()
        if disabled:
            return disabled
        publication = self._get_publication(request, publication_id)
        if publication is None:
            return Response(
                {"detail": "Publication was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        data = request.data if isinstance(request.data, dict) else {}
        publication, created = submit_publication_for_review(
            publication=publication,
            authorization=request.org_memory_authorization,
            actor=request.org_memory_actor.user,
            confirm_redacted=data.get("confirm_redacted") is True,
        )
        publication.refresh_from_db()
        return Response(
            {
                **_publication_payload(publication),
                "created": created,
            }
        )


class OrgMemoryPublicationRevokeView(OrgMemoryPublicationView):
    def post(self, request, publication_id):
        disabled = self._disabled()
        if disabled:
            return disabled
        data = request.data if isinstance(request.data, dict) else {}
        if data.get("confirm") is not True:
            raise PublicationError("Publication revocation requires confirm=true.")
        publication = self._get_publication(request, publication_id)
        if publication is None:
            return Response(
                {"detail": "Publication was not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        publication, created = revoke_publication(
            publication=publication,
            actor=request.org_memory_actor.user,
            reason=data.get("reason") or "manual_publication_revocation",
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        publication.refresh_from_db()
        return Response(
            {
                **_publication_payload(publication),
                "created": created,
            }
        )
