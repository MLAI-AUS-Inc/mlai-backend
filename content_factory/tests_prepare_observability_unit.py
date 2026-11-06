"""Prepare diagnostics remain useful without exposing receipt text to logs."""
from types import SimpleNamespace
from contextlib import contextmanager, nullcontext
from io import StringIO
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from . import tests_one_click_operations_unit as fixtures
from .website_contract import WebsiteAuthorityError


class PrepareObservabilityTests(SimpleTestCase):
    def test_dispatch_and_failure_are_structured(self):
        fixture = fixtures.PrepareJourneyTests()
        with self.assertLogs("content_factory.website_prepare", level="INFO") as logged:
            fixture.run_step("scan")
        self.assertIn("step=scan", logged.output[-1])
        self.assertIn("company=9", logged.output[-1])
        self.assertIn("state=pending", logged.output[-1])
        with self.assertLogs("content_factory.website_prepare", level="INFO") as logged:
            fixture.run_step("setup", Mock(side_effect=WebsiteAuthorityError("worker_unavailable", "private receipt text", retryable=True)))
        self.assertIn("reason=worker_unavailable", logged.output[-1])
        self.assertNotIn("private receipt text", " ".join(logged.output))

    def test_unexpected_exception_has_traceback(self):
        with self.assertLogs("content_factory.website_prepare", level="INFO") as logged:
            fixtures.PrepareJourneyTests().run_step("scan", Mock(side_effect=RuntimeError("unexpected")))
        self.assertIn("Traceback", logged.output[0])
        self.assertIn("reason=website_prepare_unavailable", logged.output[-1])

    def test_unexpected_error_logs_real_type_and_frames_without_provider_text(self):
        def dispatch(*args, **kwargs):
            message = "private provider token=sensitive-fixture-value"
            raise TypeError(message)
        with self.assertLogs("content_factory.website_prepare", level="INFO") as logged:
            fixtures.PrepareJourneyTests().run_step("scan", dispatch)
        self.assertIn("error_type=TypeError", logged.output[0])
        self.assertIn("in dispatch", logged.output[0])
        self.assertIn("Traceback", logged.output[0])
        self.assertNotIn("sensitive-fixture-value", " ".join(logged.output))
        self.assertNotIn("private provider", " ".join(logged.output))

    def test_scheduler_lists_bounded_operation_outcomes(self):
        from . import website_reconciliation as reconciliation
        op = fixtures.PrepareJourneyTests().operation()
        with patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as rows, \
                patch.object(reconciliation, "monitor_cleanup_pull_requests"), \
                patch("content_factory.website_support.reconcile_support_verifications"), \
                patch("content_factory.website_prepare.advance_prepare", return_value=op):
            rows.return_value.exclude.return_value.order_by.return_value.values_list.return_value = list(range(25))
            rows.return_value.exists.return_value = True
            def selected(*args, **kwargs):
                if kwargs.get("action") and kwargs["action"] != "prepare":
                    absent=Mock();absent.exists.return_value=False;return absent
                return rows.return_value
            rows.side_effect=selected
            report = reconciliation.process_website_connection_operations(limit=25)
        self.assertEqual(report["processed"], 25)
        self.assertEqual(len(report["operations"]), 20)
        self.assertEqual(report["operations"][0], {"id": "0", "action": "prepare", "outcome": "pending"})

    def test_inspection_formatter_includes_all_diagnostic_fields(self):
        from .management.commands.inspect_website_prepare import inspection_report
        now = timezone.now()
        connection = SimpleNamespace(pk="connection", generation=3, state="connected", github_repo="fixture/site",
            repository_id=123, branch="main", app_root="", installation_id="4", verified_sha="a" * 40,
            blockers=[{"code": "scan_stale"}], capabilities={"read": True, "write": False})
        operation = SimpleNamespace(pk="operation", action="prepare", state="needs_user", attempts=2,
            next_attempt_at=None, created_at=now, updated_at=now, receipt={"status": "needs_you", "current_step": "permissions",
                "reason_code": "github_workflow_permission_required", "message": "Review permissions", "step_failures": {},
                "completed_steps": ["scan"], "child_run_id": "child", "user_action": {"id": "review_github_permissions"}})
        config = SimpleNamespace(article_system={"pending_article_system_setup": {"mode": "existing", "route_path": "/stories",
            "source_scan_run_id": "scan", "setup_run_id": "setup"}})
        snapshot = SimpleNamespace(run_id="scan", source_sha="b" * 40, evidence={"repository_inventory": {
            "discovery_complete": True, "article_surface_resolution": {"existence_state": "confirmed", "listing_surface": {"route": "/stories"}}}})
        target = SimpleNamespace(target_key="stories", adapter="static_markdown", source_sha="b" * 40, verified_at=now)
        report = inspection_report(connection, config, [operation], snapshot, [target])
        self.assertEqual(report["connection"]["capabilities"], ["read"])
        self.assertEqual(report["operations"][0]["receipt"]["reason_code"], "github_workflow_permission_required")
        self.assertNotIn("message", report["operations"][0]["receipt"])
        self.assertEqual(report["pending_article_system_setup"]["route"], "/stories")
        self.assertEqual(report["inventory"]["existence_state"], "confirmed")
        self.assertEqual(report["targets"][0]["key"], "stories")
        self.assertEqual(report["pending_article_system_setup"]["setup_run"], "setup")

    def test_inspect_starts_read_only_transaction_before_its_first_query(self):
        from .management.commands import inspect_website_prepare as inspect
        from founder_tools.models import VibeRaisingCompany
        from .models import OrganizationContentConfig
        events = []
        @contextmanager
        def cursor():
            yield SimpleNamespace(execute=lambda sql: events.append(sql))
        database = SimpleNamespace(vendor="postgresql", cursor=cursor)
        website = Mock()
        website.blockers = []
        website.operations.select_related.return_value.order_by.return_value = []
        website.targets.filter.return_value = []
        website.scan_snapshots.filter.return_value.exclude.return_value.filter.return_value.order_by.return_value.first.return_value = None
        config = SimpleNamespace(website_connection=website, article_system={})
        company = SimpleNamespace(pk="fixture", organization=SimpleNamespace(), profile=SimpleNamespace(user=SimpleNamespace()))
        command = inspect.Command(stdout=StringIO())
        def select_company(*args, **kwargs):
            self.assertEqual(events, ["SET TRANSACTION READ ONLY"])
            return rows
        rows = Mock()
        rows.filter.return_value.first.return_value = company
        with patch.object(inspect, "database", database), patch.object(inspect.transaction, "atomic", return_value=nullcontext()), \
                patch.object(VibeRaisingCompany.objects, "select_related", side_effect=select_company), \
                patch.object(OrganizationContentConfig.objects, "select_related") as configs, \
                patch.object(inspect, "inspection_report", return_value={"connection": {"id": "fixture"}}), \
                patch("content_factory.website_journey.journey_for_context", return_value={"nextAction": None, "prerequisites": {}, "allowedActions": []}) as journey:
            configs.return_value.filter.return_value.first.return_value = config
            command.handle(company_id="fixture", domain=None, journey=True)
        self.assertEqual(json.loads(command.stdout.getvalue())["journey"]["allowedActions"], [])
        journey.assert_called_once()
