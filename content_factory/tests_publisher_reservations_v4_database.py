"""Actual original/current/acceptance transactions in disposable PostgreSQL CI.

Never run locally without separate migration approval. Only source/provider
reads and the explicitly synthetic release profile are replaced in these tests.
"""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import website_connections as authority, website_goal as goals
from . import website_publisher_reservations_v4 as reservations
from .models import OrganizationContentConfig
from .tests_publisher_reservations_v4_unit import example
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteConnectionTarget
from .website_setup_consent import seal
from .website_verification import record_ci_attestation, current_ci_attestation


class PublisherV4ReservationDatabaseTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "postgresql":
            self.skipTest("Requires disposable PostgreSQL CI")
        self.value, self.profile, accepted = example()
        scope = self.value["candidate"]["payload"]["original_scope"]
        self.org = Organization.objects.create(domain="fixture.invalid", name="Authored native fixture")
        self.website = WebsiteConnection.objects.create(id=scope["website_connection_id"], organization=self.org,
            github_repo=scope["github_repo"], repository_id=scope["repository_id"], installation_id="45", branch=scope["branch"],
            app_root="", site_url=scope["site_origin"], generation=scope["connection_generation"],
            verified_sha=self.value["current_binding"]["source_sha"], last_verified_at=timezone.now(),
            capabilities={"inventoryReady": True, "generationReady": True, "publishingReady": True})
        self.target = WebsiteConnectionTarget.objects.create(connection=self.website, generation=self.website.generation,
            target_key=accepted["target_id"], adapter="stack_native_collection", source_sha=self.website.verified_sha,
            contract=accepted, capabilities={"publishingReady": True}, verified_at=timezone.now())
        self.config = OrganizationContentConfig.objects.create(organization=self.org, website_connection=self.website,
            github_repo=self.website.github_repo, default_publish_target_id=self.target.target_key,
            publish_targets=[deepcopy(accepted)], article_template="# Article", design_guide="# Design")
        self.parent = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="prepare", state="running", idempotency_key="native-parent", payload={"attempt": 1, "company_id": "9"})
        goal = goals.new_goal(str(self.parent.pk), goals.scope_for(self.website, "9"), target="first_article_live", now=timezone.now())
        goal["consent"] = seal(goal, {"version": 1, "publish_mode": "auto", "page_policy": "auto", "page_mode": "existing",
            "page_route": "/stories", "ci_mode": "hosted", "billing": {"policy": "reserved", "expected_points": 6, "accepted": True}},
            user_id=4, now=timezone.now())
        self.parent.receipt = {"goal": goal, "setup_run_id": "native-original-setup"}
        self.parent.save(update_fields=["receipt"])
        self.setup = WebsiteConnectionOperation.objects.create(id=scope["setup_operation_id"], connection=self.website,
            generation=self.website.generation, action="workflow", state="completed", idempotency_key="native-original-setup",
            payload={"workflow": "article_system_setup", "attempt": scope["setup_operation_attempt"], "run_id": "native-original-setup"})
        self.source = ContentFactoryRun.objects.create(run_id="native-original-setup", organization=self.org, domain=self.org.domain,
            github_repo=self.website.github_repo, workflow="article_system_setup", status="completed",
            run_request={**authority.contract_for(self.website), "deletion_epoch": 0, "operation_id": str(self.setup.pk),
                "operation_attempt": scope["setup_operation_attempt"], "expected_source_sha": self.value["candidate"]["payload"]["baseline"]["source_sha"]})
        rollout = override_settings(ARTICLES_PUBLISHING_INPUTS_V4_ENABLED=True, ARTICLES_HOSTED_ATTESTATION_ENABLED=True,
            ARTICLES_HOSTED_VERIFICATION_ENABLED=True, ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True,
            ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=[str(self.org.pk)])
        rollout.enable(); self.addCleanup(rollout.disable)
        profile = patch.object(reservations, "supported_profile", side_effect=lambda: deepcopy(self.profile))
        profile.start(); self.addCleanup(profile.stop)
        head = patch.object(authority, "verify_repository_head", side_effect=self.head)
        head.start(); self.addCleanup(head.stop)
        self.initial_goal = deepcopy(self.parent.receipt)

    def head(self, website, sha):
        self.assertFalse(connection.in_atomic_block, "GitHub read must not run inside authority locks")
        return sha

    def observer(self, request):
        self.assertFalse(connection.in_atomic_block, "Source observation must run outside authority locks")
        self.assertEqual(request["source_run_id"], self.source.run_id)
        self.assertEqual(request["scope"], self.value["candidate"]["payload"]["original_scope"])
        raw = self.value["candidate"]["payload"]["baseline"]
        expected = raw if request["expected_source_sha"] == raw["source_sha"] else self.value["current_binding"]
        self.assertEqual(request["expected_source_sha"], expected["source_sha"])
        return deepcopy(expected)

    def reserve(self):
        self.original = reservations.reserve_original(self.parent.pk, observe_source=self.observer)
        self.child = reservations.reserve_execution(self.original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=self.observer)
        self.proof = deepcopy(self.value["receipt"])
        self.proof["identity"]["operation_id"] = str(self.child.pk)
        self.proof["evidence_digest"] = reservations.protocol.digest({key: value for key, value in self.proof.items() if key != "evidence_digest"})
        return self.child

    def provider(self, *args, **kwargs):
        self.assertFalse(connection.in_atomic_block, "CF read must not run inside authority locks")
        self.assertEqual(args, ("get", f"/api/runs/{self.child.pk}/hosted-native-verification"))
        self.assertFalse(kwargs)
        return {"status": "completed", "run_id": str(self.child.pk), "verification": deepcopy(self.proof)}

    def accept(self, provider=None, proof=None):
        with patch("content_factory.website_sealed_candidate.authenticated_request", side_effect=provider or self.provider) as read:
            result = record_ci_attestation(deepcopy(proof or self.proof), origin="authenticated_hosted")
        return result, read

    def stored(self):
        self.website.refresh_from_db(); self.target.refresh_from_db(); self.config.refresh_from_db()
        original = WebsiteConnectionOperation.objects.get(pk=self.original.pk)
        child = WebsiteConnectionOperation.objects.get(pk=self.child.pk)
        run = ContentFactoryRun.objects.get(run_id=str(self.child.pk))
        return {"website_caps": deepcopy(self.website.capabilities), "website_sha": self.website.verified_sha,
            "target_contract": deepcopy(self.target.contract), "target_caps": deepcopy(self.target.capabilities),
            "target_stamp": self.target.updated_at, "target_verified": self.target.verified_at,
            "config_targets": deepcopy(self.config.publish_targets), "config_stamp": self.config.updated_at,
            "original_state": original.state, "original_payload": deepcopy(original.payload), "original_receipt": deepcopy(original.receipt),
            "original_stamp": original.updated_at, "child_state": child.state, "child_receipt": deepcopy(child.receipt),
            "child_stamp": child.updated_at, "run_status": run.status, "run_result": deepcopy(run.result), "run_stamp": run.updated_at}

    def test_raw_original_is_saved_before_proposal_and_unchanged_retry_does_not_reobserve(self):
        observer = Mock(side_effect=self.observer)
        original = reservations.reserve_original(self.parent.pk, observe_source=observer)
        before = (deepcopy(original.payload), original.updated_at)
        replay = reservations.reserve_original(self.parent.pk, observe_source=observer)
        self.assertEqual(replay.pk, original.pk)
        self.assertEqual((replay.payload, replay.updated_at), before)
        self.assertNotIn("proposal", replay.payload)
        self.assertEqual(replay.payload["facts"], self.value["candidate"]["payload"]["baseline"])
        observer.assert_called_once()

    def test_execution_is_reserved_once_under_original_goal_without_resetting_its_budget(self):
        self.original = reservations.reserve_original(self.parent.pk, observe_source=self.observer)
        observer = Mock(side_effect=self.observer)
        child = reservations.reserve_execution(self.original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=observer)
        stamp = child.updated_at
        replay = reservations.reserve_execution(self.original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=observer)
        self.assertEqual(replay.pk, child.pk)
        self.assertEqual(replay.updated_at, stamp)
        self.assertEqual(replay.payload, child.payload)
        observer.assert_called_once()
        self.parent.refresh_from_db()
        self.assert_original_goal_preserved()
        self.assertEqual(self.parent.receipt["goal"]["child_run_history"], [str(child.pk)])
        self.assertEqual(ContentFactoryRun.objects.filter(run_id=str(child.pk)).count(), 1)
        self.target.refresh_from_db(); self.website.refresh_from_db()
        self.assertFalse(self.target.capabilities["publishingReady"])
        self.assertFalse(self.website.capabilities["publishingReady"])

    @override_settings(ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_GOAL_ACTIVE_SECONDS=1000, ARTICLES_GOAL_MODEL_USD=20)
    def test_child_budget_and_attribution_stay_frozen_after_parent_usage_advances(self):
        self.parent.receipt["goal"]["budget_used"] = {"active_seconds": 120, "model_cost": 4}
        self.parent.save(update_fields=["receipt"])
        self.reserve()
        saved = deepcopy(self.child.payload["binding"])
        self.assertEqual(saved["goal_budget"]["remaining_active_seconds"], 880)
        self.assertEqual(saved["goal_budget"]["remaining_model_usd"], 16)
        self.assertEqual(saved["first_article_goal_operation_id"], str(self.parent.pk))
        self.parent.refresh_from_db()
        self.parent.receipt["goal"]["budget_used"] = {"active_seconds": 300, "model_cost": 8}
        self.parent.save(update_fields=["receipt"])
        observer = Mock(side_effect=AssertionError("An already reserved child cannot observe or reset its budget"))
        repeated = reservations.reserve_execution(self.original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=observer)
        self.assertEqual(repeated.payload["binding"], saved)
        observer.assert_not_called()
        from .website_cost_usage import attribution
        self.assertIsNotNone(attribution(self.parent, ContentFactoryRun.objects.get(run_id=str(self.child.pk))))

    @override_settings(ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=True, ARTICLES_GOAL_ACTIVE_SECONDS=1000, ARTICLES_GOAL_MODEL_USD=20)
    def test_exhausted_goal_cannot_observe_current_source_or_allocate_another_child(self):
        original = reservations.reserve_original(self.parent.pk, observe_source=self.observer)
        self.parent.receipt["goal"]["budget_used"] = {"active_seconds": 1000, "model_cost": 4}
        self.parent.save(update_fields=["receipt"])
        observer = Mock(side_effect=AssertionError("No work after the original goal budget is exhausted"))
        with self.assertRaises(WebsiteAuthorityError):
            reservations.reserve_execution(original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=observer)
        observer.assert_not_called()
        self.assertFalse(ContentFactoryRun.objects.filter(workflow="hosted_native_verification").exists())

    def test_full_owned_child_history_is_held_instead_of_dropping_an_old_obligation(self):
        original = reservations.reserve_original(self.parent.pk, observe_source=self.observer)
        self.parent.receipt["goal"]["child_run_history"] = ["owned-original-" + str(index) for index in range(64)]
        self.parent.save(update_fields=["receipt"])
        observer = Mock(side_effect=AssertionError("Do not discard older child history to allocate more work"))
        with self.assertRaises(WebsiteAuthorityError):
            reservations.reserve_execution(original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=observer)
        observer.assert_not_called()
        self.parent.refresh_from_db()
        self.assertEqual(len(self.parent.receipt["goal"]["child_run_history"]), 64)

    def test_authenticated_usage_is_retained_when_the_proof_is_committed(self):
        self.reserve()
        from .website_goal_budget import COUNTERS
        usage = {"version": 1, "run_id": str(self.child.pk), "generation": 0, "state_version": 1, **{key: 0 for key in COUNTERS}}
        def metered(*args, **kwargs):
            return {**self.provider(*args, **kwargs), "usage": usage, "generation": 0, "state_version": 1}
        self.accept(provider=metered)
        run = ContentFactoryRun.objects.get(run_id=str(self.child.pk))
        self.assertEqual(run.result["usage"], usage)
        self.assertEqual(run.result["state_version"], 1)

    def test_real_attestation_entry_commits_target_config_original_child_and_run_atomically(self):
        self.reserve()
        with CaptureQueriesContext(connection) as queries:
            accepted, read = self.accept()
        read.assert_called_once()
        self.assertTrue(any("FOR UPDATE" in item["sql"] for item in queries.captured_queries))
        self.assertEqual(accepted.pk, self.child.pk)
        state = self.stored()
        self.assertEqual(state["original_state"], "completed")
        self.assertEqual(state["child_state"], "completed")
        self.assertEqual(state["run_status"], "completed")
        self.assertEqual(state["run_result"]["verification"], self.proof)
        self.assertEqual(state["target_contract"]["contract"]["publishing_inputs_v2"], self.value["candidate"])
        self.assertNotIn("publishing_inputs_v1", state["target_contract"]["contract"])
        self.assertEqual(state["config_targets"], [state["target_contract"]])
        self.assertEqual(state["target_contract"]["verification"], state["child_receipt"])
        self.assertTrue(state["target_caps"]["nativeSetupVerified"])
        self.assertFalse(state["target_caps"]["publishingReady"])
        self.assertFalse(state["child_receipt"]["allowed"])
        self.assertIsNone(current_ci_attestation(self.website, self.target))
        self.parent.refresh_from_db()
        self.assert_original_goal_preserved()

    def assert_original_goal_preserved(self):
        before = self.initial_goal["goal"]
        after = self.parent.receipt["goal"]
        for key in ("goal_id", "scope", "consent", "started_at", "budget_used", "attempts_by_class"):
            self.assertEqual(after[key], before[key])

    def test_identical_result_and_reservation_replays_do_not_refetch_or_renew_any_stamp(self):
        self.reserve(); self.accept()
        before = self.stored()
        replay, read = self.accept(provider=Mock(side_effect=AssertionError("No CF read on committed replay")))
        read.assert_not_called()
        self.assertEqual(replay.pk, self.child.pk)
        observer = Mock(side_effect=AssertionError("No source observation on committed replay"))
        original = reservations.reserve_original(self.parent.pk, observe_source=observer)
        child = reservations.reserve_execution(original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=observer)
        self.assertEqual(child.pk, replay.pk)
        observer.assert_not_called()
        self.assertEqual(self.stored(), before)

    def test_weaker_scan_cannot_erase_the_saved_native_setup_or_switch_the_selected_target(self):
        from .incident_guards import protect_certified_config
        self.reserve(); self.accept()
        before = self.stored()
        incoming = {"source_sha": self.website.verified_sha, "publish_targets": [
            {"target_id": self.target.target_key, "delivery_adapter": "bundle_only"}]}
        authority.record_scan_evidence(self.website, incoming)
        self.config.refresh_from_db(); self.target.refresh_from_db()
        protected = protect_certified_config(self.config, {"publish_targets": incoming["publish_targets"], "default_publish_target_id": "another"})
        self.assertEqual(self.target.contract, before["target_contract"])
        self.assertEqual(protected["publish_targets"], [before["target_contract"]])
        self.assertEqual(protected["default_publish_target_id"], self.target.target_key)
        self.assertFalse(self.target.capabilities["publishingReady"])

    def test_original_observation_race_cannot_create_a_reservation(self):
        def revoked(request):
            facts = self.observer(request)
            self.website.state = "disconnected"
            self.website.save(update_fields=["state"])
            return facts
        with self.assertRaises(WebsiteAuthorityError):
            reservations.reserve_original(self.parent.pk, observe_source=revoked)
        self.assertFalse(WebsiteConnectionOperation.objects.filter(action="publisher-original").exists())

    def test_execution_observation_race_rolls_back_child_and_does_not_freeze_a_new_proposal(self):
        original = reservations.reserve_original(self.parent.pk, observe_source=self.observer)
        def cancelled(request):
            facts = self.observer(request)
            self.parent.state = "cancelled"
            self.parent.save(update_fields=["state"])
            return facts
        with self.assertRaises(WebsiteAuthorityError):
            reservations.reserve_execution(original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=cancelled)
        original.refresh_from_db()
        self.assertNotIn("proposal", original.payload)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(payload__workflow="hosted_native_verification").count(), 0)
        self.assertEqual(ContentFactoryRun.objects.filter(workflow="hosted_native_verification").count(), 0)

    def test_missing_original_setup_operation_is_not_hydrated_from_the_candidate(self):
        self.setup.delete()
        observer = Mock(side_effect=AssertionError("Missing original history must stop before provider work"))
        with self.assertRaises(WebsiteAuthorityError):
            reservations.reserve_original(self.parent.pk, observe_source=observer)
        observer.assert_not_called()

    def test_changed_current_source_facts_cannot_be_supplied_by_the_arriving_proof(self):
        self.reserve()
        before = self.stored()
        changed = deepcopy(self.proof)
        changed["current"]["source_tree_sha"] = "e" * 40
        changed["evidence_digest"] = reservations.protocol.digest({key: value for key, value in changed.items() if key != "evidence_digest"})
        provider = Mock(side_effect=AssertionError("Bad submitted identity must not fetch"))
        with self.assertRaises(WebsiteAuthorityError):
            self.accept(provider=provider, proof=changed)
        provider.assert_not_called()
        self.assertEqual(self.stored(), before)

    def test_provider_result_must_equal_the_submitted_exact_child_proof(self):
        self.reserve(); before = self.stored()
        def different(*args, **kwargs):
            result = self.provider(*args, **kwargs)
            result["verification"]["evidence_digest"] = "e" * 64
            return result
        with self.assertRaises(WebsiteAuthorityError): self.accept(provider=different)
        self.assertEqual(self.stored(), before)

    def race_case(self, mutation):
        self.reserve()
        def raced(*args, **kwargs):
            result = self.provider(*args, **kwargs)
            mutation()
            return result
        with self.assertRaises(WebsiteAuthorityError): self.accept(provider=raced)
        self.child.refresh_from_db(); self.original.refresh_from_db(); self.target.refresh_from_db()
        self.assertNotEqual(self.original.state, "completed")
        self.assertNotEqual(self.child.receipt.get("status"), "setup_verified")
        self.assertNotEqual(self.target.contract.get("verification", {}).get("status"), "setup_verified")

    def test_target_edit_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: WebsiteConnectionTarget.objects.filter(pk=self.target.pk).update(contract={**self.target.contract, "owner_edit": True}))

    def test_parent_attempt_change_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: WebsiteConnectionOperation.objects.filter(pk=self.parent.pk).update(payload={**self.parent.payload, "attempt": 2}))

    def test_child_cancel_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: WebsiteConnectionOperation.objects.filter(pk=self.child.pk).update(state="cancelled"))

    def test_disconnect_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: WebsiteConnection.objects.filter(pk=self.website.pk).update(state="disconnected"))

    def test_pause_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: WebsiteConnection.objects.filter(pk=self.website.pk).update(state="paused"))

    def test_cancelled_run_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: ContentFactoryRun.objects.filter(run_id=str(self.child.pk)).update(status="cancelled"))

    def test_original_retirement_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: WebsiteConnectionOperation.objects.filter(pk=self.original.pk).update(state="cancelled"))

    def test_release_profile_change_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: self.profile.update(qualification_digest="d" * 64))

    def test_original_request_change_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: ContentFactoryRun.objects.filter(pk=self.source.pk).update(run_request={**self.source.run_request, "changed_original_intent": True}))

    def test_selected_target_change_during_provider_read_refuses_acceptance(self):
        self.race_case(lambda: OrganizationContentConfig.objects.filter(pk=self.config.pk).update(default_publish_target_id="different-selection"))

    def test_failure_after_target_config_child_and_run_writes_rolls_everything_back(self):
        self.reserve(); before = self.stored()
        actual_save = WebsiteConnectionOperation.save
        def fail_at_original(instance, *args, **kwargs):
            if instance.pk == self.original.pk and instance.state == "completed":
                raise RuntimeError("Synthetic final receipt write failure")
            return actual_save(instance, *args, **kwargs)
        with patch.object(WebsiteConnectionOperation, "save", new=fail_at_original), self.assertRaises(RuntimeError):
            self.accept()
        self.assertEqual(self.stored(), before)

    def test_interleaved_duplicate_providers_commit_one_identical_result(self):
        self.reserve(); calls = []
        def interleaved(*args, **kwargs):
            result = self.provider(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                inner, _ = self.accept()
                self.assertEqual(inner.pk, self.child.pk)
            return result
        outer, _ = self.accept(provider=interleaved)
        self.assertEqual(outer.pk, self.child.pk)
        self.assertEqual(WebsiteConnectionOperation.objects.filter(action="publisher-original").count(), 1)
        self.assertEqual(ContentFactoryRun.objects.filter(run_id=str(self.child.pk)).count(), 1)
        self.assertEqual(self.stored()["original_state"], "completed")

    def test_interleaved_current_observations_cannot_disagree_about_an_immutable_commit(self):
        original = reservations.reserve_original(self.parent.pk, observe_source=self.observer)
        def interleaved(request):
            facts = self.observer(request)
            reservations.reserve_execution(original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=self.observer)
            return {**facts, "source_tree_sha": "e" * 40}
        with self.assertRaises(WebsiteAuthorityError):
            reservations.reserve_execution(original.pk, candidate=self.value["candidate"], records=self.value["records"], observe_source=interleaved)
        children = WebsiteConnectionOperation.objects.filter(payload__workflow="hosted_native_verification")
        self.assertEqual(children.count(), 1)
        self.assertEqual(children.first().payload[reservations.PENDING]["current_binding"], self.value["current_binding"])

    def test_committed_replay_cannot_ignore_new_consent_or_config_target_changes(self):
        self.reserve(); self.accept()
        changed = deepcopy(self.parent.receipt)
        goal = changed["goal"]
        answers = deepcopy(goal["consent"]["answers"]); answers["publish_mode"] = "review"
        goal["consent"] = seal(goal, answers, user_id=4, now=timezone.now())
        self.parent.receipt = changed; self.parent.save(update_fields=["receipt"])
        with self.assertRaises(WebsiteAuthorityError): self.accept()
