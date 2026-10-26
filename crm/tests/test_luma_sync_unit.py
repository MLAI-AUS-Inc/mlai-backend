"""DB-free checks that a Luma guest sync pass stays inside the scheduler tick."""

from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache.backends.locmem import LocMemCache
from django.test import SimpleTestCase, override_settings

from crm.luma_sync import (
    PASS_SECONDS,
    SYNC_RESUME_KEY,
    SYNC_THROTTLE_KEY,
    run_luma_guest_sync_scheduler,
    sync_recent_luma_guests,
)
from integrations.services.luma import LumaAPIError


def events(*event_ids):
    return [SimpleNamespace(luma_event_id=event_id) for event_id in event_ids]


class PassBudgetTests(SimpleTestCase):
    """Each event takes 25 fake seconds against a 60-second deadline."""

    def run_pass(self, due=(), *, errors=None, stored=(), **options):
        clock = [0.0]
        read = []

        def sync_event_guests(service, event, **kwargs):
            read.append(event.luma_event_id)
            clock[0] += 25
            if event.luma_event_id in (errors or {}):
                raise errors[event.luma_event_id]
            return {"guests": 1}

        with patch("crm.luma_sync.sync_event_catalog", return_value=[]) as catalog, patch(
            "crm.luma_sync.events_due_for_guest_sync", return_value=list(due)
        ) as due_query, patch("crm.luma_sync.CrmEvent") as crm_event, patch(
            "crm.luma_sync.sync_event_guests", side_effect=sync_event_guests
        ), patch("crm.luma_sync.CrmAuditEvent"):
            crm_event.objects.filter.return_value = events(*stored)
            summary = sync_recent_luma_guests(
                object(), deadline=60, clock=lambda: clock[0], **options
            )
        return summary, read, catalog, due_query

    def test_no_event_starts_after_the_deadline(self):
        summary, read, _, _ = self.run_pass(events("evt-1", "evt-2", "evt-3", "evt-4", "evt-5"))

        # evt-3 starts at 50 seconds, inside the budget, and is allowed to finish.
        self.assertEqual(read, ["evt-1", "evt-2", "evt-3"])
        self.assertEqual(summary["events_synced"], 3)
        self.assertEqual(summary["events_left"], ["evt-4", "evt-5"])

    def test_a_pass_inside_its_budget_leaves_nothing(self):
        summary, read, catalog, _ = self.run_pass(events("evt-1", "evt-2"))

        self.assertEqual(read, ["evt-1", "evt-2"])
        self.assertEqual(summary["events_left"], [])
        self.assertEqual(summary["events_in_catalog"], 0)
        catalog.assert_called_once()

    def test_a_resumed_pass_reads_only_the_events_left_in_their_order(self):
        summary, read, catalog, due_query = self.run_pass(
            event_ids=["evt-4", "evt-gone", "evt-2"], stored=("evt-2", "evt-4")
        )

        self.assertEqual(read, ["evt-4", "evt-2"])
        self.assertIsNone(summary["events_in_catalog"])
        self.assertEqual(summary["events_left"], [])
        catalog.assert_not_called()
        due_query.assert_not_called()

    def test_a_rejected_key_stops_the_pass(self):
        for status_code in (401, 403):
            with self.subTest(status_code=status_code), self.assertRaises(LumaAPIError):
                self.run_pass(
                    events("evt-1", "evt-2"),
                    errors={"evt-1": LumaAPIError("no", status_code=status_code)},
                )

    def test_an_outage_fails_only_that_event(self):
        summary, read, _, _ = self.run_pass(
            events("evt-1", "evt-2"), errors={"evt-1": LumaAPIError("down", status_code=503)}
        )

        self.assertEqual(read, ["evt-1", "evt-2"])
        self.assertEqual(summary["failed_events"], ["evt-1"])
        self.assertEqual(summary["events_left"], [])

    def test_a_rate_limit_ends_the_pass_without_a_hand_over(self):
        summary, read, _, _ = self.run_pass(
            events("evt-1", "evt-2"), errors={"evt-1": LumaAPIError("slow", status_code=429)}
        )

        self.assertEqual(read, ["evt-1"])
        self.assertTrue(summary["rate_limited"])
        self.assertEqual(summary["events_left"], [])


@override_settings(CRM_LUMA_SYNC_ENABLED=True, LUMA_API_KEY="key", CRM_LUMA_SYNC_INTERVAL_SECONDS=1800)
class RunnerHandOverTests(SimpleTestCase):
    def setUp(self):
        self.cache = LocMemCache("crm-luma-sync-unit", {})
        patcher = patch("crm.luma_sync.cache", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.cache.clear)

    @staticmethod
    def summary(*, left=(), failed=()):
        return {"failed_events": list(failed), "rate_limited": False, "events_left": list(left)}

    def test_out_of_time_hands_the_rest_to_the_next_tick(self):
        passes = [self.summary(left=["evt-3", "evt-4"]), self.summary()]
        with patch("crm.luma_sync.sync_recent_luma_guests", side_effect=passes) as sync:
            first = run_luma_guest_sync_scheduler(service=object(), clock=lambda: 100.0)
            self.assertEqual(self.cache.get(SYNC_RESUME_KEY), ["evt-3", "evt-4"])
            self.assertIsNone(self.cache.get(SYNC_THROTTLE_KEY))
            second = run_luma_guest_sync_scheduler(service=object(), clock=lambda: 300.0)
            third = run_luma_guest_sync_scheduler(service=object())

        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["events_left"], ["evt-3", "evt-4"])
        self.assertEqual(second["status"], "completed")
        self.assertEqual(third, {"status": "skipped", "reason": "throttled"})
        opening, resumed = (call.kwargs for call in sync.call_args_list)
        self.assertIsNone(opening["event_ids"])
        self.assertEqual(opening["deadline"], 100.0 + PASS_SECONDS)
        self.assertEqual(resumed["event_ids"], ["evt-3", "evt-4"])
        self.assertEqual(resumed["deadline"], 300.0 + PASS_SECONDS)
        self.assertIsNone(self.cache.get(SYNC_RESUME_KEY))

    def test_failed_events_still_hand_over_the_events_not_reached(self):
        with patch(
            "crm.luma_sync.sync_recent_luma_guests",
            return_value=self.summary(left=["evt-3"], failed=["evt-1"]),
        ):
            result = run_luma_guest_sync_scheduler(service=object())

        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.cache.get(SYNC_RESUME_KEY), ["evt-3"])
        self.assertIsNone(self.cache.get(SYNC_THROTTLE_KEY))

    def test_a_rejected_key_while_resuming_fails_and_drops_the_hand_over(self):
        self.cache.set(SYNC_RESUME_KEY, ["evt-3"])
        with patch(
            "crm.luma_sync.sync_recent_luma_guests",
            side_effect=LumaAPIError("no", status_code=403),
        ) as sync:
            result = run_luma_guest_sync_scheduler(service=object())
            again = run_luma_guest_sync_scheduler(service=object())

        self.assertEqual(sync.call_args.kwargs["event_ids"], ["evt-3"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "luma_key_rejected")
        self.assertIsNone(self.cache.get(SYNC_RESUME_KEY))
        self.assertEqual(again["reason"], "throttled")
