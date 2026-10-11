from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from django.core.management.base import CommandError

from core.management.commands.run_periodic_command import Command


class LoopComplete(Exception):
    pass


class PeriodicCommandTests(unittest.TestCase):
    def command(self):
        return Command(stdout=StringIO(), stderr=StringIO())

    def test_success_writes_health_only_after_a_successful_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "health"
            marker.touch()

            def tick(*args, **kwargs):
                self.assertFalse(marker.exists(), "stale health must be removed before work")

            def sleep(interval):
                self.assertTrue(marker.exists())
                self.assertEqual(interval, 60)
                raise LoopComplete

            with patch("core.management.commands.run_periodic_command.call_command", side_effect=tick), \
                 patch("core.management.commands.run_periodic_command.close_old_connections"), \
                 patch("core.management.commands.run_periodic_command.time.sleep", side_effect=sleep):
                with self.assertRaises(LoopComplete):
                    self.command().handle(command_name="run_scheduled_discovery", poll_seconds=60,
                                          health_file=str(marker), continue_on_error=False)

    def test_required_failure_removes_health_and_exits_without_another_tick(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "health"

            def tick(*args, **kwargs):
                marker.touch()
                raise CommandError("required runner failed")

            with patch("core.management.commands.run_periodic_command.call_command", side_effect=tick), \
                 patch("core.management.commands.run_periodic_command.close_old_connections"), \
                 patch("core.management.commands.run_periodic_command.time.sleep") as sleep:
                with self.assertRaisesRegex(CommandError, "required runner failed"):
                    self.command().handle(command_name="run_scheduled_discovery", poll_seconds=60,
                                          health_file=str(marker), continue_on_error=False)
                self.assertFalse(marker.exists())
                sleep.assert_not_called()

    def test_memory_scheduler_retries_after_failure_in_the_same_process(self):
        sleeps = []

        def sleep(interval):
            sleeps.append(interval)
            if len(sleeps) == 2:
                raise LoopComplete

        with patch("core.management.commands.run_periodic_command.call_command",
                   side_effect=[CommandError("temporary error"), None]) as tick, \
             patch("core.management.commands.run_periodic_command.close_old_connections"), \
             patch("core.management.commands.run_periodic_command.logger"), \
             patch("core.management.commands.run_periodic_command.time.sleep", side_effect=sleep):
            with self.assertRaises(LoopComplete):
                self.command().handle(command_name="schedule_memory_work", poll_seconds=30,
                                      health_file=None, continue_on_error=True)
            self.assertEqual(tick.call_count, 2)
            self.assertEqual(sleeps, [30, 30])

    def test_nonpositive_poll_interval_never_runs_work(self):
        for interval in (0, -1):
            with self.subTest(interval=interval), \
                 patch("core.management.commands.run_periodic_command.call_command") as tick:
                with self.assertRaises(CommandError):
                    self.command().handle(command_name="schedule_memory_work", poll_seconds=interval,
                                          health_file=None, continue_on_error=True)
                tick.assert_not_called()
