"""Replay the approved 0044 target generation constraint in a disposable database.

Uses the existing socket-only test cluster and network/credential isolation.
Never accepts production connection settings or reads .env.
"""

from concurrent.futures import ThreadPoolExecutor
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import test_website_connections_database as isolated


def validate_migration_scope():
    """Require exactly the reviewed constraint replacement after 0043."""
    from django.db import migrations
    from django.db.migrations.loader import MigrationLoader
    migration = MigrationLoader(None).disk_migrations[
        ("content_factory", "0044_website_target_generation_key")
    ]
    assert migration.dependencies == [("content_factory", "0043_merge_credentials_website")]
    added, removed = migration.operations
    assert isinstance(added, migrations.AddConstraint)
    assert added.constraint.fields == ("connection", "generation", "target_key")
    assert added.constraint.name == "cf_web_target_generation_unique"
    assert isinstance(removed, migrations.RemoveConstraint)
    assert removed.name == "cf_web_target_key_unique"
    print("Approved 0044 constraint replacement verified.", flush=True)


def replay_with_legacy_rows():
    """Verify forward/reverse behavior, retained proof, and concurrent upserts."""
    from django.db import IntegrityError, connection, connections, transaction
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    old_targets = [
        (app, "0043_merge_credentials_website") if app == "content_factory" else (app, name)
        for app, name in executor.loader.graph.leaf_nodes()
    ]
    executor.migrate(old_targets)
    apps = executor.loader.project_state(old_targets).apps
    Organization = apps.get_model("organizations", "Organization")
    Website = apps.get_model("content_factory", "WebsiteConnection")
    Target = apps.get_model("content_factory", "WebsiteConnectionTarget")
    org = Organization.objects.create(domain="migration.example.test", name="Synthetic generation migration")
    website = Website.objects.create(organization=org, github_repo="example/site")
    row = Target.objects.create(connection=website, generation=1, target_key="featured",
                                source_sha="a" * 40, contract={"proof": "synthetic-preserved"})
    before = Target.objects.values().get(pk=row.pk)
    try:
        with transaction.atomic():
            Target.objects.create(connection=website, generation=2, target_key="featured")
    except IntegrityError:
        pass
    else:
        raise AssertionError("Old constraint unexpectedly admitted a new generation")

    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    plan = [(m.app_label, m.name, backwards) for m, backwards in executor.migration_plan(leaves)]
    assert plan == [("content_factory", "0044_website_target_generation_key", False)], plan
    executor.migrate(leaves)
    Target = executor.loader.project_state(leaves).apps.get_model("content_factory", "WebsiteConnectionTarget")
    assert Target.objects.values().get(pk=row.pk) == before

    def upsert(_):
        try:
            return Target.objects.get_or_create(connection_id=website.pk, generation=2, target_key="featured")[0].pk
        finally:
            connections.close_all()

    if connection.vendor == "postgresql":
        with ThreadPoolExecutor(max_workers=4) as pool:
            winners = list(pool.map(upsert, range(4)))
        assert len(set(winners)) == 1
    else:
        Target.objects.get_or_create(connection_id=website.pk, generation=2, target_key="featured")
    assert Target.objects.filter(connection_id=website.pk, generation=2, target_key="featured").count() == 1
    try:
        with transaction.atomic():
            Target.objects.create(connection_id=website.pk, generation=1, target_key="featured")
    except IntegrityError:
        pass
    else:
        raise AssertionError("New constraint admitted a duplicate within one generation")

    # Reversal cannot silently discard targets from another consent generation.
    try:
        MigrationExecutor(connection).migrate(old_targets)
    except IntegrityError:
        pass
    else:
        raise AssertionError("Rollback with colliding historical generations unexpectedly succeeded")
    assert Target.objects.values().get(pk=row.pk) == before
    Target.objects.filter(connection_id=website.pk, generation=2).delete()
    executor = MigrationExecutor(connection)
    executor.migrate(old_targets)
    Reverted = executor.loader.project_state(old_targets).apps.get_model("content_factory", "WebsiteConnectionTarget")
    assert Reverted.objects.values().get(pk=row.pk) == before
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, Target._meta.db_table)
    assert constraints["cf_web_target_generation_unique"]["unique"]
    assert "cf_web_target_key_unique" not in constraints
    print("0043 -> 0044 -> 0043 -> 0044 retains target proof; PostgreSQL concurrent upserts preserve uniqueness.", flush=True)


if __name__ == "__main__":
    isolated.validate_migration_scope = validate_migration_scope
    isolated.replay_with_legacy_rows = replay_with_legacy_rows
    isolated.main()
