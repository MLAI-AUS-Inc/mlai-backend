"""Campaign streams on a second registered domain (mlai.org.au), with mlai.au kept for people and system mail.

Database-free: proves the production configuration is accepted and keeps
marketing, event updates and system mail on separate identities.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from crm.blast_email import email_block
from crm.blasts import BlastError
from crm.deliverability import policy, volume
from crm.deliverability.streams import assign_stream, monitored_sender, protected_domain, stream_definitions
from crm.models import Blast, SenderMailbox, SendingStream

ORG_STREAMS = (
    "news:news.mlai.org.au:hello@news.mlai.org.au:marketing;"
    "events:events.mlai.org.au:events@events.mlai.org.au:event_update;"
    "notify:notify.mlai.au:notifications@notify.mlai.au:transactional"
)
ORG_SETTINGS = {
    "CRM_STREAMS": ORG_STREAMS,
    "CRM_PROTECTED_DOMAINS": ("mlai.au", "mlai.org.au"),
    "CRM_RESEND_MONITORED_SENDERS": (),
    "MLAI_SYSTEM_FROM_EMAIL": "MLAI <notifications@notify.mlai.au>",
}
MELBOURNE = ZoneInfo("Australia/Melbourne")


def stream(key, domain, address, purposes, *, state="warming", transactional=False):
    mailbox = SenderMailbox(email=address, domain=domain, enabled=True, route="resend")
    return SendingStream(key=key, domain=domain, mailbox=mailbox, purposes=purposes,
                         transactional=transactional, state=state)


def org_streams(*, news_state="warming"):
    return [
        stream("news", "news.mlai.org.au", "hello@news.mlai.org.au", ["marketing"], state=news_state),
        stream("events", "events.mlai.org.au", "events@events.mlai.org.au", ["event_update"]),
        stream("notify", "notify.mlai.au", "notifications@notify.mlai.au", [], transactional=True),
    ]


@override_settings(**ORG_SETTINGS)
class OrgStreamConfigTests(SimpleTestCase):
    def test_three_streams_are_accepted_with_one_purpose_each(self):
        news, events, notify = stream_definitions()
        self.assertEqual((news["key"], news["domain"], news["purposes"]), ("news", "news.mlai.org.au", ["marketing"]))
        self.assertEqual((events["key"], events["domain"], events["purposes"]),
                         ("events", "events.mlai.org.au", ["event_update"]))
        self.assertTrue(notify["transactional"])
        self.assertFalse(news["transactional"] or events["transactional"])

    def test_both_root_domains_are_protected_but_their_sending_subdomains_are_not(self):
        for domain in ("mlai.au", "mlai.org.au", "MLAI.org.au."):
            self.assertTrue(protected_domain(domain), domain)
        for domain in ("news.mlai.org.au", "events.mlai.org.au", "notify.mlai.au"):
            self.assertFalse(protected_domain(domain), domain)

    def test_a_campaign_stream_on_either_root_domain_is_refused(self):
        for raw in ("news:mlai.org.au:hello@mlai.org.au:marketing",
                    "news:mlai.au:hello@mlai.au:marketing",
                    # The same purpose on two streams would make stream choice ambiguous.
                    ORG_STREAMS + ";more:more.mlai.org.au:hi@more.mlai.org.au:event_update"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                stream_definitions(raw)

    def test_environment_value_for_protected_domains_parses_as_a_list(self):
        values = policy.environment_settings({"CRM_PROTECTED_DOMAINS": "mlai.au, MLAI.org.au"})
        self.assertEqual(values["CRM_PROTECTED_DOMAINS"], ("mlai.au", "mlai.org.au"))

    def test_feedback_covers_both_campaign_domains_and_leaves_system_mail_alone(self):
        for sender in ("hello@news.mlai.org.au", "events@events.mlai.org.au", "anyone@mlai.org.au", "sam@mlai.au"):
            self.assertTrue(monitored_sender(sender), sender)
        for sender in ("notifications@notify.mlai.au", "a@evilnews.mlai.org.au", "a@mlai.org.au.example.net"):
            self.assertFalse(monitored_sender(sender), sender)


@override_settings(**ORG_SETTINGS)
class OrgStreamAssignmentTests(SimpleTestCase):
    def assign(self, purpose, streams=None):
        blast = Blast(purpose=purpose, channels=["email"])
        with patch("crm.models.SendingStream.objects.select_related", return_value=streams or org_streams()):
            assign_stream(blast)
        return blast

    def test_marketing_goes_to_news_and_event_updates_go_to_events(self):
        marketing = self.assign("marketing")
        update = self.assign("event_update")
        self.assertEqual((marketing.stream.key, marketing.mailbox.email), ("news", "hello@news.mlai.org.au"))
        self.assertEqual((update.stream.key, update.mailbox.email), ("events", "events@events.mlai.org.au"))

    def test_a_stream_missing_from_configuration_cannot_be_chosen(self):
        with override_settings(CRM_STREAMS="news:news.mlai.org.au:hello@news.mlai.org.au:marketing"), \
                self.assertRaises(BlastError) as caught:
            self.assign("event_update")
        self.assertEqual(caught.exception.code, "stream_not_configured")


@override_settings(**ORG_SETTINGS, CRM_SENDING_ENABLED=True, CRM_GUARDIAN_ENABLED=True, CRM_GUARDIAN_DRY_RUN=False,
                   LISTMONK_BASE_URL="http://listmonk.test", LISTMONK_API_USER="synthetic", LISTMONK_API_TOKEN="synthetic")
class OrgStreamIsolationTests(SimpleTestCase):
    def setUp(self):
        self.enterContext(patch("crm.deliverability.guardian.admission_ready", return_value=True))
        self.enterContext(patch("crm.sending_domains.is_verified", return_value=True))

    def blast(self, purpose, streams):
        chosen = next(item for item in streams if purpose in item.purposes)
        return Blast(purpose=purpose, channels=["email"], stream=chosen, mailbox=chosen.mailbox)

    def test_a_held_news_stream_never_holds_event_updates(self):
        streams = org_streams(news_state="held")
        self.assertEqual(email_block(self.blast("marketing", streams)), "stream_held")
        self.assertIsNone(email_block(self.blast("event_update", streams)))

    def test_a_blast_on_the_system_stream_is_refused(self):
        notify = org_streams()[2]
        blast = Blast(purpose="event_update", channels=["email"], stream=notify, mailbox=notify.mailbox)
        self.assertEqual(email_block(blast), "stream_not_for_blasts")


@override_settings(CRM_EVENT_SEND_WINDOW="mon-sun 08:00-20:00 Australia/Melbourne",
                   CRM_SEND_WINDOW="mon-fri 09:00-16:00 Australia/Melbourne", CRM_BLACKOUT_DATES=())
class EveningEventWindowTests(SimpleTestCase):
    def test_a_starts_soon_reminder_can_go_in_the_evening(self):
        events = org_streams()[1]
        evening = datetime(2026, 11, 13, 19, 15, tzinfo=MELBOURNE)
        self.assertIsNotNone(volume.window(events, evening, critical=True))
        self.assertIsNone(volume.window(events, datetime(2026, 11, 13, 20, 5, tzinfo=MELBOURNE), critical=True))
        # Ordinary campaigns keep office hours.
        self.assertIsNone(volume.window(events, evening, critical=False))
