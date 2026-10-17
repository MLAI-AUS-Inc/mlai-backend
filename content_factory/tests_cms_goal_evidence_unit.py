"""Only current private native pipeline facts can fill native goal milestones."""

from copy import deepcopy
import json
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_contract as goals
from . import cms_goal_evidence as evidence
from . import cms_capture as capture
from . import cms_article_publication as publishing
from . import tests_cms_article_publication_unit as fixtures
from .cms_api_transport import CmsApiError
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


@override_settings(ARTICLES_CHANNEL_DISCOVERY_ENABLED=True, ARTICLES_PUBLISHING_CHANNELS_ENABLED=True,
    ARTICLES_CMS_CHANNEL_ENABLED=True, ARTICLES_CHANNEL_ARTIFACTS_ENABLED=True,
    CONNECTOR_CREDENTIAL_KEYS=json.dumps({"test": fixtures.fixtures.fixtures.TEST_KEY}), CONNECTOR_CREDENTIAL_ACTIVE_KEY_ID="test")
class CmsGoalEvidenceTests(SimpleTestCase):
    platform = "wordpress"
    reserve, set_handoff, acknowledge = fixtures.reserve, fixtures.set_handoff, fixtures.acknowledge
    advance = fixtures.CmsArticlePublicationTests.advance

    def setUp(self):
        fixtures.prepare(self)
        self.capture = capture.start_capture(self.context, self.config, user=self.user,
            data={**contract_for(self.connection), "client_request_id": str(uuid4())})
        with patch.object(capture, "request_json", return_value=([], {"pages": 0})):
            capture.advance_capture(self.capture.pk, now=self.now)
        self.assertEqual(self.capture.state, "completed")
        self.scope, _policy = evidence.current_scope(self.context, self.config, user=self.user)
        self.goal = goals.new_goal(str(uuid4()), self.scope, now=self.now)
        self.artifact_id = str(self.parent.pk)

    def collect(self, **kwargs):
        return evidence.collect(self.goal, self.context, self.config, user=self.user, run=kwargs.get("run", self.run),
            artifact_operation_id=kwargs.get("artifact_operation_id", self.artifact_id), now=self.now)

    def test_full_native_pipeline_fills_real_milestones_without_scheduling_or_git_facts(self):
        self.advance(); self.advance(); self.advance()
        self.fetch.reset_mock(); self.http.reset_mock(); self.public_get.reset_mock()
        proofs, original, references = self.collect()
        self.assertEqual(set(proofs), set(goals.KINDS))
        value = goals.bind_source(self.goal, original, expected_scope=self.scope, now=self.now)
        value = goals.refresh(value, proofs, expected_scope=self.scope, now=self.now)
        self.assertTrue(goals.complete(value, expected_scope=self.scope, now=self.now))
        self.assertEqual(references["first_article_live"], str(self.operation.pk))
        for forbidden in ("source_sha", "backend_merge", "repository_access", "synthetic-application-password"):
            self.assertNotIn(forbidden, json.dumps(value))
        self.fetch.assert_not_called(); self.http.assert_not_called(); self.public_get.assert_not_called()

    def test_provider_ack_and_copied_live_boolean_do_not_fill_public_milestones(self):
        self.advance(); self.advance()
        self.operation.receipt["publishing_verified"] = True
        proofs, original, _references = self.collect()
        self.assertNotIn("live", proofs); self.assertNotIn("first_article_live", proofs)
        self.assertIn("first_draft", proofs); self.assertIn("approved", proofs)
        value = goals.bind_source(self.goal, original, expected_scope=self.scope, now=self.now)
        self.assertFalse(goals.complete(goals.refresh(value, proofs, expected_scope=self.scope, now=self.now), expected_scope=self.scope, now=self.now))

    def test_rechecksummed_completed_publication_receipt_does_not_replace_owning_validation(self):
        self.advance(); self.advance(); self.advance()
        self.operation.receipt["publicProofDigest"] = "f" * 64
        with self.assertRaises(WebsiteAuthorityError): self.collect()

    def test_original_refund_or_foreign_reserved_artifact_cannot_supply_article_evidence(self):
        with self.assertRaises(WebsiteAuthorityError): self.collect(artifact_operation_id=str(uuid4()))
        self.refund.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError): self.collect()

    def test_new_run_generation_ignores_old_stage_approval_and_publication_proofs(self):
        self.advance(); self.advance(); self.advance()
        self.run.result["generation"] += 1
        proofs, original, references = self.collect()
        self.assertEqual(original["run_generation"], 3)
        self.assertEqual(set(proofs), {"access", "understood", "integrated"})
        self.assertNotIn("first_draft", references)

    def test_setup_facts_do_not_require_a_paid_run_and_cannot_forget_an_already_bound_source(self):
        proofs, original, _references = self.collect(run=None)
        self.assertEqual(set(proofs), {"access", "understood", "integrated"})
        self.assertIsNone(original)
        self.goal = goals.bind_source(self.goal, {"run_id": self.run.run_id, "run_generation": 2,
            "envelope_digest": self.run.run_request["publishing_channel_artifact"]["digest"]}, expected_scope=self.scope, now=self.now)
        with self.assertRaises(WebsiteAuthorityError): self.collect(run=None)

    def test_private_capture_scope_or_receipt_changes_cannot_be_inventory_proof(self):
        self.capture.receipt["inventoryDigest"] = "f" * 64
        with self.assertRaises(WebsiteAuthorityError): self.collect()
        self.capture.receipt = capture._progress(self.capture.payload["inventory"])
        self.capture.payload["binding"]["generation"] = 9
        with self.assertRaises(WebsiteAuthorityError): self.collect()

    def test_changed_owner_or_deleted_credential_is_denied_before_private_article_reads(self):
        self.user.is_active = False
        with patch.object(evidence, "original_billing") as paid, self.assertRaises(WebsiteAuthorityError): self.collect()
        paid.assert_not_called()
        self.user.is_active = True; self.private.state = "deleted"
        with patch.object(evidence, "original_billing") as paid, self.assertRaises(WebsiteAuthorityError): self.collect()
        paid.assert_not_called()

    def test_disabled_native_evidence_does_not_read_private_rows(self):
        with override_settings(ARTICLES_CHANNEL_ARTIFACTS_ENABLED=False), patch.object(WebsiteConnectionOperation.objects, "filter") as rows:
            with self.assertRaises(WebsiteAuthorityError): self.collect()
        rows.assert_not_called()
