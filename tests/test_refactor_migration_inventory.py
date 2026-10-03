"""Exercise approval and file-integrity guards without constructing a database."""

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from django.db.migrations.loader import MigrationLoader

from scripts import test_refactor_database as harness


class MigrationInventoryGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "migration.py"
        self.source.write_text("# synthetic migration source; never executed\n")
        self.inventory = self.root / "inventory.json"
        self.document = {
            "approval_status": "approved",
            "migration_count": 1,
            "migrations": [{"app": "synthetic", "name": "0001",
                            "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}],
        }
        self.loader = SimpleNamespace(
            disk_migrations={
                ("synthetic", "0001"): SimpleNamespace(__module__="synthetic_migration")
            },
            detect_conflicts=lambda: {},
        )

    def validate(self):
        self.inventory.write_text(json.dumps(self.document))
        with (
            patch.object(harness, "APPROVED_INVENTORY", self.inventory),
            patch("django.db.migrations.loader.MigrationLoader", return_value=self.loader),
            patch.object(harness.importlib, "import_module",
                         return_value=SimpleNamespace(__file__=str(self.source))),
        ):
            harness.validate_inventory()

    def test_exact_approved_file_is_accepted_without_a_database(self):
        self.validate()

    def test_pending_approval_is_rejected(self):
        self.document["approval_status"] = "pending"
        with self.assertRaisesRegex(RuntimeError, "still needs approval"):
            self.validate()

    def test_new_migration_outside_inventory_is_rejected(self):
        self.loader.disk_migrations[("synthetic", "0002")] = SimpleNamespace()
        with self.assertRaisesRegex(RuntimeError, "set differs"):
            self.validate()

    def test_changed_file_hash_is_rejected(self):
        self.source.write_text("# changed synthetic source\n")
        with self.assertRaisesRegex(RuntimeError, "Approved migration changed"):
            self.validate()

    def test_duplicate_inventory_entry_is_rejected(self):
        self.document["migrations"].append(self.document["migrations"][0].copy())
        with self.assertRaisesRegex(RuntimeError, "invalid count or duplicate"):
            self.validate()

    def test_conflicting_graph_is_rejected(self):
        self.loader.detect_conflicts = lambda: {"synthetic": ["0001", "0002"]}
        with self.assertRaisesRegex(RuntimeError, "conflicting leaf nodes"):
            self.validate()


class DisposableDatabaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.disposable = self.root / "disposable"
        self.disposable.mkdir()

    def sqlite_allowed(self, name):
        return harness.database_is_disposable(
            {"ENGINE": "django.db.backends.sqlite3", "NAME": name},
            engine="sqlite", directory=self.disposable, database={},
        )

    def test_local_file_and_known_memory_names_are_allowed(self):
        for name in (self.disposable / "test.sqlite3", ":memory:",
                     "file:memorydb_default?mode=memory&cache=shared"):
            with self.subTest(name=name):
                self.assertTrue(self.sqlite_allowed(name))

    def test_parent_traversal_cannot_escape_disposable_directory(self):
        self.assertFalse(self.sqlite_allowed(self.disposable / ".." / "outside.sqlite3"))

    def test_symlink_cannot_escape_disposable_directory(self):
        outside = self.root / "outside.sqlite3"
        outside.touch()
        link = self.disposable / "database.sqlite3"
        link.symlink_to(outside)
        self.assertFalse(self.sqlite_allowed(link))

    def test_file_uri_that_only_looks_like_memory_is_rejected(self):
        for name in ("file:memorydb_default", "file:memorydb_default?mode=rwc",
                     "file:/outside.sqlite3", ""):
            with self.subTest(name=name):
                self.assertFalse(self.sqlite_allowed(name))

    def test_postgres_requires_exact_private_cluster_and_database(self):
        database = {"ENGINE": "django.db.backends.postgresql", "HOST": str(self.disposable),
                    "NAME": "refactor", "PORT": "55439", "USER": "refactor"}
        def allowed(config):
            return harness.database_is_disposable(
                config, engine="postgres", directory=self.disposable, database=database,
            )
        for name in ("refactor", "test_refactor", None):
            self.assertTrue(allowed({**database, "NAME": name}))
        for field, wrong in (("HOST", "/tmp"), ("PORT", "5432"),
                             ("USER", "postgres"), ("NAME", "existing")):
            with self.subTest(field=field):
                self.assertFalse(allowed({**database, field: wrong}))
