"""Read-only activity extraction using synthetic rows, never a database/provider."""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from crm import engagement_sources as sources
from crm.engagement_scoring import ActivityEvidence, score_engagement

NOW = datetime(2026, 11, 3, 1, tzinfo=timezone.utc)
RECENT = NOW - timedelta(days=5)
EMAIL = "member@sample.au"
AUTHOR = "U12345678"


class Rows:
    def __init__(self, rows=(), stats=None):
        self.rows = list(rows)
        self.stats = stats or {}
        self.calls = []

    def __iter__(self):
        return iter(self.rows)

    def __getitem__(self, key):
        return self.rows[key]

    def filter(self, *args, **kwargs):
        self.calls.append(("filter", args, kwargs))
        return self

    def annotate(self, *args, **kwargs):
        self.calls.append(("annotate", args, kwargs))
        return self

    def order_by(self, *args):
        return self

    def values(self, *args):
        self.calls.append(("values", args, {}))
        return self

    def values_list(self, *args, **kwargs):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def aggregate(self, **kwargs):
        return self.stats


def receipt(author=AUTHOR, **changes):
    return {"payload__event__type": "message", "payload__event__user": author,
        "payload__event__subtype": "", **changes}


def channel(**changes):
    return {"id": 1, "slack_visibility": "public", "slack_deleted": False,
        "slack_channel_name": "community", "pilot_settings": {},
        "enabled": True, "mirror_decision": "mirror", "sync_paused_reason": "", "hold_reason": "",
        "reader_grant_id": None, "reader_grant__status": None, "reader_grant__revoked_at": None,
        "has_source_history": True, "sync_state__latest_source_activity": "", "sync_state__last_successful_delivery_at": None,
        "sync_state__last_successful_scan_at": NOW - timedelta(minutes=20), **changes}


def contact(events=(), **columns):
    return SimpleNamespace(pk="contact-1", email=EMAIL, attributes={
        "marketing_review_hold": True,
        "engagement_activity": {"version": "source-events-v1", "as_of": "2026-11-02", "events": list(events)},
        "engagement_import": {"source_columns": columns},
    })


@override_settings(
    CRM_LUMA_SYNC_ENABLED=True,
    MESSAGE_SYNC_SLACK_BOT_WORKSPACE_ID="T12345678",
    SLACK_BRIDGE_BOT_USER_ID="U99999999",
    LISTMONK_BASE_URL="https://mail.invalid", LISTMONK_API_USER="synthetic", LISTMONK_API_TOKEN="synthetic",
)
class ActivitySourceTests(SimpleTestCase):
    def setUp(self):
        self.users = Rows([{"id": 1, "email_key": EMAIL, "slack_id": AUTHOR, "last_login": RECENT}])
        self.events = Rows([], {"total": 77, "synced": 17, "last_success": NOW - timedelta(minutes=20)})
        self.registrations = Rows()
        self.channels = Rows([channel()])
        self.identities = Rows()
        self.receipts = Rows([receipt()])
        self.links = Rows([{"source_author_id": AUTHOR, "source_channel_id": "C12345678",
            "source_message_id": f"{int(RECENT.timestamp())}.000001", "source_payload__delivery_type": "create"}])
        self.deliveries = Rows()
        self.audit = Rows()
        for target, rows in (
            ("CrmEvent", self.events), ("EventRegistration", self.registrations),
            ("CommunityBridgeChannel", self.channels), ("CommunityBridgeIdentityLink", self.identities),
            ("CommunityBridgeReceipt", self.receipts), ("CommunityBridgeMessageLink", self.links),
            ("BlastDelivery", self.deliveries), ("CrmAuditEvent", self.audit),
        ):
            self.enterContext(patch.object(getattr(sources, target), "objects", rows))
        self.enterContext(patch.object(sources, "get_user_model", return_value=SimpleNamespace(objects=self.users)))
        self.cursor = self.enterContext(patch.object(sources.cache, "get", return_value=NOW.isoformat()))

    def collect(self, person=None):
        return sources.collect_activity([person or contact()], as_of=NOW)

    def test_exact_primary_account_and_source_dates_without_mutating_preferences(self):
        person = contact(**{"Last clicked at": RECENT.isoformat(), "Comments (30d)": "20"})
        before = deepcopy(person.attributes)
        self.registrations.rows = [{"contact_id": person.pk, "approval_status": "approved", "checked_in_at": RECENT,
            "event__luma_event_id": "evt-one", "event__start_at": RECENT - timedelta(hours=1)}]
        self.deliveries.rows = [{"pk": 123, "contact_id": person.pk, "first_clicked_at": RECENT}]
        result = self.collect(person)
        evidence = result["activities"][EMAIL]
        self.assertEqual({row.kind for row in evidence}, {"app_login", "slack_message", "checkin", "newsletter_click"})
        self.assertEqual(next(row for row in evidence if row.kind == "checkin").occurred_at, RECENT - timedelta(hours=1))
        self.assertEqual(next(row for row in evidence if row.kind == "checkin").event_id, "luma:evt-one")
        self.assertTrue(all(not row.human_verified for row in evidence if row.kind == "newsletter_click"))
        self.assertEqual(person.attributes, before)
        self.assertEqual(result["coverage"]["luma"]["events_unsynced"], 60)
        self.assertFalse(result["coverage"]["newsletter"]["substack_automatic"])
        self.assertIn(("filter", (), {"email_key__in": {EMAIL: person}}), self.users.calls)

    def test_missing_batch_activity_is_partial_coverage_not_an_outage(self):
        self.users.rows = []
        result = self.collect()
        self.assertEqual(result["activities"][EMAIL], [])
        self.assertTrue(all(row["status"] == "partial" for row in result["coverage"].values()))
        self.assertFalse(self.receipts.calls)
        self.assertFalse(self.links.calls)

    def test_private_test_and_deleted_channels_are_not_queried_for_messages(self):
        self.channels.rows = [channel(slack_visibility="private"), channel(slack_channel_name="crm-e2e-test"),
            channel(slack_deleted=True), channel(pilot_settings={"environment": "staging"})]
        result = self.collect()
        self.assertNotIn("slack_message", [row.kind for row in result["activities"][EMAIL]])
        self.assertFalse(self.receipts.calls)
        self.assertFalse(self.links.calls)

    def test_public_receipts_exclude_bot_with_user_id_and_reactions(self):
        self.receipts.rows = [receipt(), receipt(**{"payload__event__bot_id": "B12345678"})]
        self.assertNotIn("slack_message", [row.kind for row in self.collect()["activities"][EMAIL]])
        self.receipts.rows = [receipt()]
        self.links.rows[0]["source_payload__delivery_type"] = "reaction_add"
        self.assertNotIn("slack_message", [row.kind for row in self.collect()["activities"][EMAIL]])

    def test_slack_source_timestamp_controls_age_not_new_ingestion_time(self):
        self.links.rows[0].update(source_message_id=f"{int((NOW - timedelta(days=181)).timestamp())}.000001", created_at=NOW)
        self.assertNotIn("slack_message", [row.kind for row in self.collect()["activities"][EMAIL]])
        fields = [call[1] for call in self.links.calls if call[0] == "values"]
        self.assertFalse(any("created_at" in row or "source_payload" in row for row in fields))
        self.assertFalse(any("payload" in row for name, row, _ in self.receipts.calls if name == "values"))

    def test_dated_seed_retained_outside_managed_catalog(self):
        raw = {"source": "luma", "kind": "checkin", "event_id": "luma:evt-outside-key", "occurred_at": RECENT.isoformat(), "human_verified": True}
        result = self.collect(contact([raw]))
        self.assertIn(ActivityEvidence(**raw), [ActivityEvidence(row.source, row.kind, row.event_id, row.occurred_at.isoformat(), row.human_verified) for row in result["activities"][EMAIL]])

    def test_only_a_complete_newer_luma_sync_can_remove_seed(self):
        raw = {"source": "luma", "kind": "checkin", "event_id": "luma:evt-one", "occurred_at": RECENT.isoformat(), "human_verified": True}
        person = contact([raw])
        self.events.rows = [{"luma_event_id": "evt-one", "guests_synced_at": datetime(2026, 11, 2, 1, tzinfo=timezone.utc)}]
        self.assertTrue(any(row.kind == "checkin" for row in self.collect(person)["activities"][EMAIL]))
        self.events.rows[0]["guests_synced_at"] = NOW
        self.assertFalse(any(row.kind == "checkin" for row in self.collect(person)["activities"][EMAIL]))

    def test_unchecked_registration_is_not_attendance(self):
        self.registrations.rows = [{"contact_id": "contact-1", "approval_status": "approved", "checked_in_at": None,
            "event__luma_event_id": "evt-one", "event__start_at": RECENT}]
        self.assertIn("registration", [row.kind for row in self.collect()["activities"][EMAIL]])
        self.assertNotIn("checkin", [row.kind for row in self.collect()["activities"][EMAIL]])
        self.registrations.rows[0]["approval_status"] = "declined"
        self.assertNotIn("registration", [row.kind for row in self.collect()["activities"][EMAIL]])

    def test_stale_ingestion_is_explicit_and_does_not_retimestamp_activity(self):
        self.events.stats["last_success"] = NOW - timedelta(days=4)
        self.cursor.return_value = (NOW - timedelta(days=4)).isoformat()
        result = self.collect(contact(**{"Last clicked at": RECENT.isoformat()}))
        self.assertEqual(result["coverage"]["luma"]["status"], "stale")
        self.assertEqual(result["coverage"]["newsletter"]["status"], "stale")
        self.assertEqual(next(row for row in result["activities"][EMAIL] if row.kind == "newsletter_click").occurred_at, RECENT)

    def test_a_fresh_channel_does_not_hide_a_stale_active_channel(self):
        self.channels.rows.append(channel(id=2, sync_state__last_successful_scan_at=NOW - timedelta(days=4)))
        result = self.collect()
        self.assertEqual(result["coverage"]["slack"]["status"], "stale")
        self.assertEqual(result["coverage"]["slack"]["stale_active_channels"], 1)
        self.channels.rows[-1]["slack_archived"] = True
        self.assertEqual(self.collect()["coverage"]["slack"]["status"], "partial")

    def test_empty_orphan_mapping_is_a_coverage_gap_not_a_global_outage(self):
        orphan = channel(id=2, has_source_history=False, sync_state__last_successful_scan_at=NOW - timedelta(days=10))
        self.channels.rows.append(orphan)
        coverage = self.collect()["coverage"]["slack"]
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["stale_active_channels"], 0)
        self.assertEqual(coverage["unmonitored_channels"], 1)
        self.channels.rows = [orphan]
        coverage = self.collect()["coverage"]["slack"]
        self.assertEqual(coverage["status"], "partial")
        self.assertFalse(coverage["expected"])

    def test_current_reader_or_source_activity_keeps_stale_feed_protected(self):
        for authority in (
            {"has_source_history": True},
            {"reader_grant_id": 1, "reader_grant__status": "active"},
            {"sync_state__latest_source_activity": "1793000000.000001"},
            {"sync_state__last_successful_delivery_at": RECENT},
        ):
            with self.subTest(authority=authority):
                self.channels.rows = [channel(has_source_history=False,
                    sync_state__last_successful_scan_at=NOW - timedelta(days=10)) | authority]
                coverage = self.collect()["coverage"]["slack"]
                self.assertEqual(coverage["status"], "stale")
                self.assertEqual(coverage["stale_active_channels"], 1)

    def test_explicitly_paused_held_disabled_or_archived_feed_is_not_expected(self):
        for state in ({"sync_paused_reason": "no_reader"}, {"mirror_decision": "hold"},
            {"hold_reason": "source_closed"}, {"enabled": False}, {"slack_archived": True}):
            with self.subTest(state=state):
                self.channels.rows = [channel(sync_state__last_successful_scan_at=NOW - timedelta(days=10)) | state]
                coverage = self.collect()["coverage"]["slack"]
                self.assertEqual(coverage["status"], "partial")
                self.assertEqual(coverage["stale_active_channels"], 0)

    def test_luma_pass_freshness_handles_empty_catalog_and_partial_failure(self):
        self.events.stats["last_success"] = NOW - timedelta(days=10)
        self.audit.rows = [{"created_at": NOW, "detail": {"failed_events": [], "events_left": [], "rate_limited": False}}]
        self.assertEqual(self.collect()["coverage"]["luma"]["status"], "partial")
        self.audit.rows[0]["detail"]["failed_events"] = ["evt-unavailable"]
        self.assertEqual(self.collect()["coverage"]["luma"]["status"], "stale")

    def test_source_failure_is_not_zero_activity_and_does_not_leak_exception_text(self):
        with patch.object(sources, "_slack", side_effect=RuntimeError("secret personal data")):
            result = self.collect()
        self.assertEqual(result["coverage"]["slack"]["status"], "unavailable")
        self.assertTrue(result["coverage"]["slack"]["expected"])
        self.assertNotIn("secret", str(result))

    @override_settings(LISTMONK_BASE_URL="", LISTMONK_API_USER="", LISTMONK_API_TOKEN="")
    def test_legacy_dated_newsletter_source_can_age_without_connected_provider(self):
        self.cursor.return_value = None
        result = self.collect(contact(**{"Last clicked at": RECENT.isoformat(), "Comments (30d)": "10", "Shares (30d)": "20"}))
        self.assertEqual(result["coverage"]["newsletter"]["status"], "partial")
        self.assertFalse(result["coverage"]["newsletter"]["expected"])
        self.assertEqual([row.kind for row in result["activities"][EMAIL] if row.source == "newsletter"], ["newsletter_click"])

    @override_settings(CRM_SUBSTACK_ACTIVITY_ENABLED=True)
    def test_fresh_substack_browser_export_uses_explicit_unverified_clicks_only(self):
        person = contact()
        person.attributes["engagement_newsletter"] = {"source": "substack_export", "exported_at": NOW.isoformat(),
            "sha256": "a" * 64, "last_clicked_at": RECENT.isoformat(), "last_opened_at": NOW.isoformat(), "Comments (30d)": 100}
        before = deepcopy(person.attributes)
        result = self.collect(person)
        coverage = result["coverage"]["newsletter"]
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["substack_mode"], "browser_export")
        self.assertEqual(coverage["substack_status"], "fresh")
        self.assertTrue(coverage["substack_automatic"])
        self.assertTrue(coverage["substack_export_available"])
        self.assertFalse(coverage["email_opens_scored"])
        activity = [item for item in result["activities"][EMAIL] if item.source == "newsletter"]
        self.assertEqual(activity, [ActivityEvidence("newsletter", "newsletter_click", f"newsletter-export-click:{RECENT.isoformat()}", RECENT, False)])
        self.assertEqual(person.attributes, before)

    @override_settings(CRM_SUBSTACK_ACTIVITY_ENABLED=True)
    def test_global_export_freshness_does_not_require_batch_clicks(self):
        result = self.collect()
        self.assertFalse(any(item.source == "newsletter" for item in result["activities"][EMAIL]))
        self.assertTrue(result["coverage"]["newsletter"]["substack_export_available"])
        self.assertEqual(result["coverage"]["newsletter"]["substack_last_exported_at"], NOW.isoformat())

    @override_settings(CRM_SUBSTACK_ACTIVITY_ENABLED=True)
    def test_stale_expected_substack_export_defers_even_when_listmonk_is_fresh(self):
        self.cursor.side_effect = lambda key: (NOW - timedelta(days=3)).isoformat() if key == sources.SUBSTACK_CURSOR_KEY else NOW.isoformat()
        result = self.collect(contact(**{"Last clicked at": RECENT.isoformat()}))
        coverage = result["coverage"]["newsletter"]
        self.assertEqual(coverage["status"], "stale")
        self.assertTrue(coverage["expected"])
        self.assertTrue(coverage["substack_automatic"])
        self.assertFalse(coverage["substack_export_available"])
        rating = score_engagement(result["activities"][EMAIL], as_of=NOW, coverage=result["coverage"])
        self.assertFalse(rating["coverage"]["refresh_safe"])

    @override_settings(CRM_SUBSTACK_ACTIVITY_ENABLED=True)
    def test_before_first_export_is_an_explicit_partial_coverage_gap(self):
        self.cursor.side_effect = lambda key: None if key == sources.SUBSTACK_CURSOR_KEY else NOW.isoformat()
        coverage = self.collect()["coverage"]["newsletter"]
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["substack_status"], "awaiting_first_export")
        self.assertFalse(coverage["substack_automatic"])
        self.assertFalse(coverage["substack_export_available"])
        self.assertIn("awaits its first successful import", coverage["detail"])

    @override_settings(CRM_SUBSTACK_ACTIVITY_ENABLED=True)
    def test_blank_substack_click_keeps_actual_legacy_click_evidence(self):
        person = contact(**{"Last clicked at": RECENT.isoformat()})
        person.attributes["engagement_newsletter"] = {"source": "substack_export", "exported_at": NOW.isoformat(), "last_clicked_at": None}
        result = self.collect(person)
        clicks = [item for item in result["activities"][EMAIL] if item.kind == "newsletter_click"]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0].occurred_at, RECENT)
        self.assertFalse(clicks[0].human_verified)

    def test_global_export_cursor_recovers_from_audit_and_uses_newest_success(self):
        self.audit.rows = [{"status": "completed", "exported_at": NOW.isoformat(), "changed": 0, "clicks": 0}]
        self.cursor.return_value = (NOW - timedelta(days=3)).isoformat()
        self.assertEqual(sources._substack_export_cursor(), NOW)
        self.cursor.side_effect = RuntimeError("Cache unavailable")
        self.assertEqual(sources._substack_export_cursor(), NOW)

    @override_settings(CRM_SUBSTACK_ACTIVITY_ENABLED=True)
    def test_export_after_refresh_time_is_not_fresh_or_valid_automatic_evidence(self):
        self.cursor.side_effect = lambda key: (NOW + timedelta(hours=1)).isoformat() if key == sources.SUBSTACK_CURSOR_KEY else NOW.isoformat()
        coverage = self.collect()["coverage"]["newsletter"]
        self.assertEqual(coverage["status"], "stale")
        self.assertFalse(coverage["substack_export_available"])
        self.assertFalse(coverage["substack_automatic"])

    def test_timestamp_validation_keeps_unknown_and_future_activity_out(self):
        self.assertIsNone(sources.source_time("2026-11-02T12:00:00"))
        self.assertEqual(sources.source_time("2026-11-02"), date(2026, 11, 2))
        self.assertIsNone(sources.slack_message_time("reaction:123"))
        self.users.rows[0]["last_login"] = NOW + timedelta(hours=1)
        result = self.collect(contact(**{"Last clicked at": (NOW + timedelta(hours=1)).isoformat()}))
        self.assertFalse(any(row.kind in {"app_login", "newsletter_click"} for row in result["activities"][EMAIL]))

    def test_row_limit_is_explicit_and_blocks_safe_refresh(self):
        self.links.rows *= 3
        with patch.object(sources, "MAX_ROWS", 2):
            result = self.collect()
        self.assertEqual(result["coverage"]["slack"]["status"], "stale")
        self.assertTrue(result["coverage"]["slack"]["row_limit_reached"])

