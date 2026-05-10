from django.db import migrations
from django.db.models import Q


def forwards(apps, schema_editor):
    OrganizationContentConfig = apps.get_model("content_factory", "OrganizationContentConfig")
    connected_credentials = (
        Q(github_token_encrypted__isnull=False)
        & ~Q(github_token_encrypted="")
    ) | (
        Q(github_refresh_token_encrypted__isnull=False)
        & ~Q(github_refresh_token_encrypted="")
    ) | (
        Q(github_installation_id__isnull=False)
        & ~Q(github_installation_id="")
    )
    (
        OrganizationContentConfig.objects.filter(article_delivery_mode__in=["content_only", "publish_code"])
        .exclude(github_repo__isnull=True)
        .exclude(github_repo="")
        .filter(connected_credentials)
        .update(article_delivery_mode="review_draft")
    )


def backwards(apps, schema_editor):
    # Stale content-only/publish-code preferences cannot be reconstructed safely.
    return None


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0009_researchedkeyword_topic_picker_metrics"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
