"""Test the approved logo URL migration in a fresh, disposable PostgreSQL cluster.

Requires local initdb and pg_ctl binaries. No existing database, environment
credentials, provider calls or other migrations are used. Run with Python 3.11.
"""

import importlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4


def main():
    initdb = shutil.which("initdb")
    pg_ctl = shutil.which("pg_ctl")
    if not initdb or not pg_ctl:
        raise SystemExit("Install local PostgreSQL binaries (initdb and pg_ctl).")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    # macOS limits Unix socket paths; its default temp directory is too long.
    with TemporaryDirectory(prefix="mlai-logo-postgres-", dir="/tmp") as temporary:
        data = str(Path(temporary) / "data")
        process_environment = {"PATH": os.defpath, "LC_ALL": "C"}
        subprocess.run(
            [initdb, "-D", data, "-A", "trust", "-U", "logo_regression", "--no-locale"],
            env=process_environment, check=True, capture_output=True,
        )
        log = Path(temporary) / "postgres.log"
        try:
            subprocess.run(
                [pg_ctl, "-D", data, "-l", str(log),
                 "-o", f"-h 127.0.0.1 -p {port} -k {temporary}", "-w", "start"],
                env=process_environment, check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as error:
            raise SystemExit(log.read_text() if log.exists() else error.output.decode()) from error
        try:
            return run_regression(port)
        finally:
            subprocess.run(
                [pg_ctl, "-D", data, "-m", "fast", "-w", "stop"],
                env=process_environment, check=True, capture_output=True,
            )


def run_regression(port):
    safe_environment = {
        "PATH": os.defpath,
        "APP_ENV": "test",
        "APP_RELEASE": "logo-postgres-regression",
        "DEBUG": "true",
        "SECRET_KEY": "synthetic-local-test-key",
        "DATABASE_URL": f"postgresql://logo_regression@127.0.0.1:{port}/postgres",
        "DJANGO_SETTINGS_MODULE": "mlai.settings",
        "ALLOWED_HOSTS": "testserver,localhost",
    }
    with patch.dict(os.environ, safe_environment, clear=True), \
         patch("dotenv.load_dotenv", return_value=False), \
         patch("requests.sessions.Session.request", side_effect=AssertionError("Provider access forbidden")):
        import django
        django.setup()
        from django.db import connection, DataError, transaction
        from django.db.migrations.loader import MigrationLoader
        from founder_tools.models import VibeRaisingCompany
        from founder_tools.profile_fields import save_company_branding

        class LogoPostgresRegression(unittest.TestCase):
            def test_existing_urls_survive_migration_and_long_upload_can_save_and_clear(self):
                company_id = uuid4()
                existing_url = "https://example.test/existing-logo.png"
                url = (
                    "https://firebasestorage.googleapis.com/v0/b/mlai-main-website.firebasestorage.app/o/"
                    f"company-avatars%2F{company_id}%2F{uuid4().hex}.png?alt=media&token={uuid4()}"
                )
                self.assertGreater(len(url), 200)
                # Reproduce the production schema using synthetic rows only.
                with connection.cursor() as cursor:
                    cursor.execute("""
                        CREATE TABLE vibe_raising_viberaisingcompany (
                            id uuid PRIMARY KEY, avatar_url varchar(200),
                            updated_at timestamp with time zone NOT NULL DEFAULT now()
                        )
                    """)
                    cursor.execute(
                        "INSERT INTO vibe_raising_viberaisingcompany (id, avatar_url) VALUES (%s, %s)",
                        [company_id, existing_url],
                    )
                company = VibeRaisingCompany(id=company_id, organization_id=None, avatar_url=existing_url)
                company._state.adding = False
                with self.assertRaises(DataError):
                    save_company_branding(company, SimpleNamespace(id=1), url)
                self.assertEqual(self.saved_url(connection, company_id), existing_url)

                # Apply only the migration approved for this regression.
                loader = MigrationLoader(None)
                state = loader.project_state([("founder_tools", "0010_company_default_audience_visibility")])
                migration = importlib.import_module(
                    "founder_tools.migrations.0011_company_avatar_url_length"
                ).Migration("0011_company_avatar_url_length", "founder_tools")
                self.assertEqual(len(migration.operations), 1)
                with connection.schema_editor() as editor:
                    migration.apply(state, editor)
                self.assertEqual(self.saved_url(connection, company_id), existing_url)
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT character_maximum_length FROM information_schema.columns
                        WHERE table_name = 'vibe_raising_viberaisingcompany' AND column_name = 'avatar_url'
                    """)
                    self.assertEqual(cursor.fetchone()[0], 2048)
                save_company_branding(company, SimpleNamespace(id=1), url)
                self.assertEqual(self.saved_url(connection, company_id), url)
                # The outer caller's rollback still restores the previous logo.
                with transaction.atomic():
                    save_company_branding(company, SimpleNamespace(id=1), existing_url)
                    transaction.set_rollback(True)
                self.assertEqual(self.saved_url(connection, company_id), url)
                save_company_branding(company, SimpleNamespace(id=1), "")
                self.assertIsNone(self.saved_url(connection, company_id))

            @staticmethod
            def saved_url(database, company_id):
                with database.cursor() as cursor:
                    cursor.execute(
                        "SELECT avatar_url FROM vibe_raising_viberaisingcompany WHERE id = %s", [company_id]
                    )
                    return cursor.fetchone()[0]

        try:
            result = unittest.TextTestRunner(verbosity=2).run(
                unittest.defaultTestLoader.loadTestsFromTestCase(LogoPostgresRegression)
            )
            return 0 if result.wasSuccessful() else 1
        finally:
            connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
