"""Verify the specifically approved 0041 article migration in a disposable database.

Never reads .env or accepts a database URL. PostgreSQL mode creates its own
socket-only cluster; SQLite mode uses a new temporary directory. Historical
migrations bootstrap synthetic test databases. Requires explicit local migration
approval per AGENTS.md; never invoke against an existing database.
"""

import argparse
import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
KEYS = '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}'


def validate_migration_scope():
    from django.db import migrations
    from django.db.migrations.loader import MigrationLoader
    loader = MigrationLoader(None)
    migration = loader.disk_migrations[("content_factory", "0041_writtenarticle_editorial_attribution")]
    assert migration.dependencies == [("content_factory", "0038_delete_seo_topicmap_researchsession")]
    fields = {op.name for op in migration.operations if isinstance(op, migrations.AddField)}
    indexes = {op.index.name for op in migration.operations if isinstance(op, migrations.AddIndex)}
    assert len(migration.operations) == 10
    assert fields == {"editorial_snapshot", "original_editorial_snapshot", "audience_id", "audience_version",
                      "offer_id", "offer_version", "conversion_intent", "editorial_provenance_status"}
    assert indexes == {"wa_org_audience_idx", "wa_org_offer_idx"}
    assert all(op.model_name == "writtenarticle" for op in migration.operations)
    print("Approved migration scope verified: eight article fields and two indexes.", flush=True)


def replay_with_legacy_rows():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    old_targets = [
        (app, "0038_delete_seo_topicmap_researchsession") if app == "content_factory" else (app, name)
        for app, name in executor.loader.graph.leaf_nodes()
    ]
    executor.migrate(old_targets)
    old_apps = executor.loader.project_state(old_targets).apps
    Organization = old_apps.get_model("organizations", "Organization")
    Article = old_apps.get_model("content_factory", "WrittenArticle")
    org = Organization.objects.create(domain="migration.example.test")
    article = Article.objects.create(organization=org, title="Historical article", slug="legacy",
                                     category="guides", primary_keyword="existing topic")
    legacy_values = Article.objects.values().get(pk=article.pk)
    with connection.cursor() as cursor:
        old_columns = {column.name for column in connection.introspection.get_table_description(cursor, Article._meta.db_table)}
        old_constraints = connection.introspection.get_constraints(cursor, Article._meta.db_table)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    Article = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model("content_factory", "WrittenArticle")
    row = Article.objects.values().get(pk=article.pk)
    assert {key: row[key] for key in legacy_values} == legacy_values
    assert {key: value for key, value in row.items() if key not in legacy_values} == {
        "editorial_snapshot": None, "original_editorial_snapshot": None,
        "audience_id": "", "audience_version": None, "offer_id": "", "offer_version": None,
        "conversion_intent": None, "editorial_provenance_status": "unknown",
    }
    with connection.cursor() as cursor:
        columns = {column.name for column in connection.introspection.get_table_description(cursor, Article._meta.db_table)}
        constraints = connection.introspection.get_constraints(cursor, Article._meta.db_table)
    assert columns - old_columns == set(row) - set(legacy_values)
    assert old_columns <= columns
    for name, definition in old_constraints.items():
        assert constraints[name] == definition, name
    assert constraints["wa_org_audience_idx"]["columns"] == ["organization_id", "audience_id"]
    assert constraints["wa_org_offer_idx"]["columns"] == ["organization_id", "offer_id"]
    assert constraints["wa_org_audience_idx"]["index"] and constraints["wa_org_offer_idx"]["index"]
    print("0038 → 0041 replay passed: legacy row unchanged, unknown attribution, eight columns and both indexes present.", flush=True)


def run_checks(args, directory, database):
    sys.path.insert(0, str(ROOT))
    from django.db.backends.base.base import BaseDatabaseWrapper
    original_ensure = BaseDatabaseWrapper.ensure_connection

    def ensure_local_database(wrapper):
        config = wrapper.settings_dict
        if args.engine == "postgres":
            safe = (
                config["ENGINE"] == "django.db.backends.postgresql"
                and config.get("HOST") == database["HOST"]
                # Django's test-db creator uses NAME=None for the maintenance
                # connection, still strictly within our fresh socket cluster.
                and config.get("NAME") in {None, "icp", "test_icp"}
            )
        else:
            name = str(config.get("NAME", ""))
            safe = config["ENGINE"] == "django.db.backends.sqlite3" and (
                name.startswith(str(directory) + "/") or name.startswith("file:memorydb_") or name == ":memory:"
            )
        if not safe:
            raise RuntimeError("Refusing a connection outside this disposable icp database.")
        return original_ensure(wrapper)

    with (
        patch("dotenv.load_dotenv", return_value=False),
        patch("socket.socket.connect", side_effect=AssertionError("External network forbidden during icp tests")),
        patch("socket.create_connection", side_effect=AssertionError("External network forbidden during icp tests")),
        patch.object(BaseDatabaseWrapper, "ensure_connection", ensure_local_database),
    ):
        module = importlib.import_module("mlai.settings")
        module.DATABASES = {"default": database}
        module.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth_endpoint"] = None
        module.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth_magic_link"] = None
        import django
        django.setup()
        validate_migration_scope()
        from django.core.management import call_command
        try:
            if args.replay:
                replay_with_legacy_rows()
            if args.labels:
                call_command("test", *args.labels, interactive=False, verbosity=2, exclude_tags=args.exclude_tag)
            call_command("check")
            call_command("makemigrations", check=True, dry_run=True, interactive=False)
        finally:
            from django.db import connections
            connections.close_all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("postgres", "sqlite"), default="postgres")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--exclude-tag", action="append", default=[])
    parser.add_argument("labels", nargs="*")
    args = parser.parse_args()
    if not args.labels and not args.replay:
        parser.error("Select test labels or --replay.")
    programs = {}
    if args.engine == "postgres":
        for program in ("initdb", "pg_ctl", "createdb"):
            programs[program] = shutil.which(program)
            if not programs[program]:
                parser.error(f"Local PostgreSQL program missing: {program}")
    safe_environment = {
        "PATH": os.defpath, "LC_ALL": "C", "APP_ENV": "test", "DEBUG": "true",
        "APP_RELEASE": "disposable-icp-tests",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "SECRET_KEY": "synthetic-disposable-icp-signing-key",
        "DATABASE_URL": "sqlite:///:memory:",
        "ALLOWED_HOSTS": "testserver,localhost",
        "CONNECTOR_CREDENTIAL_KEYS": KEYS,
        "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
    }
    with tempfile.TemporaryDirectory(prefix="mlai-icp-db-", dir="/tmp") as temporary:
        directory = Path(temporary)
        started = False
        with patch.dict(os.environ, safe_environment, clear=True):
            try:
                if args.engine == "postgres":
                    data, sockets = directory / "data", directory / "socket"
                    sockets.mkdir(mode=0o700)
                    with (directory / "setup.log").open("w") as output:
                        subprocess.run([programs["initdb"], "-D", str(data), "-U", "icp",
                                        "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8"],
                                       check=True, stdout=output, stderr=subprocess.STDOUT)
                        with (data / "postgresql.conf").open("a") as config:
                            config.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{sockets}'\nport = 55439\nfsync = off\nshared_buffers = '32MB'\nmax_connections = 25\n")
                        subprocess.run([programs["pg_ctl"], "-D", str(data), "-l", str(directory / "postgres.log"), "-w", "start"],
                                       check=True, stdout=output, stderr=subprocess.STDOUT)
                        started = True
                        subprocess.run([programs["createdb"], "-h", str(sockets), "-p", "55439", "-U", "icp", "icp"], check=True)
                    database = {
                        "ENGINE": "django.db.backends.postgresql", "NAME": "icp",
                        "HOST": str(sockets), "PORT": "55439", "USER": "icp",
                        "TEST": {"NAME": "test_icp"},
                    }
                else:
                    database = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(directory / "icp.sqlite3")}
                print(f"Using new disposable {args.engine} database; .env and external network excluded.", flush=True)
                run_checks(args, directory, database)
            except subprocess.CalledProcessError:
                for name in ("setup.log", "postgres.log"):
                    path = directory / name
                    if path.exists():
                        print(path.read_text()[-4000:], file=sys.stderr)
                raise
            finally:
                if started:
                    subprocess.run([programs["pg_ctl"], "-D", str(data), "-m", "immediate", "-w", "stop"],
                                   check=True, stdout=subprocess.DEVNULL)
                print("Disposable database processes stopped; temporary files will be removed.", flush=True)


if __name__ == "__main__":
    main()
