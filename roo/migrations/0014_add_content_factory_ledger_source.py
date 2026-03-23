from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0013_move_medhack_to_hospital"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ledger",
            name="source",
            field=models.CharField(
                choices=[
                    ("TASK", "Task"),
                    ("COWORKING", "Coworking"),
                    ("EVENT", "Event"),
                    ("MERCH", "Merch"),
                    ("CONTENT_FACTORY", "Content Factory"),
                    ("TOOLS", "Tools"),
                    ("DONATION", "Donation"),
                    ("MANUAL", "Manual"),
                    ("LEGACY", "Legacy"),
                ],
                default="LEGACY",
                max_length=20,
            ),
        ),
    ]
