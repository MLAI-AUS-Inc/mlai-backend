"""New setup children may use a real unmarked scan without enrolling its parent."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TransactionTestCase, override_settings
from workflow_runs.models import ContentFactoryRun
from .run_child_admission import prepare_scan_child_dispatch
from .run_ownership import (CHILDREN_RESULT_KEY, WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY,
    RunOwnershipConflict, owner_receipt, current_execution_ticket)
from . import tests_r3_child_admission as scan_fixture
from .tests_run_legacy_source import DESCRIPTOR, resigned
from .website_models import WebsiteConnectionOperation


@override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=True, ARTICLES_RUN_FACT_AUTHORITY_ENABLED=True,
                   ARTICLES_RUN_EVENT_AUTHORITY_ENABLED=True, CONTENT_FACTORY_URL="https://cf.fixture.invalid",
                   CONTENT_FACTORY_API_KEY="synthetic-source-key")
class LegacyScanChildTests(TransactionTestCase):
    def setUp(self):
        scan_fixture.ScanChildAdmissionTests.setUp(self)
        self.parent = ContentFactoryRun.objects.create(organization=self.org, domain=self.org.domain,
            github_repo=self.connection.github_repo, workflow="scan", run_id="actual-legacy-scan",
            status="awaiting_approval", slack_user_id=self.actor, run_request={key: value for key, value in self.parent.run_request.items()
                if key not in {WIRE_REQUEST_KEY, WIRE_EXECUTION_KEY}}, result={"scaffold_plan": {"retained": True}})
        self.descriptor = resigned({**DESCRIPTOR, "source_run_id": self.parent.run_id, "domain": self.parent.domain,
            "github_repo": self.parent.github_repo,
            "request_sha256": hashlib.sha256(json.dumps(self.parent.run_request, sort_keys=True,
                separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()})

    def remote(self):
        response = Mock(status_code=200, headers={})
        response.iter_content.return_value = [json.dumps(self.descriptor).encode()]
        return response

    def prepare(self, payload=None):
        return prepare_scan_child_dispatch(run=self.parent, user=self.user, actor_id=self.actor,
            payload={} if payload is None else payload)

    def test_actual_descriptor_admits_child_before_post_and_preserves_every_parent_clock_and_original_byte(self):
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        payload = {}
        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=self.remote()) as get:
            child = self.prepare(payload)
        self.assertTrue(get.call_args.args[0].endswith("/api/runs/actual-legacy-scan/continuation-source"))
        self.assertTrue(get.call_args.kwargs["stream"])
        self.assertNotIn(WIRE_REQUEST_KEY, payload)
        self.assertNotIn(WIRE_EXECUTION_KEY, payload)
        dispatch = payload["child_dispatch_v1"]
        self.assertEqual(dispatch["legacy_source_v1"], self.descriptor)
        self.assertEqual(child.run_request["legacy_source_v1"], self.descriptor)
        self.assertEqual(dispatch[WIRE_REQUEST_KEY], child.run_request[WIRE_REQUEST_KEY])
        self.assertEqual(dispatch[WIRE_EXECUTION_KEY], current_execution_ticket(child))
        self.assertIsNotNone(owner_receipt(child))
        self.parent.refresh_from_db()
        self.assertIsNone(owner_receipt(self.parent))
        after = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        self.assertEqual({key: value for key, value in after.items() if key != "result"},
                         {key: value for key, value in before.items() if key != "result"})
        self.assertEqual(after["result"]["scaffold_plan"], before["result"]["scaffold_plan"])
        if os.environ.get("R3_CAPTURE_LEGACY_SCAN") == "1":
            Path("/Users/samdonegan/Documents/Code/founder-journey-worktrees/recovery/R3_BE_LEGACY_SCAN_CHILD.fixture.json").write_text(json.dumps({
                "synthetic": True, "producer": "actual PostgreSQL backend scan continuation route",
                "parent_request": self.parent.run_request, "legacy_source_v1": self.descriptor,
                "approval_payload": payload, "child_request": child.run_request}, indent=2) + "\n")

    def test_existing_child_replays_original_descriptor_after_switch_off_without_any_new_source_read(self):
        original, repeated = {}, {}
        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=self.remote()):
            child = self.prepare(original)
        self.parent.refresh_from_db()
        operation = WebsiteConnectionOperation.objects.get(pk=child.run_request["operation_id"])
        WebsiteConnectionOperation.objects.filter(pk=operation.pk).update(state="completed", receipt={"actual_proof": "retained"})
        ContentFactoryRun.objects.filter(pk=child.pk).update(status="completed")
        before = WebsiteConnectionOperation.objects.values().get(pk=operation.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False), \
                patch("content_factory.vibe_marketing_views.http_client.get") as get:
            self.assertEqual(self.prepare(repeated).pk, child.pk)
        get.assert_not_called()
        self.assertEqual(original, repeated)
        self.assertEqual(WebsiteConnectionOperation.objects.values().get(pk=operation.pk), before)

    def test_on_missing_parent_identity_or_descriptor_fails_without_unowned_child_or_operation(self):
        for field in ("organization_id", "github_repo"):
            legacy = deepcopy(self.parent)
            setattr(legacy, field, None if field == "organization_id" else "")
            with self.assertRaises(RunOwnershipConflict):
                prepare_scan_child_dispatch(run=legacy, user=self.user, actor_id=self.actor, payload={})
        bad = self.remote()
        bad.status_code = 404
        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=bad), self.assertRaises(RunOwnershipConflict):
            self.prepare()
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 0)
        self.parent.refresh_from_db()
        self.assertIsNone(owner_receipt(self.parent))

    def test_off_legacy_protocol_is_exact_and_does_not_read_or_create_any_new_authority(self):
        payload = {"original": "legacy"}
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        with override_settings(ARTICLES_RUN_OWNERSHIP_ENABLED=False), \
                patch("content_factory.vibe_marketing_views.http_client.get") as get:
            self.assertIsNone(self.prepare(payload))
        get.assert_not_called()
        self.assertEqual(payload, {"original": "legacy"})
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.parent.pk), before)
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 0)

    def test_legacy_source_head_change_requires_fresh_scan_without_restamping_its_descriptor(self):
        from .website_contract import WebsiteAuthorityError
        before = ContentFactoryRun.objects.values().get(pk=self.parent.pk)
        self.source_response.json.return_value = {"sha": "b" * 40}
        with patch("content_factory.vibe_marketing_views.http_client.get", return_value=self.remote()), \
                self.assertRaises(WebsiteAuthorityError) as denied:
            self.prepare()
        self.assertEqual(denied.exception.code, "website_source_changed")
        self.assertEqual(WebsiteConnectionOperation.objects.count(), 0)
        self.assertEqual(ContentFactoryRun.objects.values().get(pk=self.parent.pk), before)

    def test_actual_web_scan_approval_sends_child_descriptor_with_no_parent_echoes(self):
        from . import vibe_marketing_views as views
        response = Mock(status_code=202, content=b"{}")
        response.json.return_value = {"run_id": self.parent.run_id, "scaffold_job_id": "legacy-web-setup-child"}
        with patch.object(views.http_client, "get", return_value=self.remote()), \
                patch.object(views.http_client, "post", return_value=response) as post:
            result = views._call_content_factory_run_action(run_id=self.parent.run_id, action="approve",
                workflow=self.parent.workflow, payload={"slack_user_id": self.actor})
        sent = post.call_args.kwargs["json"]
        self.assertNotIn(WIRE_REQUEST_KEY, sent)
        self.assertNotIn(WIRE_EXECUTION_KEY, sent)
        self.assertEqual(sent["child_dispatch_v1"]["legacy_source_v1"], self.descriptor)
        child = ContentFactoryRun.objects.get(run_id=result["scaffold_job_id"])
        self.assertEqual(child.run_request["legacy_source_v1"], self.descriptor)
        self.parent.refresh_from_db()
        self.assertIsNone(owner_receipt(self.parent))

    def test_actual_authenticated_app_scan_approval_sends_the_separate_owned_child(self):
        from integrations import api_views_content_factory_app as app_views
        from . import vibe_marketing_views as views
        from rest_framework.test import APIRequestFactory, force_authenticate
        response = Mock(status_code=202, content=b"{}")
        response.json.return_value = {"run_id": self.parent.run_id, "scaffold_job_id": "legacy-app-setup-child"}
        request = APIRequestFactory().post("/approve", {}, format="json")
        force_authenticate(request, user=self.user)
        with patch.object(views.http_client, "get", return_value=self.remote()), \
                patch.object(app_views, "_content_factory_request", return_value=response) as post:
            result = app_views.ContentFactoryAppRunControlView.as_view()(request, run_id=self.parent.run_id, action="approve")
        self.assertEqual(result.status_code, 200, result.data)
        sent = post.call_args.kwargs["payload"]
        self.assertNotIn(WIRE_REQUEST_KEY, sent)
        self.assertEqual(sent["child_dispatch_v1"]["legacy_source_v1"], self.descriptor)
        self.assertIsNotNone(owner_receipt(ContentFactoryRun.objects.get(run_id="legacy-app-setup-child")))
