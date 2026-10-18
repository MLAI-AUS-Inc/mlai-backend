"""A stopped preparation, its cleanup follow-up and later source changes.

TalaThrive (14-15 Oct): preparation was stopped while a duplicate setup ran
after MLAI merged setup PR #40. The stopped preparation never reopened for that
merge, its queued preview-cleanup follow-up became the panel's "current
operation" with the old "Preparing articles: setup." text, checks for superseded
pushes retried hourly for a week, and the next-step button was the reason
sentence. These run without a database or provider access.
"""
from contextlib import nullcontext
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.utils import timezone

from . import vibe_marketing_views  # noqa: F401 -- decorates with transaction.atomic at import; load before it is patched
from . import website_operations as operations
from . import website_prepare as prepare
from . import website_reconciliation as reconciliation
from .website_contract import WebsiteAuthorityError
from .website_journey import current_operation, project_journey
from . import tests_prepare_journey_unit as replay


class OperationRows:
    """The few queryset lookups current_operation uses, over in-memory rows."""

    def __init__(self, rows):
        self.rows = list(rows)

    @staticmethod
    def _matches(row, lookups):
        for key, value in lookups.items():
            name, _, test = key.partition("__")
            actual = getattr(row, name)
            if test == "in":
                matched = actual in value
            elif test == "gt":
                matched = actual > value
            else:
                matched = actual == value
            if not matched:
                return False
        return True

    def filter(self, **lookups):
        return OperationRows(row for row in self.rows if self._matches(row, lookups))

    def exclude(self, **lookups):
        return OperationRows(row for row in self.rows if not self._matches(row, lookups))

    def order_by(self, field):
        return OperationRows(sorted(self.rows, key=lambda row: getattr(row, field.lstrip("-")), reverse=field.startswith("-")))

    def first(self):
        return self.rows[0] if self.rows else None


class CurrentOperationTests(SimpleTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.stopped_at = self.now - timedelta(minutes=30)

    def op(self, action, state, *, minutes, updated=None, generation=3):
        created = self.now - timedelta(minutes=minutes)
        return SimpleNamespace(pk=uuid4(), action=action, state=state, generation=generation,
            created_at=created, updated_at=updated or created)

    def connection(self, *rows):
        return SimpleNamespace(generation=3, operations=OperationRows(rows))

    def test_queued_cleanup_never_stands_in_for_stopped_preparation(self):
        stopped = self.op("prepare", "cancelled", minutes=600, updated=self.stopped_at)
        child = self.op("workflow", "cancelled", minutes=40, updated=self.stopped_at)
        cleanup = self.op("cancel-operation", "pending", minutes=29)
        recheck = self.op("source-reverify", "pending", minutes=10)
        self.assertIs(current_operation(self.connection(stopped, child, cleanup, recheck)), stopped)

    def test_owner_work_started_after_stopping_becomes_the_operation(self):
        stopped = self.op("prepare", "cancelled", minutes=600, updated=self.stopped_at)
        scan = self.op("workflow", "running", minutes=5)
        cleanup = self.op("cancel-operation", "pending", minutes=29)
        self.assertIs(current_operation(self.connection(stopped, cleanup, scan)), scan)

    def test_live_preparation_wins_over_its_newer_children(self):
        live = self.op("prepare", "needs_attention", minutes=600)
        child = self.op("workflow", "blocked", minutes=20)
        self.assertIs(current_operation(self.connection(live, child)), live)

    def test_without_preparation_background_follow_ups_are_skipped(self):
        scan = self.op("workflow", "completed", minutes=60)
        recheck = self.op("source-reverify", "pending", minutes=5)
        self.assertIs(current_operation(self.connection(scan, recheck)), scan)
        self.assertIsNone(current_operation(self.connection(recheck, self.op("worker_followup", "pending", minutes=1))))
        self.assertIsNone(current_operation(self.connection(self.op("prepare", "pending", minutes=1, generation=2))))


class NextActionLabelTests(SimpleTestCase):
    def journey(self, *, discovery_complete=True, permissions=None):
        return project_journey(company_id=9, domain="fixture.test", website={"status": "connected", "repositoryId": 123,
            "connectionGeneration": 3, "writePolicy": {"allowed": True}, "verifiedSha": "a" * 40},
            capabilities={"repositoryAccessVerified": True, "repositorySourceSha": "a" * 40},
            discovery={"complete": discovery_complete, "sourceSha": "a" * 40}, permissions=permissions)

    def test_buttons_are_verbs_and_reasons_travel_beside_them(self):
        cases = (
            (self.journey(discovery_complete=False), "scan", "Scan repository", "scan_required", "Scan the current repository."),
            (self.journey(permissions={"workflows": False, "checks": True, "statuses": True,
                "settingsUrl": "https://github.com/settings/installations/4"}), "permissions", "Review GitHub permissions",
                "github_workflow_permission_required",
                "Approve Workflows: write to install or update .github/workflows/mlai-articles-verification.yml."),
            (self.journey(), "setup", "Set up articles", "integration_required", "Prepare and verify your articles integration."),
        )
        for journey, action, label, code, reason in cases:
            with self.subTest(action=action):
                self.assertEqual(journey["nextAction"]["id"], action)
                self.assertEqual(journey["nextAction"]["label"], label)
                self.assertEqual(journey["nextAction"]["reason"], reason)
                # The code stays on the step the action names.
                step = next(item for item in journey["steps"] if item["id"] == journey["nextAction"]["step"])
                self.assertEqual(step["reasonCode"], code)


class StoppedPreparationReceiptTests(SimpleTestCase):
    def test_stopping_preparation_replaces_the_in_progress_message(self):
        fixture = replay.PrepareJourneyReplayTests()
        fixture.setUp()
        op, connection = fixture.op, fixture.op.connection
        op.pk = uuid4()
        op.receipt = {"status": "working", "current_step": "setup", "message": "Preparing articles: setup."}
        connection.blockers = []
        connection.operations = Mock()
        connection.repository_mutations = Mock()
        connection.repository_mutations.filter.return_value.values.return_value = []
        connection.operations.select_for_update.return_value.filter.return_value.first.return_value = op
        fixture.config.organization = connection.organization
        fixture.config.github_repo = connection.github_repo
        with patch("content_factory.website_connections.authority_guard", return_value=nullcontext(connection)), \
                patch("workflow_runs.models.ContentFactoryRun.objects.filter") as rows, \
                patch.object(operations.WebsiteConnectionOperation.objects, "get_or_create", return_value=(Mock(), True)) as followup:
            rows.return_value.first.return_value = None
            rows.return_value.values_list.return_value = []
            operations.cancel_operation(fixture.config, data={"website_connection_id": str(uuid4()), "connection_generation": 3,
                "operation_id": str(op.pk)}, idempotency_key="stop")
        self.assertEqual((op.state, op.receipt["status"]), ("cancelled", "cancelled_by_user"))
        self.assertEqual(op.receipt["message"], "Website preparation stopped.")
        copied = followup.call_args.kwargs["defaults"]["receipt"]
        self.assertEqual((copied["status"], copied["message"]), ("cancelled_by_user", "Website preparation stopped."))


class SourceWakeTests(SimpleTestCase):
    def setUp(self):
        self.fixture = replay.PrepareJourneyReplayTests()
        self.fixture.setUp()
        self.op = self.fixture.op
        self.website = self.op.connection
        self.website.authorized_by = self.fixture.user
        self.website.authorized_by_id = self.fixture.user.pk
        self.website.operations = Mock()
        self.website.operations.filter.return_value.order_by.return_value.first.return_value = self.op
        self.op.state = "cancelled"
        self.op.payload = {**self.op.payload, "attempt": 11, "repair_setup": True, "run_id": "stopped-setup"}
        self.op.receipt = {"status": "cancelled_by_user", "current_step": "setup", "message": "Website preparation stopped.",
            "child_run_id": "stopped-setup", "remote_effects": [{"branch": "feature/stopped-setup"}],
            "remote_cleanup_pending": True, "remote_outcome_unknown": False, "cancellation_undoes_remote_writes": False}

    def wake(self, owned_merge_run_id=""):
        from .models import OrganizationContentConfig
        from .website_models import WebsiteConnection
        from founder_tools.models import VibeRaisingCompany
        from workflow_runs.models import ContentFactoryRun
        reopen = []

        def operation_rows(**lookups):
            rows = Mock()
            def update(**values):
                reopen.append((lookups, values))
                return int(self.op.state in lookups["state__in"])
            rows.update.side_effect = update
            return rows

        def setup_runs(**lookups):
            current = (lookups.get("run_id") == "merged-setup" and lookups.get("workflow") == "article_system_setup"
                and lookups.get("organization_id") == 7 and lookups.get("run_request__connection_generation") == 3
                and lookups.get("run_request__website_connection_id") == str(self.website.pk))
            return Mock(exists=Mock(return_value=current))

        with patch.object(WebsiteConnection.objects, "select_related") as connections, \
                patch.object(OrganizationContentConfig.objects, "filter") as configs, \
                patch.object(VibeRaisingCompany.objects, "filter") as companies, \
                patch.object(prepare, "authority_guard", return_value=nullcontext(self.website)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, False)), \
                patch.object(prepare.WebsiteConnectionOperation.objects, "filter", side_effect=operation_rows), \
                patch.object(ContentFactoryRun.objects, "filter", side_effect=setup_runs):
            connections.return_value.filter.return_value.first.return_value = self.website
            configs.return_value.first.return_value = self.fixture.config
            companies.return_value.first.return_value = self.fixture.context.company
            result = prepare.wake_prepare_for_source(self.website.pk, organization_id=7, owned_merge_run_id=owned_merge_run_id)
        lookups, values = next((lookups, values) for lookups, values in reopen if "state__in" in lookups)
        return result, lookups, values

    def test_mlai_setup_merge_reopens_stopped_preparation_to_verify_it(self):
        result, lookups, values = self.wake("merged-setup")
        self.assertIs(result, self.op)
        self.assertIn("cancelled", lookups["state__in"])
        receipt, payload = values["receipt"], values["payload"]
        self.assertEqual((values["state"], receipt["status"], receipt["current_step"]), ("pending", "working", "scan"))
        self.assertEqual(receipt["merged_setup_run_id"], "merged-setup")
        self.assertEqual(receipt["message"], "Rechecking the changed website source.")
        self.assertIsNone(receipt["child_run_id"])
        for key in ("remote_effects", "remote_cleanup_pending", "remote_outcome_unknown", "cancellation_undoes_remote_writes"):
            self.assertNotIn(key, receipt)
        # The merged head is verified first: no forced repair of a setup that already shipped.
        self.assertEqual((payload["attempt"], payload["source_reverify"]), (12, True))
        self.assertNotIn("repair_setup", payload)
        self.assertNotIn("run_id", payload)
        self.assertEqual(self.website.operations.filter.call_args_list, [
            call(generation=3, action="prepare"),
            call(generation=3, action="source-reverify", state="pending"),
        ])
        self.assertEqual(self.website.operations.filter.return_value.update.call_args.kwargs["receipt"]["status"], "managed_by_prepare")

    def test_customer_push_leaves_stopped_preparation_stopped(self):
        result, lookups, _ = self.wake()
        self.assertIsNone(result)
        self.assertNotIn("cancelled", lookups["state__in"])
        self.website.operations.filter.assert_called_once_with(generation=3, action="prepare")
        self.website.operations.filter.return_value.update.assert_not_called()

    def test_owned_merge_of_anything_but_a_current_setup_leaves_it_stopped(self):
        result, lookups, _ = self.wake("published-article")
        self.assertIsNone(result)
        self.assertNotIn("cancelled", lookups["state__in"])

    def test_open_preparation_still_reopens_for_any_push(self):
        self.op.state = "needs_attention"
        result, lookups, values = self.wake()
        self.assertIs(result, self.op)
        self.assertNotIn("merged_setup_run_id", values["receipt"])
        self.assertEqual(values["receipt"]["message"], "Rechecking the changed website source.")


class PushWakeTests(SimpleTestCase):
    def push(self, owned_run_id):
        website = SimpleNamespace(pk=uuid4(), generation=3, branch="main", github_repo="synthetic/site", verified_sha="a" * 40,
            repository_id=42, state="connected", installation_id="45", organization=SimpleNamespace(domain="synthetic.test"),
            capabilities={"publishingReady": True, "previewSupported": True}, configuration_version=5, blockers=[], save=Mock())
        config = SimpleNamespace(website_connection=website, organization_id=4, default_publish_target_id="featured")
        owned = {"run_id": owned_run_id, "connection_identity": reconciliation._merge_connection_identity(website)} if owned_run_id else None
        with patch.object(reconciliation.OrganizationContentConfig.objects, "filter") as configs, \
                patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch("organizations.models.Organization.objects.select_for_update"), \
                patch.object(reconciliation.WebsiteConnection.objects, "select_for_update") as connections, \
                patch.object(reconciliation.WebsiteScanSnapshot.objects, "get_or_create", return_value=(object(), True)), \
                patch.object(reconciliation, "_find_owned_merge", return_value=owned), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "get_or_create"), \
                patch.object(prepare, "wake_prepare_for_source") as wake:
            configs.return_value.select_related.return_value = [config]
            connections.return_value.get.return_value = website
            reconciliation.handle_website_github_event("push", {"repository": {"id": 42}, "after": "b" * 40, "ref": "refs/heads/main"})
        return website, wake

    def test_mlai_merge_wakes_preparation_with_its_run(self):
        website, wake = self.push("merged-setup")
        wake.assert_called_once_with(website.pk, organization_id=4, owned_merge_run_id="merged-setup")
        self.assertTrue(website.capabilities["publishingReady"])
        self.assertEqual(website.blockers, [])

    def test_customer_push_wakes_preparation_without_one(self):
        website, wake = self.push("")
        wake.assert_called_once_with(website.pk, organization_id=4, owned_merge_run_id="")
        self.assertFalse(website.capabilities["publishingReady"])


class SupersededSourceCheckTests(SimpleTestCase):
    def process(self, error):
        from . import website_connections as authority
        website = SimpleNamespace(pk=uuid4(), generation=3, github_repo="synthetic/site", repository_id=42, app_root="",
            branch="main", organization=SimpleNamespace(domain="synthetic.test"), targets=Mock())
        op = SimpleNamespace(pk=uuid4(), connection=website, generation=3, attempts=150, next_attempt_at=None, save=Mock(),
            payload={"source_sha": "c" * 40, "target_id": "featured", "scan_required": True}, receipt={"status": "verification_pending"})
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as stored, \
                patch.object(authority, "authority_guard", side_effect=error):
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            outcome = reconciliation._process_source_reverification(op.pk, timezone.now())
        return outcome, stored.return_value.update.call_args.kwargs

    def test_a_check_for_a_superseded_push_settles_instead_of_retrying_forever(self):
        moved = WebsiteAuthorityError("website_source_changed", "The repository changed since this verification.")
        remapped = WebsiteAuthorityError("website_source_changed", "The repository changed since this verification.")
        remapped.code = "remapped_by_failure_taxonomy"
        for error in (moved, remapped):
            with self.subTest(code=error.code):
                outcome, update = self.process(error)
                self.assertEqual((outcome, update["state"]), ("cancelled", "cancelled"))
                self.assertEqual((update["receipt"]["status"], update["receipt"]["retryable"]), ("superseded", False))

    def test_a_check_waiting_for_its_target_keeps_retrying(self):
        outcome, update = self.process(WebsiteAuthorityError("verified_target_required", "Waiting for a target.", retryable=True))
        self.assertEqual((outcome, update["state"], update["receipt"]["status"]), ("pending", "pending", "verification_pending"))
