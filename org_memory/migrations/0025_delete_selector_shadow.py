from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('org_memory', '0024_alter_memoryoutboxevent_event_type'),
    ]

    operations = [
        migrations.DeleteModel(
            name='MemorySelectorShadowResult',
        ),
        migrations.DeleteModel(
            name='MemorySelectorShadowRun',
        ),
    ]
