"""Run the specifically approved refactor migrations in a disposable database.

Never reads .env or accepts a database URL. PostgreSQL mode creates its own
temporary, socket-only cluster; SQLite mode uses a new temporary directory.
The historical migration inventory must match before any migration is applied.
"""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NEW_MIGRATIONS = {
    ("content_factory", "0039_encrypt_github_credentials"),
    ("content_factory", "0040_backfill_github_credential_envelopes"),
}
KEYS = '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}'


def validate_inventory():
    from django.db.migrations.loader import MigrationLoader
    inventory = json.loads((ROOT / "docs/refactor-test-migrations-2026-09-14.json").read_text())
    approved = {(row["app"], row["name"]): row["sha256"] for row in inventory["migrations"]}
    loader = MigrationLoader(None)
    if set(loader.disk_migrations) != set(approved) | NEW_MIGRATIONS:
        raise RuntimeError("Migration set differs from the specifically approved inventory.")
    for key, expected in approved.items():
        module = importlib.import_module(loader.disk_migrations[key].__module__)
        if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected:
            raise RuntimeError(f"Approved migration changed: {key[0]}.{key[1]}")
    print(f"Approved migration inventory verified: {len(approved)} existing + 2 new.", flush=True)


def replay_with_legacy_rows():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from integrations.fields import encrypt_credential_value, LegacyPlaintextEncryptedTextField
    executor = MigrationExecutor(connection)
    old_targets = [
        (app, "0038_delete_seo_topicmap_researchsession") if app == "content_factory" else (app, name)
        for app, name in executor.loader.graph.leaf_nodes()
    ]
    executor.migrate(old_targets)
    old_apps = executor.loader.project_state(old_targets).apps
    Organization = old_apps.get_model("organizations", "Organization")
    Config = old_apps.get_model("content_factory", "OrganizationContentConfig")
    existing_ciphertext = encrypt_credential_value("synthetic-existing-replay")
    seeds = [("synthetic-replay-access", "synthetic-replay-refresh"), (existing_ciphertext, None), (None, "")]
    ids = []
    for index, (access, refresh) in enumerate(seeds):
        org = Organization.objects.create(domain=f"replay-{index}.example.test")
        ids.append(Config.objects.create(
            organization=org, github_token_encrypted=access, github_refresh_token_encrypted=refresh,
        ).pk)
    executor = MigrationExecutor(connection)
    executor.migrate([("content_factory", "0039_encrypt_github_credentials")])
    state = executor.loader.project_state([("content_factory", "0039_encrypt_github_credentials")])
    Config = state.apps.get_model("content_factory", "OrganizationContentConfig")
    assert isinstance(Config._meta.get_field("github_token_encrypted"), LegacyPlaintextEncryptedTextField)
    assert Config.objects.get(pk=ids[0]).github_token_encrypted == "synthetic-replay-access"
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    with connection.cursor() as cursor:
        cursor.execute("SELECT github_token_encrypted, github_refresh_token_encrypted FROM content_factory_org_config ORDER BY id")
        rows = cursor.fetchall()
    assert all(value.startswith("mlai-enc:v1:") for value in rows[0])
    assert rows[1:] == [(existing_ciphertext, None), (None, "")]
    print("Fresh migration replay and seeded 0038 → 0039 → 0040 conversion passed.", flush=True)


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
                and config.get("NAME") in {None, "refactor", "test_refactor"}
            )
        else:
            name = str(config.get("NAME", ""))
            safe = config["ENGINE"] == "django.db.backends.sqlite3" and (
                name.startswith(str(directory) + "/") or name.startswith("file:memorydb_") or name == ":memory:"
            )
        if not safe:
            raise RuntimeError("Refusing a connection outside this disposable refactor database.")
        return original_ensure(wrapper)

    with (
        patch("dotenv.load_dotenv", return_value=False),
        patch("socket.socket.connect", side_effect=AssertionError("External network forbidden during refactor tests")),
        patch("socket.create_connection", side_effect=AssertionError("External network forbidden during refactor tests")),
        patch.object(BaseDatabaseWrapper, "ensure_connection", ensure_local_database),
    ):
        module = importlib.import_module("mlai.settings")
        module.DATABASES = {"default": database}
        import django
        django.setup()
        validate_inventory()
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
        "APP_RELEASE": "disposable-refactor-tests",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "SECRET_KEY": "synthetic-disposable-refactor-signing-key",
        "DATABASE_URL": "sqlite:///:memory:",
        "ALLOWED_HOSTS": "testserver,localhost",
        "CONNECTOR_CREDENTIAL_KEYS": KEYS,
        "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
    }
    with tempfile.TemporaryDirectory(prefix="mlai-refactor-db-", dir="/tmp") as temporary:
        directory = Path(temporary)
        started = False
        with patch.dict(os.environ, safe_environment, clear=True):
            try:
                if args.engine == "postgres":
                    data, sockets = directory / "data", directory / "socket"
                    sockets.mkdir(mode=0o700)
                    with (directory / "setup.log").open("w") as output:
                        subprocess.run([programs["initdb"], "-D", str(data), "-U", "refactor",
                                        "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8"],
                                       check=True, stdout=output, stderr=subprocess.STDOUT)
                        with (data / "postgresql.conf").open("a") as config:
                            config.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{sockets}'\nport = 55439\nfsync = off\nshared_buffers = '32MB'\nmax_connections = 25\n")
                        subprocess.run([programs["pg_ctl"], "-D", str(data), "-l", str(directory / "postgres.log"), "-w", "start"],
                                       check=True, stdout=output, stderr=subprocess.STDOUT)
                        started = True
                        subprocess.run([programs["createdb"], "-h", str(sockets), "-p", "55439", "-U", "refactor", "refactor"], check=True)
                    database = {
                        "ENGINE": "django.db.backends.postgresql", "NAME": "refactor",
                        "HOST": str(sockets), "PORT": "55439", "USER": "refactor",
                        "TEST": {"NAME": "test_refactor"},
                    }
                else:
                    database = {"ENGINE": "django.db.backends.sqlite3", "NAME": str(directory / "refactor.sqlite3")}
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
