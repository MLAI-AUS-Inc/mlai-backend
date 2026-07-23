import uuid

from django.db import migrations, models


def backfill_analytics_ids(apps, schema_editor):
    WrittenArticle = apps.get_model("content_factory", "WrittenArticle")
    for article in WrittenArticle.objects.filter(analytics_id__isnull=True).iterator(chunk_size=500):
        article.analytics_id = uuid.uuid4()
        article.save(update_fields=["analytics_id"])


class Migration(migrations.Migration):
    dependencies = [
        ("content_factory", "0031_merge_callback_lease_learning_entries"),
    ]

    operations = [
        migrations.AddField(
            model_name="writtenarticle",
            name="analytics_id",
            field=models.UUIDField(editable=False, null=True),
        ),
        migrations.RunPython(backfill_analytics_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="writtenarticle",
            name="analytics_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name="writtenarticle",
            name="canonical_path",
            field=models.CharField(blank=True, db_index=True, default="", max_length=1024),
        ),
        migrations.AddField(
            model_name="writtenarticle",
            name="canonical_url",
            field=models.URLField(blank=True, default="", max_length=2048),
        ),
    ]
