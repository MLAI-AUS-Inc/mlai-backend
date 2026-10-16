"""Reconnect retains accepted definitions while all new-generation proof starts empty."""
from copy import deepcopy
from contextlib import nullcontext
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from organizations.models import Organization

from . import website_site_recipe_reconnect as reconnect
from .models import OrganizationContentConfig
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget
from .website_target_contract import native_target_digest


@override_settings(ARTICLES_SITE_CONTRACT_V2_ENABLED=True, ARTICLES_HOSTED_ATTESTATION_ENABLED=True,
    ARTICLES_HOSTED_VERIFICATION_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class SiteRecipeReconnectTests(SimpleTestCase):
    def setUp(self):
        self.user = get_user_model()(pk=4)
        self.connection = WebsiteConnection(organization=Organization(pk=7, domain="fixture.test"),
            authorized_by=self.user, github_repo="fixture/site", repository_id=12, installation_id="4",
            branch="main", site_url="fixture.test", generation=2)
        self.config = OrganizationContentConfig(organization=self.connection.organization,
            website_connection=self.connection, default_publish_target_id="articles")
        example = json.loads((Path(__file__).resolve().parents[1] / "contracts/native-target-identity-v1.example.json").read_text())["examples"][0]
        definition = deepcopy(example["target"])
        definition.update(target_id="articles", site_contract_version=2, site_contract_digest="e" * 64,
            site_verification_probe={"slug": "fixture", "title": "Fixture article"}, publish_capability="direct")
        definition["contract"]["contract_digest"] = native_target_digest(definition)
        definition["verification"] = {"status": "verified", "source_sha": "a" * 40,
            "website_connection_id": str(self.connection.pk), "connection_generation": 2, "repository_id": 12}
        self.previous = WebsiteConnectionTarget(connection=self.connection, generation=2, target_key="articles",
            source_sha="a" * 40, contract=definition, verified_at=timezone.now(), capabilities={"publishingReady": True})
        self.rows = MagicMock()
        self.rows.filter.return_value = self.rows.order_by.return_value = self.rows
        self.rows.__getitem__.return_value = [self.previous]
        self.rows.first.return_value = self.previous
        self.targets = patch.object(WebsiteConnection, "targets", self.rows)
        self.targets.start()
        self.addCleanup(self.targets.stop)

    def candidate(self):
        return reconnect.candidate_for(self.connection, self.config)

    def enroll(self):
        candidate = self.candidate()
        self.connection.generation = 3
        self.connection.verified_sha = ""
        self.connection.capabilities = {}
        self.config.publish_targets, self.config.default_publish_target_id = [], None
        self.config.save = Mock()
        captured = {}
        def reserve(**kwargs):
            captured.update(kwargs)
            return WebsiteConnectionOperation(**kwargs["defaults"]), True
        with patch.object(WebsiteConnectionTarget.objects, "get_or_create") as target, \
                patch.object(WebsiteConnectionOperation.objects, "get_or_create", side_effect=reserve):
            op = reconnect.enroll_after_bind(self.config, self.connection, candidate, user=self.user)
        return op, target.call_args.kwargs["defaults"], captured

    def test_closed_definition_strips_all_old_authority_and_does_not_mutate_original(self):
        original = deepcopy(self.previous.contract)
        self.previous.contract.update(capabilities={"publishingReady": True}, source_sha="a" * 40, secret_unrecognized="ignored")
        candidate = self.candidate()
        self.assertEqual(candidate["previous_target_id"], str(self.previous.pk))
        definition = candidate["definition"]
        self.assertEqual(definition["site_contract_digest"], "e" * 64)
        self.assertNotIn("verification", definition)
        self.assertNotIn("capabilities", definition)
        self.assertNotIn("source_sha", definition)
        self.assertNotIn("secret_unrecognized", definition)
        self.assertNotIn("marked_paths", definition["contract"]["live_marker"])
        self.assertEqual(self.previous.contract["verification"], original["verification"])

    def test_disabled_ambiguous_unverified_or_foreign_rows_cannot_enroll(self):
        with override_settings(ARTICLES_SITE_CONTRACT_V2_ENABLED=False):
            self.assertIsNone(self.candidate())
        self.rows.__getitem__.return_value = [self.previous, deepcopy(self.previous)]
        self.assertIsNone(self.candidate())
        self.rows.__getitem__.return_value = [self.previous]
        for field, value in (("verified_at", None), ("capabilities", {"publishingReady": False}),
                ("generation", 3), ("source_sha", "invalid"), ("connection_id", "foreign")):
            original = getattr(self.previous, field)
            setattr(self.previous, field, value)
            with self.subTest(field=field):
                self.assertIsNone(self.candidate())
            setattr(self.previous, field, original)

    def test_new_generation_is_provisional_and_verification_is_durably_reserved(self):
        op, target, captured = self.enroll()
        self.assertEqual(target["source_sha"], "")
        self.assertIsNone(target["verified_at"])
        self.assertFalse(target["capabilities"]["publishingReady"])
        self.assertNotIn("verification", target["contract"])
        self.assertTrue(target["contract"]["provisional"])
        self.assertEqual(op.generation, 3)
        self.assertEqual(op.action, "source-reverify")
        self.assertEqual(op.payload["previous_target_id"], str(self.previous.pk))
        self.assertEqual(op.payload["source_sha"], "a" * 40)
        self.assertFalse(op.payload["scan_required"])
        self.assertEqual(self.config.default_publish_target_id, "articles")
        self.assertEqual(self.connection.verified_sha, "")
        self.assertEqual(self.connection.capabilities, {})
        self.assertIn(":site-recipe-reconnect:3", captured["idempotency_key"])

    def review_fixture(self):
        parent, _, _ = self.enroll()
        parent.state = "pending"
        operations = MagicMock()
        operations.filter.return_value.first.return_value = parent
        origin = WebsiteConnectionOperation(connection=self.connection, generation=3, action="workflow", state="completed",
            payload={"site_recipe_reconnect_operation_id": str(parent.pk), "binding": {"run_id": "fresh-hosted",
                "site_recipe_version": 2, "expected_source_sha": "a" * 40, "site_contract_digest": "e" * 64}})
        data = {"run_id": "fresh-hosted", "source_sha": "a" * 40, "target_id": "articles",
            "contract_digest": self.previous.contract["contract"]["contract_digest"]}
        return parent, operations, origin, data

    def test_only_exact_previously_reviewed_source_supplies_review_provenance(self):
        parent, operations, origin, data = self.review_fixture()
        with patch.object(WebsiteConnection, "operations", operations):
            self.assertTrue(reconnect.trusted_source_review(self.connection, origin, data))
            self.assertEqual(self.connection.verified_sha, "")
            self.assertEqual(self.connection.capabilities, {})
            for key, value in (("source_sha", "b" * 40), ("target_id", "other"),
                    ("contract_digest", "f" * 64), ("run_id", "other")):
                with self.subTest(key=key):
                    self.assertFalse(reconnect.trusted_source_review(self.connection, origin, {**data, key: value}))
            for key, value in (("requested_by_user_id", "other-owner"), ("deletion_epoch", 1),
                    ("previous_generation", 3), ("site_contract_digest", "f" * 64), ("scope", {})):
                original = deepcopy(parent.payload[key])
                parent.payload[key] = value
                with self.subTest(key=key):
                    self.assertFalse(reconnect.trusted_source_review(self.connection, origin, data))
                parent.payload[key] = original
            origin.state = "cancelled"
            self.assertFalse(reconnect.trusted_source_review(self.connection, origin, data))

    def test_purged_missing_or_changed_prior_definition_has_no_review_provenance(self):
        _, operations, origin, data = self.review_fixture()
        with patch.object(WebsiteConnection, "operations", operations):
            self.rows.first.return_value = None
            self.assertFalse(reconnect.trusted_source_review(self.connection, origin, data))
            self.rows.first.return_value = self.previous
            self.previous.contract["site_contract_digest"] = "f" * 64
            self.assertFalse(reconnect.trusted_source_review(self.connection, origin, data))

    def reconcile_routing(self, *, saved_ci=None, expired=False, failure=None, scan_ready=False):
        """Test reconciliation routing; separate tests execute actual proof promotion."""
        from . import website_connections as authority, website_hosted_verification as hosted
        from . import website_reconciliation as reconciliation, website_verification as verification
        # Resolve lazy modules before replacing Django's shared transaction
        # primitive; importing decorators under that patch changes their meaning.
        from . import vibe_marketing_views as _views
        op, target_defaults, _ = self.enroll()
        now = timezone.now()
        op.created_at, op.next_attempt_at, op.save = now - timedelta(hours=25) if expired else now, None, Mock()
        op.receipt["ci_operation_id"] = "saved-ci" if saved_ci else None
        if scan_ready:
            op.receipt["scan_run_id"] = "fresh-scan"
        current = WebsiteConnectionTarget(connection=self.connection, generation=3, target_key="articles", **target_defaults)
        self.rows.first.return_value = current
        def hosted_step(parent, *args):
            if failure:
                raise failure
            parent.receipt["hosted_verification_run_id"] = "fresh-child"
            return {"status": "pending"}
        from .website_contract import WebsiteAuthorityError
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(WebsiteConnectionOperation.objects, "select_for_update") as claim, \
                patch.object(WebsiteConnectionOperation.objects, "filter") as update, \
                patch.object(authority, "authority_guard", side_effect=lambda *a, **k: nullcontext(self.connection)), \
                patch.object(OrganizationContentConfig.objects, "get", return_value=self.config), \
                patch.object(hosted, "advance", side_effect=hosted_step) as build, \
                patch.object(verification, "current_ci_attestation", return_value=saved_ci), \
                patch.object(reconnect, "scan_complete", return_value=scan_ready), \
                patch.object(verification, "discover_source_attestation", side_effect=AssertionError("Reconnect must not request legacy GitHub CI")), \
                patch.object(verification, "verify_live_deployment", side_effect=WebsiteAuthorityError("live_route_unavailable", "Waiting for deployment.")) as deploy:
            claim.return_value.select_related.return_value.filter.return_value.first.return_value = op
            update.return_value.update.return_value = 1
            state = reconciliation._process_source_reverification(op.pk, now)
        return state, update.return_value.update.call_args.kwargs, build, deploy, current

    def test_pending_hosted_job_retains_its_reference_and_has_no_ready_target(self):
        state, saved, build, deploy, target = self.reconcile_routing()
        self.assertEqual(state, "pending")
        self.assertEqual(saved["receipt"]["hosted_verification_run_id"], "fresh-child")
        self.assertIsNotNone(saved["next_attempt_at"])
        build.assert_called_once()
        deploy.assert_not_called()
        self.assertFalse(target.capabilities["publishingReady"])

    def test_deployment_wait_reuses_saved_build_and_retains_bounded_schedule(self):
        state, saved, build, deploy, _ = self.reconcile_routing(saved_ci={"source_sha": "a" * 40}, scan_ready=True)
        self.assertEqual(state, "pending")
        self.assertEqual(saved["receipt"]["ci_operation_id"], "saved-ci")
        self.assertTrue(saved["receipt"]["retryable"])
        self.assertIsNotNone(saved["next_attempt_at"])
        build.assert_not_called()
        deploy.assert_called_once()

    def test_inventory_refresh_is_reserved_after_proof_before_live_completion(self):
        state, saved, build, deploy, _ = self.reconcile_routing(saved_ci={"source_sha": "a" * 40})
        self.assertEqual(state, "pending")
        self.assertEqual(saved["receipt"]["status"], "inventory_pending")
        build.assert_not_called()
        deploy.assert_not_called()

    def test_exhausted_window_stops_before_any_new_verification(self):
        state, saved, build, deploy, _ = self.reconcile_routing(expired=True)
        self.assertEqual(state, "needs_attention")
        self.assertIsNone(saved["next_attempt_at"])
        build.assert_not_called()
        deploy.assert_not_called()

    def test_deterministic_failed_build_does_not_relaunch_forever(self):
        from .website_contract import WebsiteAuthorityError
        state, saved, build, deploy, _ = self.reconcile_routing(failure=WebsiteAuthorityError("NATIVE_VERIFICATION_FAILED", "Build needs repair."))
        self.assertEqual(state, "needs_attention")
        self.assertIsNone(saved["next_attempt_at"])
        build.assert_called_once()
        deploy.assert_not_called()

    def scan_fixture(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_connections import contract_for
        parent, _, _ = self.enroll()
        scan = WebsiteConnectionOperation(connection=self.connection, generation=3, action="workflow", state="completed",
            payload={"workflow": "repo_scan", "run_id": "reconnect-scan", "attempt": 1})
        request = {**contract_for(self.connection), "run_id": "reconnect-scan", "operation_id": str(scan.pk),
            "operation_attempt": 1, "deletion_epoch": 0, "expected_source_sha": "a" * 40,
            "scaffold_if_missing": False, "auto_setup_preview": False, "scan_purpose": "setup"}
        run = ContentFactoryRun(run_id="reconnect-scan", workflow="repo_scan", status="completed",
            organization=self.connection.organization, domain="fixture.test", github_repo="fixture/site", run_request=request)
        operations = MagicMock()
        operations.filter.return_value.first.return_value = scan
        self.config.refresh_from_db = Mock()
        self.config.last_scanned_sha = "a" * 40
        self.config.article_template, self.config.design_guide = "# Article\nTemplate", "# Design\nGuide"
        return parent, scan, run, operations

    def test_current_completed_read_only_scan_accepts_templates_without_granting_proof(self):
        from workflow_runs.models import ContentFactoryRun
        parent, _, run, operations = self.scan_fixture()
        with patch.object(ContentFactoryRun.objects, "filter") as load, patch.object(WebsiteConnection, "operations", operations):
            load.return_value.first.return_value = run
            self.assertTrue(reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id}))
            run.status = "running"
            self.assertFalse(reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id}))
        self.assertEqual(self.connection.verified_sha, "")
        self.assertEqual(self.connection.capabilities, {})

    def test_foreign_writable_failed_or_cancelled_scan_cannot_complete_reconnect(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_contract import WebsiteAuthorityError
        parent, scan, run, operations = self.scan_fixture()
        original = deepcopy(run.run_request)
        with patch.object(ContentFactoryRun.objects, "filter") as load, patch.object(WebsiteConnection, "operations", operations):
            load.return_value.first.return_value = run
            for key, value in (("connection_generation", 2), ("connection_generation", True), ("expected_source_sha", "b" * 40),
                    ("repository_id", 13), ("branch", "other"), ("app_root", "other"), ("scaffold_if_missing", True),
                    ("auto_setup_preview", True), ("scan_purpose", "article")):
                run.run_request = {**original, key: value}
                with self.subTest(key=key), self.assertRaises(WebsiteAuthorityError):
                    reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id})
            run.run_request = original
            for state in ("failed", "cancelled", "denied"):
                run.status = state
                with self.subTest(state=state), self.assertRaises(WebsiteAuthorityError):
                    reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id})
            run.status, scan.state = "completed", "cancelled"
            with self.assertRaises(WebsiteAuthorityError):
                reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id})

    def test_wrong_source_or_invalid_template_from_completed_scan_stays_unavailable(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_contract import WebsiteAuthorityError
        parent, _, run, operations = self.scan_fixture()
        with patch.object(ContentFactoryRun.objects, "filter") as load, patch.object(WebsiteConnection, "operations", operations):
            load.return_value.first.return_value = run
            self.config.last_scanned_sha = "b" * 40
            with self.assertRaises(WebsiteAuthorityError):
                reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id})
            self.config.last_scanned_sha = "a" * 40
            self.config.design_guide = ""
            with self.assertRaises(WebsiteAuthorityError):
                reconnect.scan_complete(self.connection, self.config, parent, {"scan_run_id": run.run_id})

    def test_post_verification_scan_cannot_replace_enrolled_selection_with_weaker_candidate(self):
        from .incident_guards import protect_certified_config
        _, defaults, _ = self.enroll()
        selected = WebsiteConnectionTarget(connection=self.connection, generation=3, target_key="articles", **defaults)
        selected.source_sha, selected.verified_at = "a" * 40, timezone.now()
        selected.capabilities = {"publishingReady": True}
        selected.contract["verification"] = {"status": "verified", "source_sha": "a" * 40}
        self.connection.verified_sha = "a" * 40
        self.rows.__iter__.side_effect = lambda: iter([selected])
        raw = {"publish_targets": [{"target_id": "scanned-fallback", "delivery_adapter": "react_component"}],
            "default_publish_target_id": "scanned-fallback"}
        kept = protect_certified_config(self.config, raw, sha="a" * 40)
        self.assertEqual(kept["default_publish_target_id"], "articles")
        self.assertIn(selected.contract, kept["publish_targets"])
        self.assertEqual(next(row for row in kept["publish_targets"] if row["target_id"] == "articles")["site_contract_digest"], "e" * 64)
