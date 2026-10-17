"""Current-owner withdrawal uses real journals and synthetic provider boundaries."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from django.test import SimpleTestCase, override_settings

from . import cms_unpublish_authority as authority
from . import cms_unpublish_controller as controller
from . import cms_unpublish_start as starts
from . import cms_post_contract as posts
from . import tests_cms_capture_unit as fixtures
from .cms_api_transport import CmsApiError
from .cms_connection_contract import seal_credential
from .tests_cms_post_contract_unit import intent, issued, response
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation


@override_settings(**{**fixtures.CmsCaptureTests._overridden_settings, "ARTICLES_CMS_UNPUBLISH_ENABLED": True})
class CmsUnpublishRuntimeTests(SimpleTestCase):
    platform = "wordpress"

    def setUp(self):
        fixtures.CmsCaptureTests.setUp(self)
        self.depth = 0
        @contextmanager
        def atomic(*_args, **_kwargs):
            self.depth += 1
            try: yield
            finally: self.depth -= 1
        self.connection.refresh_from_db = Mock()
        self.binding["platform"] = self.platform
        if self.platform == "ghost":
            self.private.payload = {"schema_version": 1, "binding": self.binding,
                "sealed_credential": seal_credential(self.binding, secret="a" * 24 + ":" + "b" * 64)}
        self.intent = intent(self.platform, binding=self.binding)
        sent, _effect = issued(self.intent)
        ack = self.native()
        confirmed = posts.observe_posts(self.intent, sent, ack, lookup=False)
        self.publication = WebsiteConnectionOperation(id=self.intent["operation_id"], connection=self.connection,
            generation=self.connection.generation, action="cms-publish", state="completed", idempotency_key="original-publication",
            payload={"schema_version": 1, "binding": self.binding, "credential_operation_id": str(self.private.pk),
                "approval_operation_id": str(uuid4()), "staged_revision": self.intent["artifact_sha256"],
                "billing_digest": "d" * 64, "intent": self.intent, "post_state": confirmed, "public_observation": None, "failures": 0},
            receipt={"status": "article_live", "publishing_verified": True})
        self.publication.save, self.publication.refresh_from_db = Mock(), Mock()
        self.saved_rows.append(self.publication)
        self.data.update(publication_operation_id=str(self.publication.pk), publication_intent_digest=self.intent["digest"],
            public_url=confirmed["public_url"], confirmed=True)
        self.native_status = "publish" if self.platform == "wordpress" else "published"
        for item in (patch("django.db.transaction.atomic", side_effect=atomic),
                patch("django.db.transaction.get_connection", side_effect=lambda: SimpleNamespace(in_atomic_block=self.depth > 0)),
                patch("django.utils.timezone.now", side_effect=lambda: self.now)):
            item.start(); self.addCleanup(item.stop)
        self.http = patch.object(controller, "request_json", side_effect=self.request)
        self.provider = self.http.start(); self.addCleanup(self.http.stop)
        self.public_patch = patch.object(controller.public, "fetch_absence", side_effect=self.public)
        self.public_get = self.public_patch.start(); self.addCleanup(self.public_patch.stop)
        self.unlocked.side_effect = lambda: self.assertEqual(self.depth, 0)

    def native(self):
        row = response(self.intent, status=getattr(self, "native_status", "publish" if self.platform == "wordpress" else "published"))
        if self.platform == "ghost": row["updated_at"] = "2026-10-18T01:00:00.000Z"
        return row if self.platform == "wordpress" else {"posts": [row]}

    def request(self, binding, credential, **effect):
        self.assertEqual(self.depth, 0)
        self.assertEqual(binding, self.binding)
        self.assertEqual(effect["identifier"], self.publication.payload["post_state"]["provider_id"])
        self.assertIsNotNone(credential)
        if effect["method"] != "GET":
            self.assertEqual(self.job.payload["journal"]["phase"], "issued")
            self.assertEqual(self.job.payload["journal"]["issue_count"], 1)
            expected = {"status": "draft"} if self.platform == "wordpress" else {
                "posts": [{"status": "draft", "updated_at": "2026-10-18T01:00:00.000Z"}]}
            self.assertEqual(effect["payload"], expected)
            self.native_status = "draft"
        return self.native(), {}

    def public(self, intent, journal, **_kwargs):
        self.assertEqual(self.depth, 0)
        self.assertEqual(journal["phase"], "provider_draft")
        self.assertEqual(intent["public_url"], self.data["public_url"])
        return 404

    def accept(self, **changes):
        self.job = starts.start(self.context, self.config, user=self.user, data={**self.data, **changes}, now=self.now)
        return self.job

    def advance(self):
        self.now = max(self.now, self.job.next_attempt_at or self.now) + timedelta(seconds=1)
        return controller.advance(self.job.pk, now=self.now)

    def test_one_status_update_then_separate_provider_and_public_proofs_complete_original_request(self):
        original = deepcopy(self.publication.payload)
        self.accept(); self.provider.assert_not_called(); self.public_get.assert_not_called()
        self.advance()
        self.assertEqual(self.job.payload["journal"]["phase"], "provider_draft")
        self.assertIsNone(self.job.payload["public_observation"])
        self.assertEqual(self.job.state, "pending")
        self.public_get.assert_not_called()
        self.advance()
        self.assertEqual((self.job.state, self.job.receipt["status"]), ("completed", "article_withdrawn"))
        self.assertEqual([call.kwargs["method"] for call in self.provider.call_args_list],
            ["GET", "POST" if self.platform == "wordpress" else "PUT", "GET"])
        self.assertEqual(self.publication.payload, original)
        self.assertEqual(self.job.payload["public_reads"], 1)
        self.assertNotIn("lease_id", self.job.payload)

    def test_lost_write_reply_recovers_same_id_by_reads_without_another_status_write(self):
        self.accept()
        def request(*args, **kwargs):
            result = self.request(*args, **kwargs)
            if kwargs["method"] != "GET": raise CmsApiError("transient")
            return result
        self.provider.side_effect = request
        self.advance()
        self.assertEqual((self.job.payload["journal"]["phase"], self.job.payload["journal"]["issue_count"]), ("issued", 1))
        self.advance()
        self.assertEqual(self.job.state, "completed")
        self.assertEqual(sum(call.kwargs["method"] != "GET" for call in self.provider.call_args_list), 1)

    def test_unknown_write_still_published_exhausts_eight_observations_without_resending(self):
        self.accept()
        def unknown(binding, credential, **effect):
            if effect["method"] != "GET": raise CmsApiError("transient")
            return self.request(binding, credential, **effect)
        self.provider.side_effect = unknown
        for _ in range(10): self.advance()
        self.assertEqual((self.job.state, self.job.payload["journal"]["phase"]), ("blocked", "reconcile_exhausted"))
        self.assertEqual(self.job.payload["journal"]["probe_count"], 8)
        self.assertEqual(sum(call.kwargs["method"] != "GET" for call in self.provider.call_args_list), 1)
        self.public_get.assert_not_called()

    def test_already_owned_draft_requires_no_write_and_public_success_is_still_separate(self):
        self.native_status = "draft"
        self.accept(); self.advance()
        self.assertEqual(self.job.state, "completed")
        self.assertEqual(self.job.payload["journal"]["issue_count"], 0)
        self.assertEqual([call.kwargs["method"] for call in self.provider.call_args_list], ["GET"])
        self.public_get.assert_called_once()

    def test_draft_preview_url_is_never_followed_or_saved_as_the_original_public_url(self):
        self.native_status = "draft"
        original = self.native
        def preview():
            value = original()
            row = value if self.platform == "wordpress" else value["posts"][0]
            row["link" if self.platform == "wordpress" else "url"] = "https://fixture.test/p/private-preview/?token=synthetic-preview-secret"
            return value
        with patch.object(self, "native", side_effect=preview):
            self.accept(); self.advance()
        self.assertEqual(self.job.state, "completed")
        self.assertEqual(self.job.payload["intent"]["public_url"], self.data["public_url"])
        self.assertNotIn("synthetic-preview-secret", str(self.job.payload))
        self.assertEqual([call.kwargs["method"] for call in self.provider.call_args_list], ["GET"])

    def test_explicit_new_request_after_verified_completion_preserves_old_receipt_and_never_reuses_unknown_write(self):
        self.accept(); self.advance(); self.advance()
        previous, receipt = self.job, deepcopy(self.job.receipt)
        self.native_status = "publish" if self.platform == "wordpress" else "published"  # owner republished
        self.accept(client_request_id=str(uuid4()))
        self.assertNotEqual(self.job.pk, previous.pk)
        self.advance(); self.advance()
        self.assertEqual(self.job.state, "completed")
        self.assertEqual(previous.receipt, receipt)
        self.assertEqual(previous.payload["journal"]["issue_count"], 1)
        self.assertEqual(self.job.payload["journal"]["issue_count"], 1)

    def test_same_nonce_replays_original_without_new_deadline_and_another_nonce_cannot_reset_it(self):
        self.accept(); original = deepcopy(self.job.payload)
        self.now += timedelta(minutes=20)
        self.assertIs(self.accept(), self.job)
        self.assertEqual(self.job.payload, original)
        with self.assertRaises(WebsiteAuthorityError): self.accept(client_request_id=str(uuid4()))
        self.provider.assert_not_called()

    def test_original_active_source_is_fenced_without_touching_completed_or_unrelated_goals_or_finance(self):
        roots = []
        for state, source in (("running", self.intent["source_run_id"]), ("completed", self.intent["source_run_id"]), ("pending", "another-writing-run")):
            root = WebsiteConnectionOperation(connection=self.connection, generation=self.connection.generation, action="cms-goal",
                state=state, payload={"source_run_id": source, "lease_id": str(uuid4())},
                receipt={"first_article_billing": {"status": "reserved"}, "publication_achievement": {"past": True}})
            root.save = Mock(); self.saved_rows.append(root); roots.append(root)
        with patch("content_factory.cms_goal_billing.reserve") as spend, patch("content_factory.cms_goal_billing.release") as credit:
            self.accept()
        self.assertEqual([row.state for row in roots], ["cancelled", "completed", "pending"])
        self.assertEqual(roots[0].receipt["first_article_billing"], {"status": "reserved"})
        self.assertEqual(roots[1].receipt["publication_achievement"], {"past": True})
        self.assertNotIn("lease_id", roots[0].payload)
        spend.assert_not_called(); credit.assert_not_called()

    def test_owner_revocation_during_get_fences_status_write_and_result(self):
        self.accept()
        def revoked(*args, **kwargs):
            result = self.request(*args, **kwargs); self.user.is_active = False
            return result
        self.provider.side_effect = revoked
        self.advance()
        self.assertEqual(self.job.state, "blocked")
        self.assertEqual(self.job.payload["journal"]["issue_count"], 0)
        self.assertEqual([call.kwargs["method"] for call in self.provider.call_args_list], ["GET"])

    def test_changed_content_and_repeated_invalid_reply_cannot_issue_an_update(self):
        self.accept()
        self.provider.return_value = ({"foreign": "article"}, {})
        self.provider.side_effect = None
        self.advance(); self.advance()
        self.assertEqual(self.job.state, "blocked")
        self.assertEqual(self.job.payload["journal"]["issue_count"], 0)
        self.public_get.assert_not_called()

    def test_disabled_rollout_owner_mismatch_uncertain_create_or_changed_confirmation_are_refused(self):
        for changes in ({"confirmed": False}, {"publication_intent_digest": "0" * 64}, {"public_url": "https://fixture.test/foreign/"}):
            with self.subTest(changes=changes), self.assertRaises(WebsiteAuthorityError): self.accept(**changes)
        with override_settings(ARTICLES_CMS_UNPUBLISH_ENABLED=False), self.assertRaises(WebsiteAuthorityError): self.accept()
        self.user.is_active = False
        with self.assertRaises(WebsiteAuthorityError): self.accept()
        self.user.is_active = True
        self.publication.payload["post_state"]["phase"] = "create_issued"
        with self.assertRaises(WebsiteAuthorityError): self.accept()
        self.provider.assert_not_called()

    def test_owner_read_recovers_actual_nonce_without_provider_or_post_replay_after_deadline(self):
        from .cms_unpublish_status import packet
        self.assertIsNone(packet(self.context, self.config, user=self.user, request_id=str(uuid4()))["cmsWithdrawal"])
        offered = packet(self.context, self.config, user=self.user, publication_id=str(self.publication.pk))["cmsWithdrawal"]
        self.assertEqual(offered["state"], "available")
        self.assertNotIn("html", str(offered))
        self.accept(); self.now += timedelta(minutes=20)
        read = packet(self.context, self.config, user=self.user, request_id=self.data["client_request_id"])["cmsWithdrawal"]
        self.assertEqual((read["requestId"], read["operationId"]), (self.data["client_request_id"], str(self.job.pk)))
        self.provider.assert_not_called(); self.public_get.assert_not_called()

    def test_newer_lease_wins_a_late_status_reply_without_losing_issued_write(self):
        self.accept(); newer = str(uuid4())
        def request(*args, **kwargs):
            result = self.request(*args, **kwargs)
            if kwargs["method"] != "GET":
                self.job.payload["lease_id"] = newer
                self.job.next_attempt_at = self.now + timedelta(seconds=180)
            return result
        self.provider.side_effect = request
        self.advance()
        self.assertEqual((self.job.state, self.job.payload["lease_id"]), ("running", newer))
        self.assertEqual(self.job.payload["journal"]["phase"], "issued")
        self.provider.side_effect = self.request
        self.advance()
        self.assertEqual(self.job.state, "completed")
        self.assertEqual(sum(call.kwargs["method"] != "GET" for call in self.provider.call_args_list), 1)

    def test_two_lost_workers_stop_without_resetting_original_deadline_or_provider_journal(self):
        self.accept(); deadline = self.job.payload["deadline"]
        self.job.state = "running"
        self.job.payload["lease_id"] = str(uuid4())
        self.job.next_attempt_at = self.now
        with patch.object(controller, "_step", side_effect=RuntimeError("synthetic worker exit")):
            with self.assertRaises(RuntimeError): self.advance()
        self.advance()
        self.assertEqual((self.job.state, self.job.receipt["status"]), ("blocked", "withdrawal_worker_stopped"))
        self.assertEqual(self.job.payload["deadline"], deadline)
        self.assertNotIn("lease_id", self.job.payload)
        self.provider.assert_not_called()

    def test_deadline_and_disabled_execution_do_not_start_more_provider_or_public_calls(self):
        self.accept(); original = deepcopy(self.job.payload)
        with override_settings(ARTICLES_CMS_UNPUBLISH_ENABLED=False): self.assertIsNone(self.advance())
        self.assertEqual(self.job.payload, original)
        self.now += timedelta(minutes=16)
        self.advance()
        self.assertEqual((self.job.state, self.job.receipt["status"]), ("blocked", "withdrawal_deadline_reached"))
        self.provider.assert_not_called(); self.public_get.assert_not_called()

    def test_provider_draft_without_public_absence_keeps_waiting_with_fixed_budget(self):
        self.accept(); self.advance()
        self.public_get.side_effect = CmsApiError("public_article_unverified")
        for _ in range(10): self.advance()
        self.assertEqual(self.job.state, "blocked")
        self.assertEqual((self.job.payload["public_reads"], self.job.payload["journal"]["issue_count"]), (8, 1))
        self.assertIsNone(self.job.payload["public_observation"])
        self.assertNotIn("withdrawal_verified", self.job.receipt)

    def test_withdrawal_status_removes_live_link_but_preserves_completed_checklist(self):
        from .cms_unpublish_status import apply
        self.accept(); self.advance(); self.advance()
        root = SimpleNamespace(connection_id=self.connection.pk, generation=self.connection.generation,
            payload={"source_run_id": self.intent["source_run_id"]})
        result = {"state": "done", "publicUrl": self.data["public_url"], "milestones": [{"name": "first_article_live", "state": "passed"}]}
        apply(result, root, self.context, self.config, user=self.user)
        self.assertEqual((result["state"], result["title"], result["cmsWithdrawal"]["publicState"]), ("done", "Article unpublished", "withdrawn"))
        self.assertNotIn("publicUrl", result)
        self.assertEqual(result["milestones"], [{"name": "first_article_live", "state": "passed"}])

    def test_verified_withdrawal_cannot_complete_a_first_article_goal_that_never_finished(self):
        from .cms_unpublish_status import apply
        self.accept(); self.advance(); self.advance()
        root = SimpleNamespace(pk=uuid4(), connection_id=self.connection.pk, generation=self.connection.generation,
            payload={"source_run_id": self.intent["source_run_id"]})
        result = {"state": "stopped", "milestones": [{"name": "first_article_live", "state": "pending"}]}
        apply(result, root, self.context, self.config, user=self.user)
        self.assertEqual((result["state"], result["title"]), ("stopped", "Article unpublished"))
        self.assertEqual(result["milestones"], [{"name": "first_article_live", "state": "pending"}])

    def test_owner_api_is_no_store_and_generic_operation_read_cannot_export_private_journal(self):
        from .website_views import WebsiteConnectionActionView, WebsiteConnectionOperationView
        self.accept()
        request = SimpleNamespace(user=self.user, query_params={"client_request_id": self.data["client_request_id"]})
        with patch("content_factory.website_views._context", return_value=(self.context, self.config, None)):
            result = WebsiteConnectionActionView().get(request, "cms-unpublish")
            private = WebsiteConnectionOperationView().get(request, self.job.pk)
        self.assertEqual((result.status_code, result["Cache-Control"]), (200, "private, no-store"))
        self.assertEqual(result.data["cmsWithdrawal"]["requestId"], self.data["client_request_id"])
        self.assertEqual(private.status_code, 404)
        self.assertNotIn("synthetic-application-password", str(result.data))
        self.provider.assert_not_called()


class GhostUnpublishRuntimeTests(CmsUnpublishRuntimeTests):
    platform = "ghost"
