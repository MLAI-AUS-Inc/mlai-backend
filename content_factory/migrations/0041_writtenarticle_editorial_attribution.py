"""Persist the original and current writing decisions without inferring history."""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("content_factory", "0038_delete_seo_topicmap_researchsession")]

    operations = [
        migrations.AddField(
            model_name="writtenarticle", name="editorial_snapshot",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="original_editorial_snapshot",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="audience_id",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="audience_version",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="offer_id",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="offer_version",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="conversion_intent",
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name="writtenarticle", name="editorial_provenance_status",
            field=models.CharField(default="unknown", max_length=32),
        ),
        migrations.AddIndex(
            model_name="writtenarticle",
            index=models.Index(fields=["organization", "audience_id"], name="wa_org_audience_idx"),
        ),
        migrations.AddIndex(
            model_name="writtenarticle",
            index=models.Index(fields=["organization", "offer_id"], name="wa_org_offer_idx"),
        ),
    ]
