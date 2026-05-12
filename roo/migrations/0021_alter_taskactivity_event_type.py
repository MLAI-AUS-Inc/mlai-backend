from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("roo", "0020_taskassignment_claimed_points_snapshot"),
    ]

    operations = [
        migrations.AlterField(
            model_name="taskactivity",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("updated", "Updated"),
                    ("published", "Published"),
                    ("claimed", "Claimed"),
                    ("unclaimed", "Unclaimed"),
                    ("submitted", "Submitted"),
                    ("changes_requested", "Changes Requested"),
                    ("approved", "Approved"),
                    ("cancelled", "Cancelled"),
                    ("blocked", "Blocked"),
                    ("unblocked", "Unblocked"),
                ],
                max_length=30,
            ),
        ),
    ]
