from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0014_add_content_factory_ledger_source"),
        ("core", "0033_add_content_factory_progress_tracking"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentfactoryjob",
            name="billing_amount",
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="billing_ledger",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="content_factory_jobs",
                to="roo.ledger",
            ),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="billing_source_job_id",
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="billing_status",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="contentfactoryjob",
            name="client_request_id",
            field=models.CharField(blank=True, db_index=True, max_length=100, null=True),
        ),
    ]
