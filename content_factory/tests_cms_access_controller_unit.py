"""Encrypted access survives page/worker loss without another owner grant."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_access_start as starts
from . import cms_access_controller as controller
from . import cms_access_authority as authority
from . import cms_owner_access as access
from . import tests_cms_owner_access_unit as fixtures
from .channel_artifacts import authorize_payload
from .cms_connection_authority import current_credential
from .cms_connection_contract import seal_credential
from .cms_api_transport import CmsApiError
from .website_contract import WebsiteAuthorityError


@override_settings(**{**fixtures.CmsOwnerAccessTests._overridden_settings, "ARTICLES_CMS_ACCESS_RENEWAL_ENABLED": True,
    "ARTICLES_CMS_STATUS_ENABLED": True})
class CmsAccessControllerTests(SimpleTestCase):
    configure = fixtures.CmsOwnerAccessTests.configure
    reserve = fixtures.CmsOwnerAccessTests.reserve
    hold = fixtures.CmsOwnerAccessTests.hold
    prepare_generation = fixtures.CmsOwnerAccessTests.prepare_generation
    consume = fixtures.CmsOwnerAccessTests.consume
    paid_run = fixtures.CmsOwnerAccessTests.paid_run

    def setUp(self):
        fixtures.CmsOwnerAccessTests.setUp(self)
        self.stack.enter_context(patch("django.utils.timezone.now", side_effect=lambda: self.now))
        self.goal_operation.updated_at = self.now
        form = access.for_operation(self.context, self.config, user=self.user, operation_id=str(self.goal_operation.pk))["cmsAccess"]
        self.data = {"company_id": form["companyId"], "website_connection_id": form["connectionId"],
            "connection_generation": form["connectionGeneration"], "configuration_revision": form["configurationRevision"],
            "operation_id": form["operationId"], "access_epoch": form["accessEpoch"], "client_request_id": str(uuid4()),
            "platform": form["platform"], "public_origin": form["publicOrigin"], "api_origin": form["apiOrigin"],
            "channel_scope": form["channelScope"], "channel_revision": form["channelRevision"], "credential_origin_confirmed": True,
            "username": "renewed-owner", "secret": "synthetic-durable-replacement-password"}
        self.transport = self.stack.enter_context(patch.object(controller, "request_json", side_effect=self.read))

    def read(self, binding, credential, **options):
        self.assertEqual(options["method"], "GET")
        self.assertEqual(binding, self.binding)
        if options["resource"] == "site":
            self.assertIsNone(credential)
            return {"home": binding["public_origin"], "url": binding["api_origin"]}, {}
        self.assertEqual(credential.secret, self.data["secret"])
        return {"id": int(self.identity["principal_id"]), "capabilities": {"edit_posts": True, "publish_posts": True}}, {}

    def accept(self, **changes):
        self.job = starts.start(self.context, self.config, user=self.user, data={**self.data, **changes})
        return self.job

    def advance(self, seconds=0):
        self.now += timedelta(seconds=seconds)
        return controller.advance(self.job.pk, now=self.now)

    def test_acceptance_persists_only_encrypted_proposal_before_http_without_new_charge_or_source(self):
        original = deepcopy((self.private.payload, self.run.run_request, self.outbox.payload))
        job = self.accept()
        self.assertEqual((job.action, job.state), (authority.ACTION, "pending"))
        self.assertTrue(job.payload["sealed_credential"].startswith("mlai-enc:v1:"))
        self.assertNotIn(self.data["secret"], str(job.payload))
        self.assertEqual((self.private.payload, self.run.run_request, self.outbox.payload), original)
        self.assertEqual(set(self.goal_operation.receipt["native_budget"]["access_usage"]), {str(job.pk)})
        self.assertEqual(self.goal_operation.receipt["native_budget"]["usage_reports"], {})
        self.transport.assert_not_called(); self.http.assert_not_called(); self.spend.assert_called_once()

    def test_identical_owner_nonce_replays_one_pending_grant_and_changed_inputs_cannot_replace_it(self):
        original = self.accept()
        packet = original.payload["sealed_credential"]
        self.assertIs(self.accept(), original)
        for changes in ({"secret": "synthetic-different-key"}, {"username": "another-owner"}, {"client_request_id": str(uuid4())},
                {"connection_generation": True}, {"credential_origin_confirmed": False}, {"extra": "discarded"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.accept(**changes)
        self.assertEqual(original.payload["sealed_credential"], packet)
        self.assertEqual(len([row for row in self.saved if row.action == authority.ACTION]), 1)
        self.transport.assert_not_called(); self.spend.assert_called_once()

    def test_page_close_does_not_prevent_actual_verification_and_same_key_rotation(self):
        original = deepcopy((self.run.run_request, self.outbox.payload, self.config.article_system, self.connection.configuration_version))
        self.accept()
        result = self.advance()
        self.assertEqual((result.state, result.receipt["status"], result.payload["sealed_credential"]), ("completed", "access_updated", None))
        self.assertEqual(self.transport.call_count, 2)

        self.assertEqual(current_credential(self.context, self.config, self.connection, user=self.user)[1].secret, self.data["secret"])
        self.assertEqual((self.run.run_request, self.outbox.payload, self.config.article_system, self.connection.configuration_version), original)
        self.assertEqual(str(authorize_payload(self.run.run_request).operation.pk), self.run.run_request["publishing_channel_artifact"]["artifact_operation_id"])
        used = self.goal_operation.receipt["native_budget"]["access_usage"][str(self.job.pk)]
        self.assertEqual((used["active_seconds"], used["provider_seconds"]), (180, 360))
        self.assertEqual(self.goal_operation.state, "needs_user")
        self.assertEqual(self.goal_operation.next_attempt_at, self.now)
        self.spend.assert_called_once(); self.disconnect.assert_not_called(); self.new_connection.assert_not_called()

    def test_a_different_real_principal_cannot_replace_the_original_account(self):
        self.accept(); original = deepcopy(self.private.payload)
        def another_principal(*args, **kwargs):
            body, headers = self.read(*args, **kwargs)
            if kwargs["resource"] == "users": body["id"] += 1
            return body, headers
        self.transport.side_effect = another_principal
        self.assertEqual(self.advance().state, "needs_user")
        self.assertEqual(self.private.payload, original)
        self.assertIsNone(self.job.payload["sealed_credential"])

    def test_a_real_identity_without_publish_permission_needs_owner_access(self):
        self.accept(); original = deepcopy(self.private.payload)
        def no_publishing(*args, **kwargs):
            body, headers = self.read(*args, **kwargs)
            if kwargs["resource"] == "users": body["capabilities"]["publish_posts"] = False
            return body, headers
        self.transport.side_effect = no_publishing
        self.assertEqual(self.advance().state, "needs_user")
        self.assertEqual(self.private.payload, original)
        self.assertEqual(self.job.receipt["failure"]["failure_class"], "needs_user")

    def test_a_reply_after_lease_expiry_cannot_commit_the_replacement(self):
        self.accept(); original = deepcopy(self.private.payload)
        def late(*args, **kwargs):
            body = self.read(*args, **kwargs)
            self.now += timedelta(seconds=181)
            return body
        self.transport.side_effect = late
        self.assertEqual(self.advance().state, "pending")
        self.assertEqual(self.private.payload, original)
        self.assertEqual(self.job.receipt["failure"]["code"], "RUN_STALLED_WORKER_LOST")
        self.transport.assert_called_once()

    def test_newer_worker_lease_wins_a_late_reply(self):
        self.accept(); original = deepcopy(self.private.payload)
        replacement = str(uuid4())
        def superseded(*args, **kwargs):
            body = self.read(*args, **kwargs)
            self.job.payload["lease_id"] = replacement
            return body
        self.transport.side_effect = superseded
        self.assertIsNone(self.advance())
        self.assertEqual((self.job.state, self.job.payload["lease_id"]), ("running", replacement))
        self.assertEqual(self.private.payload, original)
        self.transport.assert_called_once()

    def test_rollout_pause_during_get_retains_ciphertext_without_an_authenticated_followup(self):
        self.accept(); packet = self.job.payload["sealed_credential"]
        setting = override_settings(ARTICLES_CMS_ACCESS_RENEWAL_ENABLED=False)
        def paused(*args, **kwargs):
            body = self.read(*args, **kwargs)
            setting.enable()
            return body
        self.addCleanup(setting.disable)
        self.transport.side_effect = paused
        self.assertEqual(self.advance().state, "pending")
        self.assertEqual(self.job.receipt["status"], "access_paused")
        self.assertEqual(self.job.payload["sealed_credential"], packet)
        self.transport.assert_called_once()

    def test_new_access_failure_epoch_refuses_old_owner_ack_and_old_worker(self):
        self.accept()
        self.goal_operation.receipt["failure_occurrence_id"] = str(uuid4())
        with self.assertRaises(WebsiteAuthorityError): self.accept()
        self.assertEqual(self.advance().state, "cancelled")
        fresh = access.for_operation(self.context, self.config, user=self.user, operation_id=str(self.goal_operation.pk))["cmsAccess"]
        self.assertNotEqual(fresh["accessEpoch"], self.data["access_epoch"])
        self.assertEqual(self.accept(access_epoch=fresh["accessEpoch"], client_request_id=str(uuid4())).state, "pending")
        self.transport.assert_not_called()

    def test_original_active_budget_stops_before_any_provider_get(self):
        self.accept()
        from . import cms_goal_refunds
        with override_settings(ARTICLES_GOAL_ACTIVE_SECONDS=1), patch.object(cms_goal_refunds, "reserve_refund") as settlement:
            self.goal_operation.receipt["native_budget"]["budget_used"]["active_seconds"] = 1
            self.assertEqual(self.advance().state, "needs_attention")
        self.assertEqual(self.goal_operation.state, "needs_attention")
        self.assertEqual(self.job.receipt["budget_stop_reason"], "active_compute")
        self.assertIsNone(self.job.payload["sealed_credential"])
        settlement.assert_called_once(); self.transport.assert_not_called()

    def test_repeated_same_transient_failure_escalates_and_cannot_loop_forever(self):
        self.accept(); self.transport.side_effect = CmsApiError("transient")
        from . import cms_goal_refunds
        with patch.object(cms_goal_refunds, "reserve_refund") as settlement:
            self.assertEqual(self.advance().state, "pending")
            self.assertEqual(self.advance(30).receipt["effective_failure_class"], "repairable")
            self.assertEqual(self.advance(60).state, "needs_attention")
        self.assertEqual(self.goal_operation.state, "needs_attention")
        self.assertEqual(self.job.receipt["effective_failure_class"], "strategy")
        self.assertIsNone(self.job.payload["sealed_credential"])
        self.assertEqual(self.advance(300).attempts, 3)
        settlement.assert_called_once(); self.assertEqual(self.transport.call_count, 3)

    def test_lost_completed_acknowledgement_reuses_original_journal_without_more_http_or_key_updates(self):
        original = self.accept(); self.advance()
        packet = deepcopy(self.private.payload)
        self.transport.reset_mock()
        self.assertIs(self.advance(), original)
        self.assertIs(self.accept(), original)
        self.assertEqual(self.private.payload, packet)
        self.transport.assert_not_called(); self.spend.assert_called_once()

    def test_transient_failure_resumes_the_same_encrypted_grant_and_counts_original_usage(self):
        self.accept(); packet = self.job.payload["sealed_credential"]
        self.transport.side_effect = CmsApiError("transient")
        result = self.advance()
        self.assertEqual((result.state, result.receipt["failure"]["failure_class"]), ("pending", "transient"))
        self.assertEqual(self.job.payload["sealed_credential"], packet)
        self.assertEqual(self.advance().attempts, 1)
        self.transport.side_effect = self.read
        self.assertEqual(self.advance(30).state, "completed")
        used = self.goal_operation.receipt["native_budget"]["access_usage"][str(self.job.pk)]
        self.assertEqual((used["active_seconds"], used["provider_seconds"]), (360, 540))
        self.assertEqual(self.goal_operation.receipt["native_budget"]["attempts_by_class"]["transient"], 1)
        self.spend.assert_called_once()

    def test_dead_worker_is_recorded_once_and_new_worker_finishes_original_grant(self):
        self.accept()
        lease = str(uuid4())
        self.job.state = "running"; self.job.attempts = 1
        self.job.payload["lease_id"] = lease; self.job.next_attempt_at = self.now
        result = self.advance()
        self.assertEqual((result.state, result.receipt["failure"]["code"]), ("pending", "RUN_STALLED_WORKER_LOST"))
        self.assertEqual(self.advance().attempts, 1)
        self.assertEqual(self.advance(30).state, "completed")
        self.assertEqual(self.goal_operation.receipt["native_budget"]["attempts_by_class"]["transient"], 1)
        self.assertEqual(self.transport.call_count, 2)

    def test_wrong_credentials_or_changed_real_principal_need_only_owner_access_and_wipe_proposal(self):
        original = deepcopy(self.private.payload)
        self.accept()
        self.transport.side_effect = CmsApiError("credentials_required")
        result = self.advance()
        self.assertEqual((result.state, result.payload["sealed_credential"]), ("needs_user", None))
        self.assertEqual(result.receipt["failure"]["failure_class"], "needs_user")
        self.assertEqual(self.private.payload, original)
        with self.assertRaises(WebsiteAuthorityError): self.accept()
        self.assertEqual(self.accept(client_request_id=str(uuid4())).state, "pending")
        self.spend.assert_called_once()

    def test_newer_key_owner_revocation_and_root_cancellation_fence_pending_work(self):
        self.accept()
        self.private.payload["sealed_credential"] = seal_credential(self.binding, username="newer", secret="synthetic-more-recent-key")
        newer = deepcopy(self.private.payload)
        self.assertEqual(self.advance().state, "cancelled")
        self.assertIsNone(self.job.payload["sealed_credential"])
        self.assertEqual(self.private.payload, newer)
        self.transport.assert_not_called()

    def test_owner_change_during_public_probe_prevents_authenticated_traffic_and_commit(self):
        original = deepcopy(self.private.payload)
        self.accept()
        def revoked(*args, **kwargs):
            result = self.read(*args, **kwargs)
            self.user.is_active = False
            return result
        self.transport.side_effect = revoked
        self.assertEqual(self.advance().state, "cancelled")
        self.assertEqual(self.private.payload, original)
        self.assertIsNone(self.job.payload["sealed_credential"])
        self.transport.assert_called_once()

    def test_expiry_wipes_proposal_even_if_execution_rollout_is_off(self):
        self.accept()
        with override_settings(ARTICLES_CMS_ACCESS_RENEWAL_ENABLED=False):
            self.assertEqual(self.advance().state, "pending")
            self.assertEqual(self.advance(86400).state, "needs_user")
        self.assertIsNone(self.job.payload["sealed_credential"])
        self.transport.assert_not_called()

    def test_independent_expiry_reconciler_erases_only_due_private_proposals_while_disabled(self):
        from .cms_access_status import expire_due
        self.accept(); packet = self.job.payload["sealed_credential"]
        with override_settings(ARTICLES_CMS_ACCESS_RENEWAL_ENABLED=False):
            expire_due(now=self.now, connection_id=self.connection.pk)
            self.assertEqual(self.job.payload["sealed_credential"], packet)
            expire_due(now=self.now + timedelta(days=1), connection_id=self.connection.pk)
        self.assertEqual((self.job.state, self.job.payload["sealed_credential"]), ("needs_user", None))
        self.transport.assert_not_called()

    def test_public_json_lease_ciphertext_swap_or_new_failure_epoch_cannot_authorize_worker(self):
        self.accept()
        with self.assertRaises(WebsiteAuthorityError): controller._guard({"operation_id": str(self.job.pk), "lease_id": str(uuid4())})
        self.job.payload["sealed_credential"] = seal_credential(self.binding, username="swapped", secret="synthetic-another-key")
        self.assertEqual(self.advance().state, "cancelled")
        self.assertIsNone(self.job.payload["sealed_credential"])
        self.transport.assert_not_called()

    def test_disabled_acceptance_and_malformed_key_do_not_store_a_proposal(self):
        with override_settings(ARTICLES_CMS_ACCESS_RENEWAL_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.accept()
        with self.assertRaises(WebsiteAuthorityError): self.accept(secret="bad")
        self.assertFalse(any(row.action == authority.ACTION for row in self.saved))
        self.transport.assert_not_called(); self.spend.assert_called_once()

    def test_pending_owner_projection_and_parent_barrier_need_no_more_input_or_old_key_retry(self):
        from .cms_goal_controller import advance_goal
        from .cms_goal_status import for_context
        self.accept()
        original_attempts = self.goal_operation.attempts
        packet = access.for_operation(self.context, self.config, user=self.user, operation_id=str(self.goal_operation.pk))
        self.assertIsNone(packet["cmsAccess"])
        self.assertEqual(packet["cmsAccessOperation"]["requestId"], self.data["client_request_id"])
        self.assertNotIn(self.data["secret"], str(packet))
        self.assertNotIn("credential_digest", str(packet))
        self.assertIs(advance_goal(self.goal_operation.pk, now=self.now), self.goal_operation)
        self.assertEqual(self.goal_operation.attempts, original_attempts)
        result = for_context(self.context, self.config, user=self.user, now=self.now)
        self.assertEqual((result["state"], result["title"], result["action"]), ("working", "Checking CMS access", None))
        self.assertEqual(result["nextAttemptAt"], self.job.next_attempt_at.isoformat())
        self.transport.assert_not_called()

    def test_owner_restore_route_acknowledges_actual_encrypted_journal_before_provider_work(self):
        from types import SimpleNamespace
        from .website_views import WebsiteConnectionActionView
        request = SimpleNamespace(user=self.user, data=self.data)
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)):
            result = WebsiteConnectionActionView().post(request, "cms-restore-access")
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result["Cache-Control"], "private, no-store")
        self.assertEqual(result.data["cmsAccessOperation"]["requestId"], self.data["client_request_id"])
        self.assertNotIn(self.data["secret"], str(result.data))
        self.transport.assert_not_called()

    def test_lifecycle_cancellation_wipes_pending_secret_without_touching_active_native_key(self):
        self.accept(); original = deepcopy(self.private.payload)
        controller.cancel_for_connection(self.connection)
        self.assertEqual((self.job.state, self.job.payload["sealed_credential"]), ("cancelled", None))
        self.assertEqual(self.private.payload, original)
        self.assertEqual(self.advance().state, "cancelled")
        self.transport.assert_not_called()

    def test_identity_gets_are_unlocked_and_original_spend_reads_own_short_transactions(self):
        from contextlib import contextmanager
        self.accept(); depth = []
        @contextmanager
        def atomic(*_args, **_kwargs):
            depth.append(True)
            try: yield
            finally: depth.pop()
        def read(*args, **kwargs):
            self.assertEqual(depth, [])
            return self.read(*args, **kwargs)
        self.transport.side_effect = read
        def locked_spend(*_args, **_kwargs):
            self.assertTrue(depth, "The original spend read requires an owning transaction")
            return self.ledger_rows.return_value
        from .cms_access_status import for_root
        with patch("django.db.transaction.atomic", side_effect=atomic), \
                patch.object(self.ledger_rows, "side_effect", locked_spend):
            self.assertEqual(for_root(self.goal_operation)["state"], "pending")
            self.assertEqual(self.advance().state, "completed")
        self.assertEqual(depth, [])
        self.assertEqual(self.transport.call_count, 2)
