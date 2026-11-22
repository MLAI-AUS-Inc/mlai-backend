import importlib.util
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("scheduler_tick", Path(__file__).with_name("scheduler-tick.py"))
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class SchedulerTickTests(unittest.TestCase):
    def setUp(self):
        self.controller = Mock()
        self.controller.locked.return_value = nullcontext(True)
        self.controller.journal.exists.return_value = False
        self.controller.paused.exists.return_value = False
        self.controller.peer_recovery_pending.return_value = False

    def test_tick_uses_verified_running_image_and_bounded_command(self):
        self.assertEqual(scheduler.tick(self.controller), "completed")
        self.controller.running.assert_called_once_with(self.controller.values.return_value)
        args = self.controller.run.call_args.args[0]
        self.assertIn(scheduler.CONTAINER, args)
        self.assertIn("timeout", args)
        self.assertIn("JOBS_SCHEDULER_ENABLED=false", args)
        self.assertIn('call_command("run_scheduled_discovery")', args[-1])

    def test_busy_lock_or_paused_recovering_deploy_skips_tick(self):
        self.controller.locked.return_value = nullcontext(False)
        scheduler.tick(self.controller)
        self.controller.locked.return_value = nullcontext(True)
        for gate in (self.controller.journal.exists, self.controller.paused.exists,
                     self.controller.peer_recovery_pending):
            gate.return_value = True
            scheduler.tick(self.controller)
            gate.return_value = False
        self.controller.run.assert_not_called()

    def test_image_or_network_failure_prevents_tick(self):
        self.controller.running.side_effect = RuntimeError("runtime drift")
        with self.assertRaises(RuntimeError):
            scheduler.tick(self.controller)
        self.controller.run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
