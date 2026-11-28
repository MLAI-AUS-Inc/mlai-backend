"""B24: a run the system stopped restarts or gets one notice; a founder's own cancel gives nothing."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from workflow_runs.models import ContentFactoryRun

from . import system_stopped_runs as stopped
from .founder_journey_testing import make_history_free_org
from .tests_live_url_verification import MERGED, _connect, _next_js_target
from .website_connections import contract_for, transition_connection


def _run(status="cancelled", *, error="", result=None, run_request=None, workflow="confirmed_topic"):
    return SimpleNamespace(run_id="r-1", workflow=workflow, status=status, error=error, result=result or {},
        run_request=run_request or {}, slack_user_id="")


class StopCauseTests(SimpleTestCase):
    def test_a_founder_cancel_is_never_a_system_stop(self):
        for run in (_run(result={"cancellation_reason": "owner_cancelled", "cancelled_by": "owner"}),
                    _run(result={"cancellation_reason": "draft_deleted"}),
                    _run(error="Cancelled by user.")):
            with self.subTest(result=run.result, error=run.error):
                self.assertTrue(stopped.founder_cancelled(run))
                self.assertIsNone(stopped.stop_cause(run))

    def test_a_transition_cancel_is_a_system_stop(self):
        run = _run(error="Website connection changed.", result={"cancellation_reason": "system_stop",
            "cancelled_by": "system", "cancellation_transition": "reset"})
        self.assertEqual(stopped.stop_cause(run)["transition"], "reset")

    def test_a_scheduler_run_is_never_a_system_stop(self):
        # Background work never restarts on its own or gives a founder notice (found by F14).
        system = {"cancellation_reason": "system_stop", "cancelled_by": "system", "cancellation_transition": "reset"}
        for run in (_run(error="Website connection changed.", result=system,
                         run_request={"request_source": "mlai_backend_scheduler"}),
                    _run(error="Website connection changed.", result={**system, "run_owner_v1": {
                        "identity": {"principal": {"kind": "scheduler"}}}})):
            with self.subTest(run=run.run_request or run.result):
                self.assertIsNone(stopped.stop_cause(run))
        self.assertIsNotNone(stopped.stop_cause(_run(error="Website connection changed.", result=system,
                                                     run_request={"request_source": "roo_slackbot"})))

    def test_operation_terminal_is_decided_by_its_raise_site(self):
        sites = {"website_operations.validate_operation": True, "setup_delivery_receiver.check_current_scope": True,
            "website_operations.cancel_operation_finished": False, "website_operations.cancel_operation_worker_finished": False,
            "website_operations.resume_completed": False, "": False}
        for site, system in sites.items():
            with self.subTest(site=site):
                rejection = {"code": "website_operation_terminal", **({"raise_site": site} if site else {})}
                run = _run("failed", result={"error_code": "website_operation_terminal", "callback_rejection": rejection})
                self.assertEqual(stopped.stop_cause(run) is not None, system)

    def test_a_worker_reported_terminal_finds_its_site_in_the_refusals(self):
        run = _run("failed", result={"error_code": "website_operation_terminal", "authority_refusals": [
            {"code": "website_operation_terminal", "raise_site": "website_operations.validate_operation"}]})
        self.assertEqual(stopped.stop_cause(run)["raise_site"], "website_operations.validate_operation")

    def test_every_terminal_raise_site_in_the_code_is_classified(self):
        import ast
        from pathlib import Path
        root = Path(__file__).resolve().parent
        found = set()
        for name in ("website_operations.py", "setup_delivery_receiver.py"):
            tree = ast.parse((root / name).read_text())
            wrapped = {id(node.args[0]) for node in ast.walk(tree)
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "raise_site"}
            for node in ast.walk(tree):
                if id(node) in wrapped:
                    continue
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") in {"WebsiteAuthorityError", "DeliveryScopeError", "_scope_error"}:
                    if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "website_operation_terminal":
                        site = next((kw.value.value for kw in node.keywords if kw.arg == "raise_site"), None)
                        found.add(site or f"{name}:{node.lineno}")
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "raise_site":
                    found.add(node.args[1].value)
        # Every raise site is stamped (no bare line numbers) and classified.
        stamped = {site for site in found if ":" not in site}
        self.assertEqual(found, stamped, found - stamped)
        self.assertEqual(stamped, set(stopped.TERMINAL_RAISE_SITES))


class SystemStoppedRunTests(TestCase):
    """The Tala Thrive fixture: a connected, verified site and a confirmed_topic run."""

    def setUp(self):
        self.built = make_history_free_org(domain="tala.example.test")
        self.connection = _connect(self.built, verified_sha=MERGED)
        self.connection.authorized_by = self.built.owner
        self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True}
        self.connection.save(update_fields=["authorized_by", "capabilities"])
        _next_js_target(self.connection)
        self.config = self.built.config
        self.config.refresh_from_db()

    def make_run(self, **fields):
        return ContentFactoryRun.objects.create(**{"run_id": "tala-run", "workflow": "confirmed_topic",
            "domain": self.built.organization.domain, "organization": self.built.organization,
            "github_repo": self.connection.github_repo, "run_request": contract_for(self.connection),
            "status": "running", "result": {"topic": "Synthetic topic"}, **fields})

    def test_a_system_cancel_on_a_connected_site_restarts_once(self):
        run = self.make_run()
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        run.refresh_from_db()
        self.assertEqual((run.status, run.result["cancellation_transition"]), ("cancelled", "reset"))
        self.connection.refresh_from_db()
        with patch.object(stopped, "_owner_context", return_value=SimpleNamespace()), \
                patch.object(stopped, "_restart", return_value="replacement-run") as restart:
            self.assertEqual(stopped.follow_up(run), "restarted")
            self.assertIsNone(stopped.follow_up(ContentFactoryRun.objects.get(pk=run.pk)))
        restart.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.result[stopped.FOLLOWUP_KEY]["restart_run_id"], "replacement-run")
        self.assertIsNone(stopped.notice_for(run))

    def test_a_system_cancel_that_cannot_restart_gets_one_notice_with_restart(self):
        run = self.make_run()
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        run.refresh_from_db()
        with patch.object(stopped, "_owner_context", return_value=None):
            self.assertEqual(stopped.follow_up(run), "notice")
        run.refresh_from_db()
        notice = stopped.notice_for(run)
        self.assertEqual(notice["action"], {"id": "restart", "label": "Restart"})
        self.assertTrue(stopped.restartable(run))
        # The notice is the run's draft card; the founder sees it once.
        from .vibe_marketing_views import _recent_article_drafts
        cards = [card for card in _recent_article_drafts(self.built.organization) if card["runId"] == run.run_id]
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["notice"]["kind"], "system_stopped")
        self.assertTrue(cards[0]["restartAvailable"])
        self.assertIsNone(stopped.follow_up(ContentFactoryRun.objects.get(pk=run.pk)))

    def test_clear_saved_setup_is_never_ours_to_fix(self):
        from django.utils import timezone
        from . import founder_journey_p2, registry_policy
        from .founder_setup_state import setup_state
        setup_run = self.make_run(run_id="tala-setup", workflow="article_system_setup")
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        setup_run.refresh_from_db()
        self.assertEqual((setup_run.status, setup_run.result["cancellation_transition"]), ("cancelled", "reset"))
        # B24 follows up article runs only; the setup simply starts again.
        self.assertIsNone(stopped.stop_cause(setup_run))
        state = setup_state(self.built.organization, company=self.built.company, user=self.built.owner)
        self.assertNotIn("fixing", [step["state"] for step in state["steps"]])
        with patch.object(registry_policy, "_policy_v2_for", return_value=True):
            self.assertIsNone(founder_journey_p2.run_decision(self.built.organization, setup_run, config=self.config,
                                                              user=self.built.owner, now=timezone.now()))

    def test_a_founder_cancel_gives_nothing(self):
        run = self.make_run(status="cancelled", result={"topic": "Synthetic topic", "cancellation_reason": "owner_cancelled",
            "cancelled_by": "owner"})
        self.assertIsNone(stopped.follow_up(run))
        self.assertEqual(stopped.run_system_stopped_run_sweep()["decided"], {})
        run.refresh_from_db()
        self.assertNotIn(stopped.FOLLOWUP_KEY, run.result)
        self.assertFalse(stopped.restartable(run))
        from .vibe_marketing_views import _recent_article_drafts
        self.assertFalse([card for card in _recent_article_drafts(self.built.organization) if card["runId"] == run.run_id])

    def test_a_run_a_revoke_stopped_is_never_restarted_on_its_own(self):
        run = self.make_run()
        transition_connection(self.config, action="revoke", expected=contract_for(self.connection), idempotency_key="revoke-1")
        run.refresh_from_db()
        with patch.object(stopped, "_restart", side_effect=AssertionError("revoke must not restart")):
            self.assertEqual(stopped.follow_up(run), "notice")

    def test_a_paused_site_waits_for_resume(self):
        run = self.make_run(status="failed", result={"topic": "Synthetic topic", "error_code": "website_publishing_paused",
            "callback_rejection": {"code": "website_publishing_paused"}})
        transition_connection(self.config, action="pause", expected=contract_for(self.connection), idempotency_key="pause-1")
        self.assertIsNone(stopped.follow_up(run))
        run.refresh_from_db()
        self.assertNotIn(stopped.FOLLOWUP_KEY, run.result)

    def test_the_sweep_follows_up_each_stop_once(self):
        run = self.make_run()
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        with patch.object(stopped, "_owner_context", return_value=None):
            self.assertEqual(stopped.run_system_stopped_run_sweep()["decided"], {"notice": 1})
            self.assertEqual(stopped.run_system_stopped_run_sweep()["decided"], {})
        run.refresh_from_db()
        self.assertEqual(run.result[stopped.FOLLOWUP_KEY]["decision"], "notice")

    def test_two_passes_never_both_restart_one_run(self):
        run = self.make_run()
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        stale = ContentFactoryRun.objects.get(pk=run.pk)
        calls = []

        def restart(current, context):
            # A second pass reading the same row before the first recorded anything.
            calls.append(current.run_id)
            self.assertIsNone(stopped.follow_up(stale))
            return "replacement-run"

        with patch.object(stopped, "_owner_context", return_value=SimpleNamespace()), \
                patch.object(stopped, "_restart", side_effect=restart):
            self.assertEqual(stopped.follow_up(ContentFactoryRun.objects.get(pk=run.pk)), "restarted")
        self.assertEqual(calls, ["tala-run"])
        run.refresh_from_db()
        self.assertEqual(run.result[stopped.FOLLOWUP_KEY]["decision"], "restarted")
        self.assertFalse(stopped.restartable(run))

    def test_a_run_the_founder_already_restarted_is_left_alone(self):
        run = self.make_run(status="failed", result={"topic": "Synthetic topic", "error_code": "website_connection_changed",
            "restart_child_run_id": "founder-replacement", "restart_receipt": {"state": "completed"}})
        with patch.object(stopped, "_restart", side_effect=AssertionError("restarted twice")):
            self.assertIsNone(stopped.follow_up(run))
            self.assertEqual(stopped.run_system_stopped_run_sweep()["decided"], {})

    def test_a_legacy_stop_with_no_known_transition_is_announced_not_restarted(self):
        run = self.make_run(status="cancelled", error="Website connection changed.")
        self.assertEqual(stopped.stop_cause(run)["transition"], "unknown")
        with patch.object(stopped, "_restart", side_effect=AssertionError("unknown stop restarted")):
            self.assertEqual(stopped.follow_up(run), "notice")

    def test_a_stop_older_than_the_restart_window_is_announced(self):
        from datetime import timedelta
        from django.utils import timezone
        run = self.make_run()
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        run.refresh_from_db()
        later = timezone.now() + stopped.RESTART_WINDOW + timedelta(hours=1)
        with patch.object(stopped, "_owner_context", return_value=SimpleNamespace()), \
                patch.object(stopped, "_restart", side_effect=AssertionError("old stop restarted")):
            self.assertEqual(stopped.follow_up(run, now=later), "notice")

    def test_a_revoke_after_the_stop_blocks_the_automatic_restart(self):
        run = self.make_run()
        transition_connection(self.config, action="reset", expected=contract_for(self.connection), idempotency_key="reset-1")
        self.connection.refresh_from_db()
        transition_connection(self.config, action="revoke", expected=contract_for(self.connection), idempotency_key="revoke-1")
        self.connection.refresh_from_db()
        self.connection.state = "connected"
        self.connection.save(update_fields=["state"])
        run.refresh_from_db()
        with patch.object(stopped, "_owner_context", return_value=SimpleNamespace()), \
                patch.object(stopped, "_restart", side_effect=AssertionError("consent withdrawn since")):
            self.assertEqual(stopped.follow_up(run), "notice")

    def test_ordinary_failures_cannot_crowd_a_stop_out_of_the_sweep(self):
        for index in range(stopped.SWEEP_LIMIT * 5):
            self.make_run(run_id=f"plain-{index}", status="failed", result={"error_code": "model_timeout"})
        run = self.make_run(run_id="fenced", status="failed", result={"topic": "Synthetic topic",
            "error_code": "website_connection_changed", "callback_rejection": {"code": "website_connection_changed"}})
        from datetime import timedelta
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=run.updated_at - timedelta(minutes=5))
        with patch.object(stopped, "_owner_context", return_value=None):
            self.assertEqual(stopped.run_system_stopped_run_sweep()["decided"], {"notice": 1})
        run.refresh_from_db()
        self.assertEqual(run.result[stopped.FOLLOWUP_KEY]["decision"], "notice")
