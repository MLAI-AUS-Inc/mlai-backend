"""PostgreSQL-backed checks for segment rules, suppression and the segment API (step B3)."""

import uuid
from datetime import timedelta

from django.db.models import Q
from django.db.models.functions import Lower
from django.test import TestCase
from django.utils import timezone

from community_chat.models import CommunityChatDevice
from core.models import AccountBan, User
from crm.models import Contact, CrmAuditEvent, EventRegistration, Segment
from crm.segments import evaluate, quick_segment_definition, segment_contacts

from .api_fixtures import PREFIX, CrmApiMixin, ban, crm_enabled, make_contact, make_event, register

WHAT = "What do you do?"


class Population:
    """Seven synthetic people across one past and one upcoming event."""

    def build(self):
        now = self.now = timezone.now()
        self.meetup = make_event("evt-meetup", start=now - timedelta(days=30), name="Meetup")
        self.summit = make_event("evt-summit", start=now + timedelta(days=10), name="Summit")
        account = User.objects.create_user("ada-account@example.test")
        CommunityChatDevice.objects.create(user=account, public_key="e" * 64, status="verified")
        self.ada = make_contact(
            "ada@example.com",
            user=account,
            display_name="Ada",
            marketing_consent=Contact.MarketingConsent.GRANTED,
            tags=["founder", "speaker"],
            city="Sydney",
            company="Analytical Bank",
            attributes={"favourite_model": "Claude Opus"},
            events_attended_count=3,
            last_attended_at=now - timedelta(days=20),
            phone_e164="+61412345678",
            sms_status=Contact.SmsStatus.ACTIVE,
            whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED,
        )
        self.bob = make_contact(
            "bob@example.com",
            display_name="Bob",
            source=Contact.Source.IMPORT,
            email_status=Contact.EmailStatus.UNSUBSCRIBED,
            tags=["founder"],
            city="Melbourne",
            attributes={"favourite_model": "gpt"},
            events_attended_count=1,
            last_attended_at=now - timedelta(days=400),
        )
        self.cat = make_contact(
            "cat@example.com",
            display_name="Cat",
            source=Contact.Source.MANUAL,
            marketing_consent=Contact.MarketingConsent.GRANTED,
            city="sydney",
        )
        ban("cat@example.com")
        self.dan = make_contact(
            "dan@example.com",
            display_name="Dan",
            source=Contact.Source.IMPORT,
            tags=["speaker"],
            events_attended_count=2,
            last_attended_at=now - timedelta(days=100),
            phone_e164="+64211234567",
            sms_status=Contact.SmsStatus.ACTIVE,
        )
        self.eve = make_contact(
            "eve@example.com", display_name="Eve", marketing_consent=Contact.MarketingConsent.WITHDRAWN
        )
        self.fay = make_contact(
            "fay@example.com",
            display_name="Fay",
            source=Contact.Source.CHAT_ACCOUNT,
            events_attended_count=1,
            last_attended_at=now - timedelta(days=800),
            whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED,
        )
        self.gus = make_contact(
            "gus@example.com",
            display_name="Gus",
            email_status=Contact.EmailStatus.BOUNCED,
            marketing_consent=Contact.MarketingConsent.GRANTED,
            tags=["founder"],
            events_attended_count=1,
            last_attended_at=now - timedelta(days=30),
            phone_e164="+61498765432",
            sms_status=Contact.SmsStatus.OPTED_OUT,
        )
        checked_in = now - timedelta(days=30) + timedelta(minutes=10)
        register(
            self.meetup, self.ada, checked_in_at=checked_in,
            registration_answers=[{"label": WHAT, "value": "AI research"}],
        )
        register(
            self.meetup, self.bob,
            registration_answers=[{"label": WHAT, "value": "Banking"}, {"label": "Dietary", "value": "None"}],
        )
        register(
            self.meetup, self.dan, checked_in_at=checked_in,
            registration_answers=[{"label": "Dietary", "value": ""}, {"label": WHAT, "value": "Research engineer"}],
        )
        register(self.meetup, self.eve, "waitlist")
        register(self.meetup, self.gus, checked_in_at=checked_in, registration_answers=[{"label": WHAT, "value": ""}])
        register(self.summit, self.ada)
        register(self.summit, self.cat)
        register(self.summit, self.fay, "pending_approval")
        register(self.summit, self.eve, removed_at=now)
        register(self.summit, self.dan, "declined")

    def people(self, *contacts):
        return {contact.email for contact in contacts}

    def everyone_but(self, *contacts):
        return set(Contact.objects.values_list("email", flat=True)) - self.people(*contacts)


class SegmentRuleDatabaseTests(Population, TestCase):
    def setUp(self):
        self.build()

    def match(self, *rules, match="all", exclude=()):
        definition = {"match": match, "rules": list(rules), "exclude": list(exclude)}
        return set(segment_contacts(definition, now=self.now).values_list("email", flat=True))

    def test_event_registration(self):
        meetup = str(self.meetup.pk)
        rule = {"type": "event_registration", "event_ids": [meetup], "statuses": ["going"]}
        self.assertEqual(self.match({**rule, "checked_in": True}), self.people(self.ada, self.dan, self.gus))
        self.assertEqual(self.match({**rule, "checked_in": False}), self.people(self.bob))
        self.assertEqual(
            self.match({"type": "event_registration", "event_ids": [meetup], "statuses": ["waitlist"]}),
            self.people(self.eve),
        )
        self.assertEqual(
            self.match({"type": "event_registration", "statuses": ["pending"]}), self.people(self.fay)
        )
        # Removed and declined guests are never registered.
        self.assertEqual(
            self.match({"type": "event_registration", "event_ids": [str(self.summit.pk)]}),
            self.people(self.ada, self.cat, self.fay),
        )

    def test_events_attended_count(self):
        self.assertEqual(self.match({"type": "events_attended_count", "op": "gte", "value": 2}), self.people(self.ada, self.dan))
        self.assertEqual(
            self.match({"type": "events_attended_count", "op": "eq", "value": 1}),
            self.people(self.bob, self.fay, self.gus),
        )
        self.assertEqual(self.match({"type": "events_attended_count", "op": "lte", "value": 0}), self.people(self.cat, self.eve))

    def test_last_attended_within_days(self):
        self.assertEqual(
            self.match({"type": "last_attended_within_days", "value": 180}),
            self.people(self.ada, self.dan, self.gus),
        )

    def test_tag(self):
        self.assertEqual(self.match({"type": "tag", "op": "has_any", "values": ["speaker"]}), self.people(self.ada, self.dan))
        self.assertEqual(self.match({"type": "tag", "op": "has_all", "values": ["founder", "speaker"]}), self.people(self.ada))
        self.assertEqual(
            self.match({"type": "tag", "op": "has_none", "values": ["founder"]}),
            self.people(self.cat, self.dan, self.eve, self.fay),
        )

    def test_field(self):
        self.assertEqual(self.match({"type": "field", "field": "city", "op": "equals", "value": "SYDNEY"}), self.people(self.ada, self.cat))
        self.assertEqual(self.match({"type": "field", "field": "company", "op": "contains", "value": "bank"}), self.people(self.ada))
        self.assertEqual(self.match({"type": "field", "field": "city", "op": "is_set"}), self.people(self.ada, self.bob, self.cat))
        self.assertEqual(
            self.match({"type": "field", "field": "city", "op": "is_not_set"}),
            self.people(self.dan, self.eve, self.fay, self.gus),
        )

    def test_attribute(self):
        rule = {"type": "attribute", "key": "favourite_model"}
        self.assertEqual(self.match({**rule, "op": "contains", "value": "claude"}), self.people(self.ada))
        self.assertEqual(self.match({**rule, "op": "equals", "value": "GPT"}), self.people(self.bob))
        self.assertEqual(self.match({**rule, "op": "is_set"}), self.people(self.ada, self.bob))
        self.assertEqual(self.match({**rule, "op": "is_not_set"}), self.everyone_but(self.ada, self.bob))

    def test_registration_answer(self):
        rule = {"type": "registration_answer", "label": WHAT}
        self.assertEqual(self.match({**rule, "op": "contains", "value": "research"}), self.people(self.ada, self.dan))
        self.assertEqual(self.match({**rule, "op": "equals", "value": "banking"}), self.people(self.bob))
        self.assertEqual(self.match({**rule, "op": "is_set"}), self.people(self.ada, self.bob, self.dan))
        self.assertEqual(self.match({**rule, "op": "is_not_set"}), self.everyone_but(self.ada, self.bob, self.dan))
        self.assertEqual(
            self.match({"type": "registration_answer", "label": "Dietary", "op": "is_set"}), self.people(self.bob)
        )

    def test_chat_member(self):
        self.assertEqual(self.match({"type": "chat_member", "value": True}), self.people(self.ada))
        self.assertEqual(self.match({"type": "chat_member", "value": False}), self.everyone_but(self.ada))
        CommunityChatDevice.objects.filter(public_key="e" * 64).update(revoked_at=self.now)
        self.assertEqual(self.match({"type": "chat_member", "value": True}), set())

    def test_source(self):
        self.assertEqual(self.match({"type": "source", "values": ["import"]}), self.people(self.bob, self.dan))
        self.assertEqual(
            self.match({"type": "source", "values": ["luma", "manual"]}),
            self.people(self.ada, self.cat, self.eve, self.gus),
        )

    def test_reachable(self):
        self.assertEqual(
            self.match({"type": "reachable", "channel": "email"}),
            self.people(self.ada, self.dan, self.eve, self.fay),
        )
        self.assertEqual(self.match({"type": "reachable", "channel": "sms"}), self.people(self.ada))
        self.assertEqual(self.match({"type": "reachable", "channel": "whatsapp"}), self.people(self.ada))
        with self.settings(CRM_SMS_ALLOWED_PREFIXES=["+614", "+64"]):
            self.assertEqual(self.match({"type": "reachable", "channel": "sms"}), self.people(self.ada, self.dan))

    def test_match_any_with_exclusions(self):
        self.assertEqual(
            self.match(
                {"type": "tag", "op": "has_any", "values": ["founder"]},
                {"type": "source", "values": ["import"]},
                match="any",
                exclude=[{"type": "reachable", "channel": "email"}],
            ),
            self.people(self.bob, self.gus),
        )
        self.assertEqual(self.match(), self.everyone_but())


class ReferenceSegmentDatabaseTests(Population, TestCase):
    """Five saved-segment shapes against queries written out by hand."""

    def setUp(self):
        self.build()
        banned_emails = (
            AccountBan.objects.filter(revoked_at__isnull=True).annotate(key=Lower("email")).values("key")
        )
        self.sendable = Contact.objects.filter(email_status="active").exclude(email__in=banned_emails)
        self.consent = ~Q(marketing_consent="withdrawn") & (
            Q(marketing_consent="granted") | Q(last_attended_at__gte=self.now - timedelta(days=730))
        )

    def audience(self, definition, purpose=None, event=None):
        queryset, excluded = evaluate(definition, purpose, event, now=self.now)
        return set(queryset.values_list("email", flat=True)), excluded

    def emails(self, queryset):
        return set(queryset.values_list("email", flat=True))

    def registered_for(self, event):
        return EventRegistration.objects.filter(event=event, removed_at__isnull=True).exclude(
            approval_status="declined"
        )

    def test_checked_in_at_the_meetup_for_an_event_update(self):
        definition = {"rules": [{"type": "event_registration", "event_ids": [str(self.meetup.pk)], "checked_in": True}]}
        registered = self.registered_for(self.meetup)
        expected = self.sendable.filter(
            pk__in=registered.filter(checked_in_at__isnull=False).values("contact_id")
        ).filter(pk__in=registered.values("contact_id"))
        found, excluded = self.audience(definition, "event_update", self.meetup)
        self.assertEqual(found, self.emails(expected))
        self.assertEqual(found, self.people(self.ada, self.dan))
        self.assertEqual(excluded["bounced"], 1)

    def test_recent_founders_and_speakers_for_marketing(self):
        definition = {
            "rules": [
                {"type": "tag", "op": "has_any", "values": ["founder", "speaker"]},
                {"type": "last_attended_within_days", "value": 180},
            ]
        }
        expected = (
            self.sendable.filter(Q(tags__contains=["founder"]) | Q(tags__contains=["speaker"]))
            .filter(last_attended_at__gte=self.now - timedelta(days=180))
            .filter(self.consent)
        )
        found, _ = self.audience(definition, "marketing")
        self.assertEqual(found, self.emails(expected))
        self.assertEqual(found, self.people(self.ada, self.dan))

    def test_sydney_contacts_who_can_get_a_text(self):
        definition = {
            "rules": [
                {"type": "field", "field": "city", "op": "equals", "value": "sydney"},
                {"type": "reachable", "channel": "sms"},
            ]
        }
        expected = self.sendable.filter(
            city__iexact="sydney", sms_status="active", phone_e164__startswith="+614"
        )
        found, excluded = self.audience(definition)
        self.assertEqual(found, self.emails(expected))
        self.assertEqual(found, self.people(self.ada))
        self.assertEqual(excluded["banned"], 0)

    def test_researchers_who_are_not_chat_members_for_marketing(self):
        definition = {
            "rules": [{"type": "registration_answer", "label": WHAT, "op": "contains", "value": "research"}],
            "exclude": [{"type": "chat_member", "value": True}],
        }
        answered = EventRegistration.objects.filter(
            removed_at__isnull=True,
            registration_answers__contains=[{"label": WHAT}],
            registration_answers__icontains="research",
        )
        members = CommunityChatDevice.objects.filter(
            status="verified", revoked_at__isnull=True, user__is_active=True
        ).values("user_id")
        expected = (
            self.sendable.filter(pk__in=answered.values("contact_id"))
            .exclude(user_id__in=members)
            .filter(self.consent)
        )
        found, _ = self.audience(definition, "marketing")
        self.assertEqual(found, self.emails(expected))
        self.assertEqual(found, self.people(self.dan))

    def test_summit_going_quick_segment_for_an_event_update(self):
        definition = quick_segment_definition("going", self.summit)
        going = self.registered_for(self.summit).filter(approval_status="approved")
        expected = self.sendable.filter(pk__in=going.values("contact_id"))
        found, excluded = self.audience(definition, "event_update", self.summit)
        self.assertEqual(found, self.emails(expected))
        self.assertEqual(found, self.people(self.ada))
        self.assertEqual(excluded["banned"], 1)

    def test_exclusions_are_counted_once_under_the_first_reason(self):
        everyone = {"rules": []}
        found, excluded = self.audience(everyone)
        self.assertEqual(found, self.people(self.ada, self.dan, self.eve, self.fay))
        self.assertEqual(
            excluded, {"unsubscribed": 1, "bounced": 1, "banned": 1, "no_consent": 0, "not_registered": 0}
        )
        found, excluded = self.audience(everyone, "marketing")
        self.assertEqual(found, self.people(self.ada, self.dan))
        self.assertEqual(excluded["no_consent"], 2)
        found, excluded = self.audience(everyone, "event_update", self.summit)
        self.assertEqual(found, self.people(self.ada, self.fay))
        self.assertEqual(
            excluded, {"unsubscribed": 1, "bounced": 1, "banned": 1, "no_consent": 0, "not_registered": 2}
        )


@crm_enabled
class SegmentApiDatabaseTests(CrmApiMixin, Population, TestCase):
    def setUp(self):
        super().setUp()
        self.build()

    def test_create_list_update_and_delete(self):
        response = self.client.post(
            PREFIX + "segments/",
            {"name": "  Founders ", "definition": {"rules": [{"type": "tag", "op": "has_any", "values": ["founder"]}]}},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        segment = response.json()
        self.assertEqual(segment["name"], "Founders")
        self.assertEqual(segment["definition"]["match"], "all")
        self.assertEqual(segment["definition"]["exclude"], [])
        self.assertEqual(segment["last_count"], 1)
        self.assertIsNotNone(segment["last_counted_at"])
        url = PREFIX + f"segments/{segment['id']}/"

        listed = self.client.get(PREFIX + "segments/").json()["results"]
        self.assertEqual([item["id"] for item in listed], [segment["id"]])

        renamed = self.client.patch(url, {"name": "Founders and friends"}, format="json").json()
        self.assertEqual((renamed["name"], renamed["last_count"]), ("Founders and friends", 1))
        widened = self.client.patch(
            url, {"definition": {"rules": [{"type": "reachable", "channel": "email"}]}}, format="json"
        ).json()
        self.assertEqual(widened["last_count"], 4)
        self.assertEqual(self.client.get(url).json()["definition"]["rules"][0]["type"], "reachable")

        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.get(url).status_code, 404)
        audits = CrmAuditEvent.objects.filter(target_type="segment", action=CrmAuditEvent.Action.EDIT)
        self.assertEqual(
            sorted(audit.detail["operation"] for audit in audits), ["create", "delete", "update", "update"]
        )
        self.assertTrue(all(audit.actor == self.admin for audit in audits))

    def test_invalid_and_unavailable_definitions(self):
        invalid = self.client.post(
            PREFIX + "segments/",
            {"name": "Broken", "definition": {"rules": [{"type": "tag", "op": "has_any", "values": []}]}},
            format="json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual((invalid.json()["error"], invalid.json()["pointer"]), ("invalid_definition", "/rules/0/values"))
        missing = self.client.post(PREFIX + "segments/", {"name": "No rules"}, format="json")
        self.assertEqual(missing.json()["fields"], {"definition": ["required"]})

        engagement = {"rules": [{"type": "blast_engagement", "blast_id": str(uuid.uuid4()), "action": "clicked"}]}
        saved = self.client.post(PREFIX + "segments/", {"name": "Clickers", "definition": engagement}, format="json")
        self.assertEqual(saved.status_code, 201)
        self.assertIsNone(saved.json()["last_count"])
        preview = self.client.post(PREFIX + "segments/preview/", {"definition": engagement}, format="json")
        self.assertEqual(preview.status_code, 400)
        self.assertEqual((preview.json()["error"], preview.json()["pointer"]), ("rule_not_available", "/rules/0"))
        listed = self.client.get(PREFIX + f"contacts/?segment_id={saved.json()['id']}")
        self.assertEqual(listed.json()["error"], "rule_not_available")
        self.assertFalse(Segment.objects.filter(name="Broken").exists())

    def test_preview(self):
        for index in range(25):
            make_contact(f"extra{index:02d}@example.com", tags=["extra"])
        body = self.client.post(
            PREFIX + "segments/preview/", {"definition": {"rules": []}, "purpose": "marketing"}, format="json"
        ).json()
        self.assertEqual(body["count"], 2)
        self.assertEqual({row["email"] for row in body["sample"]}, self.people(self.ada, self.dan))
        self.assertEqual(
            body["excluded"],
            {"unsubscribed": 1, "bounced": 1, "banned": 1, "no_consent": 27, "not_registered": 0},
        )

        everyone = self.client.post(PREFIX + "segments/preview/", {"definition": {}}, format="json").json()
        self.assertEqual(everyone["count"], 29)
        self.assertEqual(len(everyone["sample"]), 20)
        self.assertEqual(everyone["sample"][0]["email"], "ada@example.com")

        event_update = self.client.post(
            PREFIX + "segments/preview/",
            {"definition": {}, "purpose": "event_update", "event_id": str(self.summit.pk)},
            format="json",
        ).json()
        self.assertEqual(event_update["count"], 2)
        self.assertEqual(event_update["excluded"]["not_registered"], 27)
        missing_event = self.client.post(
            PREFIX + "segments/preview/", {"definition": {}, "purpose": "event_update"}, format="json"
        )
        self.assertEqual(missing_event.json()["error"], "event_required")
        unknown_event = self.client.post(
            PREFIX + "segments/preview/",
            {"definition": {}, "purpose": "event_update", "event_id": str(uuid.uuid4())},
            format="json",
        )
        self.assertEqual((unknown_event.status_code, unknown_event.json()["error"]), (404, "event_not_found"))

    def test_contact_list_by_segment(self):
        segment = Segment.objects.create(
            name="Founders", definition={"rules": [{"type": "tag", "op": "has_any", "values": ["founder"]}]}
        )
        body = self.client.get(PREFIX + f"contacts/?segment_id={segment.pk}").json()
        self.assertEqual(body["count"], 3)
        self.assertEqual({row["email"] for row in body["results"]}, self.people(self.ada, self.bob, self.gus))
        missing = self.client.get(PREFIX + f"contacts/?segment_id={uuid.uuid4()}")
        self.assertEqual((missing.status_code, missing.json()["error"]), (404, "segment_not_found"))

    def test_catalog(self):
        body = self.client.get(PREFIX + "segments/catalog/").json()
        self.assertEqual(body["tags"], [{"tag": "founder", "count": 3}, {"tag": "speaker", "count": 2}])
        self.assertEqual(body["attributes"], [{"key": "favourite_model", "count": 2}])
        self.assertEqual(
            body["registration_questions"], [{"label": WHAT, "count": 3}, {"label": "Dietary", "count": 1}]
        )
        self.assertIn("blast_engagement", {item["type"] for item in body["rule_types"]})
