"""Conversation fairness and generation fences for the encrypted private outbox."""
from django.db.models import Exists, F, OuterRef
from integrations.models import SlackDmMirrorConversation, SlackDmMirrorDelivery
from .scheduler import LeaseLost


def fair_private_candidate(now, registration_prefix):
    ready = SlackDmMirrorDelivery.objects.filter(
        conversation_id=OuterRef("pk"), status="pending", available_at__lte=now,
    ).exclude(source_message_id__startswith=registration_prefix)
    active = SlackDmMirrorDelivery.objects.filter(conversation_id=OuterRef("pk"), status="processing")
    return SlackDmMirrorConversation.objects.filter(
        status="live", grant__status="active", grant__revoked_at__isnull=True,
    ).filter(Exists(ready), ~Exists(active)).order_by(
        F("sync_state__last_served_at").asc(nulls_first=True), "id",
    ).values_list("id", flat=True).first()


def guard_private_delivery(row, claimed_token):
    current = (row.metadata or {}).get("sync_delivery_lease")
    if (current or claimed_token) and (current != claimed_token or row.status != "processing"):
        raise LeaseLost("private_delivery_claim_replaced")
