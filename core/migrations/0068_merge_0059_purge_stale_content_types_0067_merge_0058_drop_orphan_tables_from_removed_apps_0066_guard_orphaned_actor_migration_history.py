from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0059_purge_stale_content_types"),
        (
            "core",
            "0067_merge_0058_drop_orphan_tables_from_removed_apps_0066_guard_orphaned_actor_migration_history",
        ),
    ]

    operations = []
