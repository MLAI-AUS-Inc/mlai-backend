from django.db import migrations
from django.utils import timezone


def activate_account_email_channels(apps, schema_editor):
    NotificationChannel = apps.get_model("content_factory", "NotificationChannel")
    now = timezone.now()
    channels = NotificationChannel.objects.filter(
        channel_type="email",
        consent_state="pending",
        user__isnull=False,
    ).select_related("user")
    for channel in channels.iterator(chunk_size=500):
        account_email = str(getattr(channel.user, "email", "") or "").strip().casefold()
        route_email = str(channel.route_id or "").strip().casefold()
        if not account_email or route_email != account_email:
            continue
        channel.consent_state = "active"
        channel.delivery_enabled = True
        channel.verified_at = now
        channel.opted_out_at = None
        channel.verification_code_hash = ""
        channel.verification_expires_at = None
        channel.verification_attempts = 0
        channel.save(
            update_fields=[
                "consent_state",
                "delivery_enabled",
                "verified_at",
                "opted_out_at",
                "verification_code_hash",
                "verification_expires_at",
                "verification_attempts",
                "updated_at",
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0032_writtenarticle_analytics_identity"),
    ]

    operations = [
        migrations.RunPython(activate_account_email_channels, migrations.RunPython.noop),
    ]
