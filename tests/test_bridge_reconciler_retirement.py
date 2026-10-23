"""A legacy worker retires only after current-process durable health succeeds."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retirement", ROOT / "scripts/retire-bridge-reconciler.py")
retirement = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retirement)


class ReconcilerRetirementTests(unittest.TestCase):
    def test_invalid_stale_disabled_or_failed_health_never_stops_old_worker(self):
        for output in ('not json', '{}', '{"enabled":false,"stale_lanes":[]}',
                       '{"enabled":true,"stale_lanes":["history"]}'):
            with self.subTest(output=output), patch.object(retirement, "command", return_value=output) as command:
                with self.assertRaises(ValueError):
                    retirement.retire(attempts=1)
                self.assertEqual(command.call_count, 1)
                self.assertIn("--local-worker", command.call_args.args[0])
        with patch.object(retirement, "command", side_effect=ValueError("private-detail")) as command:
            with self.assertRaises(ValueError):
                retirement.retire(attempts=1)
            self.assertEqual(command.call_count, 1)

    def test_recovered_worker_retires_only_the_reconciler(self):
        with patch.object(retirement, "time") as clock, patch.object(retirement, "command", side_effect=[
                '{"enabled":true,"stale_lanes":["history"]}',
                '{"enabled":true,"stale_lanes":[]}', '', '']) as command:
            clock.monotonic.return_value = 0
            retirement.retire(attempts=2)
        clock.sleep.assert_called_once_with(3)
        self.assertEqual(command.call_args_list[-2].args[0], ["docker", "compose", "stop", "--timeout", "30", "bridge-reconciler"])
        self.assertEqual(command.call_args_list[-1].args[0], ["docker", "compose", "rm", "--force", "bridge-reconciler"])

    def test_provider_timeouts_cannot_extend_the_readiness_deadline(self):
        with patch.object(retirement.time, "monotonic", side_effect=[0, 181]), patch.object(retirement, "command") as command:
            with self.assertRaisesRegex(ValueError, "deadline exceeded"):
                retirement.retire()
            command.assert_not_called()

    def test_stop_failure_does_not_remove_container(self):
        with patch.object(retirement, "command", side_effect=[
                '{"enabled":true,"stale_lanes":[]}', ValueError("stop failed")]) as command:
            with self.assertRaises(ValueError):
                retirement.retire(attempts=1)
            self.assertEqual(command.call_count, 2)

    def test_commands_preserve_stdin_and_hide_provider_output(self):
        with patch.object(retirement.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, 'secret', 'secret')) as run:
            with self.assertRaisesRegex(ValueError, '^retirement command failed$'):
                retirement.command(['docker', 'compose', 'stop', 'bridge-reconciler'])
        self.assertEqual(run.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['timeout'], 30)

    def test_retired_service_stays_in_rollback_inventory_but_not_normal_startup(self):
        result = subprocess.check_output(['bash', '-c', 'source scripts/runtime-services.sh; printf "start:%s\\n" "${bridge_runtime_services[@]}"; printf "all:%s\\n" "${all_runtime_writer_services[@]}"'], cwd=ROOT, text=True)
        self.assertNotIn('start:bridge-reconciler', result)
        self.assertIn('all:bridge-reconciler', result)
        deploy = (ROOT / 'deploy.sh').read_text()
        self.assertLess(deploy.index('python3 scripts/retire-bridge-reconciler.py'), deploy.index('trap - ERR EXIT'))

if __name__ == '__main__':
    unittest.main()
