"""Conversation fairness and generation fences for the encrypted private outbox."""
from django.conf import settings
from django.db.models import Case, Exists, F, OuterRef, Q, Value, When
from integrations.models import SlackDmMirrorConversation, SlackDmMirrorDelivery
from .scheduler import LeaseLost


def fair_private_candidate(now, registration_prefix):
    ready = SlackDmMirrorDelivery.objects.filter(
        conversation_id=OuterRef("pk"), status="pending", available_at__lte=now,
    ).exclude(source_message_id__startswith=registration_prefix)
    active = SlackDmMirrorDelivery.objects.filter(conversation_id=OuterRef("pk"), status="processing")
    candidates = SlackDmMirrorConversation.objects.filter(
        status="live", grant__status="active", grant__revoked_at__isnull=True,
    ).filter(Exists(ready), ~Exists(active))
    order = [F("sync_state__last_served_at").asc(nulls_first=True), "id"]
    if getattr(settings, "MESSAGE_SYNC_OPEN_PRIORITY_ENABLED", True):
        from integrations.services.slack_chat_refresh import prioritize_open_conversations
        candidates = prioritize_open_conversations(candidates).annotate(open_rank=Case(
            When(Q(foreground_refresh=True) & (Q(sync_state__isnull=True) | ~Q(sync_state__verified_ranges__has_key="presentation")), then=Value(0)),
            default=Value(1),
        ))
        order.insert(0, "open_rank")
    return candidates.order_by(*order).values_list("id", flat=True).first()


def guard_private_delivery(row, claimed_token):
    current = (row.metadata or {}).get("sync_delivery_lease")
    if (current or claimed_token) and (current != claimed_token or row.status != "processing"):
        raise LeaseLost("private_delivery_claim_replaced")
