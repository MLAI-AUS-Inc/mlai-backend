"""Only exact approved revisions may cross an owner or community boundary."""
from django.db.models import F, Q

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
