from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("integrations", "0036_community_bridge_identity_links")]

    operations = [
        migrations.AlterField(
            model_name="communitybridgedelivery",
            name="delivery_type",
            field=models.CharField(
                choices=[
                    ("create", "Create"),
                    ("edit", "Edit"),
                    ("delete", "Delete"),
                    ("reaction_add", "Reaction add"),
                    ("reaction_remove", "Reaction remove"),
                ],
                max_length=20,
            ),
        ),
    ]
