from django.db import transaction
from django.utils import timezone

from content_factory.models import TopicFeedback


def normalize_topic_feedback_keyword(value) -> str:
    return " ".join(str(value or "").lower().strip().split())


def serialize_topic_feedback(feedback: TopicFeedback) -> dict:
    return {
        "id": str(feedback.id),
        "domain": feedback.organization.domain,
        "keyword": feedback.keyword,
        "keyword_normalized": feedback.keyword_normalized,
        "feedback_type": feedback.feedback_type,
        "reason_code": feedback.reason_code,
        "reason_text": feedback.reason_text,
        "decline_scope": feedback.decline_scope,
        "source": feedback.source,
        "session_id": feedback.session_id,
        "active": feedback.restored_at is None,
        "created_at": feedback.created_at.isoformat() if feedback.created_at else None,
        "updated_at": feedback.updated_at.isoformat() if feedback.updated_at else None,
        "restored_at": feedback.restored_at.isoformat() if feedback.restored_at else None,
    }


def list_topic_feedback(organization, *, feedback_type="declined", include_restored=False, limit=100, offset=0):
    qs = TopicFeedback.objects.filter(
        organization=organization,
        feedback_type=(feedback_type or "declined").strip() or "declined",
    )
    if not include_restored:
        qs = qs.filter(restored_at__isnull=True)
    return list(qs.order_by("-created_at")[offset:offset + limit])


@transaction.atomic
def record_topic_feedback(
    organization,
    *,
    keyword,
    feedback_type="declined",
    reason_code="not_appropriate",
    reason_text=None,
    decline_scope="similar",
    source="homepage_topic_card",
    session_id=None,
):
    normalized = normalize_topic_feedback_keyword(keyword)
    cleaned_feedback_type = (feedback_type or "declined").strip() or "declined"
    defaults = {
        "keyword": str(keyword or "").strip(),
        "reason_code": (reason_code or "not_appropriate").strip() or "not_appropriate",
        "reason_text": reason_text or None,
        "decline_scope": (decline_scope or "similar").strip() or "similar",
        "source": (source or "homepage_topic_card").strip() or "homepage_topic_card",
        "session_id": str(session_id).strip() if session_id else None,
    }

    existing = TopicFeedback.objects.filter(
        organization=organization,
        keyword_normalized=normalized,
        feedback_type=cleaned_feedback_type,
        restored_at__isnull=True,
    ).first()
    if existing:
        for field, value in defaults.items():
            setattr(existing, field, value)
        existing.save()
        return existing, False

    return TopicFeedback.objects.create(
        organization=organization,
        keyword_normalized=normalized,
        feedback_type=cleaned_feedback_type,
        **defaults,
    ), True


@transaction.atomic
def restore_topic_feedback(feedback, *, restored_at=None):
    if feedback.restored_at is None:
        feedback.restored_at = restored_at or timezone.now()
        feedback.save(update_fields=["restored_at", "updated_at"])
    return feedback
