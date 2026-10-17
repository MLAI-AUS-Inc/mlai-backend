"""A founder's Try again replaces a stopped setup run without leaving its branch.

TalaThrive (Oct 2026): each manual retry after a blocked or failed setup child
dispatched a new Content Factory run, and so a new
``feature/<dir>-publish-route-<run>`` branch, while the replaced run's branch
stayed on the customer's repository. These run without a database or provider
access.
"""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun

from . import vibe_marketing_views  # noqa: F401 -- decorates with transaction.atomic at import; load before it is patched
from . import website_prepare as prepare
from . import website_reconciliation as reconciliation
from .models import OrganizationContentConfig
from .website_connections import contract_for
from .website_journey import current_operation
from .website_models import WebsiteConnection, WebsiteConnectionOperation
from .tests_stopped_prepare_source_unit import OperationRows

RUN_ID = "c14aff04-8d3d-4a1b-9c2e-0123456789ab"
BRANCH = "feature/stories-publish-route-c14aff048d3d"


def rows(*, first=None):
    query = MagicMock()
    for method in ("filter", "exclude", "order_by", "select_related", "select_for_update"):
        getattr(query, method).return_value = query
    query.first.return_value = first
    return query


class TryAgainQueuesReplacedSetupCleanupTests(SimpleTestCase):
    def setUp(self):
        self.user = get_user_model()(pk=4, is_active=True, email="founder@example.test")
        self.org = Organization(pk=7, domain="talathrive.test")
        self.website = WebsiteConnection(organization=self.org, authorized_by=self.user, github_repo="fixture/tala",
            repository_id=123, installation_id="4", generation=3, configuration_version=2, branch="main",
            state="connected", verified_sha="a" * 40)
        self.config = OrganizationContentConfig(organization=self.org, website_connection=self.website,
            github_repo=self.website.github_repo, article_system={})
        self.workflow_id = str(uuid4())
        self.child = ContentFactoryRun(run_id=RUN_ID, workflow="article_system_setup", status="blocked",
            organization=self.org, domain=self.org.domain, github_repo=self.website.github_repo,
            run_request={**contract_for(self.website), "operation_id": self.workflow_id, "operation_attempt": 1,
                "deletion_epoch": 0, "expected_source_sha": "a" * 40},
            result={"status": "blocked", "error_code": "ADOPTED_DIRECTORY_BUILD_UNVERIFIED", "branch_name": BRANCH,
                "article_system_setup": {"status": "blocked", "branch_name": BRANCH}})
        self.op = WebsiteConnectionOperation(pk=uuid4(), connection=self.website, generation=3, action="prepare",
            state="needs_attention", payload={"company_id": "9", "requested_by_user_id": "4", "attempt": 12, "run_id": RUN_ID},
            receipt={"current_step": "setup", "child_run_id": RUN_ID, "setup_run_id": RUN_ID, "failure_class": "repairable",
                "user_action": {"id": "open_run", "label": "Open setup run", "run_id": RUN_ID}},
            updated_at=timezone.now())
        self.op.save = Mock()

    def retry(self, *, cancel=None):
        created = {}

        def get_or_create(idempotency_key, defaults):
            if idempotency_key == f"{self.website.pk}:prepare:3":
                return self.op, False
            created[idempotency_key] = SimpleNamespace(**defaults)
            return created[idempotency_key], True
        with patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                patch.object(WebsiteConnectionOperation.objects, "get_or_create", side_effect=get_or_create), \
                patch.object(WebsiteConnectionOperation.objects, "filter", return_value=rows()), \
                patch.object(ContentFactoryRun.objects, "filter", return_value=rows(first=self.child)), \
                patch("content_factory.website_operations.cancel_operation", cancel or Mock()):
            prepare.start_prepare(self.config, expected=contract_for(self.website), retry=True)
        return created

    def test_try_again_after_a_blocked_setup_queues_its_branch_cleanup(self):
        created = self.retry()
        followup = created[f"{self.website.pk}:superseded-setup:{RUN_ID}"]
        self.assertEqual((followup.action, followup.generation, followup.connection), ("superseded-setup-cleanup", 3, self.website))
        self.assertEqual(followup.payload["binding"], {"website_connection_id": str(self.website.pk), "connection_generation": 3,
            "repository_id": 123, "operation_id": self.workflow_id, "operation_attempt": 1, "deletion_epoch": 0,
            "run_id": RUN_ID, "domain": "talathrive.test", "github_repo": "fixture/tala"})
        self.assertEqual((followup.payload["prepare_operation_id"], followup.payload["superseded_attempt"]), (str(self.op.pk), 12))
        self.assertEqual(followup.receipt["status"], "queued")
        # The new attempt itself is unchanged: it still dispatches a new setup.
        self.assertEqual((self.op.state, self.op.payload["attempt"], self.op.receipt["child_run_id"]), ("pending", 13, None))
        self.assertTrue(self.op.payload["repair_setup"])

    def test_a_failed_setup_child_is_replaced_the_same_way(self):
        self.child.status = "failed"
        self.child.result = {**self.child.result, "retryable": True}
        self.assertIn(f"{self.website.pk}:superseded-setup:{RUN_ID}", self.retry())

    def test_branches_that_may_still_matter_are_never_queued(self):
        cases = {
            "pull request": {"result": {**self.child.result, "pr_url": "https://github.com/fixture/tala/pull/41"}},
            "nested pull request": {"result": {"article_system_setup": {"status": "blocked", "pr_number": 41}}},
            "another generation": {"run_request": {**self.child.run_request, "connection_generation": 2}},
            "another repository": {"run_request": {**self.child.run_request, "repository_id": 999}},
            "another repository name": {"github_repo": "fixture/other"},
            "no operation fence": {"run_request": {key: value for key, value in self.child.run_request.items() if key != "operation_id"}},
            "not a setup": {"workflow": "repo_scan"},
            "cancelled": {"status": "cancelled"},
            "completed": {"status": "completed"},
        }
        for name, changes in cases.items():
            with self.subTest(name):
                self.setUp()
                for field, value in changes.items():
                    setattr(self.child, field, value)
                self.assertEqual(self.retry(), {})

    def test_a_removal_after_the_run_started_is_never_queued(self):
        self.website.blockers = [{"deletion_epoch": 1}]
        self.assertEqual(self.retry(), {})

    def test_a_running_child_still_takes_the_ordinary_cancellation(self):
        self.child.status = "running"
        cancel = Mock()
        self.assertEqual(self.retry(cancel=cancel), {})
        cancel.assert_called_once()

    def test_an_owner_recheck_keeps_its_saved_child_and_branch(self):
        self.op.state = "needs_user"
        self.op.receipt.update(failure_class="needs_user", user_action={"id": "merge_pr", "label": "Merge setup PR"})
        self.assertEqual(self.retry(), {})
        self.assertEqual(self.op.receipt["child_run_id"], RUN_ID)


class SupersededSetupCleanupFollowupTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.website = SimpleNamespace(pk=uuid4(), generation=3, state="connected", blockers=[])
        self.binding = {"website_connection_id": str(self.website.pk), "connection_generation": 3, "repository_id": 123,
            "operation_id": str(uuid4()), "operation_attempt": 1, "deletion_epoch": 0, "run_id": RUN_ID,
            "domain": "talathrive.test", "github_repo": "fixture/tala"}
        self.op = SimpleNamespace(pk=uuid4(), connection=self.website, generation=3, attempts=0, next_attempt_at=None,
            created_at=self.now - timedelta(minutes=1), save=Mock(), payload={"run_id": RUN_ID, "binding": self.binding})

    def process(self, response=None, *, error=None, enabled=True):
        post = Mock(return_value=response, side_effect=error)
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as stored, \
                patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                    return_value={"enabled": enabled, "base_url": "https://cf.test"}), \
                patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={"X-API-Key": "fixture"}), \
                patch("integrations.http_client.post", post):
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = self.op
            outcome = reconciliation._process_superseded_setup_cleanup(self.op.pk, self.now)
        update = stored.return_value.update.call_args.kwargs
        self.assertEqual(outcome, update["state"])
        return update, post

    @staticmethod
    def reply(status_code, body):
        return SimpleNamespace(status_code=status_code, json=lambda: body)

    def receipt(self, *refs, status="completed", pending=False):
        return {"schema_version": 1, "run_id": RUN_ID, "status": status, "superseded": True, "cleanup_pending": pending,
            "refs": list(refs), "retained_refs": [row for row in refs if row["status"] == "retained"]}

    def test_deleted_branch_settles_the_follow_up(self):
        deleted = {"status": "deleted", "github_repo": "fixture/tala", "branch_name": BRANCH}
        update, post = self.process(self.reply(200, self.receipt(deleted)))
        self.assertEqual(post.call_args.args[0], f"https://cf.test/api/connections/{self.website.pk}/superseded-setup-cleanup")
        self.assertEqual(post.call_args.kwargs["json"], self.binding)
        self.assertEqual(update["state"], "completed")
        self.assertIsNone(update["next_attempt_at"])
        self.assertEqual(update["receipt"]["refs"], [deleted])
        self.assertTrue(update["receipt"]["repository_modified"])

    def test_a_retained_branch_settles_with_its_reason(self):
        for reason in ("open_pr_exists", "merged_branch", "pull_request_exists", "branch_head_changed",
                "missing_owned_branch_receipt", "repository_no_longer_connected", "cleanup_authorization_refused"):
            with self.subTest(reason):
                self.setUp()
                retained = {"status": "retained", "reason": reason, "github_repo": "fixture/tala", "branch_name": BRANCH}
                update, _ = self.process(self.reply(200, self.receipt(retained)))
                self.assertEqual((update["state"], update["receipt"]["refs"]), ("completed", [retained]))
                self.assertFalse(update["receipt"]["repository_modified"])

    def test_unknown_outcomes_retry_with_backoff(self):
        pending = {"status": "cleanup_pending", "reason": "repository_cleanup_unavailable", "github_repo": "fixture/tala", "branch_name": BRANCH}
        for name, kwargs in (("pending", {"response": self.reply(202, self.receipt(pending, status="cleanup_pending", pending=True))}),
                ("worker error", {"response": self.reply(503, {"detail": "unavailable"})}),
                ("other run", {"response": self.reply(200, {**self.receipt(), "run_id": "another-run"})}),
                ("worker without the route", {"response": self.reply(404, {"detail": "Not Found"})}),
                ("transport", {"error": ConnectionError("reset")}),
                ("disabled", {"enabled": False})):
            with self.subTest(name):
                self.setUp()
                update, _ = self.process(**kwargs)
                self.assertEqual(update["state"], "pending")
                self.assertGreater(update["next_attempt_at"], self.now)
                self.assertTrue(update["receipt"]["last_error"])

    def test_unknown_outcomes_stop_retrying_at_the_deadline(self):
        pending = {"status": "cleanup_pending", "reason": "cleanup_authorization_unavailable", "github_repo": "fixture/tala", "branch_name": BRANCH}
        for name, kwargs in (("pending", {"response": self.reply(202, self.receipt(pending, status="cleanup_pending", pending=True))}),
                ("transport", {"error": ConnectionError("reset")}), ("disabled", {"enabled": False})):
            with self.subTest(name):
                self.setUp()
                self.op.created_at = self.now - reconciliation.SUPERSEDED_SETUP_CLEANUP_DEADLINE
                update, _ = self.process(**kwargs)
                self.assertEqual((update["state"], update["receipt"]["status"]), ("completed", "timed_out"))
                self.assertTrue(update["receipt"]["branch_cleanup_unconfirmed"])
                self.assertIsNone(update["next_attempt_at"])

    def test_refused_or_missing_runs_settle_without_retrying(self):
        for status_code, body, state, receipt in ((409, {"detail": "Live run"}, "cancelled", "refused"),
                (404, {"run_id": RUN_ID, "status": "run_missing"}, "completed", "run_missing")):
            with self.subTest(status_code):
                self.setUp()
                update, _ = self.process(self.reply(status_code, body))
                self.assertEqual((update["state"], update["receipt"]["status"]), (state, receipt))
                self.assertFalse(update["receipt"]["repository_modified"])

    def test_a_changed_connection_settles_without_contacting_the_worker(self):
        for name, change in (("disconnected", {"state": "revoked"}), ("new generation", {"generation": 4}),
                ("removal", {"blockers": [{"deletion_epoch": 1}]})):
            with self.subTest(name):
                self.setUp()
                for field, value in change.items():
                    setattr(self.website, field, value)
                update, post = self.process(self.reply(200, self.receipt()))
                self.assertEqual((update["state"], update["receipt"]["status"]), ("cancelled", "authority_changed"))
                post.assert_not_called()

    def test_the_scheduler_routes_the_follow_up(self):
        def filtered(*args, **kwargs):
            query = Mock()
            query.exclude.return_value.order_by.return_value.values_list.return_value = ["op-1"]
            query.exists.return_value = kwargs.get("action") == "superseded-setup-cleanup"
            return query
        with patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter", side_effect=filtered), \
                patch("content_factory.cms_access_status.expire_due"), \
                patch.object(reconciliation, "monitor_cleanup_pull_requests"), \
                patch("content_factory.website_support.reconcile_support_verifications"), \
                patch.object(reconciliation, "_process_superseded_setup_cleanup", return_value="completed") as processed:
            report = reconciliation.process_website_connection_operations(limit=5)
        processed.assert_called_once()
        self.assertEqual(report["operations"], [{"id": "op-1", "action": "superseded-setup-cleanup", "outcome": "completed"}])

    def test_the_follow_up_is_never_the_owners_current_operation(self):
        now = timezone.now()

        def op(action, state, minutes):
            return SimpleNamespace(pk=uuid4(), action=action, state=state, generation=3,
                created_at=now - timedelta(minutes=minutes), updated_at=now - timedelta(minutes=minutes))
        scan = op("workflow", "completed", 60)
        connection = SimpleNamespace(generation=3, operations=OperationRows([scan, op("superseded-setup-cleanup", "pending", 1)]))
        self.assertIs(current_operation(connection), scan)
