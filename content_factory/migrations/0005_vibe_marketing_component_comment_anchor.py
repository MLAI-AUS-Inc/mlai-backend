from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("content_factory", "0004_vibe_marketing_component_comment"),
    ]

    operations = [
        migrations.AddField(
            model_name="vibemarketingcomponentcomment",
            name="anchor",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
