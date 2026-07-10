from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("workflow_runs", "0004_contentfactoryrun_organization_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentfactoryrun",
            name="reconciled_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
