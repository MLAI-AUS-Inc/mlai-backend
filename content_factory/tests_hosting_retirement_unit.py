"""The removed blog-hosting surface cannot be restored by old flags or requests."""
import hashlib
import importlib
import io
import json
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
import zipfile
from django.apps import apps
from django.test import SimpleTestCase
from django.urls import resolve, Resolver404
from .publish_reviewed_draft import _send_dispatch, PublishReviewedError
from .failure_policy_engine import _rung
from .website_strategy import RUNGS

guard = importlib.import_module("content_factory.migrations.0051_remove_retired_blog_records")

class BlogHostingRemovedTests(SimpleTestCase):
    def test_legacy_models_are_not_registered(self):
        for name in ['HostedArticleSite', 'HostedArticlePublication']:
            with self.assertRaises(LookupError): apps.get_model('content_factory', name)

    def test_legacy_service_and_publish_routes_are_absent(self):
        for path in ['/api/content-factory/hosted-sites/', '/api/content-factory/hosted-sites/source/',
                     '/api/content-factory/hosted-sites/publications/legacy']:
            with self.assertRaises(Resolver404): resolve(path)

    def test_old_publish_path_has_only_the_generic_control_fallback(self):
        match=resolve('/api/v1/vibe-marketing/runs/legacy/hosted-publish')
        self.assertEqual(match.func.view_class.__name__, 'VibeMarketingRunControlView')

    def test_saved_hosted_publication_cannot_reenter_repository_dispatch(self):
        with patch('integrations.http_client.post') as remote:
            with self.assertRaises(PublishReviewedError):
                _send_dispatch(SimpleNamespace(), SimpleNamespace(run_request={'hosted_site_id':'old'}))
        remote.assert_not_called()

    def test_saved_hosted_strategy_uses_the_repository_ladder(self):
        self.assertNotIn('hosted', RUNGS)
        self.assertEqual(_rung({}, {'strategy_pin':'hosted'}), 'native_collection')


class ArchiveGuardTests(TestCase):
    def setUp(self):
        self.site = SimpleNamespace(state="retired", worker_url="", profile={"retirement": {"status": "retired"}})
        self.sites, self.publications = [self.site], []
        self.site_manager, self.publication_manager = Mock(), Mock()
        self.site_manager.using.return_value.order_by.side_effect = lambda _: self.sites
        self.publication_manager.using.return_value.order_by.side_effect = lambda _: self.publications
        self.apps = Mock()
        self.apps.get_model.side_effect = lambda _, name: SimpleNamespace(objects=(
            self.site_manager if name == "HostedArticleSite" else self.publication_manager))
        self.editor = SimpleNamespace(connection=SimpleNamespace(alias="default", vendor="sqlite"))
        self.records = {"sites": [{"model": "content_factory.hostedarticlesite", "pk": "synthetic-site",
                                  "fields": {"state": "retired"}}], "publications": []}
        self.archive = io.BytesIO()
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, content in {"records.json": json.dumps(self.records), "hosted-tables.dump": "synthetic",
                                  "pg-restore-list.txt": "synthetic", "RESTORE.txt": "synthetic"}.items():
                archive.writestr(name, content)
        self.data = self.archive.getvalue()
        self.manifest = {"archive_uri": "gs://private-test/retired.zip", "generation": "123",
                         "sha256": hashlib.sha256(self.data).hexdigest(),
                         "row_counts": {"sites": 1, "publications": 0},
                         "row_digests": {key: guard._digest(value) for key, value in self.records.items()}}
        self.blob = Mock(temporary_hold=True, storage_class="COLDLINE")
        self.blob.download_as_bytes.return_value = self.data
        self.bucket = Mock(storage_class="COLDLINE", iam_configuration=SimpleNamespace(
            uniform_bucket_level_access_enabled=True, public_access_prevention="enforced"))
        self.bucket.blob.return_value = self.blob
        self.client = Mock()
        self.client.get_bucket.return_value = self.bucket
        self.firebase = SimpleNamespace(get_storage_bucket=lambda: SimpleNamespace(client=self.client))
        self.stack = [patch.object(guard, "ARCHIVE", self.manifest),
                      patch("django.core.serializers.serialize", side_effect=lambda _, rows: json.dumps(
                          self.records["sites"] if rows else self.records["publications"])),
                      patch.dict("sys.modules", {"core.firebase_utils": self.firebase})]
        for context in self.stack:
            context.start()
            self.addCleanup(context.stop)

    def run_guard(self):
        guard.require_retirement_archive(self.apps, self.editor)

    def test_empty_install_does_not_contact_storage(self):
        self.sites = []
        self.run_guard()
        self.client.get_bucket.assert_not_called()

    def test_active_site_refuses_before_archive_access(self):
        self.site.state = "active"
        with self.assertRaisesRegex(RuntimeError, "fully retired"):
            self.run_guard()
        self.client.get_bucket.assert_not_called()

    def test_unwithdrawn_publication_refuses_before_archive_access(self):
        self.publications = [SimpleNamespace(status="live")]
        with self.assertRaisesRegex(RuntimeError, "withdrawn"):
            self.run_guard()
        self.client.get_bucket.assert_not_called()

    def test_changed_row_count_refuses_before_archive_access(self):
        self.manifest["row_counts"]["sites"] = 2
        with self.assertRaisesRegex(RuntimeError, "differ"):
            self.run_guard()
        self.client.get_bucket.assert_not_called()

    def test_public_bucket_configuration_refuses(self):
        self.bucket.iam_configuration.public_access_prevention = "inherited"
        with self.assertRaisesRegex(RuntimeError, "remain private"):
            self.run_guard()
        self.blob.download_as_bytes.assert_not_called()

    def test_removed_deletion_hold_refuses(self):
        self.blob.temporary_hold = False
        with self.assertRaisesRegex(RuntimeError, "deletion hold"):
            self.run_guard()

    def test_changed_archive_bytes_refuse(self):
        self.blob.download_as_bytes.return_value = b"changed"
        with self.assertRaisesRegex(RuntimeError, "reviewed digest"):
            self.run_guard()

    def test_exact_held_generation_and_rows_pass(self):
        self.run_guard()
        self.bucket.blob.assert_called_once_with("retired.zip", generation=123)
        self.blob.download_as_bytes.assert_called_once_with(if_generation_match=123, checksum="crc32c")
