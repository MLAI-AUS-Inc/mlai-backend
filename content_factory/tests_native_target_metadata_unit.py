"""Actual CF target examples retain identity through backend ingestion, without readiness."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from .website_contract import WebsiteAuthorityError
from .website_target_contract import native_target_digest, normalize_native_metadata


class NativeTargetMetadataTests(SimpleTestCase):
    def examples(self):
        value = json.loads((Path(__file__).resolve().parents[1] / "contracts/native-target-identity-v1.example.json").read_text())
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["kind"], "native_target_identity_examples")
        return value["examples"]

    def target(self):
        example = self.examples()[0]
        target = deepcopy(example["target"])
        target["contract"]["contract_digest"] = example["contract_digest"]
        return target

    def test_pinned_actual_producer_examples_match_the_backend_identity(self):
        for row in self.examples():
            with self.subTest(example=row["name"]):
                self.assertEqual(native_target_digest(row["target"]), row["contract_digest"])

    def test_nested_native_metadata_is_lifted_without_readiness_or_mutation(self):
        target = self.target()
        before = deepcopy(target)
        normalized = normalize_native_metadata(target)
        self.assertEqual(normalized["contract_digest"], target["contract"]["contract_digest"])
        self.assertEqual(normalized["live_marker"]["value"], target["contract"]["live_marker"]["value"])
        self.assertNotIn("marked_paths", normalized["live_marker"])
        self.assertNotIn("capabilities", normalized)
        self.assertNotIn("verification", normalized)
        self.assertEqual(target, before)
        self.assertEqual(normalize_native_metadata(normalized), normalized)
        self.assertEqual(normalize_native_metadata({**target, "contract_digest": None, "live_marker": None}), normalized)

    def test_conflicting_root_claims_wrong_digest_and_changed_route_are_rejected(self):
        for change in ({"contract_digest": "f" * 64}, {"live_marker": {"value": "f" * 64}},
                {"route_template": "/other/{slug}"}, {"contract": {"contract_digest": True, "live_marker": {"kind": "artifact_digest", "value": "d" * 64}}}):
            with self.subTest(change=change), self.assertRaises(WebsiteAuthorityError):
                normalize_native_metadata({**self.target(), **change})
        target = self.target()
        target["contract"]["live_marker"]["schema_version"] = True
        with self.assertRaises(WebsiteAuthorityError):
            normalize_native_metadata(target)

    def test_legacy_root_only_and_custom_contracts_retain_existing_behavior(self):
        for target in ({"target_id": "legacy", "contract_digest": "c" * 64, "live_marker": {"value": "d" * 64}},
                {"target_id": "custom", "delivery_adapter": "custom_contract_v1", "contract": {"contract_digest": "opaque", "live_marker": {}}}):
            self.assertEqual(normalize_native_metadata(target), target)

    def test_actual_record_scan_evidence_stores_native_identity_without_promoting_target(self):
        from . import website_connections as authority
        from .website_models import WebsiteConnectionTarget, WebsiteScanSnapshot
        from .models import OrganizationContentConfig
        raw = self.target()
        original = deepcopy(raw)
        connection = SimpleNamespace(pk="site", organization_id=7, generation=2, state="connected", verified_sha="a" * 40,
            capabilities={}, targets=Mock(), last_verified_at=None, blockers=[], save=Mock())
        candidates = Mock()
        candidates.first.return_value = None
        connection.targets.filter.side_effect = lambda **kwargs: [] if "verified_at__isnull" in kwargs else candidates
        config = SimpleNamespace(article_template="", design_guide="")
        token = authority._authority_depth.set(1)
        try:
            with patch.object(WebsiteScanSnapshot.objects, "get_or_create"), \
                    patch.object(WebsiteConnectionTarget.objects, "update_or_create") as persist, \
                    patch.object(OrganizationContentConfig.objects, "get", return_value=config):
                authority.record_scan_evidence(connection, {"source_sha": "a" * 40, "publish_targets": [raw]})
        finally:
            authority._authority_depth.reset(token)
        saved = persist.call_args.kwargs["defaults"]
        self.assertEqual(saved["contract"]["contract_digest"], original["contract"]["contract_digest"])
        self.assertEqual(saved["contract"]["live_marker"]["value"], original["contract"]["live_marker"]["value"])
        self.assertFalse(saved["capabilities"]["publishingReady"])
        self.assertIsNone(saved["verified_at"])
        self.assertFalse(connection.capabilities["publishingReady"])
        self.assertEqual(raw, original)
