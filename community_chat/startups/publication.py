"""Only exact approved revisions may cross an owner or community boundary."""
from django.db.models import F, Q
from django.db.models.functions import Coalesce

from startup_updates.models import MonthlyUpdateDraft


def approved_updates(*audiences):
    """Select publication receipts matching both the audience and content hash."""
    disclosure = Q(pk__in=[])
    for audience in audiences:
        if audience not in {"community", "public"}:
            raise ValueError("Only shared audiences have a reader.")
        disclosure |= Q(
            published_revision__audience=audience,
            published_revision__approval__audience_visibility=[audience],
        )
    return MonthlyUpdateDraft.objects.filter(
        disclosure,
        published_at__isnull=False,
        published_revision__approval__content_hash=F("published_revision__content_hash"),
    ).select_related("organization", "published_revision__snapshot")


def community_feed_order(queryset):
    """Order shared updates by the period they report on, never by their last edit.

    Re-publishing an edited update refreshes ``published_at``, so it cannot
    decide the feed position: the reporting month comes first, then the date
    the founder gave the update, then when it was first shared.
    """
    return queryset.order_by(
        F("month").desc(),
        F("update_date").desc(nulls_last=True),
        Coalesce("first_published_at", "published_at").desc(),
        "-id",
    )
