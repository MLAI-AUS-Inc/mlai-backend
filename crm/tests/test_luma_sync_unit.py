"""DB-free checks that a Luma guest sync pass stays inside the scheduler tick."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.cache.backends.locmem import LocMemCache
from django.test import SimpleTestCase, override_settings

from crm.luma_sync import (
    PASS_SECONDS,
    SYNC_RESUME_KEY,
    SYNC_THROTTLE_KEY,
    run_luma_guest_sync_scheduler,
    sync_recent_luma_guests,
    sync_event_catalog,
    sync_event_guests,
    events_due_for_guest_sync,
)
from integrations.services.luma import LumaAPIError, LumaAttendeeReportService, LumaConfigurationError


def events(*event_ids):
    return [SimpleNamespace(luma_event_id=event_id, raw_payload={"platform": "luma", "access": "manage"}) for event_id in event_ids]


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
            crm_event.objects.filter.return_value = [item if hasattr(item, "raw_payload") else events(item)[0] for item in stored]
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

    def test_resumed_view_only_and_external_events_do_not_read_guests(self):
        managed, listed, external = events("evt-managed", "evt-listed", "ext-listed")
        listed.raw_payload["access"] = "view"
        external.raw_payload["platform"] = "external"
        summary, read, _, _ = self.run_pass(
            event_ids=[listed.luma_event_id, external.luma_event_id, managed.luma_event_id],
            stored=(listed, external, managed),
        )
        self.assertEqual(read, [managed.luma_event_id])
        self.assertEqual(summary["events_due"], 1)

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
        audit_patcher = patch("crm.luma_sync.CrmAuditEvent.objects.create")
        self.audit = audit_patcher.start()
        self.addCleanup(audit_patcher.stop)

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
        self.assertTrue(self.audit.call_args.kwargs["detail"]["source_failed"])


class CatalogCoverageTests(SimpleTestCase):
    def test_direct_guest_sync_rejects_listed_event_before_any_request(self):
        event = events("evt-listed")[0]
        event.raw_payload["access"] = "view"
        service = Mock()
        with self.assertRaises(LumaConfigurationError):
            sync_event_guests(service, event)
        service.list_guests.assert_not_called()

    def test_due_query_excludes_listed_events_before_applying_limit(self):
        from django.utils import timezone
        with patch("crm.luma_sync.CrmEvent") as model:
            events_due_for_guest_sync(now=timezone.now(), recent_days=30, limit=5)
        filters = model.objects.filter.call_args.kwargs
        self.assertEqual(filters, {"raw_payload__platform": "luma", "raw_payload__access": "manage"})

    def test_catalog_paginates_all_access_levels_without_guest_requests(self):
        pages = [
            {"entries": [{"id": "evt-managed", "platform": "luma", "access": "manage"}], "has_more": True, "next_cursor": "next"},
            {"entries": [{"id": "evt-listed", "platform": "luma", "access": "view"}, {"id": "ext-listed", "platform": "external"}], "has_more": False},
        ]
        service = LumaAttendeeReportService(api_key="synthetic")
        with patch.object(service, "_get", side_effect=pages) as get, patch("crm.luma_sync.CrmEvent") as model:
            model.objects.filter.return_value = []
            sync_event_catalog(service)
            created = model.objects.bulk_create.call_args.args[0]
        self.assertEqual(len(created), 3)
        self.assertEqual([call.kwargs["luma_event_id"] for call in model.call_args_list], ["evt-managed", "evt-listed", "ext-listed"])
        for call in get.call_args_list:
            self.assertEqual(call.args[0], "/v1/calendars/events/list")
            self.assertEqual(call.kwargs["params"]["access"], ["manage", "view"])
            self.assertEqual(call.kwargs["params"]["platforms"], ["luma", "external"])
            self.assertEqual(call.kwargs["params"]["status"], "approved")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["pagination_cursor"], "next")

    def test_incomplete_calendar_scan_makes_no_database_writes(self):
        for page in (
            {"entries": [], "has_more": True},
            {"entries": [], "has_more": True, "next_cursor": "repeated"},
            {"entries": "invalid", "has_more": False},
            {"entries": [None], "has_more": False},
        ):
            service = LumaAttendeeReportService(api_key="synthetic")
            with patch.object(service, "_get", return_value=page), patch("crm.luma_sync.CrmEvent") as model:
                with self.assertRaises(LumaAPIError):
                    sync_event_catalog(service)
                model.objects.bulk_create.assert_not_called()
                model.objects.bulk_update.assert_not_called()
