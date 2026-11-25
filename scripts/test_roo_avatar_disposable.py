"""Run approved Roo avatar regressions in a fresh socket-only PostgreSQL cluster.

User approved community_chat.0016_roo_avatar and the 377 inventoried existing
migrations on 2026-11-02. No shared or production database is accepted.
"""

import hashlib
import json
from pathlib import Path
import sys

from test_account_bans_disposable import main

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = ROOT / "docs/roo-avatar-migration-inventory.json"
MIGRATION = ("community_chat", "0016_roo_avatar")


def verify_approved_migrations():
    """Verify the entire approved graph before the test runner touches schema."""
    from django.db.migrations.loader import MigrationLoader
    from django.db import migrations

    approved = json.loads(INVENTORY.read_text())
    expected = {
        (item["app"], item["name"]): item["sha256"] for item in approved["migrations"]
    }
    loader = MigrationLoader(None)
    actual = loader.disk_migrations
    if set(actual) != set(expected) | {MIGRATION}:
        raise RuntimeError("Migration graph differs from the approved inventory.")
    for key, digest in expected.items():
        path = Path(sys.modules[actual[key].__module__].__file__)
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"Approved migration changed: {key!r}")
    operations = actual[MIGRATION].operations
    if len(operations) != 1 or not isinstance(operations[0], migrations.CreateModel):
        raise RuntimeError("Expected the approved single CreateModel operation.")
    create = operations[0]
    if create.name != "CommunityRooAvatar" or {name for name, _ in create.fields} != {
        "id",
        "user",
        "community",
        "hair",
        "glasses",
        "jacket",
        "shoes",
        "version",
        "created_at",
        "updated_at",
    }:
        raise RuntimeError("Unexpected Roo avatar schema.")
    print(
        f"Verified all {len(expected)} approved existing migration hashes plus 0016_roo_avatar.",
        flush=True,
    )


if __name__ == "__main__":
    main(
        migration_check=verify_approved_migrations,
        default_labels=[
            "community_chat.roo_avatar.tests_policy",
            "community_chat.roo_avatar.tests_api",
            "community_chat.tests.test_volunteer_policy",
            "community_chat.tests.test_volunteer",
            "community_chat.tests.test_volunteer_concurrency",
            "community_chat.tests.test_volunteer_presentation",
            "community_chat.tests.test_account_profiles",
            "community_chat.tests.test_account_sessions",
            "community_chat.tests.test_session_device_binding",
        ],
    )
