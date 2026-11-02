"""The production-generated CF source descriptor is a closed identity, not consent."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import SimpleTestCase, TransactionTestCase

from .run_legacy_source import fetch_legacy_source, validate_legacy_source
from .run_ownership import RunOwnershipConflict
from .self_heal_testing import make_p2_org
from workflow_runs.models import ContentFactoryRun

# Copied byte-for-byte from CF ef928b86 production read_source fixture.
DESCRIPTOR = {"schema_version": 1, "source_run_id": "synthetic-legacy-scan", "workflow": "repo_scan",
    "domain": "synthetic-r3.example", "github_repo": "synthetic/r3-site",
    "request_sha256": "e88b135fa5f3f8df70c646b75e9ba09090c8758529e40f98e5b6117404ae984b",
    "content_sha256": "f86de969b6bab98a67aa29ac78c17aea8e591ebef6f7f29f4009706ee4676c39",
    "descriptor_sha256": "59542066124f4e62ff0ab9bbfd8f7b722f7e7fdeb17e5f171fe61ce3cb1e76fc"}


def resigned(value):
    value = deepcopy(value)
    value["descriptor_sha256"] = hashlib.sha256(json.dumps({key: item for key, item in value.items()
        if key != "descriptor_sha256"}, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")).hexdigest()
    return value


class LegacySourceDescriptorTests(SimpleTestCase):
    def run_fixture(self, **changes):
        return SimpleNamespace(run_id=DESCRIPTOR["source_run_id"], workflow="scan", domain=DESCRIPTOR["domain"],
            github_repo=DESCRIPTOR["github_repo"], result={}, **changes)

    def test_exact_production_generated_fixture_and_closed_worker_alias(self):
        original = deepcopy(DESCRIPTOR)
        self.assertEqual(validate_legacy_source(self.run_fixture(), DESCRIPTOR), DESCRIPTOR)
        self.assertEqual(DESCRIPTOR, original)
        for backend, actual in [("scaffold_articles", "article_system_setup"), ("article_generation", "direct_generate")]:
            run = self.run_fixture()
            run.workflow = backend
            value = resigned({**DESCRIPTOR, "workflow": actual})
            self.assertEqual(validate_legacy_source(run, value), value)

    def test_extra_missing_boolean_corrupt_hash_and_foreign_source_are_refused(self):
        for changed in [{**DESCRIPTOR, "schema_version": True}, {**DESCRIPTOR, "extra": "not allowed"},
                {key: value for key, value in DESCRIPTOR.items() if key != "content_sha256"},
                {**DESCRIPTOR, "content_sha256": "0" * 64},
                resigned({**DESCRIPTOR, "source_run_id": "other-source"}),
                resigned({**DESCRIPTOR, "workflow": "article_system_setup"}),
                resigned({**DESCRIPTOR, "github_repo": "foreign/repo"})]:
            with self.assertRaises(RunOwnershipConflict):
                validate_legacy_source(self.run_fixture(), changed)

    def test_descriptor_uses_ascii_not_utf8_json_hash_and_never_reconstructs_content(self):
        run = self.run_fixture()
        run.domain = "café.synthetic.example"
        value = resigned({**DESCRIPTOR, "domain": run.domain})
        self.assertEqual(validate_legacy_source(run, value), value)
        raw = {key: item for key, item in value.items() if key != "descriptor_sha256"}
        wrong = {**value, "descriptor_sha256": hashlib.sha256(json.dumps(raw, ensure_ascii=False,
            sort_keys=True, separators=(",", ":")).encode()).hexdigest()}
        with self.assertRaises(RunOwnershipConflict):
            validate_legacy_source(run, wrong)


class LegacySourceFetchTests(TransactionTestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.run = ContentFactoryRun.objects.create(organization=fixture.organization, run_id=DESCRIPTOR["source_run_id"],
            workflow="scan", domain=fixture.organization.domain, github_repo=DESCRIPTOR["github_repo"],
            run_request={"retained_original": True}, result={"retained_history": True})
        self.descriptor = resigned({**DESCRIPTOR, "domain": self.run.domain})

    def test_authenticated_get_is_outside_sql_and_preserves_exact_parent_bytes(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        def provider(url, **kwargs):
            self.assertFalse(transaction.get_connection().in_atomic_block)
            self.assertEqual(url, "http://synthetic-worker/api/runs/synthetic-legacy-scan/continuation-source")
            self.assertEqual(kwargs["headers"], {"X-API-KEY": "synthetic-internal-key"})
            self.assertEqual(kwargs["timeout"], (5, 15))
            self.assertIs(kwargs["stream"], True)
            self.assertIs(kwargs["allow_redirects"], False)
            return Mock(status_code=200, headers={}, iter_content=Mock(return_value=iter([json.dumps(self.descriptor).encode()])))
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-KEY": "synthetic-internal-key"}), \
                patch("content_factory.vibe_marketing_views.http_client.get", side_effect=provider):
            self.assertEqual(fetch_legacy_source(self.run), self.descriptor)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)

    def test_failed_read_does_not_make_up_a_descriptor(self):
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                patch("content_factory.vibe_marketing_views.http_client.get", return_value=Mock(status_code=404)):
            with self.assertRaises(RunOwnershipConflict):
                fetch_legacy_source(self.run)

    def test_size_and_total_deadline_are_bounded_and_always_close_response(self):
        from .run_legacy_source import MAX_DESCRIPTOR_BYTES
        for chunks, clock in [([b"x" * (MAX_DESCRIPTOR_BYTES + 1)], [0, 0]),
                ([b"{"], [0, 16])]:
            response = Mock(status_code=200, headers={}, iter_content=Mock(return_value=iter(chunks)))
            with patch("content_factory.vibe_marketing_views._content_factory_remote_config", return_value={"enabled": True, "base_url": "http://synthetic-worker"}), \
                    patch("content_factory.vibe_marketing_views.http_client.get", return_value=response), \
                    patch("content_factory.run_legacy_source.time.monotonic", side_effect=clock):
                with self.assertRaises(RunOwnershipConflict):
                    fetch_legacy_source(self.run)
            response.close.assert_called_once()
