from importlib import import_module

from django.db import migrations


GUARD_MODULE = "core.migrations.0064_guard_legacy_actor_migration_history"
EXPECTED_GUARD_VERSION = "state-bound-attestation-v2"


def recheck_legacy_actor_migration_attestation(apps, schema_editor):
    """Re-run the corrected guard when an earlier core.0064 was recorded."""
    guard_module = import_module(GUARD_MODULE)
    if guard_module.GUARD_VERSION != EXPECTED_GUARD_VERSION:
        raise RuntimeError(
            "core.0065 cannot verify its pinned core.0064 guard implementation"
        )
    guard_module.guard_legacy_actor_migration_history(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0064_guard_legacy_actor_migration_history"),
    ]

    operations = [
        migrations.RunPython(
            recheck_legacy_actor_migration_attestation,
            migrations.RunPython.noop,
        ),
    ]
