from io import StringIO
from unittest.mock import Mock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

class ScheduledDiscoveryHeartbeatTests(SimpleTestCase):
    @override_settings(MENTOR_INSTANT_BOOKING_ENABLED=False)
    def test_mentor_recovery_runner_has_valid_status_when_admission_is_disabled(self):
        from core.management.commands import run_scheduled_discovery as scheduler
        from core.scheduling import run_runners

        def run_mentor(runners):
            names = [name for name, _ in runners]
            self.assertEqual(names.count("mentor_booking_operations"), 1)
            self.assertEqual(
                names.index("self_heal_sweeps"),
                names.index("article_dispatch_outbox") + 1,
            )
            self.assertGreater(
                names.index("mentor_booking_operations"), names.index("self_heal_sweeps")
            )
            results, failures = run_runners([
                (name, runner) for name, runner in runners
                if name == "mentor_booking_operations"
            ])
            self.assertEqual(failures, [])
            self.assertEqual(results["mentor_booking_operations"], {
                "status": "completed", "processed": 2,
            })
            return results, failures

        with (
            patch.object(scheduler, "run_runners", side_effect=run_mentor),
            patch.object(scheduler, "process_due_bookings", return_value={"processed": 2}) as process,
            patch.object(scheduler.ScheduledDiscoveryHeartbeat, "objects") as heartbeats,
        ):
            heartbeats.get_or_create.return_value = (Mock(), False)
            scheduler.Command(stdout=StringIO()).handle()

        process.assert_called_once_with()
        update = heartbeats.filter.return_value.update.call_args.kwargs
        self.assertIn("last_succeeded_at", update)
        self.assertEqual(update["last_error"], "")

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
