"""IF-P1-21: the founder-journey minute sweeps run only while V2 is on, and never stop the scheduler."""
import sys
import types
from io import StringIO
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from . import founder_journey_sweeps as sweeps


class _FakeModules:
    """Install throwaway sweep modules under sys.modules for one test."""

    def __init__(self, **modules):
        self.modules = modules

    def __enter__(self):
        for name, functions in self.modules.items():
            module = types.ModuleType(name)
            for attribute, function in functions.items():
                setattr(module, attribute, function)
            sys.modules[name] = module
        return self

    def __exit__(self, *exc):
        for name in self.modules:
            sys.modules.pop(name, None)


class RunnerTests(SimpleTestCase):
    def test_contract_entries(self):
        self.assertEqual(sweeps.SWEEPS, (
            "content_factory.startup_first_profile:sweep",
            "content_factory.founder_notification_outbox:enqueue_ready_drafts",
            "content_factory.founder_notification_outbox:drain",
        ))

    def test_a_missing_module_is_skipped_silently(self):
        with self.assertNoLogs(sweeps.logger, "WARNING"):
            result = sweeps.run_founder_journey_sweeps(sweeps=(
                "content_factory.p1_not_merged_yet:sweep", "content_factory.founder_journey_sweeps:not_there", "bad"))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(set(result["sweeps"].values()), {"missing"})
        self.assertEqual(result["errors"], 0)

    def test_a_raising_sweep_logs_a_warning_and_the_next_still_runs(self):
        calls = []

        def boom(now):
            calls.append("boom")
            raise RuntimeError("synthetic")

        def fine(now):
            calls.append(("fine", now))
            return {"status": "sent", "sent": 1}

        with _FakeModules(p1_fake_a={"boom": boom}, p1_fake_b={"fine": fine}):
            with self.assertLogs(sweeps.logger, "WARNING") as logs:
                result = sweeps.run_founder_journey_sweeps(now="tick", sweeps=("p1_fake_a:boom", "p1_fake_b:fine"))
        self.assertEqual(calls, ["boom", ("fine", "tick")])
        self.assertEqual(result["sweeps"], {"p1_fake_a:boom": "error", "p1_fake_b:fine": "sent"})
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["status"], "completed")
        self.assertIn("founder_journey_sweep_failed sweep=p1_fake_a:boom error=RuntimeError", logs.output[0])
        self.assertNotIn("synthetic", logs.output[0].splitlines()[0])

    def test_scheduler_entry_runs_only_while_v2_is_on(self):
        with patch.object(sweeps, "run_founder_journey_sweeps", return_value={"status": "completed"}) as run:
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
                self.assertEqual(sweeps.scheduled_founder_journey_sweeps(), {"status": "disabled"})
            run.assert_not_called()
            with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True):
                self.assertEqual(sweeps.scheduled_founder_journey_sweeps(), {"status": "completed"})
            run.assert_called_once_with()


class SchedulerWiringTests(SimpleTestCase):
    def _tick(self):
        from core.management.commands import run_scheduled_discovery as scheduler
        from core.scheduling import run_runners

        seen = {}

        def isolated(runners):
            def result_for(name, runner):
                if name == "founder_journey_sweeps":
                    seen["result"] = runner()
                    return seen["result"]
                return {"status": "skipped"}
            seen["names"] = [name for name, _ in runners]
            return run_runners([(name, lambda name=name, runner=runner: result_for(name, runner))
                                for name, runner in runners])

        with (
            patch.object(scheduler, "run_runners", side_effect=isolated),
            patch.object(scheduler.ScheduledDiscoveryHeartbeat, "objects") as heartbeats,
            patch.object(sweeps, "run_founder_journey_sweeps", return_value={"status": "completed", "errors": 0}) as run,
        ):
            heartbeats.get_or_create.return_value = (Mock(), False)
            scheduler.Command(stdout=StringIO()).handle()
        return seen, run

    def test_run_scheduled_discovery_calls_the_runner_only_when_v2_is_on(self):
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=False):
            seen, run = self._tick()
        self.assertEqual(seen["names"].count("founder_journey_sweeps"), 1)
        self.assertEqual(seen["result"], {"status": "disabled"})
        run.assert_not_called()
        with override_settings(MY_STARTUP_FOUNDER_JOURNEY_V2_ENABLED=True):
            seen, run = self._tick()
        self.assertEqual(seen["result"], {"status": "completed", "errors": 0})
        run.assert_called_once_with()
