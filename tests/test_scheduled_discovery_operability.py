from io import StringIO
from unittest.mock import Mock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

class ScheduledDiscoveryHeartbeatTests(SimpleTestCase):
    def test_shared_runner_failures_preserve_heartbeat_checks(self):
        from core.management.commands import run_scheduled_discovery as scheduler
        from core.scheduling import run_runners

        for jobs_failed in (False, True):
            with self.subTest(jobs_failed=jobs_failed):
                called = []

                def run_isolated(runners):
                    def result_for(name):
                        called.append(name)
                        if name == "jobs":
                            return {"status": "failed" if jobs_failed else "queued"}
                        return {"status": "skipped"}

                    return run_runners([
                        (name, lambda name=name: result_for(name)) for name, _ in runners
                    ])

                with (
                    patch.object(scheduler, "run_runners", side_effect=run_isolated),
                    patch.object(scheduler.ScheduledDiscoveryHeartbeat, "objects") as heartbeats,
                ):
                    heartbeat = Mock()
                    heartbeats.get_or_create.return_value = (heartbeat, False)
                    command = scheduler.Command(stdout=StringIO())
                    if jobs_failed:
                        with self.assertRaisesRegex(CommandError, "jobs"):
                            command.handle()
                    else:
                        command.handle()

                heartbeat.save.assert_called_once()
                self.assertIn("coding_reconciliation", called)
                self.assertNotIn("office_manager", called)
                self.assertEqual(len(called), len(set(called)))
                update = heartbeats.filter.return_value.update.call_args.kwargs
                if jobs_failed:
                    self.assertIn("last_failed_at", update)
                    self.assertNotIn("last_succeeded_at", update)
                else:
                    self.assertIn("last_succeeded_at", update)
                    self.assertEqual(update["last_error"], "")
