from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0015_merge_20260610_0823'),
    ]

    operations = [
        migrations.AddField(
            model_name='writtenarticle',
            name='source_run_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
