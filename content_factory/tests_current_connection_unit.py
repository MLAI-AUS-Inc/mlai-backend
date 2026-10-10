from types import SimpleNamespace
from django.test import SimpleTestCase
from .website_contract import WebsiteAuthorityError


class CurrentConnectionTests(SimpleTestCase):
    def fixture(self):
        original = SimpleNamespace(organization_id=1, repository_id=42, installation_id="install", github_repo="fixture/site")
        current = SimpleNamespace(**vars(original), state="connected")
        run = SimpleNamespace(organization_id=1, status="running", domain="example.test", github_repo="fixture/site")
        return run, current, original

    def test_same_organisation_repository_and_installation_can_rebind(self):
        from .website_current import validate_current_binding
        run, current, original = self.fixture()
        validate_current_binding(run, current, original, domain="example.test", repository_id=42)
        current.state = "paused"
        validate_current_binding(run, current, original, domain="example.test", repository_id=42)

    def test_scope_offboarding_and_changed_installation_return_gone(self):
        from .website_current import validate_current_binding
        for field, value in (("organization_id", 2), ("repository_id", 99), ("installation_id", "other"), ("state", "disconnected")):
            run, current, original = self.fixture()
            setattr(current, field, value)
            with self.subTest(field=field), self.assertRaises(WebsiteAuthorityError) as raised:
                validate_current_binding(run, current, original, domain="example.test", repository_id=42)
            self.assertEqual(raised.exception.status, 410)
        run, current, original = self.fixture()
        run.status = "cancelled"
        with self.assertRaises(WebsiteAuthorityError):
            validate_current_binding(run, current, original, domain="example.test", repository_id=42)


    def test_authorized_portable_recovery_has_no_repository_capability(self):
        from .website_current import portable_recovery_request
        from .portable_drafts import original_portable_run
        request = portable_recovery_request({"workflow": "article_generation", "topic": "Paid draft", "client_request_id": "paid-key",
            "website_connection_id": "website", "connection_generation": 2, "repository_id": 42,
            "operation_id": "operation", "github_token": "secret", "expected_source_sha": "a" * 40,
            "publish_targets": [{"id": "native"}], "github_repo": "fixture/site", "connection_contract": {"ready": True}})
        self.assertTrue(original_portable_run(SimpleNamespace(run_request=request)))
        self.assertEqual(request["topic"], "Paid draft")
        self.assertEqual(request["github_repo"], "")
        self.assertNotIn("github_token", request)
        self.assertNotIn("publish_targets", request)

    def test_portable_recovery_strips_nested_and_camel_case_repository_capabilities(self):
        from .website_current import portable_recovery_request
        request = portable_recovery_request({"topic": "Paid draft", "articleTemplate": {"secret": True},
            "cached": {"repoExecutionContract": {"build": "private"}, "publishResolution": "publish_code"}})
        self.assertEqual(request["topic"], "Paid draft")
        self.assertNotIn("articleTemplate", request)
        self.assertEqual(request["cached"], {"publishResolution": "content_only"})

    def test_portable_progress_keeps_unchanged_editorial_admission_after_historical_repo_is_cleared(self):
        from .tests_editorial_snapshot_unit import admitted_snapshot
        from .portable_drafts import portable_run_update_allowed
        from .website_current import portable_recovery_request
        original = admitted_snapshot()
        saved = portable_recovery_request(original["run_request"])
        run = SimpleNamespace(run_id="run", workflow="article_generation", domain="example.test",
                              github_repo="", run_request=saved)
        payload = {"run_id": "run", "workflow": "article_generation", "domain": "example.test", "github_repo": "",
                   "status": "running", "run_request": saved}
        self.assertTrue(portable_run_update_allowed(run, payload))
        from copy import deepcopy
        changed = deepcopy(payload)
        changed["run_request"]["editorial_admission"]["github_repo"] = "other/repo"
        self.assertFalse(portable_run_update_allowed(run, changed))


class CurrentConnectionRecoveryTests(SimpleTestCase):
    def setUp(self):
        from contextlib import nullcontext
        from unittest.mock import Mock, patch
        from uuid import uuid4
        from . import website_current as current
        self.current = current
        self.original_id = str(uuid4()); self.operation_id = str(uuid4())
        self.run = SimpleNamespace(run_id="run", organization_id=1, status="running", workflow="article_generation",
            domain="example.test", github_repo="fixture/site", result={}, save=Mock(),
            run_request={"website_connection_id": self.original_id, "connection_generation": 1, "repository_id": 42,
                         "operation_id": self.operation_id, "operation_attempt": 1, "deletion_epoch": 0, "topic": "Paid draft"})
        self.original = SimpleNamespace(pk=self.original_id, id=self.original_id, organization_id=1, repository_id=42,
            installation_id="install", github_repo="fixture/site", operations=Mock())
        self.original.operations.filter.return_value.exists.return_value = False
        self.website = SimpleNamespace(**{**vars(self.original), "generation": 2, "state": "connected", "configuration_version": 5})
        self.operation = SimpleNamespace(pk=self.operation_id, connection_id=self.original_id, generation=1, state="running", receipt={},
            payload={"run_id": "run", "attempt": 1, "installation_id": "install", "deletion_epoch": 0})
        rebound = SimpleNamespace(pk=str(uuid4()), payload={"attempt": 1})
        seams = [patch.object(current.transaction, "atomic", nullcontext),
            patch.object(current.ContentFactoryRun.objects, "filter", return_value=SimpleNamespace(first=lambda: self.run)),
            patch.object(current.ContentFactoryRun.objects, "select_for_update", return_value=SimpleNamespace(filter=lambda **kw: SimpleNamespace(first=lambda: self.run))),
            patch.object(current.Organization.objects, "select_for_update", return_value=Mock()),
            patch.object(current.OrganizationContentConfig.objects, "filter", return_value=SimpleNamespace(first=lambda: SimpleNamespace(website_connection_id=self.original_id))),
            patch.object(current.WebsiteConnection.objects, "filter", return_value=SimpleNamespace(first=lambda: self.original)),
            patch.object(current.WebsiteConnection.objects, "select_for_update", return_value=SimpleNamespace(filter=lambda **kw: SimpleNamespace(first=lambda: self.website))),
            patch.object(current.WebsiteConnectionOperation.objects, "select_for_update", return_value=SimpleNamespace(filter=lambda **kw: SimpleNamespace(first=lambda: self.operation))),
            patch.object(current.WebsiteConnectionOperation.objects, "get_or_create", return_value=(rebound, True)),
            patch("content_factory.website_connections.contract_for", side_effect=lambda w: {"website_connection_id": str(w.pk), "connection_generation": w.generation, "repository_id": w.repository_id}),
            patch("content_factory.website_operations.deletion_epoch", return_value=0)]
        for seam in seams:
            seam.start(); self.addCleanup(seam.stop)

    def get(self):
        return self.current.current_run_binding(run_id="run", domain="example.test", repository_id=42)

    def test_generation_is_rebound_from_backend_and_durable_run_is_updated(self):
        result = self.get()
        self.assertTrue(result["allowed"])
        self.assertEqual(result["connection_generation"], 2)
        self.assertEqual(self.run.run_request["operation_id"], result["operation_id"])
        self.assertEqual(len(self.run.result["connection_rebinds"]), 1)

    def test_changed_installation_cannot_hide_in_a_mutated_connection_object(self):
        self.website.installation_id = self.original.installation_id = "changed-install"
        result = self.get()
        self.assertEqual(result["http_status"], 410)
        self.assertTrue(result["portable_draft"])
        self.assertNotIn("operation_id", self.run.run_request)

    def test_missing_immutable_installation_proof_does_not_grant_rebind(self):
        self.operation.payload.pop("installation_id")
        result = self.get()
        self.assertEqual(result["http_status"], 410)
        self.assertTrue(result["portable_draft"])
        self.assertNotIn("operation_id", self.run.run_request)

    def test_cancelled_and_offboarded_runs_are_not_portable_or_rebound(self):
        self.run.status = "cancelled"
        with self.assertRaises(WebsiteAuthorityError) as denied:
            self.get()
        self.assertEqual(denied.exception.code, "run_cancelled")
        self.run.status = "running"
        self.original.operations.filter.return_value.exists.return_value = True
        with self.assertRaises(WebsiteAuthorityError):
            self.get()
        self.assertIn("operation_id", self.run.run_request)
