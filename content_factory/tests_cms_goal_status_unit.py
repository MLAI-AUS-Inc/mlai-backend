"""Native progress rechecks original evidence without scheduling or leaking scope."""

from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_goal_status as status
from . import cms_goal_contract as goals
from . import tests_cms_goal_controller_unit as fixtures
from .website_models import WebsiteConnectionOperation


@override_settings(**{**fixtures.CmsGoalControllerTests._overridden_settings,
    "ARTICLES_CMS_STATUS_ENABLED": True, "ARTICLES_WEBSITE_STATUS_V1_ENABLED": True, "ARTICLES_WEBSITE_DOCTOR_ENABLED": True})
class CmsGoalStatusTests(SimpleTestCase):
    configure = fixtures.CmsGoalControllerTests.configure
    reserve = fixtures.CmsGoalControllerTests.reserve
    hold = fixtures.CmsGoalControllerTests.hold
    prepare_generation = fixtures.CmsGoalControllerTests.prepare_generation
    consume = fixtures.CmsGoalControllerTests.consume
    paid_run = fixtures.CmsGoalControllerTests.paid_run

    def setUp(self):
        fixtures.CmsGoalControllerTests.setUp(self)
        self.connection.refresh_from_db = Mock()
        self.goal_operation.updated_at = self.now

    def read(self): return status.for_context(self.context, self.config, user=self.user, now=self.now)

    def paid(self):
        self.goal_operation.next_attempt_at = self.now + timedelta(seconds=180)
        self.paid_run()
        self.goal_operation.payload["source_run_id"] = self.run.run_id
        return self.run

    def current_proofs(self):
        source = {"run_id": self.run.run_id, "run_generation": self.run.result["generation"],
            "envelope_digest": self.run.run_request["publishing_channel_artifact"]["digest"]}
        return {name: goals.make_proof(name, expected_scope=self.scope, original_source=source,
            subject_digest="a" * 64, at=self.now) for name in goals.KINDS}, source, {"first_article_live": str(self.publication.pk)}

    def published(self):
        self.paid()
        self.run.status = "completed"
        self.goal_operation.state = "completed"
        self.goal_operation.receipt["public_url"] = "https://private-api.test/?secret=synthetic"
        self.publication = WebsiteConnectionOperation(id=uuid4(), action="cms-publish", connection=self.connection,
            generation=self.connection.generation, state="completed", payload={"post_state": {"provider_id": 42}},
            receipt={"publicUrl": self.binding["public_origin"] + "articles/original/"})
        self.saved.append(self.publication)

    def test_working_native_goal_has_real_schedule_and_no_git_merge_requirement(self):
        before = deepcopy((self.goal_operation.payload, self.goal_operation.receipt))
        value = self.read()
        self.assertEqual((value["channel"], value["state"], value["target"]), ("cms", "working", "first_article_live"))
        self.assertEqual(value["nextAttemptAt"], self.goal_operation.next_attempt_at.isoformat())
        merged = next(row for row in value["milestones"] if row["name"] == "merged")
        self.assertEqual(merged, {"name": "merged", "state": "not_required", "required": False})
        self.assertEqual(before, (self.goal_operation.payload, self.goal_operation.receipt))
        self.assertIsNone(value["doctor"]["observedSourceSha"])
        self.assertIsNone(value["doctor"]["verifiedSourceSha"])
        self.assertIsNone(value["doctor"]["budget"]["modelUsd"])
        self.assertFalse(any(row.action == "cms-goal-refund" for row in self.saved))
        self.spend.assert_called_once(); self.capture_http.assert_not_called(); self.topic.assert_not_called()

    def test_progress_and_doctor_omit_private_credentials_policy_consent_and_financial_identity(self):
        self.paid()
        value = self.read()
        rendered = json.dumps(value)
        for private in ("api_origin", "consent", "payer_user_id", "spend_ledger_id", "policy_digest", "credential_operation_id",
                "synthetic-application-password", "roo_points", self.key):
            self.assertNotIn(private, rendered)
        self.assertEqual(value["doctor"]["nativeSource"]["runId"], self.run.run_id)
        self.assertEqual(value["doctor"]["children"], [{"runId": self.run.run_id, "status": "running"}])

    def test_diagnostic_references_never_export_token_shaped_run_ids(self):
        source = {"run_id": "sk-synthetic-private-value", "run_generation": 0, "envelope_digest": "a" * 64}
        proof = goals.make_proof("first_draft", expected_scope=self.scope, original_source=source,
            subject_digest="b" * 64, at=self.now)
        value = status.authority.historical(self.goal_operation)
        projected = status._doctor(self.goal_operation, value, self.goal_operation.receipt["native_budget"], [
            {"name": "first_draft", "state": "passed", "required": True, "proof": proof}], original_source=source,
            child=SimpleNamespace(run_id=source["run_id"], status="completed"))
        self.assertIsNone(projected["nativeSource"])
        self.assertIsNone(projected["checks"][0]["proof"]["runId"])
        self.assertNotIn(source["run_id"], json.dumps(projected))

    def test_same_original_pipeline_proofs_and_owning_publication_supply_live_link(self):
        self.published()
        with patch.object(status.evidence, "collect", side_effect=lambda *args, **kwargs: self.current_proofs()):
            value = self.read()
        self.assertEqual((value["state"], value["action"], value["nextAttemptAt"]), ("done", None, None))
        self.assertEqual(value["publicUrl"], self.publication.receipt["publicUrl"])
        self.assertNotIn("private-api", json.dumps(value))
        self.assertTrue(all(not row["required"] or row["state"] == "passed" for row in value["milestones"]))
        self.assertTrue(all(row["proof"] is None or row["proof"]["kind"].startswith("cms_") and row["proof"]["sha"] is None
            for row in value["doctor"]["checks"]))

    def test_copied_complete_flags_and_parent_public_url_cannot_claim_live(self):
        self.paid()
        self.goal_operation.state = "completed"
        self.goal_operation.receipt.update(publishing_verified=True, public_url="https://private-api.test/?secret=synthetic")
        value = self.read()
        self.assertEqual(value["state"], "stopped")
        self.assertNotIn("publicUrl", value)
        self.assertNotIn("private-api", json.dumps(value))
        from .cms_goal_start import projection
        compatible = projection(self.goal_operation, user=self.user)
        self.assertEqual((compatible["state"], compatible["status"]), ("needs_attention", "verification_required"))
        self.assertNotIn("publicUrl", compatible)

    def test_stale_or_different_source_public_proof_cannot_claim_completion(self):
        self.published()
        def expired(*args, **kwargs):
            proofs, source, refs = self.current_proofs()
            for name in ("live", "first_article_live"):
                proofs[name] = goals.make_proof(name, expected_scope=self.scope, original_source=source,
                    subject_digest="a" * 64, at=self.now - timedelta(hours=1))
            return proofs, source, refs
        with patch.object(status.evidence, "collect", side_effect=expired): self.assertEqual(self.read()["state"], "stopped")
        self.publication.receipt["publicUrl"] = "https://private-api.test/?secret=synthetic"
        with patch.object(status.evidence, "collect", side_effect=lambda *args, **kwargs: self.current_proofs()):
            self.assertEqual(self.read()["state"], "stopped")

    def test_plain_wordpress_permalink_is_bound_to_exact_verified_provider_post(self):
        self.published()
        self.publication.receipt["publicUrl"] = self.binding["public_origin"] + "?p=42"
        with patch.object(status.evidence, "collect", side_effect=lambda *args, **kwargs: self.current_proofs()):
            value = self.read()
            self.assertEqual(value["state"], "done")
            self.assertEqual(value["publicUrl"], self.publication.receipt["publicUrl"])
            for query in ("?p=41", "?p=42&token=synthetic", "?token=synthetic"):
                self.publication.receipt["publicUrl"] = self.binding["public_origin"] + query
                self.assertEqual(self.read()["state"], "stopped")

    def test_recorded_manual_review_requires_exact_current_stage_revision_and_run(self):
        self.paid()
        self.goal_operation.state = "needs_user"
        stage = SimpleNamespace(pk=uuid4(), payload={"review_policy": "review"})
        self.goal_operation.receipt["user_action"] = {"id": "review_draft", "runId": self.run.run_id,
            "stageId": str(stage.pk), "reviewRevision": "a" * 64}
        with patch("content_factory.cms_article_approval.owner_stage", return_value=(stage, {
                "source_run_id": self.run.run_id, "revision": "a" * 64})):
            value = status._review_action(self.goal_operation, self.context, self.config, user=self.user, publish_mode="review")
            self.assertEqual(value["stageId"], str(stage.pk))
            self.assertEqual(value["reviewRevision"], "a" * 64)
            self.goal_operation.receipt["user_action"]["reviewRevision"] = "b" * 64
            self.assertIsNone(status._review_action(self.goal_operation, self.context, self.config, user=self.user, publish_mode="review"))

    def test_auto_setup_cannot_acquire_an_unrequested_manual_review_stop(self):
        self.paid()
        self.goal_operation.state = "needs_user"
        self.goal_operation.receipt["user_action"] = {"id": "review_draft", "runId": self.run.run_id,
            "stageId": str(uuid4()), "reviewRevision": "a" * 64}
        with patch("content_factory.cms_article_approval.owner_stage") as stage:
            self.assertEqual(self.read()["state"], "stopped")
        stage.assert_not_called()

    def test_original_access_wait_exposes_its_actual_recheck_schedule(self):
        self.paid()
        self.goal_operation.state = "needs_user"
        self.goal_operation.receipt["user_action"] = {"id": "connect_cms"}
        value = self.read()
        self.assertEqual((value["state"], value["action"]["id"]), ("needs_you", "connect_cms"))
        self.assertEqual(value["nextAttemptAt"], self.goal_operation.next_attempt_at.isoformat())
        self.capture_http.assert_not_called()

    def test_owner_and_company_changes_are_denied_before_reading_private_evidence(self):
        self.user.is_active = False
        with patch.object(status.evidence, "collect") as collect: self.assertIsNone(self.read())
        collect.assert_not_called()
        self.user.is_active = True
        self.company.profile_id = 999
        self.assertIsNone(self.read())

    def test_owner_change_during_proof_read_cannot_return_an_old_scope(self):
        def changed(*args, **kwargs):
            self.user.is_active = False
            raise ValueError("synthetic concurrent owner change")
        with patch.object(status.evidence, "collect", side_effect=changed): self.assertIsNone(self.read())

    def test_timerless_work_terminal_work_and_feature_off_do_not_show_working(self):
        self.goal_operation.next_attempt_at = None
        self.assertEqual(self.read()["state"], "stopped")
        for state in ("cancelled", "stopped", "needs_attention", "deleted"):
            self.goal_operation.state = state
            self.assertEqual(self.read()["state"], "stopped")
        with override_settings(ARTICLES_CMS_STATUS_ENABLED=False), patch.object(WebsiteConnectionOperation.objects, "filter") as rows:
            self.assertIsNone(self.read())
        rows.assert_not_called()

    def test_doctor_projects_only_registered_valid_failure_causes(self):
        from .website_failure_contract import classify_failure
        self.goal_operation.receipt["failure"] = classify_failure("CONNECTION_SERVICE_TRANSIENT", "cms_goal_controller")
        self.goal_operation.receipt["effective_failure_class"] = "repairable"
        value = self.read()["doctor"]["failure"]
        self.assertEqual((value["code"], value["class"], value["effectiveClass"]), ("CONNECTION_SERVICE_TRANSIENT", "transient", "repairable"))
        self.goal_operation.receipt["failure"]["code"] = "unregistered_synthetic_private_diagnosis"
        value = self.read()["doctor"]["failure"]
        self.assertIsNone(value["code"])
        self.assertIsNone(value["class"])
        self.assertIsNone(value["effectiveClass"])
        self.assertNotIn("unregistered", json.dumps(value))

    def test_shared_status_facade_selects_native_contract_and_does_not_make_git_setup_plan(self):
        from .website_status import add_status
        with patch("content_factory.website_setup_consent.setup_plan") as legacy:
            value = add_status({}, self.context, self.config, journey={}, user=self.user)
        self.assertEqual(value["websiteStatus"]["channel"], "cms")
        legacy.assert_not_called()


@override_settings(**CmsGoalStatusTests._overridden_settings)
class CmsManualGoalStatusTests(SimpleTestCase):
    reserve = CmsGoalStatusTests.reserve
    hold = CmsGoalStatusTests.hold
    prepare_generation = CmsGoalStatusTests.prepare_generation
    consume = CmsGoalStatusTests.consume
    paid_run = CmsGoalStatusTests.paid_run
    paid = CmsGoalStatusTests.paid
    read = CmsGoalStatusTests.read

    def configure(self, **changes):
        return fixtures.CmsGoalControllerTests.configure(self, **{"review_policy": "review", **changes})

    def setUp(self):
        from . import cms_goal_consent as consent
        real_seal = consent.seal
        with patch.object(consent, "seal", side_effect=lambda goal, **options: real_seal(goal, **{**options, "review_policy": "review"})):
            fixtures.CmsGoalControllerTests.setUp(self)
        self.connection.refresh_from_db = Mock()
        self.goal_operation.updated_at = self.now

    def test_actual_manual_setup_choice_projects_only_its_current_stage_action(self):
        self.paid()
        self.assertEqual(self.goal_operation.payload["consent"]["answers"]["publish_mode"], "review")
        self.goal_operation.state = "needs_user"
        stage = SimpleNamespace(pk=uuid4(), payload={"review_policy": "review"})
        self.goal_operation.receipt["user_action"] = {"id": "review_draft", "runId": self.run.run_id,
            "stageId": str(stage.pk), "reviewRevision": "a" * 64}
        with patch("content_factory.cms_article_approval.owner_stage", return_value=(stage, {
                "source_run_id": self.run.run_id, "revision": "a" * 64})):
            value = self.read()
        self.assertEqual(value["state"], "needs_you")
        self.assertEqual(value["action"]["stageId"], str(stage.pk))
        self.assertIsNone(value["nextAttemptAt"])
        self.spend.assert_called_once()
