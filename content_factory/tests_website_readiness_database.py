"""Website readiness record against a test database: record(), the push hook, founder and staff reads."""
from django.test import TestCase, override_settings

from . import website_readiness as readiness
from .tests_website_source_carry import PUSHED, CarriedSite, _compare
from .website_models import WebsiteCheckEvent, WebsiteReadiness


@override_settings(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True,
                   ARTICLES_READINESS_SHADOW_ENABLED=True)
class ReadinessRecordTests(CarriedSite, TestCase):
    def test_record_creates_the_row_and_appends_one_event_per_write(self):
        first = readiness.record(self.connection, trigger="backfill")
        self.assertEqual((first["state"], first["canDraft"], first["canPublish"], first["version"]), ("ready", True, True, 1))
        self.assertEqual(first["legacy"]["state"], "ready")
        self.assertEqual(first["pinnedContract"]["footprintSource"], "setup_cache")
        self.assertIn("src/app/blog/page.tsx", first["pinnedContract"]["footprint"])
        second = readiness.record(self.connection, trigger="manual", state="checking", result="skipped")
        self.assertEqual((second["state"], second["canPublish"], second["version"]), ("checking", False, 2))
        events = list(WebsiteCheckEvent.objects.filter(connection=self.connection).order_by("created_at", "pk"))
        self.assertEqual([(event.trigger, event.state_before, event.state_after) for event in events],
                         [("backfill", "", "ready"), ("manual", "ready", "checking")])
        with self.assertRaises(ValueError):
            events[0].save()

    def test_a_push_that_touches_the_setup_files_is_recorded_after_commit(self):
        readiness.record(self.connection, trigger="backfill")
        with self.captureOnCommitCallbacks(execute=True):
            self.push(_compare("src/app/blog/page.tsx"))
        row = WebsiteReadiness.objects.get(connection=self.connection)
        self.assertEqual(row.state, "checking")
        self.assertTrue(row.can_draft)
        self.assertFalse(row.can_publish)
        self.assertEqual(row.recheck["sourceSha"], PUSHED)
        event = WebsiteCheckEvent.objects.filter(connection=self.connection).order_by("-created_at", "-pk").first()
        self.assertEqual((event.trigger, event.source_sha, event.state_before, event.state_after),
                         ("push_footprint", PUSHED, "ready", "checking"))
        self.assertEqual(event.changed_files, ["src/app/blog/page.tsx"])

    def test_a_carried_push_records_no_footprint_change(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.push(_compare("src/app/page.tsx"))
        event = WebsiteCheckEvent.objects.get(connection=self.connection)
        self.assertEqual((event.trigger, event.state_after), ("push_no_footprint", "ready"))

    def test_shadow_off_writes_nothing(self):
        with override_settings(ARTICLES_READINESS_SHADOW_ENABLED=False), self.captureOnCommitCallbacks(execute=True):
            self.push(_compare("src/app/page.tsx"))
        self.assertFalse(WebsiteReadiness.objects.filter(connection=self.connection).exists())

    def test_founder_and_staff_reads(self):
        readiness.record(self.connection, trigger="backfill")
        self.config.refresh_from_db()
        payload = readiness.founder_payload(self.config)
        self.assertEqual(payload["state"], "ready")
        self.assertFalse(payload["readsEnabled"])
        self.assertEqual(readiness.readiness_fleet()["ready"], 1)
        self.assertEqual(len(readiness.check_events(self.connection)), 1)
        self.assertTrue(readiness.staff_readiness(self.connection)["currentGeneration"])
        self.assertIsNotNone(readiness.stored(self.connection))
