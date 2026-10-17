"""Initial acceptance/verification keeps one original root and held amount."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_initial_start as starts
from . import cms_initial_controller as controller
from . import cms_initial_authority as authority
from . import cms_onboarding_offer as offers
from . import cms_setup_authority as setup
from . import cms_initial_status as status
from . import tests_cms_setup_billing_unit as fixtures
from .cms_connection_authority import current_credential
from .cms_api_transport import CmsApiError
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .website_publishing_channels import scope_for
from .website_contract import WebsiteAuthorityError


@override_settings(**{**fixtures.CmsSetupBillingTests._overridden_settings, "ARTICLES_CMS_ONBOARDING_ENABLED": True})
class CmsInitialTests(SimpleTestCase):
    configure = fixtures.CmsSetupBillingTests.configure
    reserve = fixtures.CmsSetupBillingTests.reserve

    def setUp(self):
        fixtures.CmsSetupBillingTests.setUp(self)
        self.stack.enter_context(patch("django.utils.timezone.now", side_effect=lambda: self.now))
        self.saved.clear()
        self.config.website_connection = None
        self.config.save = Mock()
        self.config.github_repo = ""
        choice = self.config.article_system["publishing_channel"]
        choice["scope"] = scope_for(self.context, self.config)
        self.config.article_system = {"publishing_channel": choice}
        self.stack.enter_context(patch.object(WebsiteConnection, "operations", self.manager))
        def create_connection(**values):
            values["authorized_by_id"] = values.pop("authorized_by").pk
            row = WebsiteConnection(**values)
            row.save = Mock(); row.refresh_from_db = Mock()
            self.connection = row
            return row
        self.connection_create = self.stack.enter_context(patch.object(WebsiteConnection.objects, "create", side_effect=create_connection))
        def create(**values):
            row = WebsiteConnectionOperation(**values)
            row.created_at = row.updated_at = self.now
            row.save = Mock(side_effect=lambda **kwargs: setattr(row, "updated_at", self.now))
            row.refresh_from_db = Mock()
            self.saved.append(row)
            return row
        self.operations_create = self.stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "create", side_effect=create))
        def debit(**values):
            self.ledger.reference_id, self.ledger.idempotency_key = values["reference_id"], values["idempotency_key"]
            return self.ledger, True
        self.spend.side_effect = debit
        offer = offers.for_context(self.context, self.config, user=self.user, data={"platform": "wordpress", "public_origin": "https://fixture.test/",
            "api_origin": "https://fixture.test/", "channel_scope": choice["scope"], "channel_revision": choice["revision"]}, now=self.now)
        self.data = {"client_request_id": str(uuid4()), "offer": offer, "credential_origin_confirmed": True,
            "username": "fixture-owner", "secret": "synthetic-first-application-password", **deepcopy(self.intent["answers"])}
        self.transport = self.stack.enter_context(patch.object(controller, "request_json", side_effect=self.read))

    def read(self, binding, credential, **options):
        self.assertEqual(options["method"], "GET")
        if options["resource"] == "site":
            self.assertIsNone(credential)
            return {"home": binding["public_origin"], "url": binding["api_origin"]}, {}
        self.assertEqual(credential.secret, self.data["secret"])
        return {"id": 7, "capabilities": {"edit_posts": True, "publish_posts": True}}, {}

    def start(self, **changes):
        self.root = starts.start(self.context, self.config, user=self.user, data={**self.data, **changes}, now=self.now)
        self.job = next(row for row in self.saved if row.action == authority.ACTION)
        return self.root

    def advance(self, seconds=0):
        self.now += timedelta(seconds=seconds)
        return controller.advance(self.job.pk, now=self.now)

    def restore(self, **changes):
        from .cms_access_contract import wait_epoch
        self.retry_data = getattr(self, "retry_data", {"operation_id": str(self.root.pk), "client_request_id": str(uuid4()),
            "access_epoch": wait_epoch(self.root), "credential_origin_confirmed": True, "username": self.data["username"], "secret": self.data["secret"]})
        return starts.restore(self.context, self.config, user=self.user, data={**self.retry_data, **changes}, now=self.now)

    def test_one_acceptance_holds_once_and_stores_only_encrypted_proposal_before_any_provider_io(self):
        root = self.start()
        self.assertEqual((root.action, root.state, self.connection.state), ("cms-setup", "pending", "paused"))
        self.assertEqual(root.receipt["first_article_billing"]["cost_points"], 6)
        self.assertNotIn("goal", root.receipt)
        self.assertEqual(setup.historical(root)["accepted_at"], root.receipt["native_budget"]["started_at"])
        proposed = authority.proposal(authority.scope(self.job, user=self.user, now=self.now), now=self.now)
        self.assertTrue(proposed.row.payload["sealed_credential"].startswith("mlai-enc:v1:"))
        self.assertNotIn(self.data["secret"], str([row.payload for row in self.saved]))
        with self.assertRaises(WebsiteAuthorityError): current_credential(self.context, self.config, self.connection, user=self.user)
        self.assertEqual(len([row for row in self.saved if row.action == "channel-credential"]), 1)
        self.spend.assert_called_once(); self.transport.assert_not_called(); self.http.assert_not_called()

    def test_lost_acceptance_replays_original_grant_and_price_without_another_hold(self):
        original = self.start(); packet = deepcopy(self.job.payload)
        self.price.return_value = 99
        self.assertIs(self.start(), original)
        for changes in ({"secret": "synthetic-changed-application-password"}, {"review_policy": "review"}, {"client_request_id": str(uuid4())}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.start(**changes)
        self.assertEqual(self.job.payload, packet)
        self.spend.assert_called_once(); self.transport.assert_not_called()

    def test_page_close_still_verifies_identity_and_resumes_the_same_held_native_setup(self):
        root = self.start(); intent = deepcopy(setup.historical(root)); held = deepcopy(root.receipt["first_article_billing"])
        self.assertEqual(self.advance().state, "completed")
        self.assertEqual(self.connection.state, "connected")
        self.assertEqual(current_credential(self.context, self.config, self.connection, user=self.user)[1].secret, self.data["secret"])
        self.assertEqual((root.state, root.receipt["status"], root.next_attempt_at), ("pending", "starting", self.now))
        self.assertEqual(setup.current(root).intent, intent)
        self.assertEqual(root.receipt["first_article_billing"], held)
        self.assertIs(self.start(), root)
        self.assertEqual(len([row for row in self.saved if row.action == "cms-connect"]), 1)
        self.assertTrue(all(row.payload["sealed_credential"] is None for row in self.saved if row.action == authority.GRANT_ACTION))
        self.assertEqual(self.transport.call_count, 2); self.spend.assert_called_once()

    def test_worker_financial_guards_hold_short_transactions_while_each_provider_get_is_unlocked(self):
        from . import website_first_article_billing as records
        self.start(); actual = records._original_spend
        with patch("django.db.transaction.get_connection", return_value=SimpleNamespace(in_atomic_block=False)):
            with self.assertRaises(WebsiteAuthorityError): authority.scope(self.job)
        def held(*args, **kwargs):
            self.assertGreater(self.depth, 0)
            return actual(*args, **kwargs)
        with patch.object(records, "_original_spend", side_effect=held) as checks:
            self.assertEqual(self.advance().state, "completed")
        self.assertGreater(checks.call_count, 2)
        self.assertEqual(self.depth, 0)
        self.transport.assert_called(); self.spend.assert_called_once()

    def test_transient_retry_uses_same_ciphertext_and_original_budget_without_browser_or_new_price(self):
        self.start()
        proposal = authority.proposal(authority.scope(self.job, now=self.now), now=self.now).row
        packet = proposal.payload["sealed_credential"]
        self.transport.side_effect = CmsApiError("transient")
        self.assertEqual(self.advance().state, "pending")
        self.assertEqual(self.advance().attempts, 1)
        self.price.return_value = 99; self.transport.side_effect = self.read
        self.assertEqual(self.advance(30).state, "completed")
        usage = self.root.receipt["native_budget"]["access_usage"][str(self.job.pk)]
        self.assertEqual((usage["active_seconds"], usage["provider_seconds"]), (360, 540))
        self.assertNotEqual(packet, None); self.spend.assert_called_once()

    def test_wrong_key_is_wiped_and_fresh_key_after_expiry_keeps_original_intent_hold_and_budget(self):
        self.start(); intent = deepcopy(setup.historical(self.root)); held = deepcopy(self.root.receipt["first_article_billing"])
        self.transport.side_effect = CmsApiError("credentials_required")
        self.assertEqual(self.advance().state, "needs_user")
        original_grant = self.job.payload["grant_operation_id"]
        self.now += timedelta(days=2)
        self.transport.side_effect = self.read
        self.assertIs(self.restore(), self.root)
        self.assertNotEqual(original_grant, self.job.payload["grant_operation_id"])
        self.assertEqual(self.advance().state, "completed")
        self.assertEqual(setup.historical(self.root), intent)
        self.assertEqual(self.root.receipt["first_article_billing"], held)
        self.assertEqual(self.job.attempts, 2)
        self.spend.assert_called_once()

    def test_dead_worker_occurrence_is_counted_once_then_original_grant_resumes(self):
        self.start()
        self.job.state, self.job.attempts, self.job.next_attempt_at = "running", 1, self.now
        self.job.payload.update(lease_id=str(uuid4()), lease_grant_id=self.job.payload["grant_operation_id"])
        self.assertEqual(self.advance().receipt["failure"]["code"], "RUN_STALLED_WORKER_LOST")
        self.assertEqual(self.advance().attempts, 1)
        self.assertEqual(self.advance(30).state, "completed")
        self.assertEqual(self.root.receipt["native_budget"]["attempts_by_class"]["transient"], 1)

    def test_expired_grant_erases_key_with_execution_disabled_and_retains_same_financial_root(self):
        self.start(); held = deepcopy(self.root.receipt["first_article_billing"])
        with override_settings(ARTICLES_CMS_ONBOARDING_ENABLED=False):
            self.assertEqual(self.advance().state, "pending")
            self.assertEqual(self.advance(86400).state, "needs_user")
        self.assertEqual(self.root.receipt["first_article_billing"], held)
        self.assertTrue(all(row.payload["sealed_credential"] is None for row in self.saved if row.action == authority.GRANT_ACTION))
        self.transport.assert_not_called(); self.spend.assert_called_once()

    def test_revoked_owner_before_claim_cancels_without_any_provider_traffic(self):
        self.start(); self.user.is_active = False
        self.assertEqual(self.advance().state, "cancelled")
        self.assertEqual(self.root.state, "cancelled")
        self.assertTrue(any(row.action == "cms-setup-refund" for row in self.saved))
        self.transport.assert_not_called()

    def test_revocation_during_public_get_prevents_authenticated_get_or_key_commit(self):
        self.start()
        def revoke(*args, **kwargs):
            result = self.read(*args, **kwargs); self.user.is_active = False; return result
        self.transport.side_effect = revoke
        self.assertEqual(self.advance().state, "cancelled")
        self.assertEqual(self.connection.state, "paused")
        self.transport.assert_called_once()

    def test_json_dispatch_and_substituted_grant_are_not_an_execution_capability(self):
        self.start()
        with self.assertRaises(WebsiteAuthorityError): controller._guard({"operation_id": str(self.job.pk), "lease_id": str(uuid4())})
        self.job.payload["grant_operation_id"] = str(uuid4())
        self.assertEqual(self.advance().state, "cancelled")
        self.transport.assert_not_called()

    def test_actual_initial_root_enters_existing_discovery_and_goal_without_another_acceptance_or_spend(self):
        from .cms_setup_controller import advance_setup
        self.start(); original = deepcopy(setup.historical(self.root)); held = deepcopy(self.root.receipt["first_article_billing"])
        # The ordinary root scheduler can itself finish initial verification.
        self.assertIs(advance_setup(self.root.pk, now=self.now), self.root)
        self.assertEqual(self.job.state, "completed")
        self.binding = deepcopy(self.connection.capabilities["_cms_binding"])
        self.private = next(row for row in self.saved if row.action == "channel-credential")
        self.price.return_value = 99
        self.assertIs(advance_setup(self.root.pk, now=self.now), self.root)
        self.assertEqual((self.root.action, self.root.state), ("cms-goal", "pending"))
        self.assertEqual(str(self.root.pk), original["operation_id"])
        self.assertEqual(self.root.receipt["first_article_billing"], held)
        self.assertEqual(self.root.receipt["native_budget"]["started_at"], original["accepted_at"])
        self.assertEqual(self.root.receipt["native_budget"]["setup_usage"]["provider_seconds"], 360)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.transport.call_count, 2); self.spend.assert_called_once()

    def test_initial_nonce_status_recovery_is_read_only_and_reports_real_schedule_without_fake_milestones(self):
        self.start()
        before = deepcopy([(row.payload, row.receipt, row.state) for row in self.saved])
        packet = status.packet(self.context, self.config, user=self.user, request_id=self.data["client_request_id"], now=self.now)
        self.assertEqual(packet["cmsInitial"]["requestId"], self.data["client_request_id"])
        self.assertEqual(packet["websiteStatus"]["mode"], "connect")
        self.assertEqual(packet["websiteStatus"]["milestones"], [])
        self.assertIsNone(packet["websiteStatus"]["goalId"])
        self.assertEqual(packet["websiteStatus"]["nextAttemptAt"], self.job.next_attempt_at.isoformat())
        for private in ("sealed_credential", "principal_id", "proposal_digest", "spend_ledger_id", self.data["secret"], self.data["username"]):
            self.assertNotIn(private, str(packet))
        self.assertEqual([(row.payload, row.receipt, row.state) for row in self.saved], before)
        self.transport.assert_not_called(); self.spend.assert_called_once()

    def test_failed_key_has_explicit_restore_action_and_unknown_or_foreign_nonce_cannot_recover(self):
        self.start(); self.transport.side_effect = CmsApiError("credentials_required")
        self.advance()
        packet = status.packet(self.context, self.config, user=self.user, operation_id=str(self.root.pk), now=self.now)
        self.assertTrue(packet["cmsInitial"]["canRestore"])
        self.assertEqual(packet["websiteStatus"]["action"]["id"], "connect_initial_cms")
        for kwargs in ({"request_id": str(uuid4())}, {"operation_id": str(uuid4())},
                {"request_id": self.data["client_request_id"], "operation_id": str(self.root.pk)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(WebsiteAuthorityError):
                status.packet(self.context, self.config, user=self.user, now=self.now, **kwargs)
        with override_settings(ARTICLES_CMS_ONBOARDING_ENABLED=False):
            paused = status.packet(self.context, self.config, user=self.user, operation_id=str(self.root.pk), now=self.now)
            self.assertFalse(paused["cmsInitial"]["canRestore"])
            self.assertEqual(paused["websiteStatus"]["action"]["id"], "inspect_goal")

    def test_lost_restore_reply_replays_only_actual_accepted_nonce_before_and_after_verification(self):
        self.start(); self.transport.side_effect = CmsApiError("credentials_required"); self.advance()
        self.restore(); saved = deepcopy(self.job.payload)
        self.assertIs(self.restore(), self.root)
        with self.assertRaises(WebsiteAuthorityError): self.restore(secret="synthetic-different-key")
        self.assertEqual(self.job.payload, saved)
        self.transport.side_effect = self.read; self.assertEqual(self.advance().state, "completed")
        self.assertIs(self.restore(), self.root)
        with self.assertRaises(WebsiteAuthorityError): self.start()
        completed = status.packet(self.context, self.config, user=self.user, request_id=self.data["client_request_id"], now=self.now)
        self.assertEqual(completed["cmsInitial"]["requestId"], self.retry_data["client_request_id"])
        self.assertEqual(completed["cmsInitial"]["state"], "completed")
        self.assertFalse(completed["cmsInitial"]["canRestore"])
        self.assertNotIn("websiteStatus", completed)
        self.spend.assert_called_once()

    def test_late_worker_cannot_commit_or_rewrite_a_newer_verification_lease(self):
        self.start(); newer = str(uuid4())
        def replace_lease(*args, **kwargs):
            reply = self.read(*args, **kwargs)
            self.job.payload["lease_id"] = newer
            return reply
        self.transport.side_effect = replace_lease
        self.assertIsNone(self.advance())
        self.assertEqual((self.job.state, self.job.payload["lease_id"]), ("running", newer))
        self.assertEqual(self.connection.state, "paused")
        self.assertEqual(self.root.receipt["status"], "initial_connecting")
        self.assertFalse(any(row.action == "cms-connect" for row in self.saved))
        self.transport.assert_called_once()

    def test_actual_site_mismatch_never_sends_authenticated_get_and_stops_original_unused_hold(self):
        self.start(); self.transport.side_effect = lambda *args, **kwargs: ({"home": "https://other.test/", "url": "https://other.test/"}, {})
        self.assertEqual(self.advance().state, "needs_attention")
        self.assertEqual(self.root.state, "needs_attention")
        self.assertTrue(any(row.action == "cms-setup-refund" for row in self.saved))
        self.transport.assert_called_once(); self.spend.assert_called_once()

    def test_real_identity_without_publish_permission_only_requests_new_credentials(self):
        self.start()
        def no_publish(*args, **kwargs):
            reply, headers = self.read(*args, **kwargs)
            if kwargs["resource"] == "users": reply["capabilities"]["publish_posts"] = False
            return reply, headers
        self.transport.side_effect = no_publish
        self.assertEqual(self.advance().state, "needs_user")
        self.assertEqual(self.root.receipt["failure"]["failure_class"], "needs_user")
        self.assertFalse(any(row.action == "cms-setup-refund" for row in self.saved))
        self.assertEqual(self.connection.state, "paused")

    def test_repeated_provider_failure_is_bounded_and_settles_only_original_unused_hold(self):
        self.start(); self.transport.side_effect = CmsApiError("transient")
        for seconds in (0, 30, 60, 120):
            self.advance(seconds)
            if self.job.state == "needs_attention": break
        self.assertEqual(self.job.state, "needs_attention")
        self.assertEqual(self.root.state, "needs_attention")
        self.assertLessEqual(self.job.attempts, 4)
        self.assertTrue(any(row.action == "cms-setup-refund" for row in self.saved))
        self.assertTrue(all(row.payload["sealed_credential"] is None for row in self.saved if row.action == authority.GRANT_ACTION))
        self.spend.assert_called_once()

    def test_refunded_or_dispatched_original_hold_cannot_verify_an_initial_credential(self):
        self.start(); self.refunds.return_value.exists.return_value = True
        self.assertEqual(self.advance().state, "cancelled")
        self.transport.assert_not_called()

    def test_existing_outbox_is_never_accepted_as_unused_initial_money(self):
        self.start(); self.outbox_rows.return_value.exists.return_value = True
        self.assertEqual(self.advance().state, "cancelled")
        self.transport.assert_not_called()

    def test_ghost_reads_public_site_without_key_then_explicit_admin_destination(self):
        choice = self.config.article_system["publishing_channel"]
        choice["selection"]["platform"] = "ghost"
        self.data["offer"] = offers.for_context(self.context, self.config, user=self.user,
            data={"platform": "ghost", "public_origin": "https://fixture.test/", "api_origin": "https://admin.fixture.test/",
                "channel_scope": choice["scope"], "channel_revision": choice["revision"]}, now=self.now)
        self.data["username"], self.data["secret"] = "", "a" * 24 + ":" + "b" * 64
        self.start()
        def ghost(binding, credential, **options):
            self.assertEqual(self.depth, 0)
            self.assertEqual(binding["api_origin"], "https://admin.fixture.test/")
            self.assertEqual(options["method"], "GET")
            if options["resource"] == "site":
                self.assertIsNone(credential)
                return {"site": {"url": binding["public_origin"]}}, {}
            self.assertEqual(options, {"method": "GET", "resource": "posts", "query": {"limit": 1, "formats": "html"}})
            self.assertEqual(credential.secret, self.data["secret"])
            return {"posts": []}, {}
        self.transport.side_effect = ghost
        self.assertEqual(self.advance().state, "completed")
        self.assertEqual(self.connection.capabilities["_cms_identity"]["principal_id"], "a" * 24)
        self.transport.assert_called(); self.spend.assert_called_once()

    def test_owner_initial_get_and_accept_routes_are_no_store_without_provider_io(self):
        from .website_views import WebsiteConnectionActionView
        request = SimpleNamespace(user=self.user, data=self.data)
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)):
            response = WebsiteConnectionActionView().post(request, "cms-connect-and-setup")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            request.query_params = {"client_request_id": self.data["client_request_id"]}
            recovered = WebsiteConnectionActionView().get(request, "cms-initial")
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered["Cache-Control"], "private, no-store")
            self.assertEqual(recovered.data["cmsInitial"], response.data["cmsInitial"])
        self.transport.assert_not_called(); self.spend.assert_called_once()

    def test_owner_revocation_during_packet_read_cannot_export_a_previously_built_destination(self):
        self.start(); actual = status.find; reads = []
        def changing(*args, **kwargs):
            if reads: self.user.is_active = False
            reads.append(True)
            return actual(*args, **kwargs)
        with patch.object(status, "find", side_effect=changing), self.assertRaises(WebsiteAuthorityError):
            status.packet(self.context, self.config, user=self.user, operation_id=str(self.root.pk), now=self.now)
        self.assertEqual(len(reads), 2); self.transport.assert_not_called()

    def test_expiry_sweep_erases_orphan_ciphertext_without_rollout_owner_or_parent(self):
        self.start(); self.saved.remove(self.job); self.user.is_active = False
        self.now += timedelta(days=1)
        with override_settings(ARTICLES_CMS_ONBOARDING_ENABLED=False): status.expire_due(now=self.now)
        grant = next(row for row in self.saved if row.action == authority.GRANT_ACTION)
        self.assertEqual((grant.state, grant.payload["sealed_credential"]), ("cancelled", None))
        self.assertEqual(grant.receipt["status"], "orphan_proposal_expired")
        self.transport.assert_not_called()

    def test_initial_cleanup_wipes_all_private_proposals_without_changing_current_key_or_fabricating_proof(self):
        self.start(); controller.cancel_for_connection(self.connection)
        self.assertEqual(self.job.state, "cancelled")
        self.assertTrue(all(row.payload["sealed_credential"] is None for row in self.saved if row.action == authority.GRANT_ACTION))
        self.assertEqual(self.connection.state, "paused")
        self.assertEqual(next(row for row in self.saved if row.action == "channel-credential").state, "pending")
        self.transport.assert_not_called()

    def test_cancellation_erases_current_referenced_grant_even_if_its_stage_metadata_was_damaged(self):
        self.start()
        grant = next(row for row in self.saved if row.action == authority.GRANT_ACTION)
        grant.payload["grant"].pop("stage_operation_id")
        self.assertEqual(self.advance().state, "cancelled")
        self.assertIsNone(grant.payload["sealed_credential"])
        self.transport.assert_not_called()

    def test_failed_unclaimed_worker_cannot_cancel_a_concurrently_replaced_proposal(self):
        self.start(); newer = str(uuid4())
        def replace(*args, **kwargs):
            self.job.payload["grant_operation_id"] = newer
            raise authority._deny()
        with patch.object(authority, "scope", side_effect=replace): self.assertIsNone(self.advance())
        self.assertEqual((self.job.state, self.job.payload["grant_operation_id"]), ("pending", newer))
        self.assertEqual(self.root.state, "pending")
        self.transport.assert_not_called()
