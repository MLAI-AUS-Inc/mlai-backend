from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("founder_tools", "0001_move_vibe_raising_models"),
        ("organizations", "0001_split_content_factory_apps"),
    ]

    operations = [
        migrations.AddField(
            model_name="viberaisingcompany",
            name="organization",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="founder_companies",
                to="organizations.organization",
            ),
        ),
    ]
