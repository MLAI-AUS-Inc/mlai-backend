"""Plan E7 and E13: the ask ladder for every cohort, per-action staff thresholds and the owner wait.

Every outbound send is a test double: email goes to ``self.sent``, no device
is ever registered, and Slack is never called (alerts are only queued).
"""
from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import founder_journey as journeys, founder_journey_alerts as alerts, founder_transitions as transitions
from . import founder_notification_outbox as drafts, website_notification_transport as transport
from .founder_journey_testing import _connect, make_history_free_org, make_new_journey_org
from .models import ContentFactoryCallbackEvent, OrganizationContentConfig
from .self_heal_testing import ALL_FLAGS_ON
from .website_models import WebsiteConnection

FLAGS = {**ALL_FLAGS_ON, "RESEND_API_KEY": "synthetic", "COMMUNITY_CHAT_FRONTEND_URL": "https://chat.mlai.au",
         "ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED": True, "CONTENT_FACTORY_OPS_SLACK_CHANNEL_ID": "",
         "ARTICLES_GITHUB_OWNER_APPROVAL_PICKUP_ENABLED": True,
         "ARTICLES_FREE_FIRST_ARTICLE_DAILY_CAP": 0, "ARTICLES_FREE_FIRST_ARTICLE_SPIKE_THRESHOLD": 0}


class Clock:
    """One shared clock for ``timezone.now`` and the ledger's ``created_at``."""

    def __init__(self):
        self.at = timezone.now()

    def __call__(self):
        return self.at


@override_settings(**FLAGS)
class AskLadderTests(TestCase):
    def setUp(self):
        cache.clear()
        self.clock = Clock()
        self.start = self.clock.at
        self.sent = []
        self.enterContext(patch("django.utils.timezone.now", self.clock))
        self.enterContext(patch.object(transport, "post_email", side_effect=self.send))
        self.enterContext(patch.object(drafts, "_devices", return_value=[]))
        self.enterContext(patch.object(transport, "open_dm", side_effect=AssertionError("no DM in tests")))

    def send(self, payload, key, **kwargs):
        self.sent.append((payload, key))
        return "synthetic-message"

    def verified(self, built):
        built.owner.email_verified_at = self.start
        built.owner.save(update_fields=["email_verified_at"])
        return built

    def legacy_revoked(self):
        """A legacy-cohort startup whose GitHub access was revoked: the ``authorize_github`` ask."""
        built = self.verified(make_history_free_org())
        connection, _ = _connect(built.organization, built.config, repo="example/legacy-site")
        WebsiteConnection.objects.filter(pk=connection.pk).update(state="revoked", updated_at=self.start)
        return built

    def sweep(self, hours, *, fresh_process=False):
        self.clock.at = self.start + timedelta(hours=hours)
        if fresh_process:
            # A redeploy: nothing in-process survives (cache, cursor, loaded state).
            cache.clear()
        return transitions.run_transition_sweep(now=self.clock.at)

    def events(self, built):
        outbox = transitions.mapping(transitions.mapping(OrganizationContentConfig.objects.get(
            organization=built.organization).pillar_strategy.get("founder_notices")).get("outbox"))
        return sorted(notice["event"] for notice in outbox.values() if notice["channel"] == "email")

    def receipts(self, built, reason=transitions.STAFF_ALERT_REASON):
        strategy = OrganizationContentConfig.objects.get(organization=built.organization).pillar_strategy
        return [row for row in transitions.mapping(strategy.get(alerts.RECEIPTS_KEY)).values()
                if row.get("alert_reason") == reason]

    # --- E7 -------------------------------------------------------------

    def test_legacy_ask_sends_at_0_24_and_72_hours_then_stops(self):
        built = self.legacy_revoked()
        self.assertTrue(transitions.owns_notifications(built.organization))
        for hours in (0, 1, 24, 30, 72, 100, 200):
            self.sweep(hours)
        self.assertEqual([payload["subject"] for payload, _ in self.sent], [
            "MLAI needs access to your website on GitHub",
            "Reminder: MLAI needs access to your website on GitHub",
            "Reminder: MLAI needs access to your website on GitHub",
        ])
        self.assertEqual({payload["to"][0] for payload, _ in self.sent}, {built.owner.email})
        self.assertIn("/my-startup/website?company_id=", self.sent[0][0]["text"])

    def test_the_ladder_stops_when_the_ask_clears(self):
        built = self.legacy_revoked()
        self.sweep(0)
        WebsiteConnection.objects.filter(organization=built.organization).update(state="connected")
        for hours in (24, 72, 100):
            self.sweep(hours)
        # Access came back: the only later message is the done notice, never a reminder.
        self.assertEqual([payload["subject"] for payload, _ in self.sent],
                         ["MLAI needs access to your website on GitHub", "Your blog page is live"])

    def test_a_reminder_queued_before_the_ask_cleared_is_not_sent(self):
        built = self.legacy_revoked()
        self.sweep(0)
        self.clock.at = self.start + timedelta(hours=24)
        with patch.object(transitions, "drain", return_value={"sent": 0, "skipped": 0, "retry": 0}):
            transitions.run_transition_sweep(now=self.clock.at)
        self.assertIn("your_turn_reminder_24h", self.events(built))
        WebsiteConnection.objects.filter(organization=built.organization).update(state="connected")
        transitions.drain(self.clock.at)
        self.assertEqual(len(self.sent), 1)

    def test_no_duplicate_after_a_redeploy(self):
        built = self.legacy_revoked()
        for hours in (0, 24, 72):
            self.sweep(hours)
            self.sweep(hours, fresh_process=True)
            self.sweep(hours + 0.1, fresh_process=True)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len({key for _, key in self.sent}), 3)
        rows = ContentFactoryCallbackEvent.objects.filter(event_type=transitions.EVENT_TYPE, job_id=str(built.organization.pk))
        self.assertEqual(rows.count(), 6)  # three rungs, email and DM each, once
        self.assertEqual(self.events(built), ["your_turn", "your_turn_reminder_24h", "your_turn_reminder_72h"])

    def test_first_run_cap_and_one_catch_up_hold_for_the_legacy_cohort(self):
        old = []
        for _ in range(3):
            built = self.legacy_revoked()
            WebsiteConnection.objects.filter(organization=built.organization).update(
                updated_at=self.start - timedelta(days=30))
            old.append(built)
        with patch.object(transitions, "first_run_budget", lambda cap=2: {"left": cap, "deferred": 0}):
            result = self.sweep(0)
        self.assertEqual(result["first_run_deferred"], 1)
        self.assertEqual(len(self.sent), 2)
        for hours in (0.2, 24, 72, 200):
            self.sweep(hours)
        # Each backlog ask: one first notice and one follow-up only.
        for built in old:
            self.assertEqual(self.events(built), ["your_turn", "your_turn_reminder_24h"])

    def test_staff_alert_waits_for_each_actions_threshold_and_resolves(self):
        built = self.legacy_revoked()
        self.assertEqual(transitions.staff_threshold("authorize_github"), 120)
        self.assertEqual(transitions.staff_threshold("review_draft"), 72)
        for hours in (0, 24, 72, 100):
            self.sweep(hours)
        self.assertEqual(self.receipts(built), [])
        self.sweep(120)
        self.sweep(121, fresh_process=True)
        [receipt] = self.receipts(built)
        self.assertEqual(receipt["state"], "pending")
        self.assertEqual(receipt["details"]["ask"], "authorize_github")
        self.assertEqual(receipt["details"]["threshold_hours"], 120)
        WebsiteConnection.objects.filter(organization=built.organization).update(state="connected")
        self.sweep(122)
        [receipt] = self.receipts(built)
        self.assertEqual(receipt["state"], "resolved")
        self.assertTrue(receipt["resolved_at"])
        # A resolved alert is never delivered.
        self.assertEqual(alerts._pending_targets([built.organization.pk], {transitions.STAFF_ALERT_REASON}), [])

    def test_staff_alerts_ride_the_alerts_switch(self):
        built = self.legacy_revoked()
        with self.settings(ARTICLES_FOUNDER_JOURNEY_ALERTS_ENABLED=False):
            for hours in (0, 200):
                self.sweep(hours)
        self.assertEqual(self.receipts(built), [])

    def test_the_generic_72_hour_detector_stands_aside_for_a_tracked_ask(self):
        built = self.legacy_revoked()
        self.sweep(0)
        self.assertTrue(alerts._ladder_tracks(built.organization, "website:" + str(built.organization.pk)))
        with self.settings(MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=False):
            self.assertFalse(alerts._ladder_tracks(built.organization, "website:" + str(built.organization.pk)))

    @override_settings(MY_STARTUP_TRANSITION_NOTIFICATIONS_ENABLED=False)
    def test_flags_off_send_nothing(self):
        self.legacy_revoked()
        self.assertEqual(self.sweep(0), {"skipped": "flag_off"})
        self.assertEqual(self.sent, [])

    @override_settings(MY_STARTUP_TURN_REMINDERS_ENABLED=False)
    def test_reminders_switch_off_sends_only_the_first_notice(self):
        self.legacy_revoked()
        for hours in (0, 24, 72):
            self.sweep(hours)
        self.assertEqual(len(self.sent), 1)

    # --- E13 ------------------------------------------------------------

    def owner_request(self, built, *, requested_at=None):
        from integrations.services import github_owner_requests as owner_requests
        owner_requests.record_request(organization=built.organization, user_id=built.owner.pk,
                                      company_id=built.company.pk, github_logins=("founder-login",))
        if requested_at is not None:
            built.config.refresh_from_db()
            system = built.config.article_system
            system[owner_requests.KEY]["requested_at"] = requested_at.isoformat()
            OrganizationContentConfig.objects.filter(pk=built.config.pk).update(article_system=system)
        built.config.refresh_from_db()
        return built

    @contextmanager
    def journey_doubles(self):
        with patch.object(journeys, "terms_out_of_date", return_value=False), \
                patch.object(journeys, "article_price_and_balance", return_value=(12, 20)):
            yield

    def journey(self, built):
        with self.journey_doubles():
            return journeys.project_founder_journey(built.organization, user=built.owner, company=built.company,
                                                    now=self.clock.at)

    def test_a_pending_owner_request_shows_the_ask_with_the_owner_link(self):
        built = self.owner_request(self.verified(make_new_journey_org(domain="banksia-studio.com")))
        body = self.journey(built)
        self.assertEqual((body["state"], body["action"]["id"]), ("your_turn", "waiting_github_owner_approval"))
        self.assertEqual(body["action"]["url"], "https://github.com/apps/mlai-tools/installations/new")
        self.assertIn("invite your developer", body["body"])

    def test_another_viewer_does_not_get_the_requesters_ask(self):
        from core.models import User
        built = self.owner_request(self.verified(make_new_journey_org(domain="banksia-studio.com")))
        other = User.objects.create_user(email="other@synthetic.example.test")
        with self.journey_doubles():
            self.assertIsNone(journeys._owner_request_decision(organization=built.organization, company=built.company,
                                                               config=built.config, user=other, now=self.clock.at))

    def test_a_pending_owner_request_reminds_at_24_and_72_hours_and_alerts_staff_at_72(self):
        built = self.owner_request(self.verified(make_new_journey_org(domain="banksia-studio.com")))
        for hours in (0, 24, 48):
            self.sweep(hours)
        self.assertEqual(self.receipts(built), [])
        for hours in (72, 100, 200):
            self.sweep(hours)
        self.assertEqual([payload["subject"] for payload, _ in self.sent], [
            "Your GitHub owner hasn't approved MLAI yet",
            "Reminder: Your GitHub owner hasn't approved MLAI yet",
            "Reminder: Your GitHub owner hasn't approved MLAI yet",
        ])
        [receipt] = self.receipts(built)
        self.assertEqual((receipt["details"]["ask"], receipt["details"]["ask_item"]),
                         ("waiting_github_owner_approval", "github_owner"))
        self.assertEqual(receipt["details"]["threshold_hours"], 72)

    def test_the_owners_approval_stops_the_reminders_and_resolves_the_alert(self):
        from integrations.services import github_owner_requests as owner_requests
        built = self.owner_request(self.verified(make_new_journey_org(domain="banksia-studio.com")))
        for hours in (0, 72):
            self.sweep(hours)
        self.assertEqual(len(self.receipts(built)), 1)
        owner_requests.clear_request(built.config)
        for hours in (73, 100, 200):
            self.sweep(hours)
        self.assertEqual(len(self.sent), 2)
        [receipt] = self.receipts(built)
        self.assertEqual(receipt["state"], "resolved")

    def test_an_expired_request_falls_back_to_the_repository_picker(self):
        from integrations.services import github_owner_requests as owner_requests
        built = self.owner_request(self.verified(make_new_journey_org(domain="banksia-studio.com")),
                                   requested_at=self.start - timedelta(days=owner_requests.REQUEST_DAYS + 1))
        body = self.journey(built)
        self.assertEqual(body["action"]["id"], "choose_repository")
        self.assertEqual(body["action"]["url"], "https://chat.mlai.au/my-startup/website?github=multiple_repos&company_id="
                         + str(built.company.pk))
        self.assertIn("expired", body["body"])
        self.sweep(0)
        self.assertEqual([payload["subject"] for payload, _ in self.sent], ["Choose your website's repository"])

    def test_a_legacy_founders_owner_request_is_reminded_too(self):
        built = self.owner_request(self.verified(make_history_free_org(domain="legacy-banksia.studio")))
        for hours in (0, 24, 72):
            self.sweep(hours)
        self.assertEqual(len(self.sent), 3)

    def test_the_ask_goes_only_to_the_founder_who_asked(self):
        from core.models import User
        from .self_heal_testing import fake_member_context
        built = self.owner_request(self.verified(make_new_journey_org(domain="banksia-studio.com")))
        member = User.objects.create_user(email="member@synthetic.example.test", email_verified_at=self.start)
        with fake_member_context(members=(member.pk,), owner_ids=(built.owner.pk,)):
            recipients = transitions.resolve_recipients(built.organization, item="github-owner:1",
                                                        ask="waiting_github_owner_approval")
        self.assertEqual([user.pk for user in recipients], [built.owner.pk])
