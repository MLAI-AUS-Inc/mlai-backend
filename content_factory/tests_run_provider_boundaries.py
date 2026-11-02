"""Actual provider facts and native admission preserve owned Run execution."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch
import json
from pathlib import Path

from django.db import transaction
from django.test import TestCase, TransactionTestCase, override_settings
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import User
from workflow_runs.models import ContentFactoryRun
from . import hosted_articles as hosted
from .run_ownership import (bind_admitted_run, reserve_admitted_run, user_principal,
    owner_receipt, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY, public_run_value, HOSTED_PUBLICATION_OBSERVATION_KEY)
from .tests_hosted_articles import article, DIGEST
from .tests_run_event_authority import FLAGS
from .tests_website_connections import WebsiteDatabaseFixture
from .website_contract import WebsiteAuthorityError
from .website_support import _project_legacy_verification, owner_support_operation


@override_settings(**FLAGS, ARTICLES_HOSTED_SITES_ENABLED=True)
class OwnedHostedProviderTests(TestCase):
    def setUp(self):
        from .tests_hosted_articles import HostedArticleTests
        HostedArticleTests.setUp(self)
        raw = {"domain": self.org.domain, "client_request_id": "owned-hosted-child",
            "hosted_site_id": str(self.site.pk), "delivery_mode": "content_only", "delivery_mode_confirmed": True}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org, workflow="direct_generate",
            principal=user_principal(self.user.pk, f"mlai_user:{self.user.pk}"), payload=raw), "owned-hosted-worker")
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result={**self.run.result,
            "article_approval_v1": {"source": "publish_reviewed", "approved_by_user_id": self.user.pk},
            "publish_reviewed_v1": {"source_run_id": "original-reviewed"}})
        self.run.refresh_from_db()
        hosted.request_publication(site=self.site, run=self.run, user=self.user, allow_continuation=True)
        self.before = ContentFactoryRun.objects.values().get(pk=self.run.pk)

    def assert_execution_retained(self):
        after = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        for key in ("status", "current_step", "approval_state", "run_request", "created_at", "updated_at",
                    "last_event_emitted_at", "reconciled_at", "error", "billing_status", "billing_ledger_id"):
            self.assertEqual(after[key], self.before[key], key)
        return after

    def progress(self, **data):
        return hosted.record_progress(domain=self.site.domain, run_id=self.run.run_id, data=data)

    def test_real_render_deploy_live_records_provider_facts_without_owned_execution_writes(self):
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False, ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=False):
            self.progress(status="rendering", article=article(), revision_digest=DIGEST)
            self.assert_execution_retained()
            self.progress(status="deployed", deployment={"worker_name": "mlai-blog-x",
                "url": "https://mlai-blog-x.mlai.workers.dev", "manifest_digest": "m" * 64})
            publication = self.progress(status="live", verified_digest=DIGEST)
        after = self.assert_execution_retained()
        self.assertEqual(publication.status, "live")
        self.assertEqual(after["result"][HOSTED_PUBLICATION_OBSERVATION_KEY]["status"], "live")
        self.assertIn("deploy_v1", after["result"])
        self.assertNotIn(HOSTED_PUBLICATION_OBSERVATION_KEY, public_run_value(after["result"]))

    def test_provider_failure_is_an_observation_not_a_competing_execution_failure(self):
        self.progress(status="rendering", article=article(), revision_digest=DIGEST)
        self.progress(status="failed", reason="Synthetic failed build")
        after = self.assert_execution_retained()
        self.assertEqual(after["result"][HOSTED_PUBLICATION_OBSERVATION_KEY]["status"], "failed")
        self.assertNotIn("error_code", after["result"])


@override_settings(**FLAGS)
class OwnedNativeVerificationTests(WebsiteDatabaseFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        from .website_models import WebsiteConnectionOperation
        self.website.verified_sha = "a" * 40
        self.website.state = "connected"
        self.website.save(update_fields=["verified_sha", "state"])
        operation = WebsiteConnectionOperation.objects.create(connection=self.website,
            generation=self.website.generation, action="workflow", state="pending", idempotency_key="native-parent",
            payload={"attempt": 1})
        raw = {**self.binding, "domain": self.org.domain, "github_repo": self.website.github_repo,
            "client_request_id": "native-parent", "expected_source_sha": "a" * 40,
            "operation_id": str(operation.pk), "operation_attempt": 1, "deletion_epoch": 0}
        self.run = bind_admitted_run(reserve_admitted_run(organization=self.org,
            principal=user_principal(123, "user:123"), workflow="article_system_setup", payload=raw), "native-parent-worker")
        operation.payload = {**operation.payload, "run_id": self.run.run_id}
        operation.save(update_fields=["payload"])
        self.user = User.objects.create_user(email="native-requester@synthetic.example.test")
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from founder_tools.services import get_founder_company_context
        profile = VibeRaisingProfile.objects.create(user=self.user, role="founder")
        company = VibeRaisingCompany.objects.create(profile=profile, organization=self.org,
            domain=self.org.domain, name="Native verification fixture")
        self.context = get_founder_company_context(self.user, company_id=company.pk)
        self.website.authorized_by = self.user
        self.website.save(update_fields=["authorized_by"])
        self.data = {**self.binding, "company_id": str(self.context.company.pk), "run_id": self.run.run_id, "phase": "verify", "idempotency_key": "native-one",
            "contract": {"schema_version": 1, "reviewed": True, "runtime_family": "static", "runtime_version": "1",
                "build_command": ["build"], "content_path_pattern": "content/{slug}.md",
                "route_template": "/articles/{slug}", "listing_route": "/articles"},
            "verification": {"source_sha": "a" * 40, "target_id": "custom", "seed_slug": "mlai-verification", "expected_h1": "MLAI verification article", "file_overrides": {}}}

    def test_actual_authenticated_route_admits_exact_native_child_before_post_and_replays_flags_off(self):
        from .website_views import WebsiteConnectionActionView
        parent = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        sent = []
        def send(url, **kwargs):
            wire = kwargs["json"]
            child = ContentFactoryRun.objects.get(run_id=wire["run_id"])
            self.assertIsNotNone(owner_receipt(child))
            self.assertEqual(owner_receipt(child)["identity"]["principal"]["user_id"], str(self.user.pk))
            self.assertEqual(child.workflow, "article_system_setup")
            self.assertEqual(child.run_request, wire)
            self.assertEqual(wire["source_run_id"], self.run.run_id)
            self.assertEqual(wire[WIRE_REQUEST_KEY]["dispatch_key"], "native-one")
            self.assertIn(WIRE_EXECUTION_KEY, wire)
            sent.append(deepcopy(wire))
            return SimpleNamespace(status_code=202, json=lambda: {"status": "queued"})
        def call():
            request = APIRequestFactory().post("/", self.data, format="json")
            force_authenticate(request, user=self.user)
            return WebsiteConnectionActionView.as_view()(request, action="custom-contract")
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                    return_value={"enabled": True, "base_url": "https://worker.test"}), \
                patch("integrations.http_client.post", side_effect=send) as dispatch:
            first = call()
            self.assertLess(first.status_code, 300, first.data)
            with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False):
                second = call()
            self.assertLess(second.status_code, 300, second.data)
        dispatch.assert_called_once()
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), parent)
        if __import__('os').environ.get('FOUNDER_EXPORT_NATIVE_FIXTURE'):
            Path(__import__('os').environ['FOUNDER_EXPORT_NATIVE_FIXTURE']).write_text(json.dumps({
                "schema_version": 1, "endpoint": "/api/connections/{id}/custom-contract",
                "workflow": "article_system_setup", "parent_request": self.run.run_request,
                "payload": sent[0]}, indent=2) + "\n")

    def test_native_proof_projection_keeps_owned_run_but_legacy_keeps_its_exact_projection(self):
        before = ContentFactoryRun.objects.values().get(pk=self.run.pk)
        legacy = ContentFactoryRun.objects.create(run_id="legacy-native", domain=self.org.domain, status="queued")
        with transaction.atomic():
            _project_legacy_verification(self.run.run_id, status="completed", outcome={"status": "verified"})
            _project_legacy_verification(legacy.run_id, status="completed", outcome={"status": "verified"})
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.run.pk), before)
        legacy.refresh_from_db()
        self.assertEqual((legacy.status, legacy.result), ("completed", {"status": "verified"}))

    def test_new_native_admission_cannot_infer_requester_from_source_payer_or_owner(self):
        with self.assertRaises(WebsiteAuthorityError), patch("integrations.http_client.post") as send:
            owner_support_operation(self.context, self.config, action="custom-contract", data=self.data)
        send.assert_not_called()

    def test_missing_seeded_article_inputs_refuse_before_any_child_or_http(self):
        before = ContentFactoryRun.objects.count()
        for field in ("seed_slug", "expected_h1"):
            data = deepcopy(self.data)
            data["verification"].pop(field)
            with self.subTest(field=field), patch("integrations.http_client.post") as send, \
                    self.assertRaises(WebsiteAuthorityError) as error:
                owner_support_operation(self.context, self.config, action="custom-contract", data=data, user=self.user)
            self.assertEqual(error.exception.status, 422)
            send.assert_not_called()
        self.assertEqual(ContentFactoryRun.objects.count(), before)

    def test_legacy_parent_stays_unmarked_while_new_native_child_retains_authenticated_descriptor(self):
        import hashlib, os
        from django.db import connection
        from .website_models import WebsiteConnectionOperation
        from .website_operations import reserve_workflow_operation
        key = "legacy-native-parent"
        raw = {**self.binding, "client_request_id": key, "expected_source_sha": "a" * 40}
        operation = reserve_workflow_operation(self.website, workflow="repo_scan", payload=raw)
        raw["run_id"] = key
        operation.payload = {**operation.payload, "run_id": key}
        operation.save(update_fields=["payload"])
        parent = ContentFactoryRun.objects.create(run_id=key, workflow="repo_scan", organization=self.org,
            github_repo=self.config.github_repo, domain=self.org.domain, status="completed", run_request=raw)
        before = ContentFactoryRun.objects.values().get(pk=parent.pk)
        digest = lambda value: hashlib.sha256(json.dumps(value, sort_keys=True,
            separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        source_status = {"workflow": "repo_scan", "generation": 0, "result": {
            "requested_action": "setup_article_system", "result": {"scaffold_plan": {"retained": True}}}}
        descriptor = {"schema_version": 1, "source_run_id": key, "workflow": "repo_scan",
            "domain": self.org.domain, "github_repo": self.config.github_repo,
            "request_sha256": digest(raw),
            "content_sha256": "f86de969b6bab98a67aa29ac78c17aea8e591ebef6f7f29f4009706ee4676c39"}
        descriptor["descriptor_sha256"] = digest(descriptor)
        def read(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            response = Mock(status_code=200, headers={})
            response.iter_content.return_value = [json.dumps(descriptor).encode()]
            return response
        sent = []
        def dispatch(url, **kwargs):
            wire = kwargs["json"]
            self.assertEqual(wire["legacy_source_v1"], descriptor)
            self.assertNotEqual(wire[WIRE_REQUEST_KEY]["dispatch_key"], key)
            self.assertEqual(wire["source_run_id"], key)
            child = ContentFactoryRun.objects.get(run_id=wire["run_id"])
            self.assertIsNotNone(owner_receipt(child))
            self.assertEqual(child.run_request, wire)
            sent.append(deepcopy(wire))
            return SimpleNamespace(status_code=202, json=lambda: {"status": "queued"})
        data = {**deepcopy(self.data), "run_id": key, "idempotency_key": "legacy-native-new-child"}
        with patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                return_value={"enabled": True, "base_url": "https://worker.test"}), \
                patch("integrations.http_client.get", side_effect=read) as get, \
                patch("integrations.http_client.post", side_effect=dispatch) as post:
            child = owner_support_operation(self.context, self.config, action="custom-contract", data=data, user=self.user)
        self.assertEqual(get.call_args.kwargs["allow_redirects"], False)
        post.assert_called_once()
        self.assertIsNotNone(child)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=parent.pk), before)
        if os.environ.get("FOUNDER_EXPORT_LEGACY_NATIVE_FIXTURE"):
            Path(os.environ["FOUNDER_EXPORT_LEGACY_NATIVE_FIXTURE"]).write_text(json.dumps({
                "schema_version": 1, "source_request": raw, "source_status": source_status,
                "legacy_source_v1": descriptor, "payload": sent[0]}, indent=2) + "\n")
