from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("vibe_raising", "0001_initial"),
        ("founder_tools", "0001_move_vibe_raising_models"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveField(
                    model_name="viberaisingprofile",
                    name="active_company",
                ),
                migrations.RemoveField(
                    model_name="viberaisingprofile",
                    name="user",
                ),
                migrations.RemoveField(
                    model_name="viberaisingcompany",
                    name="profile",
                ),
                migrations.DeleteModel(
                    name="VibeRaisingProfile",
                ),
                migrations.DeleteModel(
                    name="VibeRaisingCompany",
                ),
            ],
        ),
    ]
