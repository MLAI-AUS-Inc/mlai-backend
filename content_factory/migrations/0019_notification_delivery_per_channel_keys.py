from django.db import migrations


def rewrite_idempotency_keys(apps, schema_editor):
    """Rewrite legacy {run_id}:{event_type} keys to {run_id}:{channel_id}:{event_type}.

    Legacy keys contain exactly one colon (UUIDs contain none), so they are
    safely distinguishable from the per-channel format. Uniqueness is preserved
    because legacy keys were already unique per (run, event_type).
    """
    NotificationDelivery = apps.get_model("content_factory", "NotificationDelivery")
    for delivery in NotificationDelivery.objects.all().iterator():
        key = delivery.idempotency_key or ""
        if key.count(":") != 1:
            continue
        expected = f"{delivery.automation_run_id}:{delivery.channel_id}:{delivery.event_type}"
        if key != expected:
            delivery.idempotency_key = expected
            delivery.save(update_fields=["idempotency_key"])


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0018_notification_channel_verification"),
    ]

    operations = [
        migrations.RunPython(rewrite_idempotency_keys, migrations.RunPython.noop),
    ]
