from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('roo', '0044_cybertestcheckout')]
    operations = [
        migrations.AddField(model_name='cybertestcheckout', name='pricing_snapshot', field=models.JSONField(default=dict)),
        migrations.AddField(model_name='cybertestcheckout', name='settlement', field=models.JSONField(null=True, blank=True)),
        migrations.AddField(model_name='cybertestcheckout', name='settled_at', field=models.DateTimeField(null=True, blank=True)),
    ]
