from django.test import SimpleTestCase

from core.management.commands.run_scheduled_discovery import (
    _office_manager_scheduler_failed,
)


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
