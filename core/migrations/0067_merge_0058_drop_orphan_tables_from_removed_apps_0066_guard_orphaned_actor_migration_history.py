from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0058_drop_orphan_tables_from_removed_apps"),
        ("core", "0066_guard_orphaned_actor_migration_history"),
    ]

    operations = []
