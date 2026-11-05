"""Synthetic PostgreSQL proof for the two approved hosted-blog table removals."""
import hashlib
import importlib
import io
import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import zipfile

from django.core import serializers
from django.db import close_old_connections, connection, connections, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, tag

CLEANUP_MIGRATION_NAME = "0051_remove_retired_blog_records"
TABLES = {"content_factory_hosted_article_site", "content_factory_hosted_article_publication"}


@tag("postgres-only")
class HostedBlogArchiveMigrationTests(TransactionTestCase):
    def setUp(self):
        super().setUp()
        if connection.vendor != "postgresql":
            self.skipTest("The archive guard's table-lock boundary requires PostgreSQL")
        self.module = importlib.import_module("content_factory.migrations." + CLEANUP_MIGRATION_NAME)
        self.addCleanup(self.restore_schema)
        executor = MigrationExecutor(connection)
        executor.migrate([("content_factory", "0050_retire_hosted_article_sites")])
        executor = MigrationExecutor(connection)
        old_apps = executor.loader.project_state(list(executor.loader.applied_migrations)).apps
        Organization = old_apps.get_model("organizations", "Organization")
        self.Site = old_apps.get_model("content_factory", "HostedArticleSite")
        self.Publication = old_apps.get_model("content_factory", "HostedArticlePublication")
        self.Article = old_apps.get_model("content_factory", "WrittenArticle")
        self.org = Organization.objects.create(domain="archive-migration.example.test", name="Synthetic archive")
        self.article = self.Article.objects.create(organization_id=self.org.pk, title="Preserved article",
            slug="preserved-article", category="guides", primary_keyword="synthetic archive")
        self.article_before = self.Article.objects.values().get(pk=self.article.pk)
        self.site = self.Site.objects.create(organization_id=self.org.pk, domain="archive-migration.example.test",
            state="retired", worker_url="", profile={"retirement": {"status": "retired"}})

    def restore_schema(self):
        # Only synthetic rows in this disposable test database are removed.
        # This also restores the current graph after an intentional guard refusal.
        if TABLES <= set(connection.introspection.table_names()):
            self.Publication.objects.all().delete()
            self.Site.objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def migrate(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_active_site_refuses_without_contacting_storage(self):
        self.Site.objects.filter(pk=self.site.pk).update(state="active")
        factory = Mock()
        with patch.dict(sys.modules, {"core.firebase_utils": SimpleNamespace(get_storage_bucket=factory)}):
            with self.assertRaisesRegex(RuntimeError, "fully retired"):
                self.migrate()
        factory.assert_not_called()
        self.assertTrue(TABLES <= set(connection.introspection.table_names()))
        self.assertTrue(self.Site.objects.filter(pk=self.site.pk, state="active").exists())

    def test_changed_retired_rows_refuse_before_contacting_storage(self):
        # Synthetic fixture fingerprints can never match the real pinned export.
        factory = Mock()
        with patch.dict(sys.modules, {"core.firebase_utils": SimpleNamespace(get_storage_bucket=factory)}):
            with self.assertRaisesRegex(RuntimeError, "differ from"):
                self.migrate()
        factory.assert_not_called()
        self.assertTrue(TABLES <= set(connection.introspection.table_names()))
        self.assertEqual(self.Article.objects.values().get(pk=self.article.pk), self.article_before)

    def test_exact_protected_archive_drops_only_legacy_tables_and_excludes_a_late_writer(self):
        records = {"sites": json.loads(serializers.serialize("json", self.Site.objects.order_by("pk"))),
                   "publications": json.loads(serializers.serialize("json", self.Publication.objects.order_by("pk")))}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, value in {"records.json": json.dumps(records), "hosted-tables.dump": "synthetic",
                                "pg-restore-list.txt": "synthetic", "RESTORE.txt": "synthetic"}.items():
                archive.writestr(name, value)
        data = output.getvalue()
        manifest = {"archive_uri": "gs://private-synthetic-archive/retired.zip", "generation": "123",
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "row_counts": {name: len(rows) for name, rows in records.items()},
                    "row_digests": {name: hashlib.sha256(json.dumps(rows, sort_keys=True,
                        separators=(",", ":")).encode()).hexdigest() for name, rows in records.items()}}
        writer_finished = threading.Event()
        outcomes, threads = [], []

        def writer():
            close_old_connections()
            try:
                with transaction.atomic():
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout='200ms'")
                    self.Site.objects.filter(pk=self.site.pk).update(base_path="/late-write/")
                outcomes.append("unexpected_write")
            except Exception as error:
                cause = getattr(error, "__cause__", None)
                outcomes.append(getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
                                or type(error).__name__)
            finally:
                connections.close_all()
                writer_finished.set()

        def download(**kwargs):
            self.assertEqual(kwargs, {"if_generation_match": 123, "checksum": "crc32c"})
            thread = threading.Thread(target=writer)
            threads.append(thread)
            thread.start()
            self.assertTrue(writer_finished.wait(5), "A late writer did not reach the lock-timeout boundary")
            self.assertEqual(outcomes, ["55P03"], "A late writer bypassed the migration's table locks")
            return data

        blob = Mock(temporary_hold=True, storage_class="COLDLINE")
        blob.download_as_bytes.side_effect = download
        bucket = Mock(storage_class="COLDLINE", iam_configuration=SimpleNamespace(
            uniform_bucket_level_access_enabled=True, public_access_prevention="enforced"))
        bucket.blob.return_value = blob
        client = Mock()
        client.get_bucket.return_value = bucket
        factory = Mock(return_value=SimpleNamespace(client=client))
        try:
            with patch.object(self.module, "ARCHIVE", manifest), patch.dict(sys.modules, {
                    "core.firebase_utils": SimpleNamespace(get_storage_bucket=factory)}):
                self.migrate()
                self.assertTrue(TABLES.isdisjoint(connection.introspection.table_names()))
                self.assertEqual(self.Article.objects.values().get(pk=self.article.pk), self.article_before)
                bucket.blob.assert_called_once_with("retired.zip", generation=123)
                blob.reload.assert_called_once_with(if_generation_match=123)
                # Reversing the schema creates empty tables; it does not restore archived rows.
                executor = MigrationExecutor(connection)
                executor.migrate([("content_factory", "0050_retire_hosted_article_sites")])
                self.assertEqual(self.Site.objects.count(), 0)
                self.assertEqual(self.Publication.objects.count(), 0)
                self.assertEqual(self.Article.objects.values().get(pk=self.article.pk), self.article_before)
                factory.assert_called_once_with()
        finally:
            for thread in threads:
                thread.join(timeout=8)
        self.assertTrue(writer_finished.is_set())
