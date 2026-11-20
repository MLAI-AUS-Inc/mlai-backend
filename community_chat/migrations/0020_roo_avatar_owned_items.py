from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("community_chat", "0019_roo_avatar_fur")]
    operations = [
        migrations.AddField(
            model_name="communityrooavatar",
            name="owned_items",
            field=models.JSONField(default=list, blank=True),
        ),
    ]
