from __future__ import annotations

import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from content_analytics.services.locations import record_article_location


logger = logging.getLogger(__name__)
LOCATION_FIELDS = {
    "canonical_url",
    "canonical_path",
    "live_url",
    "live_verified_at",
    "publish_status",
    "published_at",
}


@receiver(
    post_save,
    sender="content_factory.WrittenArticle",
    dispatch_uid="content_analytics_record_written_article_location",
)
def record_written_article_location(sender, instance, raw=False, created=False, update_fields=None, **kwargs):
    if raw:
        return
    if not created and update_fields is not None and LOCATION_FIELDS.isdisjoint(update_fields):
        return
    try:
        record_article_location(instance)
    except Exception:
        # Article generation/publishing is the source of truth and must not be
        # failed by an analytics sidecar. Every sync reconciles again, so a
        # transient migration/lock failure is self-healing.
        logger.warning(
            "article_analytics_location_signal_failed article=%s",
            getattr(instance, "pk", ""),
            exc_info=True,
        )
