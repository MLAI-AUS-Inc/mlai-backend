"""Portable drafts keep editorial progress without gaining repository authority."""
from unittest.mock import patch
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun
from .models import OrganizationContentConfig, ContentFactoryJob
from .service_views import ContentFactoryCallbackView, ContentFactoryRunView, ContentFactoryOrgConfigView
from .website_connections import portable_run_update_allowed


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
