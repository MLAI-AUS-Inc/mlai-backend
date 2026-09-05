"""Current validity of immutable, authoritative source receipts."""

from django.db.models import Q

from .models import VolunteerSourceReceipt


def source_is_invalidated(receipt):
    """Resolve author deletions or authorised moderation without changing history.

    Kind-5 author deletion may lack a channel after relay deletion, so its
    canonical actor plus original signed event identity is the boundary.
    Kind-9005 moderation receipts require a public channel and server-checked
    Points Admin authority when ingested.
    """
    if receipt.metadata.get("invalidated") or receipt.metadata.get("service_account"):
        return True
    if receipt.origin != "relay":
        return False
    source_id = receipt.source.get("source_id")
    if not source_id:
        return False
    author_id = receipt.target_id if receipt.kind == "reaction" else receipt.actor_id
    authors = Q(
        source__source_id=source_id, actor_id=author_id, metadata__deletion_kind=5
    )
    target_ids = [source_id]
    if receipt.kind == "reaction" and receipt.source.get("message_id"):
        reaction_id = receipt.source["message_id"]
        target_ids.append(reaction_id)
        authors |= Q(
            source__source_id=reaction_id,
            actor_id=receipt.actor_id,
            metadata__deletion_kind=5,
        )
    moderation = Q(
        source__source_id__in=target_ids,
        metadata__deletion_kind=9005,
        source__channel_id=receipt.source.get("channel_id"),
    )
    return (
        VolunteerSourceReceipt.objects.filter(
            authors | moderation,
            community=receipt.community,
            kind="invalidation",
        )
        .exclude(status="ineligible")
        .exists()
    )
