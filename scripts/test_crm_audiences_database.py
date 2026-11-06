"""Run CRM regressions on an approved disposable PostgreSQL schema only.

The inventory digest is a guard, not authorization. Obtain approval for its exact
migration contents first. No dotenv, real credentials or external network is used.
A kept test database may be reused on the same dedicated local instance, which
must be removed after validation.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import pwd
import socket
import sys
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migration-inventory", type=Path, required=True)
    parser.add_argument("--approved-inventory-sha256", required=True)
    parser.add_argument("--postgres-port", type=int, required=True)
    parser.add_argument("--keep-test-database", action="store_true")
    parser.add_argument("--test-label", action="append", required=True)
    args = parser.parse_args()
    if not 1024 < args.postgres_port < 65536 or args.postgres_port == 5432:
        raise SystemExit("Use a dedicated non-default local PostgreSQL port.")
    raw = args.migration_inventory.read_bytes()
    if hashlib.sha256(raw).hexdigest() != args.approved_inventory_sha256:
        raise SystemExit("Migration approval inventory changed.")
    expected = {(r["app"], r["name"]): r["sha256"] for r in json.loads(raw)["migrations"]}
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    user = pwd.getpwuid(os.getuid()).pw_name
    name = "test_crm_audiences"
    environment = {
        "PATH": os.defpath, "APP_ENV": "test", "APP_RELEASE": "crm-audience-synthetic-tests",
        "DEBUG": "true", "SECRET_KEY": "synthetic-test-signing-key",
        "DJANGO_SETTINGS_MODULE": "mlai.settings", "ALLOWED_HOSTS": "testserver,localhost",
        "DATABASE_URL": f"postgresql://{user}@127.0.0.1:{args.postgres_port}/postgres",
    }
    original_connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[:2] == ("127.0.0.1", args.postgres_port):
            return original_connect(sock, address)
        raise AssertionError("External network forbidden")

    with patch.dict(os.environ, environment, clear=True), patch("dotenv.load_dotenv", return_value=False), patch("socket.socket.connect", local_only), patch("socket.create_connection", side_effect=AssertionError("External network forbidden")):
        import django
        from django.conf import settings
        settings.DATABASES["default"]["TEST"] = {"NAME": name}
        settings.CACHES = {key: {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": f"crm-tests-{key}"} for key in settings.CACHES}
        settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
        django.setup()
        from django.db.backends.base.base import BaseDatabaseWrapper
        from django.db.migrations.loader import MigrationLoader
        from django.test.runner import DiscoverRunner
        loader = MigrationLoader(None)
        if set(loader.disk_migrations) != set(expected):
            raise SystemExit("Migration inventory changed; obtain exact approval.")
        for key, migration in loader.disk_migrations.items():
            module = importlib.import_module(migration.__module__)
            if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != expected[key]:
                raise SystemExit(f"Migration changed: {key}")
        original = BaseDatabaseWrapper.ensure_connection

        def disposable_only(wrapper):
            config = wrapper.settings_dict
            if not (config["ENGINE"] == "django.db.backends.postgresql" and config.get("HOST") == "127.0.0.1" and str(config.get("PORT")) == str(args.postgres_port) and config["NAME"] in {None, "postgres", "__no_db__", name}):
                raise AssertionError("Only the disposable CRM test database is permitted")
            return original(wrapper)

        with patch.object(BaseDatabaseWrapper, "ensure_connection", disposable_only):
            failures = DiscoverRunner(verbosity=2, interactive=False, keepdb=args.keep_test_database).run_tests(args.test_label)
        return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
