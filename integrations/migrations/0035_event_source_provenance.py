from django.db import migrations, models


def mark_existing_luma_sources(apps, schema_editor):
    del schema_editor
    for model_name in ("ReconciliationRule", "XeroStatementSuggestion"):
        model = apps.get_model("integrations", model_name)
        model.objects.filter(event_source_id__gt="", event_source_type="").update(
            event_source_type="luma"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("integrations", "0034_reconciliation_learning_decisions"),
    ]

    operations = [
        migrations.AddField(
            model_name="reconciliationrule",
            name="event_source_type",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddField(
            model_name="xerostatementsuggestion",
            name="event_source_type",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.RunPython(mark_existing_luma_sources, migrations.RunPython.noop),
    ]
