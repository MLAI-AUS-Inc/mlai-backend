"""Rating refresh safety with synthetic observations and no database or network."""

import ast
import inspect
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from crm import engagement_refresh as refresh
from crm.engagement import RESERVED_ATTRIBUTE_KEYS, marketing_review_hold
from crm.engagement_scoring import ActivityEvidence, SOURCES, VERSION

NOW = datetime(2026, 11, 1, 19, tzinfo=timezone.utc)  # 06:00 Melbourne, Nov 2.


def coverage(**overrides):
    result = {source: {"status": "available", "expected": True} for source in SOURCES}
    result.update(overrides)
    return result


def previous_rating(**overrides):
    result = {
        "stars": 5, "score": 75, "version": "previous-reviewed-version",
        "as_of": "2026-11-01", "reason": "Previously observed activity",
        "coverage": {"provisional": True, "summary": "Previous source coverage"},
    }
    result.update(overrides)
    return result


def apply(attributes, *, sources=None, observations=None):
    if observations is None:
        observations = [ActivityEvidence("luma", "checkin", "luma:synthetic-event", "2026-11-01")]
    return refresh.apply_observation(
        attributes, activities=observations,
        coverage=coverage() if sources is None else sources, now=NOW,
    )


class ApplyObservationTests(SimpleTestCase):
    def test_updates_only_rating_and_receipt_without_reading_preferences_for_score(self):
        attributes = {
            "marketing_review_hold": True,
            "engagement_rating": previous_rating(),
            "engagement_import": {"source_columns": {"Subscription source": "import"}},
            "engagement_activity": {"version": "source-events-v1", "events": []},
            "custom": {"preference": "do not email", "nested": [1, 2]},
        }
        before = deepcopy(attributes)
        with patch.object(refresh, "score_engagement", wraps=refresh.score_engagement) as scorer:
            result, status = apply(attributes)
        self.assertEqual(status, "updated")
        self.assertEqual(result["engagement_rating"]["version"], VERSION)
        self.assertEqual(result["engagement_rating"]["as_of"], "2026-11-02")
        self.assertEqual(result["engagement_rating"]["score"], 20)
        for key in attributes.keys() - {"engagement_rating"}:
            self.assertEqual(result[key], before[key])
        self.assertEqual(attributes, before)
        self.assertTrue(marketing_review_hold(result))
        self.assertEqual(set(scorer.call_args.kwargs), {"as_of", "coverage"})
        self.assertIsInstance(scorer.call_args.args[0][0], ActivityEvidence)
        self.assertNotIn("retry_at", result["engagement_refresh"])

    def test_existing_hold_values_are_preserved_exactly(self):
        for value in (True, False, None, 0, "false", []):
            with self.subTest(value=value):
                original = {"marketing_review_hold": value}
                result, _ = apply(original)
                self.assertEqual(result["marketing_review_hold"], value)
                self.assertEqual(marketing_review_hold(result), marketing_review_hold(original))
        result, _ = apply({})
        self.assertNotIn("marketing_review_hold", result)
        self.assertFalse(marketing_review_hold(result))

    def test_transient_failure_retains_rating_and_records_source_reason_and_retry(self):
        sources = coverage(slack={"status": "stale", "expected": True, "detail": "Public channel scan delayed"})
        attributes = {"engagement_rating": previous_rating(), "marketing_review_hold": True}
        before = deepcopy(attributes)
        result, status = apply(attributes, sources=sources)
        self.assertEqual(status, "deferred")
        for key in ("stars", "score", "version", "as_of", "reason"):
            self.assertEqual(result["engagement_rating"][key], before["engagement_rating"][key])
        latest = result["engagement_rating"]["coverage"]["latest_refresh"]
        self.assertEqual(latest["status"], "deferred")
        self.assertEqual(latest["sources"]["slack"]["detail"], "Public channel scan delayed")
        receipt = result["engagement_refresh"]
        self.assertEqual(receipt["sources"], sources)
        self.assertEqual(receipt["retry_at"], (NOW + timedelta(minutes=15)).isoformat())
        self.assertEqual(attributes, before)
        sources["slack"]["detail"] = "changed after observation"
        self.assertEqual(receipt["sources"]["slack"]["detail"], "Public channel scan delayed")
        self.assertTrue(result["marketing_review_hold"])

    def test_unassessable_new_contact_stays_unrated(self):
        sources = {source: {"status": "unavailable", "expected": False} for source in SOURCES}
        result, status = apply({"marketing_review_hold": True}, sources=sources, observations=[])
        self.assertEqual(status, "deferred")
        self.assertNotIn("engagement_rating", result)
        self.assertEqual(result["engagement_refresh"]["status"], "deferred")

    def test_permanently_missing_provider_permits_partial_refresh(self):
        result, status = apply({"engagement_rating": previous_rating()}, sources=coverage(
            newsletter={"status": "unavailable", "expected": False, "detail": "No connected provider"},
        ))
        self.assertEqual(status, "updated")
        self.assertEqual(result["engagement_rating"]["score"], 20)
        self.assertTrue(result["engagement_rating"]["coverage"]["provisional"])
        self.assertTrue(result["engagement_rating"]["coverage"]["refresh_safe"])

    def test_newer_reviewed_snapshot_is_retained(self):
        original = previous_rating(as_of="2026-11-03T06:00:00+11:00")
        result, status = apply({"engagement_rating": original, "marketing_review_hold": True})
        self.assertEqual(status, "preserved_newer")
        self.assertEqual(result["engagement_rating"], original)
        self.assertEqual(result["engagement_refresh"]["status"], "preserved_newer")
        self.assertNotIn("retry_at", result["engagement_refresh"])

    def test_malformed_attributes_are_preserved_without_weakening_effective_hold(self):
        self.assertIn("engagement_legacy_attributes", RESERVED_ATTRIBUTE_KEYS)
        for original in (None, False, 0, "legacy", ["legacy", {"preference": "unknown"}]):
            with self.subTest(original=original):
                before = deepcopy(original)
                self.assertTrue(marketing_review_hold(original))
                result, status = apply(original)
                self.assertEqual(status, "updated")
                self.assertEqual(result["engagement_legacy_attributes"], before)
                self.assertTrue(marketing_review_hold(result))
                self.assertEqual(original, before)


@override_settings(CRM_ENGAGEMENT_REFRESH_ENABLED=True, CRM_ENGAGEMENT_REFRESH_HOUR=6)
class SchedulerTests(SimpleTestCase):
    @override_settings(CRM_ENGAGEMENT_REFRESH_ENABLED=False)
    def test_disabled_scheduler_does_not_collect_or_write(self):
        with patch.object(refresh, "refresh_ratings") as run:
            self.assertEqual(refresh.run_engagement_refresh_scheduler(), {"status": "disabled"})
        run.assert_not_called()

    def test_six_am_is_melbourne_time_in_summer_and_winter(self):
        for due in (NOW, datetime(2026, 6, 1, 20, tzinfo=timezone.utc)):
            with self.subTest(due=due), patch.object(refresh, "refresh_ratings", return_value={"status": "completed"}) as run:
                with patch.object(refresh.timezone, "now", return_value=due - timedelta(seconds=1)):
                    self.assertEqual(refresh.run_engagement_refresh_scheduler(), {"status": "not_due"})
                run.assert_not_called()
                with patch.object(refresh.timezone, "now", return_value=due):
                    self.assertEqual(refresh.run_engagement_refresh_scheduler(), {"status": "completed"})
                run.assert_called_once_with(now=due)

    def test_failure_is_deferred_without_exposing_exception_contents(self):
        with patch.object(refresh.timezone, "now", return_value=NOW), patch.object(
            refresh, "refresh_ratings", side_effect=RuntimeError("synthetic sensitive provider detail"),
        ):
            self.assertEqual(refresh.run_engagement_refresh_scheduler(), {
                "status": "deferred", "reason": "activity_source_unavailable",
            })


class RefreshGuardTests(SimpleTestCase):
    def test_completed_day_does_not_recollect_or_write(self):
        with patch.object(refresh.CrmAuditEvent.objects, "filter") as audits, patch.object(
            refresh.cache, "add",
        ) as lock, patch.object(refresh, "collect_activity") as collect, patch.object(
            refresh.Contact.objects, "bulk_update",
        ) as write:
            audits.return_value.exists.return_value = True
            self.assertEqual(refresh.refresh_ratings(now=NOW), {"status": "already_completed", "day": "2026-11-02"})
            self.assertEqual(audits.call_args.kwargs["target_id"], f"{VERSION}:2026-11-02")
            self.assertEqual(audits.call_args.kwargs["detail__status"], "completed")
        lock.assert_not_called()
        collect.assert_not_called()
        write.assert_not_called()

    def test_existing_refresh_lock_prevents_duplicate_work(self):
        with patch.object(refresh.CrmAuditEvent.objects, "filter") as audits, patch.object(
            refresh.cache, "add", return_value=False,
        ), patch.object(refresh, "collect_activity") as collect, patch.object(
            refresh.Contact.objects, "bulk_update",
        ) as write:
            audits.return_value.exists.return_value = False
            self.assertEqual(refresh.refresh_ratings(now=NOW), {"status": "locked", "day": "2026-11-02"})
        collect.assert_not_called()
        write.assert_not_called()

    def test_refresh_has_no_import_or_messaging_dependencies(self):
        tree = ast.parse(inspect.getsource(refresh))
        modules = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        } | {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
        }
        forbidden = ("blast", "warmup", "community_optins", "listmonk_sync", "luma_sync", "engagement_import", "resend", "requests", "smtplib")
        self.assertFalse(any(fragment in module for module in modules for fragment in forbidden))
        # Only activity metadata is allowed onto a Contact via this runner.
        writes = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute) and node.func.attr == "bulk_update"]
        self.assertTrue(writes)
        for write in writes:
            self.assertEqual(ast.literal_eval(write.args[1]), ["attributes", "updated_at"])
