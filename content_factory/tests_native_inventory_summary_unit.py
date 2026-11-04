"""Count disclosure is bounded, scoped and independent of readiness or writes."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import website_native_inventory as inventory


def seal(value):
    body = {key: item for key, item in value.items() if key != "digest"}
    return {**body, "digest": hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()}


@override_settings(ARTICLES_EXISTING_INVENTORY_DISPLAY_ENABLED=True)
class NativeInventorySummaryTests(SimpleTestCase):
    def setUp(self):
        self.example = json.loads((Path(__file__).resolve().parents[1] / "contracts/native-inventory-summary-v1.example.json").read_text())
        scope = self.example["source_scope"]
        self.connection = SimpleNamespace(pk=scope["website_connection_id"], generation=scope["connection_generation"],
            repository_id=scope["repository_id"], app_root=scope["app_root"], branch=scope["branch"], organization_id=7,
            github_repo="fixture/site", site_url="https://site.example", organization=SimpleNamespace(domain="site.example"),
            verified_sha="c" * 40, capabilities={"read": True, "publish": False}, blockers=[])
        self.op = SimpleNamespace(action="prepare", connection=self.connection, receipt={"setup_run_id": self.example["run_id"]})
        request = {"website_connection_id": str(self.connection.pk), "repository_id": self.connection.repository_id,
            "connection_generation": self.connection.generation, "deletion_epoch": 0}
        self.child = SimpleNamespace(run_id=self.example["run_id"], github_repo="fixture/site", domain="site.example", run_request=request,
            result={"article_system_setup_cache": {"native_inventory_summary": deepcopy(self.example)}})

    def validate(self, value):
        return inventory.validate_summary(value, connection=self.connection, run_id=self.example["run_id"])

    def reader(self):
        from workflow_runs.models import ContentFactoryRun
        rows = patch.object(ContentFactoryRun.objects, "filter")
        return rows

    def scoped_read(self):
        with self.reader() as rows, patch("content_factory.website_operations.deletion_epoch", return_value=0), \
                patch("content_factory.website_operations.validate_operation", return_value=SimpleNamespace(payload={"run_id": self.child.run_id})) as checked:
            rows.return_value.first.return_value = self.child
            result = inventory.for_operation(self.op)
            rows.assert_called_once_with(run_id=self.child.run_id, organization_id=7, workflow="article_system_setup")
            return result, checked.call_count

    def test_actual_pinned_producer_example_preserves_original_source(self):
        self.assertEqual(self.validate(self.example), self.example)
        result, checks = self.scoped_read()
        self.assertEqual(result["count"], 35)
        self.assertEqual(result["sourceSha"], "a" * 40)
        self.assertNotEqual(result["sourceSha"], self.connection.verified_sha)
        self.assertEqual(checks, 1)
        self.assertNotIn("records", result)
        self.assertEqual(self.connection.capabilities, {"read": True, "publish": False})

    def test_valid_zero_and_singular_counts_are_observations(self):
        for count in (0, 1):
            with self.subTest(count=count):
                value = seal({**self.example, "existing_article_count": count})
                self.assertEqual(self.validate(value)["existing_article_count"], count)
                status = {"state": "working", "action": {"id": "inspect_goal"}, "evidence": [], "milestones": []}
                before = deepcopy(status)
                with patch.object(inventory, "for_operation", return_value={"count": count}):
                    shown = inventory.add_display(status, self.op)
                self.assertEqual(shown["state"], "working")
                self.assertEqual(shown["action"], before["action"])
                self.assertEqual(shown["milestones"], [])
                self.assertEqual(shown["evidence"][0]["message"], f"We found {count} existing article{'s' if count != 1 else ''}.")
                self.assertEqual(status, before)

    def test_malformed_rehashed_or_foreign_summaries_are_omitted(self):
        faults = [("schema_version", True), ("schema_version", 2), ("kind", "ready"), ("run_id", "foreign"),
            ("existing_article_count", True), ("existing_article_count", -1), ("existing_article_count", 10001),
            ("existing_article_count", 3.5), ("inventory_kind", []), ("native_inventory_digest", "private body"),
            ("origin", "https://other.example"), ("origin", "https://secret@site.example"),
            ("listing_route", "//other.example"), ("listing_route", "/stories/%2e%2e/admin"),
            ("listing_route", "/stories/%2fadmin"), ("listing_route", "/stories/%5cadmin"),
            ("listing_route", "/stories/%00"), ("listing_route", "/stories/?token=secret"),
            ("listing_route", "/" + "x" * 4096)]
        for field, item in faults:
            with self.subTest(field=field, item=item):
                self.assertIsNone(self.validate(seal({**self.example, field: item})))
        self.assertIsNone(self.validate({**self.example, "digest": "f" * 64}))
        self.assertIsNone(self.validate(seal({**self.example, "records": ["private body"]})))
        for field, item in [("website_connection_id", "foreign"), ("repository_id", 124), ("repository_id", True),
                ("connection_generation", 3), ("connection_generation", True), ("branch", "elsewhere"), ("app_root", "website"), ("source_sha", "bad")]:
            with self.subTest(scope=field):
                self.assertIsNone(self.validate(seal({**self.example, "source_scope": {**self.example["source_scope"], field: item}})))

    def test_canonical_reader_rejects_foreign_original_run_and_deletion_scope(self):
        for field, item in [("website_connection_id", "foreign"), ("repository_id", 124), ("connection_generation", 3), ("deletion_epoch", 1)]:
            saved = deepcopy(self.child.run_request)
            self.child.run_request[field] = item
            result, checks = self.scoped_read()
            self.assertIsNone(result)
            self.assertEqual(checks, 0)
            self.child.run_request = saved
        from .website_contract import WebsiteAuthorityError
        with self.reader() as rows, patch("content_factory.website_operations.deletion_epoch", return_value=0), \
                patch("content_factory.website_operations.validate_operation", side_effect=WebsiteAuthorityError("stale_operation", "Synthetic stale scope")):
            rows.return_value.first.return_value = self.child
            self.assertIsNone(inventory.for_operation(self.op))

    def test_conflicting_callback_copies_never_choose_a_display_count(self):
        self.child.result["article_system_setup"] = {"article_system_setup_cache": {"native_inventory_summary": seal({**self.example, "existing_article_count": 99})}}
        self.assertIsNone(self.scoped_read()[0])
        self.child.result["article_system_setup"]["article_system_setup_cache"]["native_inventory_summary"] = deepcopy(self.example)
        self.assertEqual(self.scoped_read()[0]["count"], 35)

    def test_flag_off_and_missing_observations_add_no_query_or_guessed_zero(self):
        with override_settings(ARTICLES_EXISTING_INVENTORY_DISPLAY_ENABLED=False), patch("content_factory.website_goal_evidence._child") as read:
            status = {"state": "working", "evidence": []}
            self.assertIs(inventory.add_display(status, self.op), status)
            read.assert_not_called()
        self.child.result = {"existing_article_count": 0}
        self.assertIsNone(self.scoped_read()[0])
        self.op.action = "disconnect"
        with patch("content_factory.website_goal_evidence._child") as read:
            self.assertIsNone(inventory.for_operation(self.op))
            read.assert_not_called()

    @override_settings(ARTICLES_WEBSITE_STATUS_V1_ENABLED=True, ARTICLES_WEBSITE_DOCTOR_ENABLED=False,
        ARTICLES_WEBSITE_GOAL_RUNNER_ENABLED=True, ARTICLES_WEBSITE_GOAL_ENROLLED_ORGANIZATIONS=["7"])
    def test_actual_status_wrapper_adds_observation_without_changing_goal_or_action(self):
        from django.utils import timezone
        from datetime import timedelta
        from . import tests_website_goal_unit as fixtures, website_goal as goals, website_status as status
        op = fixtures.GoalRunnerRegressions().operation()
        op.connection = self.connection
        op.action = "prepare"
        op.receipt["setup_run_id"] = self.child.run_id
        self.connection.configuration_version = 1
        self.connection.state = "connected"
        op.updated_at = now = timezone.now()
        op.next_attempt_at = now + timedelta(seconds=30)
        op.receipt["goal"] = goals.new_goal(str(op.pk), goals.scope_for(self.connection, 9), now=now)
        context, config = SimpleNamespace(company=SimpleNamespace(pk=9)), SimpleNamespace(website_connection=self.connection)
        rows = Mock()
        rows.filter.return_value = rows
        rows.order_by.return_value = rows
        rows.first.return_value = op
        self.connection.operations = rows
        journey = {"prerequisites": {"account": {"status": "complete"}, "repository": {"status": "complete"}}}
        before = status.project_status(op, scope=goals.scope_for(self.connection, 9), source_sha="c" * 40, journey=journey, now=now)
        receipt = deepcopy(op.receipt)
        with self.reader() as children, patch("content_factory.website_operations.deletion_epoch", return_value=0), \
                patch("content_factory.website_operations.validate_operation", return_value=SimpleNamespace(payload={"run_id": self.child.run_id})), \
                patch.object(status, "source_for", return_value="c" * 40):
            children.return_value.first.return_value = self.child
            result = status.status_for_context(context, config, journey=journey, now=now)
        self.assertEqual(result["existingArticles"]["count"], 35)
        self.assertEqual(result["evidence"][-1], {"id": "native_capture", "result": "passed", "at": None, "message": "We found 35 existing articles."})
        self.assertEqual({key: item for key, item in result.items() if key not in {"existingArticles", "evidence"}},
            {key: item for key, item in before.items() if key != "evidence"})
        self.assertEqual(op.receipt, receipt)
