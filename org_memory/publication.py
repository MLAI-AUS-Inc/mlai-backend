from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from .kernel import open_review_item
from .models import (
    MemoryClaim,
    MemoryClaimStatus,
    MemoryDerivedArtifactStatus,
    MemoryEvidence,
    MemoryPublication,
    MemoryPublicationEvent,
    MemoryPublicationEventType,
    MemoryPublicationStatus,
    MemoryReviewItem,
    MemoryReviewStatus,
    MemoryReviewType,
    MemorySourceLifecycle,
    MemorySummary,
    MemorySummaryClaim,
    MemorySummaryEvidence,
    PublicKnowledgeItem,
    PublicKnowledgeStatus,
)
from .retrieval import allowed_memory_classifications


PUBLICATION_SOURCE_MODELS = {
    "claim": MemoryClaim,
    "summary": MemorySummary,
}
PUBLICATION_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,255}$")
PUBLIC_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SENSITIVITY_PATTERNS = (
    (
        "credential_material",
        re.compile(
            r"(?i)(?:api[_ -]?key|client[_ -]?secret|password|bearer\s+[A-Za-z0-9._~+/-]{12,}|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY)"
        ),
    ),
    (
        "email_address",
        re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
    ),
    (
        "phone_number",
        re.compile(
            r"(?<!\w)(?:\+(?:\d[ -]?){8,15}\d|(?:\(?\d{2,4}\)?[ -]){2,3}\d{3,4})(?!\w)"
        ),
    ),
    (
        "slack_identifier",
        re.compile(r"\b[UTWGCD][A-Z0-9]{8,}\b"),
    ),
    (
        "internal_identifier",
        re.compile(
            r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b"
        ),
    ),
    (
        "private_source_link",
        re.compile(
            r"(?i)https?://(?:drive\.google\.com|docs\.google\.com|[^\s/]+\.slack\.com|(?:www\.)?notion\.so|linear\.app)/\S+"
        ),
    ),
    (
        "financial_identifier",
        re.compile(
            r"(?i)\b(?:iban|bank account|account number|credit card|routing number|bsb)\b"
        ),
    ),
)


class PublicationError(ValueError):
    pass


def _canonical_hash(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _normalized_tags(values: Iterable[str]) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise PublicationError("tags must be a list.")
    tags = []
    for value in values:
        tag = str(value or "").strip().casefold()
        if not tag:
            continue
        if len(tag) > 50:
            raise PublicationError("Each public knowledge tag is limited to 50 characters.")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > 20:
        raise PublicationError("Public knowledge is limited to 20 tags.")
    return tags


def _validated_public_key(value: str) -> str:
    public_key = str(value or "").strip().casefold()
    if (
        not public_key
        or len(public_key) > 160
        or not PUBLIC_KEY_PATTERN.fullmatch(public_key)
    ):
        raise PublicationError(
            "public_key must be a lower-case hyphenated slug up to 160 characters."
        )
    return public_key


def _validated_payload(*, title, body, tags) -> tuple[str, str, list[str]]:
    title = str(title or "").strip()
    body = str(body or "").strip()
    if not title or len(title) > 300:
        raise PublicationError("public_title must contain between 1 and 300 characters.")
    if not body or len(body) > 20000:
        raise PublicationError("public_body must contain between 1 and 20,000 characters.")
    return title, body, _normalized_tags(tags)


def scan_public_payload(*, title: str, body: str, tags=()) -> list[dict]:
    """Return finding codes only; never retain or echo the sensitive match."""

    content = f"{title}\n{body}\n{' '.join(str(value) for value in (tags or ()))}"
    return [
        {
            "code": code,
            "severity": "block",
            "message": "Remove or replace this sensitive data before publication.",
        }
        for code, pattern in _SENSITIVITY_PATTERNS
        if pattern.search(content)
    ]


def _blocked_classifications() -> set[str]:
    raw = getattr(
        settings,
        "ORG_MEMORY_PUBLICATION_BLOCKED_CLASSIFICATIONS",
        "executive,finance,people_sensitive,no_agent",
    )
    values = raw if isinstance(raw, (list, tuple, set)) else str(raw).split(",")
    return {str(value).strip() for value in values if str(value).strip()}


def publication_source_claims(source):
    if isinstance(source, MemoryClaim):
        return MemoryClaim.objects.filter(pk=source.pk)
    if isinstance(source, MemorySummary):
        return MemoryClaim.objects.filter(summary_links__summary=source).distinct()
    return MemoryClaim.objects.none()


def _source_evidence(source):
    return MemoryEvidence.objects.filter(
        claim__in=publication_source_claims(source)
    ).select_related(
        "claim",
        "source",
        "source_version",
        "source_version__acl_snapshot",
        "chunk",
    )


def _assert_source_publishable(*, source, organization, authorization) -> None:
    if not isinstance(source, tuple(PUBLICATION_SOURCE_MODELS.values())):
        raise PublicationError("Only an active claim or current summary can be published.")
    if source.organization_id != organization.pk:
        raise PublicationError("Publication source belongs to another organisation.")

    if isinstance(source, MemoryClaim):
        if source.status != MemoryClaimStatus.ACTIVE:
            raise PublicationError("Only an active claim can enter publication review.")
    else:
        if (
            not source.is_current
            or source.status != MemoryDerivedArtifactStatus.READY
        ):
            raise PublicationError("Only a current ready summary can enter publication review.")

    allowed = set(allowed_memory_classifications(authorization))
    blocked = _blocked_classifications()
    claims = list(publication_source_claims(source))
    if not claims:
        raise PublicationError("Publication source has no claim lineage.")
    if any(
        claim.status != MemoryClaimStatus.ACTIVE
        or claim.classification not in allowed
        or claim.classification in blocked
        for claim in claims
    ):
        raise PublicationError(
            "Publication source includes inactive, restricted, or non-publishable claims."
        )

    evidence = list(_source_evidence(source))
    if not evidence:
        raise PublicationError("Publication source has no exact evidence lineage.")
    claim_ids_with_evidence = {row.claim_id for row in evidence}
    if claim_ids_with_evidence != {claim.pk for claim in claims}:
        raise PublicationError("Every published claim requires exact evidence.")
    for row in evidence:
        acl = getattr(row.source_version, "acl_snapshot", None)
        if (
            row.source.lifecycle_state != MemorySourceLifecycle.ACTIVE
            or row.source.access_revoked_at is not None
            or row.source_version.tombstoned_at is not None
            or acl is None
            or not acl.is_accessible
            or acl.revoked_at is not None
            or row.source_version.classification not in allowed
            or row.chunk.classification not in allowed
            or row.source_version.classification in blocked
            or row.chunk.classification in blocked
        ):
            raise PublicationError(
                "Publication evidence is inaccessible, restricted, or non-publishable."
            )


def publication_source_fingerprint(source) -> str:
    claims = list(
        publication_source_claims(source).order_by("pk").values(
            "id",
            "status",
            "classification",
            "statement",
            "updated_at",
        )
    )
    evidence = list(
        _source_evidence(source).order_by("pk").values(
            "id",
            "claim_id",
            "quote_hash",
            "source_version__content_hash",
            "source_version__classification",
            "chunk__classification",
            "source__lifecycle_state",
            "source__access_revoked_at",
            "source_version__tombstoned_at",
            "source_version__acl_snapshot__fingerprint",
            "source_version__acl_snapshot__is_accessible",
            "source_version__acl_snapshot__revoked_at",
        )
    )
    source_state = {
        "type": source._meta.label_lower,
        "id": str(source.pk),
        "claims": claims,
        "evidence": evidence,
    }
    if isinstance(source, MemorySummary):
        source_state["summary"] = {
            "fingerprint": source.fingerprint,
            "status": source.status,
            "is_current": source.is_current,
            "updated_at": source.updated_at,
        }
    return _canonical_hash(source_state)


def _generated_candidate(source) -> tuple[str, str]:
    if isinstance(source, MemorySummary):
        return source.title, source.body
    subject = getattr(source.subject_entity, "canonical_name", "") if source.subject_entity_id else ""
    title = subject or source.get_kind_display()
    return title[:300], source.statement


def _proposal_hash(publication) -> str:
    return _canonical_hash(
        {
            "public_key": publication.public_key,
            "title": publication.proposed_title,
            "body": publication.proposed_body,
            "tags": publication.proposed_tags,
            "redaction_notes": publication.redaction_notes,
            "source_fingerprint": publication.source_fingerprint,
        }
    )


def _event(publication, event_type, *, actor=None, metadata=None):
    return MemoryPublicationEvent.objects.create(
        publication=publication,
        event_type=event_type,
        actor_user=actor,
        payload_hash=publication.proposal_hash,
        metadata=dict(metadata or {}),
    )


def resolve_publication_source(*, organization, source_type: str, source_id: str):
    model = PUBLICATION_SOURCE_MODELS.get(str(source_type or "").strip().casefold())
    if model is None:
        raise PublicationError("source_type must be claim or summary.")
    source = model.objects.filter(pk=source_id, organization=organization).first()
    if source is None:
        raise PublicationError("Publication source was not found.")
    return source


@transaction.atomic
def create_publication_candidate(
    *,
    organization,
    source,
    authorization,
    actor,
    idempotency_key: str,
    public_key: str,
    public_title=None,
    public_body=None,
    tags=None,
    redaction_notes="",
) -> tuple[MemoryPublication, bool]:
    key = str(idempotency_key or "").strip()
    if not PUBLICATION_IDEMPOTENCY_PATTERN.fullmatch(key):
        raise PublicationError("A valid Idempotency-Key header is required.")
    _assert_source_publishable(
        source=source,
        organization=organization,
        authorization=authorization,
    )
    generated_title, generated_body = _generated_candidate(source)
    title, body, normalized_tags = _validated_payload(
        title=public_title if public_title is not None else generated_title,
        body=public_body if public_body is not None else generated_body,
        tags=tags or [],
    )
    public_key = _validated_public_key(public_key)
    redaction_notes = str(redaction_notes or "").strip()
    if len(redaction_notes) > 2000:
        raise PublicationError("redaction_notes cannot exceed 2,000 characters.")
    source_fingerprint = publication_source_fingerprint(source)
    creation_hash = _canonical_hash(
        {
            "source_type": source._meta.label_lower,
            "source_id": str(source.pk),
            "public_key": public_key,
            "title": title,
            "body": body,
            "tags": normalized_tags,
            "redaction_notes": redaction_notes,
        }
    )
    existing = MemoryPublication.objects.filter(
        organization=organization,
        idempotency_key=key,
    ).first()
    if existing is not None:
        if existing.creation_request_hash != creation_hash:
            raise PublicationError(
                "Idempotency-Key was already used for a different publication candidate."
            )
        return existing, False

    publication = MemoryPublication(
        organization=organization,
        source_content_type=ContentType.objects.get_for_model(
            source,
            for_concrete_model=False,
        ),
        source_object_id=str(source.pk),
        source_fingerprint=source_fingerprint,
        public_key=public_key,
        proposed_title=title,
        proposed_body=body,
        proposed_tags=normalized_tags,
        sensitivity_findings=scan_public_payload(
            title=title,
            body=body,
            tags=normalized_tags,
        ),
        redaction_notes=redaction_notes,
        idempotency_key=key,
        creation_request_hash=creation_hash,
        proposed_by=actor,
    )
    publication.proposal_hash = _proposal_hash(publication)
    publication.full_clean()
    try:
        with transaction.atomic():
            publication.save()
    except IntegrityError:
        existing = MemoryPublication.objects.filter(
            organization=organization,
            idempotency_key=key,
        ).first()
        if existing is None or existing.creation_request_hash != creation_hash:
            raise
        return existing, False
    _event(
        publication,
        MemoryPublicationEventType.CANDIDATE_CREATED,
        actor=actor,
        metadata={
            "source_type": source._meta.model_name,
            "finding_codes": [
                finding["code"] for finding in publication.sensitivity_findings
            ],
        },
    )
    return publication, True


@transaction.atomic
def update_publication_candidate(
    *,
    publication,
    actor,
    public_title,
    public_body,
    tags,
    redaction_notes,
) -> MemoryPublication:
    publication = MemoryPublication.objects.select_for_update().get(pk=publication.pk)
    if publication.status != MemoryPublicationStatus.DRAFT:
        raise PublicationError("Only a draft publication candidate can be edited.")
    title, body, normalized_tags = _validated_payload(
        title=public_title,
        body=public_body,
        tags=tags,
    )
    redaction_notes = str(redaction_notes or "").strip()
    if len(redaction_notes) > 2000:
        raise PublicationError("redaction_notes cannot exceed 2,000 characters.")
    previous_hash = publication.proposal_hash
    publication.proposed_title = title
    publication.proposed_body = body
    publication.proposed_tags = normalized_tags
    publication.redaction_notes = redaction_notes
    publication.sensitivity_findings = scan_public_payload(
        title=title,
        body=body,
        tags=normalized_tags,
    )
    publication.redaction_confirmed_by = None
    publication.redaction_confirmed_at = None
    publication.proposal_hash = _proposal_hash(publication)
    publication.full_clean()
    publication.save(
        update_fields=(
            "proposed_title",
            "proposed_body",
            "proposed_tags",
            "redaction_notes",
            "sensitivity_findings",
            "redaction_confirmed_by",
            "redaction_confirmed_at",
            "proposal_hash",
            "updated_at",
        )
    )
    _event(
        publication,
        MemoryPublicationEventType.CANDIDATE_EDITED,
        actor=actor,
        metadata={
            "previous_payload_hash": previous_hash,
            "finding_codes": [
                finding["code"] for finding in publication.sensitivity_findings
            ],
        },
    )
    return publication


@transaction.atomic
def submit_publication_for_review(
    *,
    publication,
    authorization,
    actor,
    confirm_redacted: bool,
) -> tuple[MemoryPublication, bool]:
    publication = (
        MemoryPublication.objects.select_for_update()
        .select_related("review_item")
        .get(pk=publication.pk)
    )
    if publication.status == MemoryPublicationStatus.PENDING_REVIEW:
        return publication, False
    if publication.status != MemoryPublicationStatus.DRAFT:
        raise PublicationError("Only a draft publication can be submitted.")
    if confirm_redacted is not True:
        raise PublicationError("Submission requires confirm_redacted=true.")
    if len(str(publication.redaction_notes or "").strip()) < 10:
        raise PublicationError(
            "redaction_notes must explain the completed human review."
        )
    findings = scan_public_payload(
        title=publication.proposed_title,
        body=publication.proposed_body,
        tags=publication.proposed_tags,
    )
    if findings:
        publication.sensitivity_findings = findings
        publication.save(update_fields=("sensitivity_findings", "updated_at"))
        raise PublicationError(
            "The public candidate still contains blocked sensitivity findings."
        )
    source = publication.source
    _assert_source_publishable(
        source=source,
        organization=publication.organization,
        authorization=authorization,
    )
    if publication_source_fingerprint(source) != publication.source_fingerprint:
        raise PublicationError(
            "The private source changed; create a fresh publication candidate."
        )

    review, created = open_review_item(
        organization=publication.organization,
        target=publication,
        review_type=MemoryReviewType.PUBLICATION,
        reason=(
            "Review the redacted public payload and approve or reject deliberate publication."
        ),
        idempotency_key=f"publication-review:{publication.pk}:{publication.proposal_hash}",
        severity="high",
    )
    publication.status = MemoryPublicationStatus.PENDING_REVIEW
    publication.review_item = review
    publication.redaction_confirmed_by = actor
    publication.redaction_confirmed_at = timezone.now()
    publication.sensitivity_findings = []
    publication.save(
        update_fields=(
            "status",
            "review_item",
            "redaction_confirmed_by",
            "redaction_confirmed_at",
            "sensitivity_findings",
            "updated_at",
        )
    )
    _event(
        publication,
        MemoryPublicationEventType.SUBMITTED,
        actor=actor,
        metadata={"review_id": str(review.pk)},
    )
    return publication, created


@transaction.atomic
def approve_publication(
    *,
    publication,
    authorization,
    actor,
) -> PublicKnowledgeItem:
    publication = (
        MemoryPublication.objects.select_for_update()
        .select_related("review_item", "proposed_by")
        .get(pk=publication.pk)
    )
    if publication.status != MemoryPublicationStatus.PENDING_REVIEW:
        raise PublicationError("Only a pending publication can be approved.")
    if (
        getattr(
            settings,
            "ORG_MEMORY_PUBLICATION_REQUIRE_SEPARATE_REVIEWER",
            True,
        )
        and publication.proposed_by_id == getattr(actor, "pk", None)
    ):
        raise PublicationError(
            "Publication approval requires a different authorised reviewer."
        )
    source = publication.source
    _assert_source_publishable(
        source=source,
        organization=publication.organization,
        authorization=authorization,
    )
    if publication_source_fingerprint(source) != publication.source_fingerprint:
        raise PublicationError(
            "The private source changed; create a fresh publication candidate."
        )
    if scan_public_payload(
        title=publication.proposed_title,
        body=publication.proposed_body,
        tags=publication.proposed_tags,
    ):
        raise PublicationError("The public candidate failed its final sensitivity check.")

    now = timezone.now()
    organization_model = publication.organization.__class__
    organization_model.objects.select_for_update().get(
        pk=publication.organization_id
    )
    previous = list(
        PublicKnowledgeItem.objects.select_for_update().filter(
            organization=publication.organization,
            public_key=publication.public_key,
            status=PublicKnowledgeStatus.ACTIVE,
        )
    )
    for item in previous:
        item.status = PublicKnowledgeStatus.SUPERSEDED
        item.superseded_at = now
        item.save(update_fields=("status", "superseded_at", "updated_at"))
    latest_revision = (
        PublicKnowledgeItem.objects.filter(
            organization=publication.organization,
            public_key=publication.public_key,
        ).aggregate(value=Max("revision"))["value"]
        or 0
    )
    content_hash = _canonical_hash(
        {
            "title": publication.proposed_title,
            "body": publication.proposed_body,
            "tags": publication.proposed_tags,
        }
    )
    item = PublicKnowledgeItem(
        organization=publication.organization,
        public_key=publication.public_key,
        revision=latest_revision + 1,
        title=publication.proposed_title,
        body=publication.proposed_body,
        tags=publication.proposed_tags,
        status=PublicKnowledgeStatus.ACTIVE,
        content_hash=content_hash,
        published_at=now,
    )
    item.full_clean()
    item.save()

    publication.status = MemoryPublicationStatus.PUBLISHED
    publication.published_item = item
    publication.approved_by = actor
    publication.approved_at = now
    publication.save(
        update_fields=(
            "status",
            "published_item",
            "approved_by",
            "approved_at",
            "updated_at",
        )
    )
    _event(
        publication,
        MemoryPublicationEventType.PUBLISHED,
        actor=actor,
        metadata={
            "public_item_id": str(item.pk),
            "public_revision": item.revision,
        },
    )
    from .public_knowledge import (
        refresh_public_search_vectors,
        schedule_public_knowledge_embedding,
    )

    refresh_public_search_vectors(item_ids=[item.pk])
    schedule_public_knowledge_embedding(item)
    return item


@transaction.atomic
def reject_publication(*, publication, actor, reason: str) -> MemoryPublication:
    publication = MemoryPublication.objects.select_for_update().get(pk=publication.pk)
    if publication.status != MemoryPublicationStatus.PENDING_REVIEW:
        raise PublicationError("Only a pending publication can be rejected.")
    publication.status = MemoryPublicationStatus.REJECTED
    publication.revocation_reason = str(reason or "publication_rejected")[:512]
    publication.save(
        update_fields=("status", "revocation_reason", "updated_at")
    )
    _event(
        publication,
        MemoryPublicationEventType.REJECTED,
        actor=actor,
        metadata={"reason": publication.revocation_reason},
    )
    return publication


def _invalidate_locked(publication, *, reason: str) -> MemoryPublication:
    if publication.status in {
        MemoryPublicationStatus.INVALIDATED,
        MemoryPublicationStatus.REVOKED,
    }:
        return publication
    now = timezone.now()
    if publication.published_item_id:
        PublicKnowledgeItem.objects.filter(
            pk=publication.published_item_id,
            status=PublicKnowledgeStatus.ACTIVE,
        ).update(
            status=PublicKnowledgeStatus.REVOKED,
            revoked_at=now,
            updated_at=now,
        )
    if publication.review_item_id:
        MemoryReviewItem.objects.filter(
            pk=publication.review_item_id,
            status__in=(MemoryReviewStatus.OPEN, MemoryReviewStatus.IN_REVIEW),
        ).update(
            status=MemoryReviewStatus.CANCELLED,
            resolution={"reason": str(reason)[:512], "automatic": True},
            resolved_at=now,
            updated_at=now,
        )
    publication.status = MemoryPublicationStatus.INVALIDATED
    publication.revoked_at = now
    publication.revocation_reason = str(reason or "private_source_changed")[:512]
    publication.save(
        update_fields=(
            "status",
            "revoked_at",
            "revocation_reason",
            "updated_at",
        )
    )
    _event(
        publication,
        MemoryPublicationEventType.INVALIDATED,
        metadata={"reason": publication.revocation_reason},
    )
    return publication


@transaction.atomic
def revoke_publication(
    *,
    publication,
    actor,
    reason: str,
    idempotency_key: str,
) -> tuple[MemoryPublication, bool]:
    key = str(idempotency_key or "").strip()
    if not PUBLICATION_IDEMPOTENCY_PATTERN.fullmatch(key):
        raise PublicationError("A valid Idempotency-Key header is required.")
    publication = (
        MemoryPublication.objects.select_for_update()
        .select_related("review_item", "published_item")
        .get(pk=publication.pk)
    )
    if publication.status == MemoryPublicationStatus.REVOKED:
        if publication.revocation_idempotency_key == key:
            return publication, False
        raise PublicationError("This publication has already been revoked.")
    if publication.status == MemoryPublicationStatus.INVALIDATED:
        raise PublicationError("This publication was automatically invalidated.")
    if publication.status not in {
        MemoryPublicationStatus.DRAFT,
        MemoryPublicationStatus.PENDING_REVIEW,
        MemoryPublicationStatus.PUBLISHED,
    }:
        raise PublicationError("This publication cannot be revoked from its current state.")
    now = timezone.now()
    if publication.published_item_id:
        PublicKnowledgeItem.objects.filter(
            pk=publication.published_item_id,
            status=PublicKnowledgeStatus.ACTIVE,
        ).update(
            status=PublicKnowledgeStatus.REVOKED,
            revoked_at=now,
            updated_at=now,
        )
    if publication.review_item_id:
        publication.review_item.status = MemoryReviewStatus.CANCELLED
        publication.review_item.resolution = {
            "reason": str(reason or "publication_revoked")[:512],
            "revoked": True,
        }
        publication.review_item.resolved_by = actor
        publication.review_item.resolved_at = now
        publication.review_item.save(
            update_fields=(
                "status",
                "resolution",
                "resolved_by",
                "resolved_at",
                "updated_at",
            )
        )
    publication.status = MemoryPublicationStatus.REVOKED
    publication.revoked_by = actor
    publication.revoked_at = now
    publication.revocation_reason = str(reason or "publication_revoked")[:512]
    publication.revocation_idempotency_key = key
    publication.save(
        update_fields=(
            "status",
            "revoked_by",
            "revoked_at",
            "revocation_reason",
            "revocation_idempotency_key",
            "updated_at",
        )
    )
    _event(
        publication,
        MemoryPublicationEventType.REVOKED,
        actor=actor,
        metadata={"reason": publication.revocation_reason},
    )
    return publication, True


def _publication_ids_for_sources(*, claim_ids, summary_ids):
    claim_type = ContentType.objects.get_for_model(
        MemoryClaim,
        for_concrete_model=False,
    )
    summary_type = ContentType.objects.get_for_model(
        MemorySummary,
        for_concrete_model=False,
    )
    return MemoryPublication.objects.filter(
        Q(
            source_content_type=claim_type,
            source_object_id__in=[str(value) for value in claim_ids],
        )
        | Q(
            source_content_type=summary_type,
            source_object_id__in=[str(value) for value in summary_ids],
        ),
        status__in=(
            MemoryPublicationStatus.DRAFT,
            MemoryPublicationStatus.PENDING_REVIEW,
            MemoryPublicationStatus.PUBLISHED,
        ),
    ).values_list("pk", flat=True)


@transaction.atomic
def retire_publications_for_claim(claim, *, reason="claim_state_changed") -> dict:
    summary_ids = MemorySummaryClaim.objects.filter(claim=claim).values_list(
        "summary_id",
        flat=True,
    )
    ids = list(
        _publication_ids_for_sources(
            claim_ids=[claim.pk],
            summary_ids=summary_ids,
        )
    )
    for publication in MemoryPublication.objects.select_for_update().filter(pk__in=ids):
        _invalidate_locked(publication, reason=reason)
    return {"publications_invalidated": len(ids)}


@transaction.atomic
def retire_publications_for_source(source, *, reason="source_changed") -> dict:
    claim_ids = MemoryEvidence.objects.filter(source=source).values_list(
        "claim_id",
        flat=True,
    )
    summary_ids = MemorySummaryEvidence.objects.filter(
        evidence__source=source,
    ).values_list("summary_id", flat=True)
    ids = list(
        _publication_ids_for_sources(
            claim_ids=claim_ids,
            summary_ids=summary_ids,
        )
    )
    for publication in MemoryPublication.objects.select_for_update().filter(pk__in=ids):
        _invalidate_locked(publication, reason=reason)
    return {"publications_invalidated": len(ids)}
