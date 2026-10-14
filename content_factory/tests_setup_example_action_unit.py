"""Keep CF's manual example cleanup scoped and visible through preparation.

The real-chain composition regression runs after the earlier Phase 1 frozen
context fix is present. On the independent main-based branch, direct handler
and owner retry tests run without that fix. No orchestration/context/projector
stubs are used in the composition regression; only DB/cache/HTTP seams.
"""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from inspect import signature
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone

from founder_tools.services import FounderCompanyContext
from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import website_prepare as prepare
from . import website_operations as operations
from . import vibe_marketing_views as views
from .models import OrganizationContentConfig
from .website_connections import contract_for
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnection, WebsiteConnectionOperation, WebsiteScanSnapshot


class SetupExampleCleanupActionTests(SimpleTestCase):
    @staticmethod
    def rows(*, first=None, items=(), exists=False):
        query = MagicMock()
        for method in ("filter", "exclude", "order_by", "select_related", "select_for_update", "prefetch_related", "values_list"):
            getattr(query, method).return_value = query
        query.first.return_value = first
        query.get.return_value = first
        query.exists.return_value = exists
        query.__iter__.side_effect = lambda: iter(items)
        query.__getitem__.return_value = list(items)
        query.update.return_value = 1
        return query

    def setUp(self):
        # B2 also reads reserved goal rows before allowing legacy redispatch.
        self.enterContext(patch.object(WebsiteConnectionOperation.objects, "filter", return_value=self.rows()))
        self.user = get_user_model()(pk=4, is_active=True, email="founder@example.test")
        self.org = Organization(pk=7, domain="fixture.test")
        self.website = WebsiteConnection(organization=self.org, authorized_by=self.user,
            github_repo="fixture/site", repository_id=123, installation_id="4", generation=3,
            configuration_version=2, branch="main", verified_sha="a" * 40)
        self.config = OrganizationContentConfig(organization=self.org, website_connection=self.website,
            github_repo=self.website.github_repo, article_system={"pending_article_system_setup": {
                "mode": "existing", "route_path": "/articles", "source_scan_run_id": "old-scan"}})
        self.op = WebsiteConnectionOperation(connection=self.website, generation=3, action="prepare", state="running",
            payload={"company_id": "9", "requested_by_user_id": "4", "attempt": 1, "run_id": "old-setup"},
            receipt={"current_step": "setup", "child_run_id": "old-setup", "scan_run_id": "old-scan", "setup_run_id": "old-setup"},
            updated_at=timezone.now())
        self.op.save = Mock()
        self.workflow = WebsiteConnectionOperation(connection=self.website, generation=3, action="workflow", state="blocked",
            payload={"run_id": "old-setup", "attempt": 1}, updated_at=timezone.now())
        self.action = {"id": "remove_setup_example", "file": "src/content/articles/registry.ts", "record": "guides/review-sample"}
        self.child = ContentFactoryRun(run_id="old-setup", workflow="article_system_setup", status="blocked",
            organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo,
            run_request={**contract_for(self.website), "operation_id": str(self.workflow.pk), "operation_attempt": 1, "deletion_epoch": 0},
            result={"status": "needs_user", "error_code": "SETUP_EXAMPLE_CLEANUP_REQUIRED", "user_action": self.action,
                "retryable": False, "resume_available": False}, error="Provider text is not the public cleanup instruction.")

    def scope(self):
        stack = ExitStack()
        stack.enter_context(patch.object(WebsiteConnection, "operations", self.rows(first=self.workflow)))
        stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", return_value=self.rows(first=self.child)))
        return stack

    def test_valid_cleanup_waits_for_owner_and_survives_public_summary(self):
        original_request = deepcopy(self.child.run_request)
        with self.scope():
            self.assertTrue(prepare._retain_setup_example_cleanup(self.op, self.child))
        summary = operations.operation_summary(self.op)
        self.assertEqual(self.op.state, "needs_user")
        self.assertIsNone(self.op.next_attempt_at)
        self.assertEqual(summary["receipt"]["status"], "needs_you")
        self.assertFalse(summary["receipt"]["retryable"])
        self.assertEqual(summary["receipt"]["user_action"]["file"], self.action["file"])
        self.assertEqual(summary["receipt"]["user_action"]["record"], self.action["record"])
        self.assertEqual(summary["receipt"]["user_action"]["label"], "Review example cleanup")
        self.assertEqual(summary["receipt"]["user_action"]["url"], "https://github.com/fixture/site/blob/main/src/content/articles/registry.ts")
        self.assertIn(self.action["file"], summary["receipt"]["message"])
        self.assertIn(self.action["record"], summary["receipt"]["message"])
        self.assertNotIn("Provider text", summary["receipt"]["message"])
        self.assertNotIn("retry automatically", summary["receipt"]["message"])
        self.assertEqual(self.child.run_request, original_request)

    def test_cf_needs_user_wire_status_is_blocked_at_both_read_boundaries(self):
        from workflow_runs.status import normalize_run_status
        for value in ("needs_user", " NEEDS_USER ", "preview_failed", "setup_pr_create_failed"):
            with self.subTest(value=value):
                self.assertEqual(normalize_run_status(value), "blocked")
                self.assertEqual(views._normalize_remote_run_status(value), "blocked")
        self.assertEqual(normalize_run_status("unknown-future-status"), "queued")
        self.assertEqual(views._normalize_remote_run_status("unknown-future-status"), "queued")

    def test_legacy_failed_cleanup_child_keeps_only_validated_owner_instruction(self):
        self.child.status = "failed"
        self.workflow.state = "failed"
        with self.scope():
            self.assertTrue(prepare._retain_setup_example_cleanup(self.op, self.child))
        self.assertEqual(self.op.state, "needs_user")
        self.child.result["error_code"] = "INVALID_ADAPTER"
        with self.scope():
            self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))

    def test_callback_nested_cleanup_is_retained_after_preview_failed_projection(self):
        self.child.result = {"status": "preview_failed", "error_code": "SETUP_EXAMPLE_CLEANUP_REQUIRED",
            "article_system_setup": {"status": "preview_failed", "error_code": "SETUP_EXAMPLE_CLEANUP_REQUIRED",
                "requested_action": self.action, "retryable": False}}
        with self.scope():
            self.assertTrue(prepare._retain_setup_example_cleanup(self.op, self.child))
        self.assertEqual(self.op.receipt["user_action"]["id"], "remove_setup_example")

    def test_malformed_or_unrelated_actions_retain_generic_blocked_handling(self):
        for changes in ({"file": "../outside.ts"}, {"file": "/absolute.ts"}, {"file": "src/.git/config"},
                {"file": "src\\registry.ts"}, {"file": "src/./registry.ts"}, {"file": "src/file\n.ts"},
                {"record": "../article"}, {"record": "<script>"}, {"record": "one/two/three"},
                {"id": "authorize_github"}, {"file": None}, {"record": None}):
            self.child.result["user_action"] = {**self.action, **changes}
            before = deepcopy(self.op.receipt)
            with self.subTest(changes=changes), self.scope():
                self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))
            self.assertEqual(self.op.receipt, before)
        self.child.result["user_action"] = self.action
        self.child.result["error_code"] = "INVALID_ADAPTER"
        with self.scope():
            self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))

    def test_cleanup_cannot_borrow_another_generation_or_operation_attempt(self):
        original = deepcopy(self.child.run_request)
        for changes in ({"connection_generation": 2}, {"repository_id": 456}, {"website_connection_id": str(uuid4())},
                {"operation_attempt": 2}, {"deletion_epoch": 1}, {"operation_id": "invalid"}):
            self.child.run_request = {**original, **changes}
            with self.subTest(changes=changes), self.scope():
                self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))
        self.child.run_request = original
        self.child.status = "cancelled"
        with self.scope():
            self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))

    def test_cleanup_requires_original_operation_and_tenant_binding(self):
        original = deepcopy(self.child.run_request)
        for key in ("operation_id", "operation_attempt", "deletion_epoch", "website_connection_id", "connection_generation", "repository_id"):
            self.child.run_request = {name: value for name, value in original.items() if name != key}
            with self.subTest(missing=key), self.scope():
                self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))
        self.child.run_request = original
        for field, value in (("organization_id", 8), ("github_repo", "other/site"), ("domain", "other.test"), ("workflow", "write_article")):
            old = getattr(self.child, field)
            setattr(self.child, field, value)
            with self.subTest(field=field), self.scope():
                self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))
            setattr(self.child, field, old)
        self.workflow.state = "cancelled"
        with self.scope():
            self.assertFalse(prepare._retain_setup_example_cleanup(self.op, self.child))

    def test_explicit_cleanup_retry_resets_only_old_work_and_requests_current_source(self):
        with self.scope():
            prepare._retain_setup_example_cleanup(self.op, self.child)
        with patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)), \
                patch.object(ContentFactoryRun.objects, "filter", return_value=self.rows(first=self.child)):
            self.assertIs(prepare.start_prepare(self.config, expected=contract_for(self.website), retry=True), self.op)
        self.assertEqual(self.op.state, "pending")
        self.assertEqual(self.op.payload["attempt"], 2)
        self.assertNotIn("run_id", self.op.payload)
        self.assertIsNone(self.op.receipt.get("child_run_id"))
        self.assertNotIn("scan_run_id", self.op.receipt)
        self.assertNotIn("setup_run_id", self.op.receipt)
        self.assertTrue(self.op.payload["manual_setup_example_cleanup_retry"])
        self.assertEqual(self.op.generation, 3)

    def test_other_needs_user_children_and_unrequested_cleanup_retry_stay_saved(self):
        for retry, action_id in ((False, "remove_setup_example"), (True, "merge_pr"), (True, "review_setup"),
                (True, "review_github_permissions"), (True, "authorize_github"), (True, "deploy_site"),
                (True, "choose_articles_page")):
            self.op.state = "needs_user"
            self.op.receipt.update(user_action={**self.action, "id": action_id}, reason_code="SETUP_EXAMPLE_CLEANUP_REQUIRED")
            with self.subTest(retry=retry, action=action_id), \
                    patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                    patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)):
                prepare.start_prepare(self.config, expected=contract_for(self.website), retry=retry)
            self.assertEqual(self.op.receipt["child_run_id"], "old-setup")
            self.assertEqual(self.op.payload["attempt"], 1)
            self.assertNotIn("retry_epoch", self.op.payload)
            self.assertNotIn("manual_setup_example_cleanup_retry", self.op.payload)

    def test_explicit_failed_work_does_not_inherit_owner_wait_from_its_step(self):
        for action_id, step, failure_class in (("retry", "verify", None), ("open_run", "merge", None),
                ("review_setup", "setup", "repairable")):
            self.setUp()
            self.op.state = "needs_user"
            self.op.receipt.update(user_action={"id": action_id}, current_step=step, failure_class=failure_class)
            with self.subTest(action=action_id), \
                    patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                    patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)), \
                    patch.object(ContentFactoryRun.objects, "filter", return_value=self.rows(first=self.child)):
                prepare.start_prepare(self.config, expected=contract_for(self.website), retry=True)
            self.assertIsNone(self.op.receipt["child_run_id"])
            self.assertEqual(self.op.payload["attempt"], 2)
            self.assertTrue(self.op.payload["retry_epoch"])

    def test_real_permission_failure_recheck_preserves_child_and_dispatch_epoch(self):
        self.op.payload["retry_epoch"] = "saved-epoch"
        grants = {"settingsUrl": "https://github.com/settings/installations/4"}
        with patch("content_factory.website_github_permissions.installation_grants", return_value=grants):
            prepare.prepare_failure(self.op, "setup", WebsiteAuthorityError(
                "github_workflow_permission_required", "Approve workflow-file access."))
        self.assertEqual(self.op.receipt["failure_class"], "needs_user")
        self.assertEqual(self.op.receipt["user_action"]["id"], "review_github_permissions")
        with patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)):
            prepare.start_prepare(self.config, expected=contract_for(self.website), retry=True)
        self.assertEqual(self.op.receipt["child_run_id"], "old-setup")
        self.assertEqual(self.op.payload["attempt"], 1)
        self.assertEqual(self.op.payload["retry_epoch"], "saved-epoch")
        self.assertEqual(self.op.receipt["dispatch_outcome"], "replayed")

    @skipUnless("user" in signature(prepare._owner_request).parameters, "Requires the earlier Phase 1.2 frozen-context fix")
    def test_real_chain_cleanup_and_owner_retry(self):
        """Real-chain marker: actual frozen context, journey and guarded dispatch."""
        self._real_chain_cleanup_and_owner_retry()

    @skipUnless("user" in signature(prepare._owner_request).parameters, "Requires the earlier Phase 1.2 frozen-context fix")
    def test_real_chain_owner_retry_of_saved_cleanup_reads_cleaned_source(self):
        """A saved owner action also leaves its terminal child behind on retry."""
        self._real_chain_cleanup_and_owner_retry(saved_cleanup=True)

    @skipUnless("user" in signature(prepare._owner_request).parameters, "Requires the earlier Phase 1.2 frozen-context fix")
    def test_real_wire_callback_replay_poll_reconciliation_and_legacy_failure_prepare_chain(self):
        """No planted terminal status: actual wire receivers establish the child."""
        for mode in ("callback", "callback_resume", "poll", "reconcile", "legacy_failure"):
            self.setUp()
            with self.subTest(mode=mode):
                self._real_chain_cleanup_and_owner_retry(wire_mode=mode)

    def _real_chain_cleanup_and_owner_retry(self, *, saved_cleanup=False, wire_mode=None):
        from integrations.models import GitHubInstallation
        from integrations.services import github_app
        context = FounderCompanyContext(profile=SimpleNamespace(user=self.user, companies=self.rows()),
            company=SimpleNamespace(pk=9, id=9, domain=self.org.domain), organization=self.org)
        self.op.state = "needs_user" if saved_cleanup else "pending"
        if saved_cleanup:
            self.op.receipt.update(status="needs_you", reason_code="SETUP_EXAMPLE_CLEANUP_REQUIRED", user_action=self.action)
        worker_run = SimpleNamespace(run_id="fresh-scan", status="queued", result={}, save=Mock())
        old_inventory = WebsiteScanSnapshot(connection=self.website, generation=3, run_id="old-scan", source_sha="a" * 40,
            detector_version="fixture", evidence={"scan_complete": True})
        repo = Mock(status_code=200)
        repo.json.return_value = {"full_name": "fixture/site", "default_branch": "main", "permissions": {"push": True}}
        commit = Mock(status_code=200)
        commit.json.return_value = {"sha": "b" * 40}
        with ExitStack() as stack:
            stack.enter_context(patch.object(get_user_model().objects, "get", return_value=self.user))
            stack.enter_context(patch.object(views, "get_founder_company_context", return_value=context))
            stack.enter_context(patch.object(views, "_get_config", return_value=self.config))
            stack.enter_context(patch.object(prepare.transaction, "atomic", side_effect=lambda *a, **kw: nullcontext()))
            leased = self.rows(first=self.op)
            leased.filter.side_effect = lambda *a, **kw: self.rows(first=self.workflow if kw.get("action") == "workflow" else self.op)
            self.workflow.save = Mock()
            stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "select_for_update", return_value=leased))
            stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "filter", return_value=self.rows()))
            stack.enter_context(patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)))
            for relation in ("targets", "repository_mutations"):
                stack.enter_context(patch.object(WebsiteConnection, relation, self.rows()))
            stack.enter_context(patch.object(WebsiteConnection, "scan_snapshots", self.rows(first=old_inventory)))
            stack.enter_context(patch.object(WebsiteConnection, "operations", self.rows(first=self.workflow)))
            stack.enter_context(patch.object(WebsiteConnection.objects, "select_related", return_value=self.rows(first=self.website)))
            stack.enter_context(patch.object(WebsiteConnection.objects, "select_for_update", return_value=self.rows(first=self.website)))
            stack.enter_context(patch.object(Organization.objects, "select_for_update", return_value=self.rows(first=self.org)))
            stack.enter_context(patch.object(OrganizationContentConfig.objects, "filter", return_value=self.rows(exists=True)))
            stack.enter_context(patch.object(ContentFactoryRun.objects, "filter", return_value=self.rows(first=self.child)))
            stack.enter_context(patch.object(GitHubInstallation.objects, "filter", return_value=self.rows()))
            stack.enter_context(patch.object(github_app, "probe_installation_liveness", return_value=github_app.INSTALLATION_LIVE))
            stack.enter_context(patch.object(github_app, "create_installation_access_token", return_value=SimpleNamespace(token="synthetic")))
            stack.enter_context(patch.object(github_app, "require_installation_repository_permissions", return_value={"contents": "write", "pull_requests": "write"}))
            stack.enter_context(patch.object(github_app, "_require_installation_profile_permissions", return_value={"workflows": "write", "checks": "read", "statuses": "read"}))
            provider = stack.enter_context(patch.object(views.http_client, "get", side_effect=[repo, commit]))
            stack.enter_context(patch.object(views.http_client, "delete"))
            stale_access = {"verified": True, "writable": True, "branch": "main", "sha": "a" * 40}
            stack.enter_context(patch.object(views.cache, "get", side_effect=lambda key:
                stale_access if key.startswith("articles-repository-access-v1:") else None))
            for method, result in (("set", None), ("add", True), ("delete", None)):
                stack.enter_context(patch.object(views.cache, method, return_value=result))
            queue = stack.enter_context(patch.object(views, "_queue_content_factory_run", return_value=worker_run))
            if wire_mode:
                from . import service_views, reconciliation
                from workflow_runs.models import ContentFactoryRunStep
                from .models import ContentFactoryCallbackEvent
                self.child.status, self.child.result, self.workflow.state = "queued", {}, "running"
                self.child.save, self.child.refresh_from_db = Mock(), Mock()
                self.child.resume_available = wire_mode == "callback_resume"
                self.child.run_request["expected_source_sha"] = self.website.verified_sha
                self.config.save = Mock()
                self.website.save = Mock()
                stack.enter_context(patch.object(OrganizationContentConfig.objects, "get", return_value=self.config))
                stack.enter_context(patch.object(Organization, "content_config", self.config))
                stack.enter_context(patch.object(Organization.objects, "filter", return_value=self.rows(first=self.org)))
                stack.enter_context(patch.object(ContentFactoryRun, "steps", self.rows()))
                stack.enter_context(patch.object(ContentFactoryRun.objects, "select_for_update", return_value=self.rows(first=self.child)))
                stack.enter_context(patch.object(ContentFactoryRun.objects, "get_or_create", return_value=(self.child, False)))
                def save_snapshot(*, defaults, **kwargs):
                    for key, value in defaults.items():
                        setattr(self.child, key, value)
                    return self.child, False
                stack.enter_context(patch.object(ContentFactoryRun.objects, "update_or_create", side_effect=save_snapshot))
                stack.enter_context(patch.object(ContentFactoryRunStep.objects, "update_or_create", return_value=(SimpleNamespace(), False)))
                events = {}
                def event_row(*, event_id, defaults):
                    created = event_id not in events
                    if created:
                        events[event_id] = ContentFactoryCallbackEvent(event_id=event_id, **defaults)
                    return events[event_id], created
                def event_rows(**filters):
                    event = events.get(filters["event_id"])
                    rows = self.rows(first={"claimed_at": event.claimed_at, "processed_at": event.processed_at} if event else None)
                    rows.values.return_value = rows
                    def update_event(**values):
                        if not event or "claimed_at__lt" in filters:
                            return 0
                        for key, value in values.items():
                            setattr(event, key, value)
                        return 1
                    rows.update.side_effect = update_event
                    return rows
                stack.enter_context(patch.object(ContentFactoryCallbackEvent.objects, "get_or_create", side_effect=event_row))
                stack.enter_context(patch.object(ContentFactoryCallbackEvent.objects, "filter", side_effect=event_rows))
                stack.enter_context(patch.object(prepare.transaction, "on_commit"))
                setup = {"status": "needs_user", "error_code": "SETUP_EXAMPLE_CLEANUP_REQUIRED", "user_action": self.action,
                    "requested_action": self.action, "next_action": "remove_setup_example", "retryable": False,
                    "retry_available": False, "resume_available": False}
                wire = {**self.child.run_request, "run_id": self.child.run_id, "job_id": self.child.run_id,
                    "workflow": "article_system_setup", "event_type": "article_system_setup_preview_failed", "status": "needs_user",
                    "error": "Remove setup example record guides/review-sample from src/content/articles/registry.ts.",
                    "error_code": "SETUP_EXAMPLE_CLEANUP_REQUIRED", "current_step": "verify_directory_build",
                    "resume_generation": 1, "is_current_attempt": True, "resume_available": False, "retryable": False,
                    "retry_available": False, "user_action": self.action, "requested_action": self.action,
                    "next_action": "remove_setup_example", "article_system_setup": setup,
                    "event_id": "fixture-cleanup-event", "generation": 1, "state_version": 2,
                    "failure": {"code": "SETUP_EXAMPLE_CLEANUP_REQUIRED", "retryable": False}}
                if wire_mode in {"callback", "callback_resume", "legacy_failure"}:
                    request = SimpleNamespace(method="POST", data=deepcopy(wire), query_params={}, headers={}, META={})
                    response = service_views.ContentFactoryCallbackView().post(request)
                    self.assertEqual(response.status_code, 200, response.data)
                    self.assertEqual(self.child.status, "blocked")
                    self.assertFalse(self.child.resume_available)
                    self.assertEqual(self.workflow.state, "blocked")
                    if wire_mode in {"callback", "callback_resume"}:
                        response = service_views.ContentFactoryCallbackView().post(request)
                        self.assertEqual(response.status_code, 200, response.data)
                    else:
                        service_views._sync_generation_callback_to_run(data={**wire, "event_type": "generation_failed", "status": "error"},
                            run_status="failed", step_status="failed")
                        self.assertEqual(self.child.status, "failed")
                elif wire_mode == "poll":
                    views._sync_local_run_from_remote(self.child, wire)
                    self.assertEqual(self.child.status, "blocked")
                    self.assertEqual(self.child.result["user_action"], self.action)
                else:
                    snapshot = {**wire, "result": {**setup, "article_system_setup": setup}, "step_order": [], "step_states": {}}
                    reconciliation._adopt_remote_payload(self.child, snapshot)
                    self.assertEqual(self.child.status, "blocked")
                self.assertEqual(self.child.result["article_system_setup"]["user_action"], self.action)
                self.assertFalse(self.child.resume_available)
            if not saved_cleanup:
                prepare.advance_prepare(self.op.pk)
                self.assertEqual(self.op.state, "needs_user", self.op.receipt)
                self.assertEqual(self.op.receipt["user_action"]["id"], "remove_setup_example")
                self.assertIsNone(self.op.next_attempt_at)
                queue.assert_not_called()
            prepare.start_prepare(self.config, expected=contract_for(self.website), retry=True)
            prepare.advance_prepare(self.op.pk)
            self.assertEqual(self.op.state, "pending", self.op.receipt)
            self.assertEqual(self.op.receipt["child_run_id"], "fresh-scan")
            self.assertEqual(self.op.receipt["scan_run_id"], "fresh-scan")
            self.assertEqual(self.op.payload["attempt"], 2)
            self.assertEqual(queue.call_args.kwargs["workflow"], "repo_scan")
            self.assertTrue(queue.call_args.kwargs["payload"]["force_refresh"])
            self.assertEqual(provider.call_count, 2)
            request = prepare._owner_request(self.op, context, self.config, "setup", user=self.user)
            self.assertEqual(request.data["source_scan_run_id"], "fresh-scan")
            self.assertEqual(request.data["website_connection_id"], str(self.website.pk))
            self.assertEqual(request.data["connection_generation"], 3)
            self.assertEqual(request.data["repository_id"], 123)
            self.assertEqual(request.data["idempotency_key"], f"prepare:{self.op.pk}:setup:2:{self.op.payload['retry_epoch']}")
            self.assertRegex(self.op.payload["retry_epoch"], r"^[a-f0-9]{32}$")
            self.assertEqual(request.data["retry_epoch"], self.op.payload["retry_epoch"])
            self.assertEqual(context.company.pk, 9)
            self.assertNotIn("prepare_user", context.__dict__)
