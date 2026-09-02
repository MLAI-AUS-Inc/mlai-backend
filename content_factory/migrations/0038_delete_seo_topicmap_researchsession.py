from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('content_factory', '0037_content_islands'),
    ]

    operations = [
        migrations.DeleteModel(
            name='ResearchSession',
        ),
        migrations.DeleteModel(
            name='TopicMap',
        ),
    ]
