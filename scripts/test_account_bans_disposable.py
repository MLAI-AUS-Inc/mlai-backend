"""Validate approved core.0069_account_ban in a fresh local PostgreSQL cluster.

Requires explicit migration approval per AGENTS.md. Uses synthetic data only,
never loads .env, and removes the socket-only cluster after the run.
"""

import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ("core", "0069_account_ban")


def check_migration_and_replay():
    from django.db import connection, migrations
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    migration = executor.loader.disk_migrations[MIGRATION]
    assert len(migration.operations) == 2
    create, constraint = migration.operations
    assert isinstance(create, migrations.CreateModel) and create.name == "AccountBan"
    assert {name for name, _ in create.fields} == {
        "id", "user", "email", "reason", "banned_by", "created_at", "updated_at",
        "revoked_at", "revoked_by", "revocation_pending",
    }
    assert isinstance(constraint, migrations.AddConstraint)
    assert constraint.constraint.name == "core_account_ban_email_ci_unique"
    assert len(migration.dependencies) == 1
    previous = migration.dependencies[0]
    assert previous[0] == "core" and previous[1].startswith("0068_")

    targets = executor.loader.graph.leaf_nodes()
    before = [previous if target == MIGRATION else target for target in targets]
    executor.migrate(before)
    old_user = executor.loader.project_state(before).apps.get_model("core", "User")
    user = old_user.objects.create(email="existing@example.test", is_active=True)
    original = old_user.objects.values().get(pk=user.pk)
    executor = MigrationExecutor(connection)
    executor.migrate(targets)
    apps = executor.loader.project_state(targets).apps
    assert apps.get_model("core", "User").objects.values().get(pk=user.pk) == original
    assert apps.get_model("core", "AccountBan").objects.count() == 0
    with connection.cursor() as cursor:
        constraints = connection.introspection.get_constraints(cursor, "core_accountban")
    assert constraints["core_account_ban_email_ci_unique"]["unique"]
    print("0068 → 0069 migration passed; existing account unchanged and email constraint present.", flush=True)


def run_checks(database, labels):
    sys.path.insert(0, str(ROOT))
    from django.db.backends.base.base import BaseDatabaseWrapper
    original = BaseDatabaseWrapper.ensure_connection

    def only_disposable_database(wrapper):
        config = wrapper.settings_dict
        if not (
            config["ENGINE"] == "django.db.backends.postgresql"
            and config.get("HOST") == database["HOST"]
            and config.get("NAME") in {None, "account_bans", "test_account_bans"}
        ):
            raise RuntimeError("Refusing a connection outside this disposable cluster.")
        return original(wrapper)

    with (
        patch("dotenv.load_dotenv", return_value=False),
        patch("socket.socket.connect", side_effect=AssertionError("External network forbidden")),
        patch("socket.create_connection", side_effect=AssertionError("External network forbidden")),
        patch.object(BaseDatabaseWrapper, "ensure_connection", only_disposable_database),
    ):
        module = importlib.import_module("mlai.settings")
        module.DATABASES = {"default": database}
        # Match manage.py test: this custom entrypoint is not detected by
        # settings._RUNNING_TESTS. Password-reset throttles remain enabled.
        for scope in ("auth_endpoint", "auth_magic_link"):
            module.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"][scope] = None
        module.CACHES = {
            alias: {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": f"ban-tests-{alias}"}
            for alias in module.CACHES
        }
        module.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        import django
        django.setup()
        from django.core.management import call_command
        from django.db import connections
        try:
            check_migration_and_replay()
            call_command("test", *labels, interactive=False, verbosity=2)
            call_command("check")
            call_command("makemigrations", check=True, dry_run=True, interactive=False)
        finally:
            connections.close_all()


def main():
    programs = {name: shutil.which(name) for name in ("initdb", "pg_ctl", "createdb")}
    if not all(programs.values()):
        raise SystemExit("Add local PostgreSQL initdb, pg_ctl and createdb to PATH.")
    labels = sys.argv[1:] or ["community_chat.tests.test_account_bans"]
    environment = {
        "PATH": os.defpath, "LC_ALL": "C", "APP_ENV": "test", "DEBUG": "true",
        "APP_RELEASE": "disposable-account-ban-tests",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "SECRET_KEY": "synthetic-disposable-ban-test-signing-key",
        "DATABASE_URL": "sqlite:///:memory:", "ALLOWED_HOSTS": "testserver,localhost",
        "CONNECTOR_CREDENTIAL_KEYS": '{"test":"MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="}',
        "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
    }
    with tempfile.TemporaryDirectory(prefix="mlai-ban-db-", dir="/tmp") as temporary:
        directory = Path(temporary)
        data, sockets = directory / "data", directory / "socket"
        sockets.mkdir(mode=0o700)
        started = False
        with patch.dict(os.environ, environment, clear=True):
            try:
                with (directory / "setup.log").open("w") as output:
                    subprocess.run([programs["initdb"], "-D", str(data), "-U", "ban_tests", "--auth-local=trust", "--auth-host=reject", "--no-locale", "-E", "UTF8"], check=True, stdout=output, stderr=subprocess.STDOUT)
                    with (data / "postgresql.conf").open("a") as config:
                        config.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{sockets}'\nport = 55439\nfsync = off\nshared_buffers = '32MB'\nmax_connections = 25\n")
                    subprocess.run([programs["pg_ctl"], "-D", str(data), "-l", str(directory / "postgres.log"), "-w", "start"], check=True, stdout=output, stderr=subprocess.STDOUT)
                    started = True
                    subprocess.run([programs["createdb"], "-h", str(sockets), "-p", "55439", "-U", "ban_tests", "account_bans"], check=True)
                print("Using a new socket-only PostgreSQL cluster; .env and external network excluded.", flush=True)
                run_checks({
                    "ENGINE": "django.db.backends.postgresql", "NAME": "account_bans",
                    "HOST": str(sockets), "PORT": "55439", "USER": "ban_tests",
                    "TEST": {"NAME": "test_account_bans"},
                }, labels)
            except subprocess.CalledProcessError:
                for name in ("setup.log", "postgres.log"):
                    path = directory / name
                    if path.exists():
                        print(path.read_text()[-4000:], file=sys.stderr)
                raise
            finally:
                if started:
                    subprocess.run([programs["pg_ctl"], "-D", str(data), "-m", "immediate", "-w", "stop"], check=True, stdout=subprocess.DEVNULL)
                print("Disposable cluster stopped; temporary files removed on exit.", flush=True)


if __name__ == "__main__":
    main()
