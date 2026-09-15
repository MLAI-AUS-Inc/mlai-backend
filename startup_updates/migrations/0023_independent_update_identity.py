from django.db import migrations, models


def preserve_publication_times(apps, schema_editor):
    Draft = apps.get_model("startup_updates", "MonthlyUpdateDraft")
    Draft.objects.using(schema_editor.connection.alias).filter(published_at__isnull=False).update(first_published_at=models.F("published_at"))


class Migration(migrations.Migration):
    dependencies = [("startup_updates", "0022_reporting_evidence_revisions")]
    operations = [
        migrations.AddField(model_name="monthlyupdatedraft", name="update_date", field=models.DateField(blank=True, null=True, db_index=True)),
        migrations.AddField(model_name="monthlyupdatedraft", name="creation_key", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="monthlyupdatedraft", name="first_published_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.RunPython(preserve_publication_times, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(name="monthlyupdatedraft", unique_together=set()),
        migrations.AddConstraint(model_name="monthlyupdatedraft", constraint=models.UniqueConstraint(fields=("organization", "creation_key"), name="update_org_creation_key")),
        migrations.AddConstraint(model_name="monthlyupdatedraft", constraint=models.UniqueConstraint(fields=("organization", "month"), condition=models.Q(creation_key__isnull=True), name="update_legacy_month_slot")),
    ]
