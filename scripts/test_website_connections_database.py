"""Verify the specifically approved 0042 website lifecycle migration in a disposable database.

Never reads .env or accepts a database URL. PostgreSQL mode creates its own
socket-only cluster; SQLite mode uses a new temporary directory. Historical
migrations bootstrap synthetic test databases. Requires explicit local migration
approval per AGENTS.md; never invoke against an existing database.
"""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import socket
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
    migration = loader.disk_migrations[("content_factory", "0042_website_connection_lifecycle")]
    names = {op.name for op in migration.operations if isinstance(op, migrations.CreateModel)}
    assert names == {"WebsiteConnection", "WebsiteConnectionTarget", "WebsiteScanSnapshot", "WebsiteTemplateRevision", "WebsiteRepositoryMutation", "WebsiteConnectionOperation"}
    added = [op for op in migration.operations if isinstance(op, migrations.AddField)]
    assert [(op.model_name, op.name) for op in added] == [("organizationcontentconfig", "website_connection")]
    assert all(isinstance(op, (migrations.CreateModel, migrations.AddField, migrations.AddIndex, migrations.AddConstraint)) for op in migration.operations)
    print("Approved website lifecycle migration scope verified.", flush=True)


def replay_with_legacy_rows():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    old_targets = [(app, "0041_writtenarticle_editorial_attribution") if app == "content_factory" else (app, name) for app, name in executor.loader.graph.leaf_nodes()]
    old_targets.append(("content_factory", "0040_backfill_github_credential_envelopes"))
    executor.migrate(old_targets)
    old_apps = executor.loader.project_state(old_targets).apps
    Organization = old_apps.get_model("organizations", "Organization")
    Config = old_apps.get_model("content_factory", "OrganizationContentConfig")
    org = Organization.objects.create(domain="migration.example.test", name="Synthetic migration")
    cfg = Config.objects.create(organization=org, github_repo="example/site", article_template="Legacy template")
    before = Config.objects.values().get(pk=cfg.pk)
    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    expected = [("content_factory", "0042_website_connection_lifecycle", False), ("content_factory", "0043_merge_credentials_website", False)]
    actual = [(migration.app_label, migration.name, backwards) for migration, backwards in pending]
    assert actual == expected, f"Historical replay plan differs: {actual!r}"
    # 0043 is the already approved historical merge node on main, with no schema
    # operations. Replaying 0042 must include this existing dependency leaf.
    assert not executor.loader.disk_migrations[("content_factory", "0043_merge_credentials_website")].operations
    # Exercise the same fresh-process Django CLI as deployment, with our
    # disposable DB/network guards and environment intact. Import diagnostics
    # vary with environment; the deployed gate preserves the exact plan block.
    code = "from types import SimpleNamespace; from pathlib import Path; import json, sys; from scripts.test_website_connections_database import run_checks; run_checks(SimpleNamespace(engine=sys.argv[1], plan_only=True), Path(sys.argv[2]), json.loads(sys.argv[3]))"
    database = {key: connection.settings_dict[key] for key in ("ENGINE", "NAME", "HOST", "PORT", "USER")}
    result = subprocess.run([sys.executable, "-c", code, "postgres" if connection.vendor == "postgresql" else "sqlite", str(Path(database["NAME"]).parent) if connection.vendor == "sqlite" else str(Path(database["HOST"]).parent), json.dumps(database)],
                            cwd=ROOT, check=True, capture_output=True, text=True)
    from scripts.validate_website_deploy_config import canonical_migration_plan
    plan = canonical_migration_plan(result.stdout)
    assert "content_factory.0042_website_connection_lifecycle" in plan
    print(plan, flush=True)
    print("Candidate migration plan SHA256: " + hashlib.sha256(plan.encode()).hexdigest(), flush=True)
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    Config = executor.loader.project_state(executor.loader.graph.leaf_nodes()).apps.get_model("content_factory", "OrganizationContentConfig")
    after = Config.objects.values().get(pk=cfg.pk)
    assert {k: after[k] for k in before} == before
    assert after["website_connection_id"] is None
    print("0041 -> 0042 preserves legacy config without implicitly granting consent.", flush=True)
    executor = MigrationExecutor(connection)
    executor.migrate(old_targets)
    RevertedConfig = executor.loader.project_state(old_targets).apps.get_model("content_factory", "OrganizationContentConfig")
    assert RevertedConfig.objects.values().get(pk=cfg.pk) == before
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    print("0042 -> 0041 -> 0042 rollback/rollforward preserves the synthetic legacy row.", flush=True)


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
            raise RuntimeError("Refusing a connection outside this disposable website database.")
        return original_ensure(wrapper)

    original_connect, original_create_connection = socket.socket.connect, socket.create_connection

    def local_connect(sock, address):
        # Real threaded HTTP regression servers exercise synchronous worker ->
        # backend reentry. Only numeric loopback is permitted, never DNS/remote.
        if sock.family in {socket.AF_INET, socket.AF_INET6} and isinstance(address, tuple) and address[0] in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("External network forbidden during icp tests")

    def local_create_connection(address, *args, **kwargs):
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("External network forbidden during icp tests")
        return original_create_connection(address, *args, **kwargs)

    with (
        patch("dotenv.load_dotenv", return_value=False),
        patch("socket.socket.connect", new=local_connect),
        patch("socket.create_connection", new=local_create_connection),
        patch.object(BaseDatabaseWrapper, "ensure_connection", ensure_local_database),
    ):
        module = importlib.import_module("mlai.settings")
        module.DATABASES = {"default": database}
        module.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth_endpoint"] = None
        module.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["auth_magic_link"] = None
        import django
        django.setup()
        if getattr(args, "plan_only", False):
            from django.core.management import execute_from_command_line
            execute_from_command_line(["manage.py", "migrate", "--plan", "--noinput"])
            return
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
        "APP_RELEASE": "disposable-website-tests",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "SECRET_KEY": "synthetic-disposable-icp-signing-key",
        "DATABASE_URL": "sqlite:///:memory:",
        "ALLOWED_HOSTS": "testserver,localhost",
        "CONNECTOR_CREDENTIAL_KEYS": KEYS,
        "CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID": "test",
    }
    with tempfile.TemporaryDirectory(prefix="mlai-website-db-", dir="/tmp") as temporary:
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
