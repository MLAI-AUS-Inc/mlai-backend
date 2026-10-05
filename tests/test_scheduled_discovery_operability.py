from io import StringIO
from unittest.mock import Mock, patch

from django.core.management.base import CommandError
from django.test import SimpleTestCase

from core.management.commands.run_scheduled_discovery import (
    _office_manager_scheduler_failed,
)


class ScheduledDiscoveryHeartbeatTests(SimpleTestCase):
    def test_shared_runner_failures_preserve_office_manager_and_heartbeat_checks(self):
        from core.management.commands import run_scheduled_discovery as scheduler
        from core.scheduling import run_runners

        for jobs_failed, office_failed in ((False, False), (True, False), (False, True)):
            with self.subTest(jobs_failed=jobs_failed, office_failed=office_failed):
                called = []

                def run_isolated(runners):
                    def result_for(name):
                        called.append(name)
                        if name == "jobs":
                            return {"status": "failed" if jobs_failed else "queued"}
                        if name == "office_manager":
                            return {"status": "claimed", "winner_dm_sent": not office_failed}
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
                    if jobs_failed or office_failed:
                        expected = "jobs" if jobs_failed else "office_manager"
                        with self.assertRaisesRegex(CommandError, expected):
                            command.handle()
                    else:
                        command.handle()

                heartbeat.save.assert_called_once()
                self.assertIn("coding_reconciliation", called)
                self.assertIn("office_manager", called)
                self.assertEqual(len(called), len(set(called)))
                update = heartbeats.filter.return_value.update.call_args.kwargs
                if jobs_failed or office_failed:
                    self.assertIn("last_failed_at", update)
                    self.assertNotIn("last_succeeded_at", update)
                else:
                    self.assertIn("last_succeeded_at", update)
                    self.assertEqual(update["last_error"], "")


class OfficeManagerSchedulerFailureClassificationTests(SimpleTestCase):
    def test_business_states_and_unrelated_false_values_are_not_failures(self):
        for result in (
            {"status": "open"},
            {"status": "claimed"},
            {"status": "closed"},
            {"status": "skipped", "reason": "weekday_not_configured"},
            {"status": "preview"},
            {"status": "closed", "capacity_available": False},
            {"status": "claimed", "delivery_statuses": {"winner_dm": "sent"}},
        ):
            with self.subTest(result=result):
                self.assertFalse(_office_manager_scheduler_failed(result))

    def test_explicit_false_delivery_results_are_failures(self):
        for key in (
            "announcement_sent",
            "message_updated",
            "winner_channel_announcement_sent",
            "winner_dm_sent",
            "end_of_day_reminder_sent",
        ):
            with self.subTest(key=key):
                self.assertTrue(
                    _office_manager_scheduler_failed(
                        {"status": "claimed", key: False}
                    )
                )

        self.assertTrue(
            _office_manager_scheduler_failed(
                {"status": "skipped", "winner_channel_retractions": [True, False]}
            )
        )

    def test_terminal_or_exhausted_delivery_state_is_a_failure(self):
        for delivery_state in (
            "failed",
            "terminal_failure",
            "permanent_failure",
            "exhausted",
            "dead_letter",
        ):
            with self.subTest(delivery_state=delivery_state):
                self.assertTrue(
                    _office_manager_scheduler_failed(
                        {
                            "status": "claimed",
                            "delivery_statuses": {
                                "winner_channel_retraction_status": delivery_state
                            },
                        }
                    )
                )

    def test_nonempty_delivery_failure_collection_is_a_failure(self):
        self.assertTrue(
            _office_manager_scheduler_failed(
                {
                    "status": "claimed",
                    "delivery_failures": ["winner_dm"],
                }
            )
        )

    def test_malformed_scheduler_result_fails_closed(self):
        for result in (None, [], "claimed"):
            with self.subTest(result=result):
                self.assertTrue(_office_manager_scheduler_failed(result))
