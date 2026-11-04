"""Post-merge recovery keeps original verifier inputs, never prior proof authority."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import tests_sealed_workflow_unit as fixtures
from . import website_prepare as prepare
from . import website_sealed_workflow as sealed
from .website_hosted_setup_lineage import KEY
from .website_contract import WebsiteAuthorityError
from .website_hosted_verification import request_for


@override_settings(ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=True, ARTICLES_HOSTED_ATTESTATION_ENABLED=True,
    ARTICLES_HOSTED_VERIFICATION_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
    ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"], ARTICLES_FAILURE_TAXONOMY_POLICY_ENABLED=True,
    ARTICLES_FAILURE_TAXONOMY_ENROLLED_ORGANIZATIONS=["7"])
class HostedPostMergeLineageTests(SimpleTestCase):
    def setUp(self):
        self.fixture = fixture = fixtures.SealedWorkflowTests(); fixture.setUp()
        value = fixture.value
        fixture.source_run.run_request.update(
            operation_id="00000000-0000-4000-8000-000000000077", operation_attempt=1,
            strategy_goal_id=str(value.prepare.pk),
            client_request_id=f"prepare:{value.prepare.pk}:setup:1:initial")
        fixture.source_run.status = "completed"
        fixture.source_run.result = {"merge_status": "merged", "merge_sha": fixture.proof["source_sha"],
            "merged_at": value.now.isoformat(),
            "publish_merge_intent": {
                "website_connection_id": str(value.website.pk), "connection_generation": value.website.generation,
                "repository_id": value.website.repository_id, "run_id": fixture.source_run.run_id,
                "base_branch": value.website.branch, "source_sha": fixture.proof["baseline_source_sha"],
                "head_sha": "e" * 40, "recorded_at": value.now.isoformat()},
            "merge_response": {"merged": True, "sha": fixture.proof["source_sha"]}}
        self.original_request, self.original_candidate = deepcopy(fixture.source_run.run_request), deepcopy(fixture.candidate)
        self.original_target = deepcopy(value.target.contract)
        value.prepare.receipt["child_run_id"] = fixture.source_run.run_id

    def merge(self):
        fixture = self.fixture
        # The actual prepare transition starts a new attempt after its owned PR
        # merges. Its original baseline and consented setup do not become new.
        prepare._begin_post_merge_attempt(fixture.value.prepare, fixture.source_run)

    def proposal(self, request):
        from .website_sealed_candidate import SCOPE_FIELDS
        return {"schema_version": 1, "source_run_id": request["source_run_id"],
            "scope": {key: request[key] for key in SCOPE_FIELDS},
            "publishing_inputs_v1": deepcopy(self.fixture.candidate)}

    def reserve_request(self):
        from workflow_runs.models import ContentFactoryRun
        fixture, value = self.fixture, self.fixture.value
        def reserve(connection, *, workflow, payload):
            self.assertEqual(workflow, "hosted_native_verification")
            value.origin.state = "running"
            payload.update(operation_id=str(value.origin.pk), operation_attempt=1, deletion_epoch=0)
            return value.origin

        with fixture.boundaries(), \
                patch("content_factory.website_sealed_candidate.fetch_candidate", side_effect=self.proposal) as fetched, \
                patch("content_factory.website_operations.reserve_workflow_operation", side_effect=reserve) as reserved, \
                patch.object(ContentFactoryRun.objects, "get_or_create", return_value=(fixture.child_run, True)):
            child, request = request_for(value.prepare, value.context, value.config, value.target, fixture.proof["source_sha"])
        reserved.assert_called_once(); fetched.assert_called_once()
        return child, request

    def test_owned_merge_reserves_fresh_verifier_from_original_private_candidate(self):
        self.merge()
        fixture, value = self.fixture, self.fixture.value
        child, request = self.reserve_request()
        self.assertEqual(value.prepare.payload["attempt"], 2)
        self.assertEqual(request["source_run_id"], fixture.source_run.run_id)
        self.assertEqual(request["expected_source_sha"], fixture.proof["source_sha"])
        self.assertEqual(child.payload["binding"]["baseline_source_sha"], self.original_request["expected_source_sha"])
        self.assertEqual(child.payload["sealed_inputs"]["candidate"], self.original_candidate)
        self.assertEqual(child.payload["sealed_inputs"]["snapshot"]["parent_attempt"], 2)
        self.assertEqual(fixture.source_run.run_request, self.original_request)
        self.assertEqual(value.target.contract, self.original_target)
        self.assertNotIn("verification", child.receipt)
        self.assertNotIn("setup_run_id", value.prepare.receipt)
        self.assertNotIn("approved", value.prepare.receipt)

    def assert_candidate_refused(self, *, during_fetch=None):
        fixture, value = self.fixture, self.fixture.value
        def fetched(request):
            response = self.proposal(request)
            if during_fetch:
                during_fetch()
            return response
        def provider(*args, **kwargs):
            response = Mock(status_code=200)
            response.json.return_value = {"sha": value.inventory.source_sha}
            return response
        with fixture.boundaries(), patch("integrations.http_client.get", side_effect=provider), \
                patch("content_factory.website_sealed_candidate.fetch_candidate", side_effect=fetched) as fetch:
            with self.assertRaises(WebsiteAuthorityError):
                sealed.prepare_candidate(value.prepare, value.context, value.config, value.target, fixture.proof["source_sha"])
        self.assertEqual(fetch.call_count, 1 if during_fetch else 0)
        self.assertEqual(value.target.contract, self.original_target)

    def test_original_parent_dispatch_and_optional_goal_claims_are_required(self):
        for change in ("foreign_client", "old_attempt", "old_epoch", "strategy", "budget", "malformed_budget", "missing_client"):
            with self.subTest(change=change):
                self.setUp(); request = self.fixture.source_run.run_request
                if change == "foreign_client": request["client_request_id"] = "prepare:foreign:setup:1:initial"
                elif change == "old_attempt": self.fixture.value.prepare.payload["attempt"] = 2
                elif change == "old_epoch": self.fixture.value.prepare.payload["retry_epoch"] = "new-epoch"
                elif change == "strategy": request["strategy_goal_id"] = "foreign"
                elif change == "budget": request["goal_budget"] = {"goal_id": "foreign"}
                elif change == "malformed_budget": request["goal_budget"] = []
                else: request.pop("client_request_id")
                self.merge(); self.assert_candidate_refused()

    def test_recovery_rejects_conflicting_source_merge_goal_and_attempt(self):
        for change in ("conflicting_setup", "conflicting_merge", "missing_record", "extra_record", "foreign_parent",
                "old_attempt", "bool_attempt", "old_epoch", "new_goal", "cancelled", "source", "merge_source",
                "merge_head", "merge_repo", "merge_bool_generation", "response_false", "response_source", "request_source", "request_epoch",
                "new_consent", "repair", "new_strategy", "nonfinite_request"):
            with self.subTest(change=change):
                self.setUp(); self.merge()
                fixture, value = self.fixture, self.fixture.value
                if change == "conflicting_setup": value.prepare.receipt["setup_run_id"] = "another-setup"
                elif change == "conflicting_merge": value.prepare.receipt["merged_setup_run_id"] = "another-setup"
                elif change == "missing_record": value.prepare.receipt.pop(KEY)
                elif change == "extra_record": value.prepare.receipt[KEY]["approval"] = True
                elif change == "foreign_parent": value.prepare.receipt[KEY]["prepare_operation_id"] = "foreign"
                elif change == "old_attempt": value.prepare.payload["attempt"] = 1
                elif change == "bool_attempt": value.prepare.payload["attempt"] = True
                elif change == "old_epoch": value.prepare.payload["retry_epoch"] = "initial"
                elif change == "new_goal": value.prepare.receipt["goal"]["goal_id"] = "foreign"
                elif change == "cancelled": value.prepare.state = "cancelled"
                elif change == "source": value.inventory.source_sha = "f" * 40
                elif change == "merge_source": fixture.source_run.result["publish_merge_intent"]["source_sha"] = "f" * 40
                elif change == "merge_head": fixture.source_run.result["publish_merge_intent"]["head_sha"] = "invalid"
                elif change == "merge_repo": fixture.source_run.result["publish_merge_intent"]["repository_id"] = 99
                elif change == "merge_bool_generation": fixture.source_run.result["publish_merge_intent"]["connection_generation"] = True
                elif change == "response_false": fixture.source_run.result["merge_response"]["merged"] = False
                elif change == "response_source": fixture.source_run.result["merge_response"]["sha"] = "f" * 40
                elif change == "request_source": fixture.source_run.run_request["expected_source_sha"] = "f" * 40
                elif change == "request_epoch": fixture.source_run.run_request["deletion_epoch"] = 1
                elif change == "repair": value.prepare.payload["repair_setup"] = True
                elif change == "new_strategy": value.prepare.receipt["goal"]["strategy_requires_candidate"] = True
                elif change == "nonfinite_request": fixture.source_run.run_request["bad"] = float("nan")
                else:
                    from .website_setup_consent import seal
                    goal = value.prepare.receipt["goal"]
                    answers = deepcopy(goal["consent"]["answers"])
                    answers["publish_mode"] = "review"
                    goal["consent"] = seal(goal, answers, user_id=4, now=value.now)
                self.assert_candidate_refused()

    def test_source_or_original_ownership_change_during_read_never_reserves_a_child(self):
        for change in ("source", "epoch", "attempt", "request", "reference", "goal"):
            with self.subTest(change=change):
                self.setUp(); self.merge()
                fixture, value = self.fixture, self.fixture.value
                def mutate():
                    if change == "source": value.inventory.source_sha = "f" * 40
                    elif change == "epoch": value.prepare.payload["retry_epoch"] = "replacement-epoch"
                    elif change == "attempt": value.prepare.payload["attempt"] = 3
                    elif change == "request": fixture.source_run.run_request["client_request_id"] = "foreign"
                    elif change == "reference": value.prepare.receipt[KEY]["request_digest"] = "f" * 64
                    else: value.prepare.receipt["goal"]["goal_id"] = "foreign"
                self.assert_candidate_refused(during_fetch=mutate)

    def test_final_local_fence_repeats_recovered_source_checks(self):
        for change in ("source", "epoch", "request", "merge"):
            with self.subTest(change=change):
                self.setUp(); self.merge()
                child, _ = self.reserve_request()
                fixture, value = self.fixture, self.fixture.value
                if change == "source": value.inventory.source_sha = "f" * 40
                elif change == "epoch": value.prepare.payload["retry_epoch"] = "replacement-epoch"
                elif change == "request": fixture.source_run.run_request["strategy_goal_id"] = "foreign"
                else: fixture.source_run.result["merge_response"]["sha"] = "f" * 40
                with fixture.boundaries(), self.assertRaises(WebsiteAuthorityError):
                    sealed.fence_pending(value.website, value.target, child.payload["sealed_inputs"])
                self.assertEqual(value.target.contract, self.original_target)

    def test_worker_merge_fields_cannot_supply_recovery_authority(self):
        from .service_views import _merge_django_owned_run_result
        self.fixture.source_run.result = _merge_django_owned_run_result({}, self.fixture.source_run.result)
        self.merge()
        self.assert_candidate_refused()

    def test_a_fresh_selected_setup_retires_the_old_input_reference(self):
        self.merge()
        value = self.fixture.value
        self.assertIn(KEY, value.prepare.receipt)
        from types import SimpleNamespace
        prepare._correlate_setup_child(value.prepare, SimpleNamespace(run_id="new-reviewed-setup"))
        self.assertNotIn(KEY, value.prepare.receipt)
        self.assertEqual(value.prepare.receipt["setup_run_id"], "new-reviewed-setup")

    def test_repeating_the_owned_merge_transition_does_not_rotate_or_drop_inputs(self):
        self.merge()
        value = self.fixture.value
        original = deepcopy((value.prepare.receipt, value.prepare.payload))
        self.merge()
        self.assertEqual((value.prepare.receipt, value.prepare.payload), original)
        self.reserve_request()
