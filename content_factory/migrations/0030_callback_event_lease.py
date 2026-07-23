from django.db import migrations, models
from django.db.models import F


def backfill_lease_stamps(apps, schema_editor):
    """
    Pre-lease rows only exist for deliveries that were acknowledged with a
    2xx (failures deleted their row), so mark them all processed. Leaving
    them unstamped would make every historical event reclaimable the moment
    a replay arrives.
    """
    ContentFactoryCallbackEvent = apps.get_model("content_factory", "ContentFactoryCallbackEvent")
    ContentFactoryCallbackEvent.objects.filter(processed_at__isnull=True).update(
        claimed_at=F("created_at"),
        processed_at=F("created_at"),
    )


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0029_notificationchannel_delivery_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentfactorycallbackevent",
            name="claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="contentfactorycallbackevent",
            name="processed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(backfill_lease_stamps, migrations.RunPython.noop),
    ]
