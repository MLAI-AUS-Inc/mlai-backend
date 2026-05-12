from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0010_backfill_review_draft_delivery_mode"),
    ]

    operations = [
        migrations.AddField(
            model_name="vibemarketingcomponentcomment",
            name="context",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
