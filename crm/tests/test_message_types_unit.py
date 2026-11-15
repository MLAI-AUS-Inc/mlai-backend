"""Invites, blasts, reminders and Ask Roo to draft, without a database."""

from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase, override_settings

from crm import message_types, reminders, roo_draft
from crm.blasts import BlastError, check_channel_rules
from crm.models import Blast, CrmEvent

MELBOURNE = ZoneInfo("Australia/Melbourne")
UTC = dt_timezone.utc


def event(**values):
    defaults = dict(name="AI Builders Night", url="https://luma.com/abc123", cover_url="https://images.lumacdn.com/c.png",
                    start_at=datetime(2026, 11, 20, 18, 0, tzinfo=MELBOURNE), end_at=None,
                    timezone="Australia/Melbourne", visibility="public", approved_count=40, guest_count=45,
                    raw_payload={"geo_address_json": {"full_address": "Stone & Chalk, 121 King St, Melbourne"},
                                 "description_md": "Demos and drinks."},
                    reminder_settings={})
    defaults.update(values)
    return CrmEvent(**defaults)


def blast(kind, purpose, *, with_event=True, **values):
    item = Blast(message_type=kind, purpose=purpose, channels=["email"], **values)
    if with_event:
        item.event = event()
    return item


class RulesTests(SimpleTestCase):
    def test_an_invite_is_marketing_tied_to_one_event(self):
        check_channel_rules(blast("invite", "marketing"))
        for item, code in ((blast("invite", "event_update"), "invite_must_be_marketing"),
                           (blast("invite", "marketing", with_event=False), "event_required"),
                           (blast("invite", "marketing", quick_segment="going"), "invite_cannot_target_guests")):
            with self.subTest(code=code), self.assertRaises(BlastError) as caught:
                check_channel_rules(item)
            self.assertEqual(caught.exception.code, code)

    def test_a_blast_goes_to_guests_as_an_event_update(self):
        check_channel_rules(blast("blast", "event_update", quick_segment="going"))
        with self.assertRaises(BlastError) as caught:
            check_channel_rules(blast("blast", "marketing"))
        self.assertEqual(caught.exception.code, "blast_must_be_event_update")

    def test_general_marketing_keeps_working_without_a_type(self):
        check_channel_rules(blast("", "marketing", with_event=False))


class LayoutTests(SimpleTestCase):
    def test_invite_leads_with_the_event_and_ends_on_register(self):
        body = message_types.layout(blast("invite", "marketing"), "Come along.", campaign="crm-blast-1")
        self.assertTrue(body.startswith("![AI Builders Night](https://images.lumacdn.com/c.png)"))
        self.assertIn("### AI Builders Night", body)
        self.assertIn("Fri 20 Nov, 6pm · Stone & Chalk, 121 King St, Melbourne", body)
        self.assertEqual(body.count("Register →"), 2)
        self.assertIn("utm_campaign=crm-blast-1", body)
        self.assertLess(body.index("Come along."), body.rindex("Register →"))

    def test_blast_puts_the_message_first_and_a_compact_card_below(self):
        body = message_types.layout(blast("blast", "event_update"), "Doors open at 5:45.", campaign="c")
        self.assertTrue(body.startswith("Doors open at 5:45."))
        self.assertIn("View event →", body)
        self.assertNotIn("![", body)

    def test_event_text_cannot_open_a_template_or_html(self):
        item = blast("blast", "event_update")
        item.event.name = "{{ .Subscriber.Email }} <script>"
        body = message_types.layout(item, "Hi", campaign="c")
        self.assertNotIn("{{", body)
        self.assertNotIn("<script>", body)

    def test_general_marketing_is_left_alone(self):
        self.assertEqual(message_types.layout(blast("", "marketing"), "Hello", campaign="c"), "Hello")


class ReminderTimingTests(SimpleTestCase):
    def at(self, *args):
        return datetime(*args, tzinfo=MELBOURNE)

    def test_an_evening_event_gets_reminders_24_hours_and_1_hour_before(self):
        item = event(start_at=self.at(2026, 11, 20, 18, 0))
        self.assertEqual(reminders.send_time(item, "day_before"), self.at(2026, 11, 19, 18, 0))
        self.assertEqual(reminders.send_time(item, "hour_before"), self.at(2026, 11, 20, 17, 0))

    def test_an_early_morning_event_moves_both_reminders_into_sending_hours(self):
        item = event(start_at=self.at(2026, 11, 21, 7, 30))
        # 7:30am the day before is too early, so it goes at 8am that day; the
        # 1-hour reminder (6:30am) would land after the event starts, so it is skipped.
        self.assertEqual(reminders.send_time(item, "day_before"), self.at(2026, 11, 20, 8, 0))
        self.assertIsNone(reminders.send_time(item, "hour_before"))
        nine = event(start_at=self.at(2026, 11, 21, 9, 0))
        self.assertEqual(reminders.send_time(nine, "day_before"), self.at(2026, 11, 20, 9, 0))
        self.assertEqual(reminders.send_time(nine, "hour_before"), self.at(2026, 11, 21, 8, 0))

    def test_a_late_event_sends_no_later_than_8pm(self):
        item = event(start_at=self.at(2026, 11, 20, 21, 30))
        self.assertEqual(reminders.send_time(item, "hour_before"), self.at(2026, 11, 20, 20, 0))

    def test_an_early_event_skips_a_reminder_that_cannot_go_in_time(self):
        item = event(start_at=self.at(2026, 11, 21, 8, 10))
        self.assertIsNone(reminders.send_time(item, "hour_before"))

    def test_private_and_tiny_events_are_skipped_unless_staff_force_them(self):
        self.assertFalse(reminders.eligible(event(visibility="private"), reminders.settings_for(event())))
        tiny = event(approved_count=1, guest_count=1)
        self.assertFalse(reminders.eligible(tiny, reminders.settings_for(tiny)))
        forced = event(approved_count=1, guest_count=1, reminder_settings={"force": True})
        self.assertTrue(reminders.eligible(forced, reminders.settings_for(forced)))

    def test_fixed_text_includes_the_staff_note(self):
        item = event(reminder_settings={"note": "Enter via the side door"})
        self.assertEqual(message_types.reminder_subject(item, "day_before"),
                         "Reminder: AI Builders Night starts tomorrow")
        body = message_types.reminder_body(item, "hour_before", "Enter via the side door")
        self.assertIn("starts in 1 hour, at 6pm.", body)
        self.assertIn("Enter via the side door", body)
        self.assertTrue(body.startswith("Hi {{ first_name }},"))

    @override_settings(CRM_AUTO_REMINDERS_ENABLED=False)
    def test_the_runner_is_off_by_default(self):
        self.assertEqual(reminders.run_reminder_scheduler()["reason"], "disabled")


class FakeResponse:
    def __init__(self, text):
        self.output_text, self.output = text, []


class FakeClient:
    def __init__(self, text):
        self.text, self.calls = text, []
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.text)


class RooDraftTests(SimpleTestCase):
    def setUp(self):
        self.blast = blast("invite", "marketing", sign_off_name="Sam from the MLAI team")
        self.enterContext(patch("crm.roo_draft.past_examples", return_value=[]))

    def test_the_model_gets_event_facts_and_no_contact_data(self):
        payload = roo_draft.build_input(self.blast, "mention free pizza")
        self.assertEqual(payload["message_type"], "invite")
        self.assertEqual(payload["event"]["starts"], "Fri 20 Nov, 6pm")
        self.assertEqual(payload["untrusted_staff_note"], "mention free pizza")
        self.assertNotIn("@", str(payload).replace("{{ first_name }}", ""))

    def test_reminders_keep_their_fixed_text(self):
        with self.assertRaises(roo_draft.RooDraftError):
            roo_draft.build_input(blast("reminder", "event_update"), "")

    def test_a_clean_draft_passes(self):
        facts = roo_draft.build_input(self.blast, "")
        out = roo_draft.check_draft(self.blast, {
            "subject": "Join us for AI Builders Night", "preview_text": "Demos, drinks and builders",
            "body_markdown": "Hi {{ first_name }},\n\nCome see demos. [Register](https://luma.com/abc123)", "sms": "x",
        }, facts)
        self.assertEqual(out["sms"], "")  # SMS isn't selected.

    def test_drafts_that_break_the_rules_are_refused(self):
        facts = roo_draft.build_input(self.blast, "")
        base = {"subject": "Hi", "preview_text": "", "sms": ""}
        for body, reason in (("Hi {{ email }}", "merge_tags"),
                             ("See https://evil.example/x", "link"),
                             ("Tickets are $20", "price"),
                             ("![x](https://luma.com/abc123)", "image")):
            with self.subTest(reason=reason), self.assertRaises(roo_draft.RooDraftError) as caught:
                roo_draft.check_draft(self.blast, {**base, "body_markdown": body}, facts)
            self.assertEqual(caught.exception.extra.get("reason"), reason)

    def test_call_model_asks_for_structured_output(self):
        client = FakeClient('{"subject": "S", "preview_text": "P", "body_markdown": "B", "sms": ""}')
        result = roo_draft.call_model({"a": 1}, client=client)
        self.assertEqual(result["subject"], "S")
        self.assertEqual(client.calls[0]["text"]["format"]["type"], "json_schema")
        self.assertFalse(client.calls[0]["store"])

    @override_settings(CRM_ROO_DRAFT_ENABLED=False)
    def test_disabled_by_default(self):
        with self.assertRaises(roo_draft.RooDraftError) as caught:
            roo_draft.draft(self.blast, "", actor=None)
        self.assertEqual(caught.exception.code, "roo_draft_disabled")
