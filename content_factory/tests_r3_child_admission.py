"""Separate scan/setup ownership before the real approval HTTP boundary."""
from copy import deepcopy
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from workflow_runs.models import ContentFactoryRun
from .run_child_admission import accept_scan_child_response, prepare_scan_child_dispatch
from .run_ownership import (BUDGET_KEY, CHILDREN_RESULT_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    RunOwnershipConflict, bind_admitted_run, owner_receipt, reserve_admitted_run, user_principal)
from .self_heal_testing import make_p2_org
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True)
class ScanChildAdmissionTests(TestCase):
    def setUp(self):
        fixture = make_p2_org()
        self.org, self.user, self.config = fixture.organization, fixture.owner, fixture.config
        self.actor = "mlai_user:" + str(self.user.pk)
        self.connection = WebsiteConnection.objects.create(organization=self.org, authorized_by=self.user,
            github_repo="synthetic/site", repository_id=42, installation_id="1001", branch="main", verified_sha="a" * 40)
        self.config.website_connection = self.connection
        self.config.github_repo = self.connection.github_repo
        self.config.save(update_fields=["website_connection", "github_repo"])
        request = {**contract_for(self.connection), "domain": self.org.domain, "github_repo": self.connection.github_repo,
                   "client_request_id": "parent-scan-key", "expected_source_sha": "a" * 40}
        self.parent = reserve_admitted_run(organization=self.org, principal=user_principal(self.user.pk, self.actor),
                                          workflow="scan", payload=request)
        self.parent = bind_admitted_run(self.parent, "parent-scan-worker")
        self.native = patch("content_factory.website_connections.verify_repository_native_target", return_value=None)
        self.native.start()
        self.addCleanup(self.native.stop)

    def prepare(self, payload=None, **kwargs):
        return prepare_scan_child_dispatch(run=self.parent, user=kwargs.get("user", self.user),
            actor_id=kwargs.get("actor_id", self.actor), payload=payload if payload is not None else {})

    def test_setup_child_and_durable_parent_receipt_exist_before_approval_http(self):
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        body = {}
        child = self.prepare(body)
        self.parent.refresh_from_db()
        dispatch = body["child_dispatch_v1"]
        self.assertEqual(child.workflow, "scaffold_articles")
        self.assertEqual(child.run_request["source_run_id"], self.parent.run_id)
        self.assertEqual(dispatch[WIRE_REQUEST_KEY], child.run_request[WIRE_REQUEST_KEY])
        self.assertEqual(dispatch[WIRE_EXECUTION_KEY], child.run_request[WIRE_EXECUTION_KEY])
        self.assertEqual(body[WIRE_REQUEST_KEY], self.parent.run_request[WIRE_REQUEST_KEY])
        self.assertNotEqual(dispatch[WIRE_REQUEST_KEY], body[WIRE_REQUEST_KEY])
        self.assertEqual(self.parent.result[CHILDREN_RESULT_KEY]["dispatches"][dispatch["client_request_id"]], dispatch)
        operation = WebsiteConnectionOperation.objects.get(pk=child.run_request["operation_id"])
        self.assertEqual(operation.payload["run_id"], child.run_id)
        after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        self.assertEqual({key: value for key, value in after.items() if key != "result"},
                         {key: value for key, value in before.items() if key != "result"})

    def test_same_parent_approval_replays_one_child_operation_key_and_exact_receipt(self):
        first_body, second_body = {}, {}
        first = self.prepare(first_body)
        second = self.prepare(second_body)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first_body, second_body)
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 1)

    @override_settings(ARTICLES_RUN_CONTROL_MODEL_USD="5.00", ARTICLES_RUN_CONTROL_MODEL_TOKENS=50000,
                       ARTICLES_RUN_SDK_CONTROL_ENABLED=True)
    def test_exact_server_sdk_budget_is_inside_original_digest_and_child_transport(self):
        from .website_contract import evidence_digest
        body = {}
        child = self.prepare(body)
        cap = {"schema_version": 1, "runtime": "maintained_sdk", "max_model_usd": "5.00", "max_model_tokens": 50000}
        self.assertEqual(body["child_dispatch_v1"][BUDGET_KEY], cap)
        request = {key: value for key, value in child.run_request.items() if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}
        self.assertEqual(owner_receipt(child)["identity"]["request_digest"], evidence_digest(request))

    def test_forged_child_identity_foreign_requester_and_revoked_scope_send_no_http_or_child(self):
        outsider = get_user_model().objects.create_user(email="foreign-child@example.test")
        for payload, kwargs in (({"child_dispatch_v1": {}}, {}), ({}, {"user": outsider, "actor_id": "mlai_user:" + str(outsider.pk)})):
            with self.subTest(kwargs=kwargs), self.assertRaises(RunOwnershipConflict):
                self.prepare(payload, **kwargs)
        WebsiteConnection.objects.filter(pk=self.connection.pk).update(state="revoked", generation=2)
        with self.assertRaises(WebsiteAuthorityError):
            self.prepare()
        self.assertEqual(ContentFactoryRun.objects.count(), 1)
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 0)

    def test_parent_response_id_cannot_be_used_as_child_and_unknown_ack_keeps_original_reservation(self):
        child = self.prepare()
        with patch("content_factory.vibe_marketing_views._lookup_content_factory_dispatch_by_key", return_value=("unknown", {})):
            response = accept_scan_child_response(child, {"run_id": self.parent.run_id})
        self.assertTrue(response.json()["dispatch_pending_resolution"])
        self.assertEqual(ContentFactoryRun.objects.count(), 2)
        child.refresh_from_db()
        self.assertNotEqual(child.pk, self.parent.pk)
        self.assertEqual(child.run_id, owner_receipt(child)["identity"]["dispatch_key"])
        self.assertEqual(accept_scan_child_response(child, {"scaffold_job_id": "actual-setup-child"}).pk, child.pk)

    def test_legacy_parent_retains_original_protocol_and_no_automatic_enrollment(self):
        legacy = ContentFactoryRun.objects.create(run_id="legacy-scan", domain=self.org.domain, workflow="scan")
        body = {"retained": "legacy"}
        self.assertIsNone(prepare_scan_child_dispatch(run=legacy, user=self.user, actor_id=self.actor, payload=body))
        self.assertEqual(body, {"retained": "legacy"})
        legacy.refresh_from_db()
        self.assertIsNone(owner_receipt(legacy))
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 0)

    def assert_child_at_http(self, payload):
        dispatch = payload["child_dispatch_v1"]
        child = ContentFactoryRun.objects.get(result__run_owner_v1__identity__dispatch_key=dispatch["client_request_id"])
        self.assertEqual(child.run_request[WIRE_REQUEST_KEY], dispatch[WIRE_REQUEST_KEY])
        self.assertEqual(child.run_request[WIRE_EXECUTION_KEY], dispatch[WIRE_EXECUTION_KEY])
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.result[CHILDREN_RESULT_KEY]["dispatches"][dispatch["client_request_id"]], dispatch)
        self.assertEqual(child.organization_id, self.org.pk)
        return child

    def response(self):
        response = Mock(status_code=202, content=b"{}")
        response.json.return_value = {"run_id": self.parent.run_id, "scaffold_job_id": "actual-child-worker"}
        return response

    def test_actual_github_approval_has_child_before_post_and_binds_only_child_ack(self):
        from integrations.services.github import decide_scan_scaffold
        seen = []

        def post(url, *, json, **kwargs):
            self.assertTrue(url.endswith(f"/api/runs/{self.parent.run_id}/approve"))
            seen.append(self.assert_child_at_http(json).pk)
            return self.response()

        with patch("integrations.services.github.http_requests.post", side_effect=post):
            result = decide_scan_scaffold(scan_run_id=self.parent.run_id, decision="approve",
                domain=self.org.domain, slack_user_id=self.actor)
        self.assertEqual(result["status_code"], 202)
        self.assertEqual(ContentFactoryRun.objects.get(run_id="actual-child-worker").pk, seen[0])
        self.assertTrue(ContentFactoryRun.objects.filter(pk=self.parent.pk, run_id=self.parent.run_id).exists())

    def test_actual_web_action_has_child_before_post(self):
        from . import vibe_marketing_views as views
        seen = []

        def post(url, *, json, **kwargs):
            seen.append(self.assert_child_at_http(json).pk)
            return self.response()

        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://worker.invalid"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views.http_client, "post", side_effect=post):
            result = views._call_content_factory_run_action(run_id=self.parent.run_id, action="approve",
                workflow=self.parent.workflow, payload={"slack_user_id": self.actor})
        self.assertEqual(result["scaffold_job_id"], "actual-child-worker")
        self.assertEqual(ContentFactoryRun.objects.get(run_id="actual-child-worker").pk, seen[0])

    def test_actual_authenticated_app_action_has_child_before_post(self):
        from rest_framework.test import APIRequestFactory, force_authenticate
        from integrations import api_views_content_factory_app as views
        seen = []

        def remote(method, path, *, payload):
            self.assertEqual((method, path), ("post", f"/api/runs/{self.parent.run_id}/approve"))
            seen.append(self.assert_child_at_http(payload).pk)
            return self.response()

        request = APIRequestFactory().post("/owned/approve", {}, format="json")
        force_authenticate(request, user=self.user)
        with patch.object(views, "_content_factory_request", side_effect=remote):
            response = views.ContentFactoryAppRunControlView.as_view()(request, run_id=self.parent.run_id, action="approve")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ContentFactoryRun.objects.get(run_id="actual-child-worker").pk, seen[0])
