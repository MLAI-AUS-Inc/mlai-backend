"""Actual named producer boundaries; only remote provider calls are replaced."""
from copy import deepcopy
import hashlib
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from core.actor_ids import preferred_actor_id_for_user
from roo.models import Ledger
from workflow_runs.models import ContentFactoryRun

from . import hosted_auto_register as auto, publish_reviewed_draft as publish
from . import website_goal as goals, website_hosted_verification as verifier
from .hosted_article_models import HostedArticleSite
from .run_ownership import (OWNER_RESULT_KEY, OWNER_EXECUTION_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
                            RunOwnershipConflict, owner_receipt, worker_workflow_allowed)
from .self_heal_testing import ALL_FLAGS_ON, fake_provider, make_p2_org
from .tests import test_p2_publish_reviewed as reviewed_fixture, test_p2_hosted_registration as hosted_fixture
from .tests_one_click_database import OneClickBillingFixture
from .website_contract import WebsiteAuthorityError, evidence_digest
from .website_models import WebsiteConnectionOperation

R3 = {"ARTICLES_RUN_OWNERSHIP_ENABLED": True, "ARTICLES_RUN_FACT_AUTHORITY_ENABLED": True,
      "ARTICLES_RUN_EVENT_AUTHORITY_ENABLED": True}


@override_settings(**{**reviewed_fixture.ReviewedPublishingTests._overridden_settings, **R3})
class NamedPublishingAdmissionTests(TransactionTestCase):
    setUp = reviewed_fixture.ReviewedPublishingTests.setUp
    start = reviewed_fixture.ReviewedPublishingTests.start

    def assert_sealed_at_http(self, payload):
        self.assertFalse(connection.in_atomic_block)
        run = ContentFactoryRun.objects.get(run_id=payload["new_run_id"])
        receipt = owner_receipt(run)
        self.assertIsNotNone(receipt)
        self.assertEqual(payload[WIRE_REQUEST_KEY], run.run_request[WIRE_REQUEST_KEY])
        self.assertEqual(payload[WIRE_EXECUTION_KEY], run.run_request[WIRE_EXECUTION_KEY])
        self.assertEqual(receipt["identity"]["principal"], {
            "kind": "user", "user_id": str(self.user.pk), "actor_id": preferred_actor_id_for_user(self.user)})
        digest_input = {key: value for key, value in payload.items() if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}
        self.assertEqual(receipt["identity"]["request_digest"], evidence_digest(digest_input))
        self.assertEqual(run.result["article_approval_v1"]["approved_body_sha256"], hashlib.sha256(self.body.encode()).hexdigest())
        self.assertEqual(run.result["points_v1"]["state"], "carried")
        self.assertEqual(payload["approved_body"], self.body)
        self.assertEqual(payload["original_billing_source_run_id"], self.run.run_id)
        return run

    def test_exact_body_charge_and_admission_exist_before_first_http(self):
        ledger_before = list(Ledger.objects.order_by("pk").values())
        seen = []

        def post(url, *, json, **kwargs):
            seen.append(self.assert_sealed_at_http(json).pk)
            return SimpleNamespace(status_code=202, json=lambda: {"run_id": json["new_run_id"]})

        self.remote.side_effect = post
        child, created = self.start()
        self.assertTrue(created)
        self.assertEqual(seen, [child.pk])
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), ledger_before)
        self.run.refresh_from_db()
        replay, created = self.start()
        self.assertFalse(created)
        self.assertEqual(replay.pk, child.pk)
        self.assertEqual(seen, [child.pk])

    def test_actual_paid_source_spend_is_carried_once_before_sealing(self):
        from roo.services import PointsService
        from roo.models import PointsAccount
        from integrations.services.article_generation import _ledger_reference_id
        from .models import ContentFactoryJob
        key = "r3-paid-source"
        actor = preferred_actor_id_for_user(self.user)
        PointsService.credit_purchased_topup(user=self.user, delta=20,
            description="Synthetic paid source funding", idempotency_key="r3-paid-funding")
        spend, _ = PointsService.spend(user=self.user, delta=12, source="CONTENT_FACTORY",
            description="Synthetic original article", created_by_slack_id=actor,
            idempotency_key="content_factory:charge:" + key, reference_type="CONTENT_FACTORY",
            reference_id=_ledger_reference_id(key))
        self.run.run_request = {"delivery_mode": "content_only", "client_request_id": key,
            "roo_points_billing_status": "charged", "roo_points_cost": 12, "roo_points_action": "article_generation",
            "roo_points_ledger_id": str(spend.pk), "roo_points_billing_user_id": self.user.pk,
            "roo_points_requested_by_user_id": self.user.pk}
        self.run.save(update_fields=["run_request"])
        ContentFactoryJob.objects.create(job_id=self.run.run_id, domain=self.org.domain, slack_user_id=actor,
            client_request_id=key, billing_ledger=spend, billing_status="charged", billing_amount=12)
        before = list(Ledger.objects.order_by("pk").values())

        def post(url, *, json, **kwargs):
            child = self.assert_sealed_at_http(json)
            self.assertEqual(child.run_request["roo_points_ledger_id"], str(spend.pk))
            self.assertEqual(child.result["points_v1"]["original_key"], spend.idempotency_key)
            return SimpleNamespace(status_code=202, json=lambda: {"run_id": child.run_id})

        self.remote.side_effect = post
        child, created = self.start()
        self.assertTrue(created)
        self.run.refresh_from_db()
        replay, created = self.start()
        self.assertFalse(created)
        self.assertEqual(replay.pk, child.pk)
        self.assertEqual(self.remote.call_count, 1)
        self.assertEqual(list(Ledger.objects.order_by("pk").values()), before)
        self.assertEqual(PointsAccount.objects.get(user=self.user).balance_microroo, 8_000_000)

    def test_lost_ack_retry_keeps_one_named_admission_and_original_digest(self):
        sent = []

        def lost(url, *, json, **kwargs):
            sent.append(deepcopy(json))
            self.assert_sealed_at_http(json)
            raise TimeoutError("synthetic lost acknowledgement")

        self.remote.side_effect = lost
        with self.assertRaises(publish.PublishReviewedError):
            self.start()
        self.run.refresh_from_db()
        child = ContentFactoryRun.objects.get(run_id=self.run.result["publish_offer_v1"]["run_id"])
        saved = deepcopy(child.result[OWNER_RESULT_KEY])
        self.remote.side_effect = lambda url, **kwargs: SimpleNamespace(status_code=202,
            json=lambda: {"run_id": kwargs["json"]["new_run_id"]})
        retried, created = self.start()
        self.assertFalse(created)
        self.assertEqual(retried.pk, child.pk)
        retried.refresh_from_db()
        self.assertEqual(retried.result[OWNER_RESULT_KEY], saved)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.assertEqual(self.remote.call_args.kwargs["json"]["approved_body"], sent[0]["approved_body"])

    def test_unbacked_charge_rolls_back_admission_before_http(self):
        Ledger.objects.all().delete()
        with self.assertRaises(publish.PublishReviewedError):
            self.start()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.remote.assert_not_called()

    def test_existing_unowned_publish_child_is_not_enrolled_on_replay(self):
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
            child, _ = self.start()
        self.run.refresh_from_db()
        before = deepcopy(child.run_request)
        replay, created = self.start()
        self.assertFalse(created)
        replay.refresh_from_db()
        self.assertIsNone(owner_receipt(replay))
        self.assertEqual(replay.run_request, before)

    def test_lost_ack_retry_rechecks_current_claims_before_another_http(self):
        self.remote.side_effect = TimeoutError("synthetic lost acknowledgement")
        with self.assertRaises(publish.PublishReviewedError):
            self.start()
        self.run.refresh_from_db()
        self.review['reviewFindings'] = [{'kind': 'claim', 'id': 'new-current-claim'}]
        with self.assertRaises(publish.PublishReviewedError):
            self.start()
        self.assertEqual(self.remote.call_count, 1)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)


@override_settings(**{**hosted_fixture.HostedRegistrationTests._overridden_settings, **R3})
class NamedHostedProxyAdmissionTests(TransactionTestCase):
    setUp = hosted_fixture.HostedRegistrationTests.setUp

    def hosted_site(self):
        return HostedArticleSite.objects.create(organization=self.org, domain=self.org.domain, state="draft",
            worker_url="https://r3-owned.example.workers.dev")

    def test_proxy_is_fixed_and_sealed_with_scheduler_principal_before_http(self):
        site = self.hosted_site()
        seen = []

        def remote(path, payload):
            self.assertFalse(connection.in_atomic_block)
            run = ContentFactoryRun.objects.get(run_id=payload["run_id"])
            receipt = owner_receipt(run)
            self.assertEqual(receipt["identity"]["principal"], {"kind": "scheduler", "service": "hosted_auto_register"})
            self.assertEqual(payload[WIRE_REQUEST_KEY], run.run_request[WIRE_REQUEST_KEY])
            self.assertTrue(worker_workflow_allowed(run.workflow, "hosted_native_verification", request=run.run_request))
            seen.append(deepcopy(payload))
            return {"run_id": run.run_id, "status": "queued"}

        with fake_provider("content_factory.failure_policy_engine", claim_pull_request_slot=lambda *a, **k: True), \
                patch.object(auto, "_remote", side_effect=remote):
            self.assertTrue(auto._proxy(site, self.site, self.built.config))
            self.assertTrue(auto._proxy(site, self.site, self.built.config))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual(ContentFactoryRun.objects.filter(run_id=seen[0]["run_id"]).count(), 1)

    def test_changed_scope_cannot_reuse_proxy_admission(self):
        site = self.hosted_site()
        with fake_provider("content_factory.failure_policy_engine", claim_pull_request_slot=lambda *a, **k: True), \
                patch.object(auto, "_remote", return_value={"status": "queued"}) as remote:
            self.assertTrue(auto._proxy(site, self.site, self.built.config))
            self.site.github_repo = "foreign/repository"
            with self.assertRaises(RunOwnershipConflict):
                auto._proxy(site, self.site, self.built.config)
        self.assertEqual(remote.call_count, 1)


@override_settings(**R3, ARTICLES_PUBLISHING_INPUTS_V3_ENABLED=False, ARTICLES_WEBSITE_GOAL_BUDGETS_ENABLED=False)
class NamedVerifierAdmissionTests(OneClickBillingFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.context.profile = self.company.profile
        self.source = ContentFactoryRun.objects.create(run_id="r3-setup-source", organization=self.org,
            domain=self.org.domain, github_repo=self.website.github_repo, workflow="article_system_setup", status="completed",
            run_request={**self.binding, "expected_source_sha": self.website.verified_sha})
        self.parent = WebsiteConnectionOperation.objects.create(connection=self.website, generation=self.website.generation,
            action="prepare", state="running", idempotency_key="r3-parent", payload={"attempt": 1,
                "company_id": str(self.company.pk), "requested_by_user_id": self.requester.pk})
        goal = goals.new_goal(str(self.parent.pk), goals.scope_for(self.website, str(self.company.pk)), now=timezone.now())
        self.parent.receipt = {"goal": goal, "setup_run_id": self.source.run_id}
        self.parent.save(update_fields=["receipt"])
        self.target = self.website.targets.get(target_key="native")

    def test_actual_verifier_has_owner_before_http_and_cannot_replace_event_lifecycle(self):
        seen = []

        def remote(method, path, *, data):
            self.assertFalse(connection.in_atomic_block)
            run = ContentFactoryRun.objects.get(run_id=data["run_id"])
            receipt = owner_receipt(run)
            self.assertEqual(receipt["identity"]["principal"]["user_id"], str(self.requester.pk))
            self.assertEqual(data[WIRE_REQUEST_KEY], run.run_request[WIRE_REQUEST_KEY])
            seen.append((run.pk, run.updated_at, deepcopy(run.result[OWNER_RESULT_KEY]), deepcopy(run.result[OWNER_EXECUTION_KEY])))
            return {"status": "running", "run_id": run.run_id, "generation": 0, "state_version": 1,
                    "verification": {"pending": True}, "usage": {"model_usd": 0}}

        with patch.object(verifier, "remote_request", side_effect=remote):
            self.assertEqual(verifier.advance(self.parent, self.context, self.config, self.target, self.website.verified_sha), {"status": "pending"})
        run = ContentFactoryRun.objects.get(pk=seen[0][0])
        self.assertEqual(run.status, "queued")
        self.assertEqual(run.updated_at, seen[0][1])
        self.assertEqual(run.result[OWNER_RESULT_KEY], seen[0][2])
        self.assertEqual(run.result[OWNER_EXECUTION_KEY], seen[0][3])
        self.assertEqual(run.result[verifier.OWNED_OBSERVATION_KEY]["status"], "running")
        self.assertNotIn("verification", run.result)

    def test_changed_requester_refuses_before_verifier_http(self):
        self.parent.payload["requested_by_user_id"] = self.payer.pk
        self.parent.save(update_fields=["payload"])
        with patch.object(verifier, "remote_request") as remote, self.assertRaises(WebsiteAuthorityError):
            verifier.advance(self.parent, self.context, self.config, self.target, self.website.verified_sha)
        remote.assert_not_called()
        self.assertFalse(self.website.operations.filter(action='workflow').exists())
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
