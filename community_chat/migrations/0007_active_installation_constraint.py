import uuid

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("community_chat", "0006_account_sessions"),
    ]

    operations = [
        migrations.AlterField(
            model_name="communitychatdevice",
            name="installation_id",
            field=models.UUIDField(default=uuid.uuid4),
        ),
        migrations.AddConstraint(
            model_name="communitychatdevice",
            constraint=models.UniqueConstraint(
                fields=("installation_id",),
                condition=Q(status__in=("pending", "verified")),
                name="chat_unique_active_installation",
            ),
        ),
    ]
