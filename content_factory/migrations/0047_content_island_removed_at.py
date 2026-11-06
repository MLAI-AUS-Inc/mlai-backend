"""Owner-approved (20 Oct 2026): record founder-removed content islands."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0046_connected_website_article_delivery_preview"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentisland",
            name="removed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
