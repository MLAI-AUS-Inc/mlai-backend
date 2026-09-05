from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('roo', '0034_coworkingbookingoperation'),
    ]

    operations = [
        migrations.AddField(
            model_name='coworkingbookingoperation',
            name='subjects',
            field=models.ManyToManyField(
                blank=True,
                related_name='coworking_booking_operations',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
