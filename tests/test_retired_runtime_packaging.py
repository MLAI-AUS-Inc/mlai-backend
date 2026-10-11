from pathlib import Path
import runpy
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from django.urls import Resolver404, resolve

ROOT = Path(__file__).resolve().parents[1]


class RetiredRuntimePackagingTests(unittest.TestCase):
    def test_delivery_omits_development_artifacts_and_preserves_runtime_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            target = Path(directory) / "target"
            runtime = (".env.example", "mlai/urls.py", "roo/migrations/0038_example.py",
                       "startup_updates/templates/example.html", "contracts/run-statuses.json",
                       "scenarios/example.json", "scripts/start-web.sh")
            excluded = (".env", ".env.local-docker", ".DS_Store", "docs/report.md",
                        ".github/workflows/deploy.yml", "roo/tests.py", "roo/tests_legacy.py",
                        "tests/test_example.py", "staticfiles/admin.css", ".venv/bin/python")
            for name in runtime + excluded:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("synthetic fixture")
            target.mkdir()
            subprocess.run(["rsync", "-a", "--exclude-from", str(ROOT / "scripts/deploy-excludes.txt"),
                            str(source) + "/", str(target) + "/"], check=True, timeout=10,
                           capture_output=True, text=True)
            for name in runtime:
                self.assertTrue((target / name).is_file(), name)
            for name in excluded:
                self.assertFalse((target / name).exists(), name)

    def test_obsolete_environment_cleanup_preserves_shared_credentials(self):
        line = next(line.strip() for line in (ROOT / "deploy.sh").read_text().splitlines()
                    if line.strip().startswith("sed -i -E"))
        expression = shlex.split(line)[3]
        fixture = ("ROO_API_KEY=synthetic-shared\nINTERNAL_API_KEY=synthetic-internal\n"
                   "OFFICE_MANAGER_SLACK_BOT_TOKEN=synthetic-retired\nVICTOR_AI_ROO_ENABLED=true\n"
                   "COMMITTEE_REMUNERATION_ENABLED=true\nCUSTOMERIO_VICTOR_REGISTRATION_TEMPLATE_ID=4\n"
                   "ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED=true\n")
        result = subprocess.run(["sed", "-E", expression], input=fixture, capture_output=True,
                                text=True, timeout=5, check=True)
        self.assertEqual(result.stdout, "ROO_API_KEY=synthetic-shared\nINTERNAL_API_KEY=synthetic-internal\n"
                                       "ORG_MEMORY_PRODUCTION_DEPLOY_ENABLED=true\n")

    def test_retired_api_routes_are_unavailable_but_shared_booking_routes_resolve(self):
        for path in ("/api/v1/victor-ai/applications/", "/api/v1/victor-ai/roo/applications/",
                     "/api/v1/points/coworking/office-manager/preflight/"):
            with self.subTest(path=path), self.assertRaises(Resolver404):
                resolve(path)
        self.assertIsNotNone(resolve("/api/v1/points/meeting-rooms/rooms/"))

    def test_firebase_import_is_lazy_and_storage_initializes_once(self):
        import firebase_admin
        from firebase_admin import credentials, storage

        app = object()

        def initialize(*args, **kwargs):
            firebase_admin._apps["[DEFAULT]"] = app
            return app

        with patch.object(firebase_admin, "_apps", {}), \
             patch.object(firebase_admin, "get_app", return_value=app), \
             patch.object(firebase_admin, "initialize_app", side_effect=initialize) as initialize_app, \
             patch.object(credentials, "Certificate"), patch.object(storage, "bucket", return_value="bucket"):
            module = runpy.run_path(str(ROOT / "core/firebase_utils.py"))
            initialize_app.assert_not_called()
            module["get_storage_bucket"].__globals__.update(
                FIREBASE_PROJECT_ID="synthetic-project", FIREBASE_CLIENT_EMAIL="test@example.invalid",
                FIREBASE_PRIVATE_KEY="synthetic-private-key")
            self.assertEqual(module["get_storage_bucket"](), "bucket")
            self.assertEqual(module["get_storage_bucket"](), "bucket")
            initialize_app.assert_called_once()
