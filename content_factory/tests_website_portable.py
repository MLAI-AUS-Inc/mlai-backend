"""Portable drafts keep editorial progress without gaining repository authority."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig, ContentFactoryJob
from .service_views import ContentFactoryCallbackView, ContentFactoryRunView, ContentFactoryOrgConfigView
from .website_connections import portable_run_update_allowed
from .tests_website_portable_wire import admitted_portable_snapshot


@override_settings(ROO_API_KEY="synthetic-test-key", INTERNAL_API_KEY="synthetic-test-key")
class PortableWebsiteDraftTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="portable.example.test", name="Portable")
        self.config = OrganizationContentConfig.objects.create(organization=self.org, company_context="Original")
        self.run = ContentFactoryRun.objects.create(run_id="portable-draft", organization=self.org,
            domain=self.org.domain, workflow="direct_generate", status="running",
            run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True})
        self.factory = APIRequestFactory()

    def request(self, method, payload):
        return getattr(self.factory, method)("/synthetic", payload, format="json", HTTP_X_API_KEY="synthetic-test-key")

    def test_portable_worker_snapshot_and_content_ready_callback_complete_without_website(self):
        payload = {"workflow": "direct_generate", "domain": self.org.domain, "status": "completed",
            "result": {"content_package": {"title": "Portable draft", "article_json": {"title": "Portable draft"}}}}
        response = ContentFactoryRunView.as_view()(self.request("put", payload), run_id=self.run.run_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.run_request["delivery_mode"], "content_only")
        self.assertEqual(self.run.status, "completed")
        with patch("content_factory.service_views.upsert_live_progress_card"):
            response = ContentFactoryCallbackView.as_view()(self.request("post", {
                "job_id": self.run.run_id, "domain": self.org.domain, "event_type": "content_ready", "slack_user_id": "",
                "content_package": payload["result"]["content_package"]}))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ContentFactoryJob.objects.get(job_id=self.run.run_id).status, "completed")
        self.config.refresh_from_db()
        self.assertIsNone(self.config.website_connection_id)
        self.assertEqual(self.config.publish_targets, [])

    def test_sender_cannot_claim_portable_or_upgrade_durable_portable_draft(self):
        for patch_payload in ({"event_type": "preview_ready"}, {"event_type": "article_complete"},
            {"event_type": "scan_complete"}, {"pr_url": "https://github.com/example/site/pull/1"},
            {"publish_targets": [{"target_id": "native"}]}, {"result": {"live_preview_url": "https://preview.invalid"}},
            {"run_request": {"delivery_mode": "publish_code"}}, {"publish_resolution": "repo_pr"}, {"resolved_delivery_mode": "publish_code"}, {"approval_state": "approved"}, {"run_request": {"connectionId": "fbc09c73-e449-4c43-88ea-385b249a7a20", "connectionGeneration": 1}}, {"resolvedDeliveryMode": "publish_code"}, {"domain": "other.example.test"}):
            payload = {"job_id": self.run.run_id, "domain": self.org.domain, "event_type": "content_ready", **patch_payload}
            with self.subTest(payload=patch_payload):
                response = ContentFactoryCallbackView.as_view()(self.request("post", payload))
                self.assertEqual(response.status_code, 409, response.data)
        self.run.run_request = {"delivery_mode": "review_draft"}
        self.run.save(update_fields=["run_request"])
        response = ContentFactoryRunView.as_view()(self.request("put", {
            "workflow": "direct_generate", "domain": self.org.domain, "status": "completed",
            "run_request": {"delivery_mode": "content_only"}}), run_id=self.run.run_id)
        self.assertEqual(response.status_code, 409)

    def test_portable_claim_never_bypasses_organization_configuration_guard(self):
        response = ContentFactoryOrgConfigView.as_view()(self.request("put", {
            "run_id": self.run.run_id, "domain": self.org.domain, "workflow": "direct_generate",
            "delivery_mode": "content_only", "article_template": "# Forged"}))
        self.assertEqual(response.status_code, 409)
        self.config.refresh_from_db()
        self.assertIsNone(self.config.article_template)

    def test_bound_run_cannot_use_portable_exception(self):
        self.run.run_request.update(website_connection_id="fbc09c73-e449-4c43-88ea-385b249a7a20", connection_generation=1)
        self.assertFalse(portable_run_update_allowed(self.run, {"status": "completed"}))

    def test_mirror_records_editorial_admission_then_preserves_it_without_publication_authority(self):
        payload = admitted_portable_snapshot()
        payload["domain"] = self.org.domain
        payload["run_request"]["editorial_admission"]["domain"] = self.org.domain
        self.run.run_request.update(roo_points_ledger_id="synthetic-charge", roo_points_cost=6)
        self.run.save(update_fields=["run_request"])
        for state in ("running", "completed"):
            response = ContentFactoryRunView.as_view()(self.request("put", {**payload, "status": state}), run_id=self.run.run_id)
            self.assertEqual(response.status_code, 200, response.data)
            self.run.refresh_from_db()
            self.assertEqual(self.run.status, state)
            self.assertEqual(self.run.run_request["editorial_admission"], payload["run_request"]["editorial_admission"])
            self.assertEqual(self.run.run_request["roo_points_ledger_id"], "synthetic-charge")
            self.assertEqual(self.run.run_request["roo_points_cost"], 6)
        response = ContentFactoryRunView.as_view()(self.request("put", {**payload, "approval_state": "approved"}), run_id=self.run.run_id)
        self.assertEqual(response.status_code, 409, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")
        self.config.refresh_from_db()
        self.assertIsNone(self.config.website_connection_id)
        self.assertEqual(self.config.publish_targets, [])

    def test_unconfirmed_saved_default_cannot_receive_portable_worker_updates(self):
        self.run.run_request.pop("delivery_mode_confirmed")
        self.run.save(update_fields=["run_request"])
        response = ContentFactoryRunView.as_view()(self.request("put", {
            "workflow": "direct_generate", "domain": self.org.domain, "status": "completed",
            "run_request": {"delivery_mode": "content_only", "delivery_mode_confirmed": True},
        }), run_id=self.run.run_id)
        self.assertEqual(response.status_code, 409, response.data)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "running")


@override_settings(ROO_API_KEY="synthetic-test-key", INTERNAL_API_KEY="synthetic-test-key")
class PortableDispatchAdmissionTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(domain="dispatch.example.test", name="Dispatch")
        self.config = OrganizationContentConfig.objects.create(organization=self.org)
        self.factory = APIRequestFactory()
        self.payload = {"domain": self.org.domain, "topic": "A reviewed topic", "target_keyword": "reviewed",
            "client_request_id": "draft-dispatch", "delivery_mode": "content_only", "delivery_mode_confirmed": True,
            "roo_points_cost": 6}
        self.context = SimpleNamespace(organization=self.org, profile=SimpleNamespace(user=None))

    def mirror(self, payload=None, status="running", event_type=""):
        request = self.factory.put("/synthetic", {
            "workflow": "confirmed_topic", "domain": self.org.domain, "status": status,
            "current_step": "research_topic", "error": "Retain worker diagnostics" if status == "failed" else "",
            "run_request": deepcopy(payload or self.payload),
            **({"event_type": event_type} if event_type else {}),
        }, format="json", HTTP_X_API_KEY="synthetic-test-key")
        return ContentFactoryRunView.as_view()(request, run_id="remote-draft")

    def queue(self, post):
        from . import vibe_marketing_views as views
        with patch.object(views, "founder_actor_id_for_user", return_value="fixture-actor"), \
             patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.invalid"}), \
             patch.object(views, "_content_factory_headers", return_value={}), \
             patch.object(views, "_lookup_content_factory_dispatch_by_key", return_value=("pending", {})), \
             patch.object(views, "http_client") as http:
            from requests import RequestException
            http.RequestException = RequestException
            http.post.side_effect = post
            run = views._queue_content_factory_run(endpoint="article", workflow="confirmed_topic",
                context=self.context, config=self.config, payload=deepcopy(self.payload))
        self.assertLessEqual(http.post.call_count, 2)
        return run

    def test_first_snapshot_precedes_queue_reply_and_keeps_one_original_row(self):
        for callback_status in ("running", "failed"):
            with self.subTest(status=callback_status):
                ContentFactoryRun.objects.all().delete()
                original_pk = []
                def post(*args, **kwargs):
                    self.assertFalse(connection.in_atomic_block)
                    original = ContentFactoryRun.objects.get(run_id="draft-dispatch")
                    original_pk.append(original.pk)
                    self.assertEqual(original.run_request["roo_points_cost"], 6)
                    response = self.mirror(kwargs["json"], status=callback_status)
                    self.assertEqual(response.status_code, 200, response.data)
                    return SimpleNamespace(status_code=202, content=b"fixture", json=lambda: {"run_id": "remote-draft", "status": "queued"})
                run = self.queue(post)
                self.assertEqual(run.pk, original_pk[0])
                self.assertEqual(run.run_id, "remote-draft")
                self.assertEqual(run.status, callback_status)
                self.assertEqual(run.current_step, "research_topic")
                self.assertEqual(run.error, "Retain worker diagnostics" if callback_status == "failed" else "")
                self.assertEqual(ContentFactoryRun.objects.count(), 1)
                self.assertNotIn("portable_dispatch_intent_reserved", run.run_request)
                self.assertNotIn("dispatch_pending_resolution", run.run_request)

    def test_lost_queue_replies_keep_callback_identity_without_ghost_or_refund(self):
        from requests import ReadTimeout
        def post(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            response = self.mirror(kwargs["json"])
            self.assertEqual(response.status_code, 200, response.data)
            raise ReadTimeout("synthetic lost queue response")
        run = self.queue(post)
        self.assertEqual(run.run_id, "remote-draft")
        self.assertEqual(run.status, "running")
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertNotIn("dispatch_pending_resolution", run.run_request)
        self.assertNotIn("pending_billing_refund", run.run_request)

    def test_late_first_mirror_binds_pending_placeholder_and_billing_identity(self):
        from requests import ReadTimeout
        def post(*args, **kwargs):
            raise ReadTimeout("synthetic lost queue response")
        run = self.queue(post)
        self.assertEqual(run.run_id, "draft-dispatch")
        self.assertEqual(run.status, "blocked")
        ContentFactoryJob.objects.create(job_id=run.run_id, domain=self.org.domain, client_request_id=run.run_id)
        response = self.mirror()
        self.assertEqual(response.status_code, 200, response.data)
        run.refresh_from_db()
        self.assertEqual(run.run_id, "remote-draft")
        self.assertEqual(run.status, "running")
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(ContentFactoryJob.objects.get().job_id, "remote-draft")

    def test_forged_first_snapshot_cannot_bind_unreserved_or_changed_intent(self):
        from .dispatch_binding import reserve_portable_dispatch_intent
        self.assertEqual(self.mirror().status_code, 409)
        run = reserve_portable_dispatch_intent(organization=self.org, workflow="confirmed_topic", actor_id="fixture-actor", payload=self.payload)
        for change in ({"topic": "Unreviewed"}, {"client_request_id": "wrong-key"},
                       {"delivery_mode": "publish_code"}, {"domain": "other.example.test"},
                       {"github_repo": "other/site"}):
            with self.subTest(change=change):
                response = self.mirror({**self.payload, **change})
                self.assertEqual(response.status_code, 409, response.data)
                run.refresh_from_db()
                self.assertEqual(run.run_id, "draft-dispatch")
                self.assertEqual(run.status, "queued")
                self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(self.mirror(event_type="preview_ready").status_code, 409)
        run.refresh_from_db()
        self.assertEqual(run.run_id, "draft-dispatch")

    def test_dispatch_binder_never_merges_another_tenants_remote_identity(self):
        from .dispatch_binding import bind_dispatch_token_run, reserve_portable_dispatch_intent
        reserved = reserve_portable_dispatch_intent(organization=self.org, workflow="confirmed_topic", actor_id="fixture-actor", payload=self.payload)
        other = Organization.objects.create(domain="other.example.test", name="Other")
        foreign = ContentFactoryRun.objects.create(run_id="remote-draft", organization=other, domain=other.domain,
            workflow="confirmed_topic", run_request={"delivery_mode": "content_only", "delivery_mode_confirmed": True})
        self.assertIsNone(bind_dispatch_token_run(client_request_id=reserved.run_id, remote_run_id=foreign.run_id))
        self.assertEqual(self.mirror().status_code, 409)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        reserved.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(reserved.run_id, "draft-dispatch")
        self.assertEqual(foreign.organization_id, other.pk)
