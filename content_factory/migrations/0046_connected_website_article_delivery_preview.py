"""Owner decision (17 Oct 2026): a connected website always delivers article previews.

Rewrite only ``content_only`` saved by a company whose selected website is
connected or paused (``portable_drafts.WEBSITE_PREVIEW_CONNECTION_STATES``) to
``review_draft``. Companies without a connected website keep portable drafts.
The reverse is a no-op: which rows were content_only is not recoverable, and
restoring it would contradict the rule.
"""

from django.db import migrations
from django.db.models.functions import Now

# Frozen copy of portable_drafts.WEBSITE_PREVIEW_CONNECTION_STATES at authoring time.
WEBSITE_PREVIEW_CONNECTION_STATES = ("connected", "paused")


def connected_content_only_configs(apps):
    OrganizationContentConfig = apps.get_model("content_factory", "OrganizationContentConfig")
    return OrganizationContentConfig.objects.filter(
        article_delivery_mode="content_only",
        website_connection__state__in=WEBSITE_PREVIEW_CONNECTION_STATES,
    )


def preview_connected_website_articles(apps, schema_editor):
    connected_content_only_configs(apps).update(article_delivery_mode="review_draft", updated_at=Now())


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0045_content_dispatch_outbox"),
    ]

    operations = [
        migrations.RunPython(preview_connected_website_articles, migrations.RunPython.noop),
    ]
