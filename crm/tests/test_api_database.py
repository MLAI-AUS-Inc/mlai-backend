"""PostgreSQL-backed checks for the contacts API (steps B1 and B2); runs in CI."""

import csv
import io
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.models import CommunityChatDevice
from core.models import User
from crm.contact_import import apply_import, read_csv
from crm.models import (
    Blast,
    BlastDelivery,
    Contact,
    ContactConsentEvent,
    ContactImport,
    CrmAuditEvent,
    InboundMessage,
)

from .api_fixtures import PREFIX, CrmApiMixin, ban, crm_enabled, make_contact, make_event, register


@crm_enabled
class AccessDatabaseTests(CrmApiMixin, TestCase):
    def test_access_answers_every_signed_in_account(self):
        response = self.client.get(PREFIX + "access/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["enabled"], body["can_view"], body["can_send"]), (True, True, True))
        self.assertEqual(body["luma_sync"], {"enabled": False, "last_synced_at": None, "status": "never"})
        self.assertEqual(set(body["channels"]), {"email", "sms", "whatsapp"})
        self.assertEqual(response["Cache-Control"], "no-store")

        member = self.member_client.get(PREFIX + "access/")
        self.assertEqual(member.status_code, 200)
        self.assertEqual(
            member.json(),
            {"enabled": True, "can_view": False, "can_send": False, "luma_sync": None, "channels": None},
        )
        self.assertEqual(APIClient().get(PREFIX + "access/").status_code, 401)

    def test_access_reports_the_last_sync_outcome(self):
        CrmAuditEvent.objects.create(
            action=CrmAuditEvent.Action.SYNC,
            target_type="luma_calendar",
            detail={"failed_events": ["evt-1"], "rate_limited": False},
        )
        self.assertEqual(self.client.get(PREFIX + "access/").json()["luma_sync"]["status"], "partial")

    def test_a_revoked_installation_loses_admin_access(self):
        CommunityChatDevice.objects.filter(user=self.admin).update(revoked_at=timezone.now())
        response = self.client.get(PREFIX + "contacts/")
        self.assertIn(response.status_code, (401, 403))

    def test_members_are_refused_everywhere(self):
        some_id = uuid.uuid4()
        requests = [
            ("get", "overview/"),
            ("get", "events/"),
            ("get", f"events/{some_id}/"),
            ("get", f"events/{some_id}/guests/"),
            ("get", "contacts/"),
            ("post", "contacts/"),
            ("get", f"contacts/{some_id}/"),
            ("patch", f"contacts/{some_id}/"),
            ("post", f"contacts/{some_id}/stop-messaging/"),
            ("get", "tags/"),
            ("post", "imports/plan/"),
            ("get", "imports/"),
            ("post", "imports/"),
            ("post", f"imports/{some_id}/rows/"),
            ("post", f"imports/{some_id}/finish/"),
            ("get", "segments/"),
            ("post", "segments/"),
            ("patch", f"segments/{some_id}/"),
            ("delete", f"segments/{some_id}/"),
            ("post", "segments/preview/"),
            ("get", "segments/catalog/"),
        ]
        for method, path in requests:
            with self.subTest(method=method, path=path):
                response = getattr(self.member_client, method)(PREFIX + path, {}, format="json")
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {"error": "not_authorised", "outcome": "not_authorised"})

    def test_the_flag_hides_every_endpoint_but_access(self):
        with override_settings(COMMUNITY_CHAT_CRM_ENABLED=False):
            response = self.client.get(PREFIX + "contacts/")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"error": "crm_disabled", "outcome": "crm_disabled"})
            access = self.client.get(PREFIX + "access/").json()
        self.assertEqual((access["enabled"], access["can_view"]), (False, False))

    def test_desktop_origin_reaches_the_crm_routes(self):
        preflight = self.client.options(
            PREFIX + "contacts/",
            HTTP_ORIGIN="tauri://localhost",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type",
        )
        self.assertEqual(preflight.status_code, 204)
        self.assertEqual(preflight["Access-Control-Allow-Origin"], "tauri://localhost")
        response = self.client.get(PREFIX + "contacts/", HTTP_ORIGIN="tauri://localhost")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Access-Control-Allow-Origin"], "tauri://localhost")


@crm_enabled
class ContactListDatabaseTests(CrmApiMixin, TestCase):
    def test_paging_keeps_filters_and_counts_only_the_first_page(self):
        for index in range(25):
            make_contact(f"person{index:02d}@example.com", tags=["founder"])
        make_contact("other@example.com")

        first = self.client.get(PREFIX + "contacts/?tag=founder&limit=10").json()
        self.assertEqual(first["count"], 25)
        self.assertEqual(len(first["results"]), 10)
        self.assertEqual(first["next"], "?tag=founder&limit=10&offset=10")
        second = self.client.get(PREFIX + "contacts/" + first["next"]).json()
        self.assertNotIn("count", second)
        third = self.client.get(PREFIX + "contacts/?tag=founder&limit=10&offset=20").json()
        self.assertIsNone(third["next"])
        self.assertEqual(len(third["results"]), 5)
        seen = self.ids(first["results"]) | self.ids(second["results"]) | self.ids(third["results"])
        self.assertEqual(len(seen), 25)
        self.assertEqual(len(self.client.get(PREFIX + "contacts/?limit=0").json()["results"]), 1)
        self.assertEqual(self.client.get(PREFIX + "contacts/?offset=x").json()["error"], "invalid_pagination")

    def test_search_matches_names_email_company_and_phone_digits(self):
        ada = make_contact(
            "ada@example.com",
            first_name="Ada",
            last_name="Lovelace",
            company="Analytical Engines",
            phone_e164="+61412345678",
            sms_status=Contact.SmsStatus.ACTIVE,
        )
        grace = make_contact("grace@navy.example", first_name="Grace", last_name="Hopper")
        cases = {
            "ada lovelace": {ada},
            "ANALYTICAL": {ada},
            "0412 345": {ada},
            "+61 412 345 678": {ada},
            "hopper": {grace},
            "navy.example": {grace},
            "lovelace hopper": set(),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                body = self.client.get(PREFIX + "contacts/", {"q": text}).json()
                self.assertEqual(self.ids(body["results"]), {str(contact.pk) for contact in expected})
                self.assertEqual(body["count"], len(expected))

    def test_filters_and_marketing_eligibility(self):
        now = timezone.now()
        granted = make_contact("granted@example.com", marketing_consent=Contact.MarketingConsent.GRANTED)
        attended = make_contact("attended@example.com", last_attended_at=now - timedelta(days=100))
        make_contact("lapsed@example.com", last_attended_at=now - timedelta(days=800))
        make_contact(
            "withdrawn@example.com",
            marketing_consent=Contact.MarketingConsent.WITHDRAWN,
            last_attended_at=now - timedelta(days=10),
        )
        make_contact(
            "unsubscribed@example.com",
            marketing_consent=Contact.MarketingConsent.GRANTED,
            email_status=Contact.EmailStatus.UNSUBSCRIBED,
        )
        make_contact("banned@example.com", marketing_consent=Contact.MarketingConsent.GRANTED)
        ban("banned@example.com")
        mobile = make_contact(
            "mobile@example.com",
            phone_e164="+61412345678",
            sms_status=Contact.SmsStatus.OPTED_OUT,
            whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED,
        )

        eligible = self.client.get(PREFIX + "contacts/?eligible=marketing").json()
        self.assertEqual(self.ids(eligible["results"]), {str(granted.pk), str(attended.pk)})
        self.assertTrue(all(row["marketing_eligible"] for row in eligible["results"]))
        everyone = {row["email"]: row for row in self.client.get(PREFIX + "contacts/").json()["results"]}
        self.assertFalse(everyone["banned@example.com"]["marketing_eligible"])
        self.assertFalse(everyone["lapsed@example.com"]["marketing_eligible"])

        def emails(query):
            return {row["email"] for row in self.client.get(PREFIX + "contacts/" + query).json()["results"]}

        self.assertEqual(emails("?email_status=unsubscribed"), {"unsubscribed@example.com"})
        self.assertEqual(emails("?has_mobile=1"), {mobile.email})
        self.assertNotIn(mobile.email, emails("?has_mobile=0"))
        self.assertEqual(emails("?whatsapp=granted"), {mobile.email})
        self.assertEqual(self.client.get(PREFIX + "contacts/?whatsapp=yes").json()["error"], "invalid_filter")

    def test_sorting(self):
        now = timezone.now()
        make_contact("zed@example.com", display_name="Zed", events_attended_count=1)
        make_contact("amy@example.com", first_name="amy", events_attended_count=5, last_attended_at=now)
        make_contact("bea@example.com", events_attended_count=5, last_attended_at=now - timedelta(days=1))

        def order(sort):
            rows = self.client.get(PREFIX + "contacts/", {"sort": sort}).json()["results"]
            return [row["email"] for row in rows]

        self.assertEqual(order("name"), ["amy@example.com", "bea@example.com", "zed@example.com"])
        self.assertEqual(order("attended"), ["amy@example.com", "bea@example.com", "zed@example.com"])
        self.assertEqual(order("recent"), ["bea@example.com", "amy@example.com", "zed@example.com"])

    def test_tags_are_counted(self):
        make_contact("a@example.com", tags=["founder", "speaker"])
        make_contact("b@example.com", tags=["founder"])
        make_contact("c@example.com")
        self.assertEqual(
            self.client.get(PREFIX + "tags/").json(),
            {"results": [{"tag": "founder", "count": 2}, {"tag": "speaker", "count": 1}]},
        )


@crm_enabled
class ContactDetailDatabaseTests(CrmApiMixin, TestCase):
    def test_detail_shape(self):
        now = timezone.now()
        account = User.objects.create_user("ada@example.com", first_name="Ada", last_name="Lovelace")
        ada = make_contact(
            "ada@example.com",
            user=account,
            phone="0412 345 678",
            phone_e164="+61412345678",
            sms_status=Contact.SmsStatus.ACTIVE,
            city="Sydney",
            attributes={"favourite_model": "claude"},
            marketing_consent=Contact.MarketingConsent.GRANTED,
            marketing_consent_source="luma:evt-1",
        )
        event = make_event("evt-1", start=now - timedelta(days=3), name="MLAI Meetup")
        register(
            event,
            ada,
            checked_in_at=now - timedelta(days=3),
            registration_answers=[{"label": "What do you do?", "value": "Research", "question_id": "q1"}],
        )
        register(make_event("evt-2", start=now + timedelta(days=3)), ada, removed_at=now)
        ContactConsentEvent.objects.create(
            contact=ada, kind="granted", source="luma:evt-1", occurred_at=now - timedelta(days=4)
        )
        blast = Blast.objects.create(
            purpose="event_update",
            event=event,
            channels=["email"],
            subject="Doors open at 6",
            status="sent",
            sent_at=now - timedelta(days=3),
        )
        BlastDelivery.objects.create(
            blast=blast,
            contact=ada,
            channel="email",
            address=ada.email,
            consent_basis="event_registration",
            status="delivered",
            first_opened_at=now - timedelta(days=2),
        )
        InboundMessage.objects.create(
            contact=ada,
            channel="sms",
            from_address="+61412345678",
            body="See you there",
            provider_id="SM-synthetic-1",
            blast=blast,
            received_at=now - timedelta(days=2),
        )

        body = self.client.get(PREFIX + f"contacts/{ada.pk}/").json()
        self.assertEqual(body["user"], {"display_name": "Ada Lovelace"})
        self.assertEqual((body["phone"], body["city"], body["source"]), ("0412 345 678", "Sydney", "luma"))
        self.assertEqual(body["consent"]["email"]["state"], "granted")
        self.assertIsNone(body["consent"]["email"]["inferred_until"])
        self.assertEqual(body["consent"]["sms"]["status"], "active")
        self.assertEqual(len(body["registrations"]), 1)
        self.assertEqual(body["registrations"][0]["event"]["name"], "MLAI Meetup")
        self.assertEqual(body["registrations"][0]["answers"], [{"label": "What do you do?", "value": "Research"}])
        self.assertEqual(body["messages"][0]["title"], "Doors open at 6")
        self.assertEqual(body["messages"][0]["status"], "delivered")
        self.assertEqual(body["replies"][0]["blast_id"], str(blast.pk))
        self.assertEqual(body["history"][0]["kind"], "granted")
        self.assertFalse(CrmAuditEvent.objects.exists())
        self.assertEqual(self.client.get(PREFIX + f"contacts/{uuid.uuid4()}/").status_code, 404)

    def test_edits_normalise_the_phone_and_audit_field_names_only(self):
        ada = make_contact("ada@example.com", first_name="Ada")
        url = PREFIX + f"contacts/{ada.pk}/"
        response = self.client.patch(
            url,
            {
                "company": "Analytical Engines",
                "phone": "0412 345 678",
                "tags": ["founder", " founder "],
                "attributes": {"favourite_model": "claude"},
                "linkedin_url": "https://www.linkedin.com/in/ada",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.json())
        body = response.json()
        self.assertEqual((body["phone_e164"], body["sms_status"]), ("+61412345678", "active"))
        self.assertEqual(body["tags"], ["founder"])
        self.assertEqual(body["first_name"], "Ada")
        audit = CrmAuditEvent.objects.get(action=CrmAuditEvent.Action.EDIT)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual((audit.target_type, audit.target_id), ("contact", str(ada.pk)))
        self.assertEqual(
            audit.detail,
            {"operation": "update", "fields": ["company", "linkedin_url", "phone", "tags", "attributes"]},
        )
        self.assertNotIn("Analytical", str(audit.detail))

        self.client.patch(url, {"company": "Analytical Engines"}, format="json")
        self.assertEqual(CrmAuditEvent.objects.count(), 1)

    def test_a_new_number_resets_sms_state_but_an_opt_out_sticks(self):
        opted_out = make_contact(
            "out@example.com", phone_e164="+61412345678", sms_status=Contact.SmsStatus.OPTED_OUT
        )
        body = self.client.patch(
            PREFIX + f"contacts/{opted_out.pk}/", {"phone": "+61 498 765 432"}, format="json"
        ).json()
        self.assertEqual((body["phone_e164"], body["sms_status"]), ("+61498765432", "opted_out"))

        landline = make_contact("land@example.com", phone_e164="+61412345678", sms_status=Contact.SmsStatus.ACTIVE)
        url = PREFIX + f"contacts/{landline.pk}/"
        body = self.client.patch(url, {"phone": "(03) 9123 4567"}, format="json").json()
        self.assertEqual((body["phone_e164"], body["sms_status"]), ("+61391234567", "invalid"))
        body = self.client.patch(url, {"phone": "0412 345 678"}, format="json").json()
        self.assertEqual(body["sms_status"], "active")
        body = self.client.patch(url, {"phone": ""}, format="json").json()
        self.assertEqual((body["phone"], body["phone_e164"], body["sms_status"]), ("", "", "none"))

    def test_invalid_edits_are_refused(self):
        ada = make_contact("ada@example.com")
        url = PREFIX + f"contacts/{ada.pk}/"
        cases = [
            ({"phone": "12"}, {"phone": ["invalid_phone"]}),
            ({"linkedin_url": "http://linkedin.com/in/ada"}, {"linkedin_url": ["https_required"]}),
            ({"email": "new@example.com"}, {"email": ["unknown_field"]}),
            ({"tags": ["t"] * 51}, {"tags": ["max_length"]}),
        ]
        for data, fields in cases:
            with self.subTest(data=data):
                response = self.client.patch(url, data, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["fields"], fields)
        self.assertFalse(CrmAuditEvent.objects.exists())
        self.assertEqual(self.client.patch(PREFIX + f"contacts/{uuid.uuid4()}/", {}, format="json").status_code, 404)

    def test_adding_a_contact(self):
        account = User.objects.create_user("new@example.com", first_name="New")
        response = self.client.post(
            PREFIX + "contacts/",
            {"email": "New@Example.com", "first_name": "New", "phone": "0412 345 678", "tags": ["vip"]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual(
            (body["email"], body["source"], body["marketing_consent"], body["phone_e164"]),
            ("new@example.com", "manual", "unknown", "+61412345678"),
        )
        self.assertEqual(Contact.objects.get(pk=body["id"]).user, account)
        audit = CrmAuditEvent.objects.get()
        self.assertEqual(audit.detail["operation"], "create")

        again = self.client.post(PREFIX + "contacts/", {"email": "new@example.com"}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json(), {"error": "contact_exists", "outcome": "contact_exists", "id": body["id"]})

        ban("blocked@example.com")
        blocked = self.client.post(PREFIX + "contacts/", {"email": "blocked@example.com"}, format="json")
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"], "email_banned")
        self.assertFalse(Contact.objects.filter(email="blocked@example.com").exists())
        invalid = self.client.post(PREFIX + "contacts/", {"email": "nope"}, format="json")
        self.assertEqual(invalid.json()["fields"], {"email": ["invalid_email"]})

    def test_stop_messaging_per_channel(self):
        ada = make_contact(
            "ada@example.com",
            marketing_consent=Contact.MarketingConsent.GRANTED,
            phone_e164="+61412345678",
            sms_status=Contact.SmsStatus.ACTIVE,
            whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED,
        )
        url = PREFIX + f"contacts/{ada.pk}/stop-messaging/"
        body = self.client.post(url, {"channels": ["email"], "reason": "Asked in person"}, format="json").json()
        self.assertEqual((body["email_status"], body["marketing_consent"]), ("unsubscribed", "withdrawn"))
        self.assertEqual((body["sms_status"], body["whatsapp_opt_in"]), ("active", "granted"))
        [event] = ContactConsentEvent.objects.filter(contact=ada)
        self.assertEqual((event.channel, event.kind, event.actor), ("email", "unsubscribed", self.admin))
        self.assertEqual(event.detail, {"reason": "Asked in person"})

        body = self.client.post(url, {"channels": ["sms", "whatsapp"]}, format="json").json()
        self.assertEqual((body["sms_status"], body["whatsapp_opt_in"]), ("opted_out", "withdrawn"))
        self.assertEqual(
            sorted(ContactConsentEvent.objects.filter(contact=ada).values_list("channel", "kind")),
            [("email", "unsubscribed"), ("sms", "withdrawn"), ("whatsapp", "withdrawn")],
        )
        audits = CrmAuditEvent.objects.filter(action=CrmAuditEvent.Action.OPT_OUT).order_by("created_at")
        self.assertEqual([audit.detail for audit in audits], [{"channels": ["email"]}, {"channels": ["sms", "whatsapp"]}])
        self.assertEqual({item["channel"] for item in body["history"]}, {"email", "sms", "whatsapp"})

        bounced = make_contact("bounced@example.com", email_status=Contact.EmailStatus.BOUNCED)
        body = self.client.post(
            PREFIX + f"contacts/{bounced.pk}/stop-messaging/", {"channels": ["email"]}, format="json"
        ).json()
        self.assertEqual((body["email_status"], body["marketing_consent"]), ("bounced", "withdrawn"))
        refused = self.client.post(url, {"channels": ["fax"]}, format="json")
        self.assertEqual(refused.status_code, 400)


@crm_enabled
class EventDatabaseTests(CrmApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.upcoming = make_event("evt-up", start=now + timedelta(days=7), name="October Meetup")
        self.later = make_event("evt-later", start=now + timedelta(days=14), name="Summit")
        self.past = make_event("evt-past", start=now - timedelta(days=7), name="September Meetup")
        self.ada = make_contact(
            "ada@example.com",
            first_name="Ada",
            phone_e164="+61412345678",
            sms_status=Contact.SmsStatus.ACTIVE,
            whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED,
        )
        self.bob = make_contact(
            "bob@example.com",
            email_status=Contact.EmailStatus.UNSUBSCRIBED,
            phone_e164="+64211234567",
            sms_status=Contact.SmsStatus.ACTIVE,
        )
        self.cat = make_contact("cat@example.com")
        ban("cat@example.com")
        self.dan = make_contact(
            "dan@example.com",
            phone_e164="+61498765432",
            sms_status=Contact.SmsStatus.OPTED_OUT,
            whatsapp_opt_in=Contact.WhatsAppOptIn.GRANTED,
        )
        self.eve = make_contact("eve@example.com")
        self.fay = make_contact("fay@example.com")
        register(self.upcoming, self.ada, registered_at=now - timedelta(days=1))
        register(self.upcoming, self.bob, registered_at=now - timedelta(days=2))
        register(self.upcoming, self.cat, "waitlist")
        register(self.upcoming, self.dan, "pending_approval")
        register(self.upcoming, self.eve, removed_at=now)
        register(self.upcoming, self.fay, "declined")
        register(self.past, self.ada, checked_in_at=now - timedelta(days=7))
        register(self.past, self.bob)

    def test_event_lists_with_counts_and_reach(self):
        upcoming = self.client.get(PREFIX + "events/").json()
        self.assertEqual([row["luma_event_id"] for row in upcoming["results"]], ["evt-up", "evt-later"])
        summary = upcoming["results"][0]
        self.assertEqual(
            summary["counts"], {"going": 2, "waitlist": 1, "pending": 1, "checked_in": 0, "no_show": 0}
        )
        self.assertEqual(summary["reach"], {"email": 2, "sms": 1, "whatsapp": 2})
        self.assertIsNone(summary["last_blast"])
        self.assertTrue(summary["start_at"].endswith("Z"))

        past = self.client.get(PREFIX + "events/?scope=past").json()["results"]
        self.assertEqual([row["luma_event_id"] for row in past], ["evt-past"])
        self.assertEqual(past[0]["counts"]["no_show"], 1)
        self.assertEqual(past[0]["counts"]["checked_in"], 1)
        self.assertEqual(
            [row["name"] for row in self.client.get(PREFIX + "events/?q=summit").json()["results"]],
            ["Summit"],
        )
        self.assertEqual(self.client.get(PREFIX + "events/?scope=all").json()["error"], "invalid_filter")

    def test_event_detail_has_blasts_and_quick_segments(self):
        blast = Blast.objects.create(
            purpose="event_update",
            event=self.upcoming,
            quick_segment="going",
            channels=["email", "sms"],
            subject="Doors open at 6",
            status="sent",
            recipient_count=2,
            sent_at=timezone.now(),
            created_by=self.admin,
        )
        BlastDelivery.objects.create(
            blast=blast, contact=self.ada, channel="sms", address="+61412345678",
            consent_basis="event_registration", status="delivered", segments=2,
        )
        body = self.client.get(PREFIX + f"events/{self.upcoming.pk}/").json()
        self.assertEqual(body["last_blast"]["id"], str(blast.pk))
        [summary] = body["blasts"]
        self.assertEqual(summary["audience"]["label"], "Going · October Meetup")
        self.assertEqual(summary["per_channel"]["sms"]["delivered"], 1)
        self.assertEqual(summary["per_channel"]["sms"]["segments"], 2)
        self.assertEqual(summary["per_channel"]["email"]["recipients"], 0)
        self.assertEqual(
            [(item["key"], item["count"]) for item in body["quick_segments"]],
            [("going", 2), ("checked_in", 0), ("no_show", 0), ("waitlist", 1)],
        )
        self.assertEqual(self.client.get(PREFIX + f"events/{uuid.uuid4()}/").status_code, 404)

    def test_guest_tabs_and_search(self):
        def guests(event, query=""):
            return self.client.get(PREFIX + f"events/{event.pk}/guests/{query}").json()

        everyone = guests(self.upcoming)
        self.assertEqual(everyone["count"], 5)
        self.assertNotIn(self.eve.email, {row["email"] for row in everyone["results"]})
        self.assertEqual([row["email"] for row in guests(self.upcoming, "?status=going")["results"]], ["ada@example.com", "bob@example.com"])
        self.assertEqual({row["email"] for row in guests(self.upcoming, "?status=waitlist")["results"]}, {"cat@example.com"})
        self.assertEqual({row["email"] for row in guests(self.upcoming, "?status=pending")["results"]}, {"dan@example.com"})
        self.assertEqual(guests(self.upcoming, "?status=no_show")["count"], 0)
        self.assertEqual({row["email"] for row in guests(self.past, "?status=no_show")["results"]}, {"bob@example.com"})
        self.assertEqual({row["email"] for row in guests(self.past, "?status=checked_in")["results"]}, {"ada@example.com"})
        self.assertEqual({row["email"] for row in guests(self.upcoming, "?q=0412")["results"]}, {"ada@example.com"})
        row = guests(self.upcoming, "?status=going")["results"][0]
        self.assertEqual(
            set(row),
            {
                "contact_id", "display_name", "email", "phone_e164", "company", "job_title",
                "approval_status", "registered_at", "checked_in_at", "ticket_names",
                "email_status", "sms_status", "whatsapp_opt_in",
            },
        )
        self.assertEqual(guests(self.upcoming, "?status=declined")["error"], "invalid_filter")

    def test_overview(self):
        Contact.objects.filter(pk=self.fay.pk).update(created_at=timezone.now() - timedelta(days=40))
        Contact.objects.filter(pk=self.ada.pk).update(marketing_consent=Contact.MarketingConsent.GRANTED)
        body = self.client.get(PREFIX + "overview/").json()
        self.assertEqual(
            body["contacts"],
            {
                "total": 6,
                "marketing_eligible": 1,
                "added_last_30_days": 5,
                "with_mobile": 3,
                "whatsapp_opted_in": 2,
            },
        )
        self.assertEqual(body["next_event"]["luma_event_id"], "evt-up")
        self.assertIsNone(body["last_blast"])


def import_rows(count):
    """Synthetic rows: invalid emails, same-chunk duplicates, a suppressed and a banned person."""

    rows = []
    for index in range(count):
        email = f"person{index}@example.com"
        if index % 97 == 0:
            email = f"not-an-email-{index}"
        elif index % 50 == 25:
            email = rows[-1]["Email"].upper()
        rows.append(
            {
                "Email": email,
                "First Name": f"Person {index}",
                "Opt-in date": "2025-03-04 09:30:00" if index % 3 else "",
                "Tags": "newsletter" if index % 2 else "newsletter; founder",
            }
        )
    return rows


def as_csv(rows):
    handle = io.StringIO()
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    handle.seek(0)
    return handle


@crm_enabled
class UploadImportDatabaseTests(CrmApiMixin, TestCase):
    def start(self, rows, **extra):
        plan = self.client.post(PREFIX + "imports/plan/", {"headers": list(rows[0])}, format="json").json()
        mapping = {column["header"]: column["target"] for column in plan["columns"]}
        response = self.client.post(
            PREFIX + "imports/",
            {
                "file_name": "optins.csv",
                "consent": "granted",
                "consent_source": "Website form",
                "tags": ["imported"],
                "mapping": mapping,
                "row_count": len(rows),
                **extra,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.json())
        return response.json()

    def upload(self, record, rows, size=1000):
        for start in range(0, len(rows), size):
            response = self.client.post(
                PREFIX + f"imports/{record['id']}/rows/",
                {"start_row": start, "rows": rows[start : start + size]},
                format="json",
            )
            self.assertEqual(response.status_code, 200, response.json())
        response = self.client.post(PREFIX + f"imports/{record['id']}/finish/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()

    def command_counts(self, rows, *, dry_run=True):
        _, parsed = read_csv(as_csv(rows))
        record = apply_import(
            parsed,
            file_name="optins.csv",
            consent="granted",
            consent_source="Website form",
            tags=["imported"],
            dry_run=dry_run,
        )
        return record.counts, record.error_sample

    def test_a_25000_row_upload_in_25_chunks_matches_the_command(self):
        rows = import_rows(25_000)
        make_contact("person5@example.com", email_status=Contact.EmailStatus.UNSUBSCRIBED)
        make_contact("person8@example.com", first_name="Kept")
        make_contact("person7@example.com")
        ban("person7@example.com")
        expected_counts, expected_errors = self.command_counts(rows)
        self.assertEqual(Contact.objects.count(), 3)

        record = self.start(rows)
        self.assertEqual((record["status"], record["rows_received"], record["row_count"]), ("running", 0, 25_000))
        finished = self.upload(record, rows)

        self.assertEqual(finished["status"], "completed")
        self.assertEqual(finished["rows_received"], 25_000)
        self.assertEqual(finished["counts"], expected_counts)
        self.assertEqual(finished["error_sample"], expected_errors)
        self.assertEqual(Contact.objects.count(), 3 + expected_counts["created"])
        self.assertEqual(Contact.objects.get(email="person8@example.com").first_name, "Kept")
        self.assertEqual(Contact.objects.get(email="person5@example.com").email_status, "unsubscribed")
        stored = ContactImport.objects.get(pk=record["id"])
        self.assertEqual(stored.origin, ContactImport.Origin.UPLOAD)
        self.assertNotIn("person", str(stored.column_plan) + str(stored.error_sample))
        audits = CrmAuditEvent.objects.filter(action=CrmAuditEvent.Action.IMPORT, target_id=record["id"])
        self.assertEqual(sorted(audit.detail["operation"] for audit in audits), ["finish", "start"])
        self.assertEqual(audits.get(detail__operation="finish").actor, self.admin)

    def test_a_dry_run_upload_reports_counts_and_writes_nothing(self):
        rows = import_rows(1500)
        expected_counts, _ = self.command_counts(rows)
        finished = self.upload(self.start(rows, dry_run=True), rows)
        self.assertTrue(finished["dry_run"])
        self.assertEqual(finished["counts"], expected_counts)
        self.assertFalse(Contact.objects.exists())
        self.assertFalse(ContactConsentEvent.objects.exists())

    def test_chunk_rules(self):
        rows = import_rows(3)
        record = self.start(rows)
        url = PREFIX + f"imports/{record['id']}/rows/"

        mismatch = self.client.post(url, {"start_row": 1, "rows": rows[1:2]}, format="json")
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json()["expected_start_row"], 0)
        first = self.client.post(url, {"start_row": 0, "rows": rows[:2]}, format="json").json()
        self.assertEqual(first["rows_received"], 2)
        replay = self.client.post(url, {"start_row": 0, "rows": rows[:2]}, format="json").json()
        self.assertEqual((replay["rows_received"], replay["counts"]), (2, first["counts"]))
        too_many = self.client.post(url, {"start_row": 2, "rows": rows}, format="json")
        self.assertEqual(too_many.json()["error"], "row_count_exceeded")
        oversized = self.client.post(url, {"start_row": 2, "rows": [rows[2]] * 1001}, format="json")
        self.assertEqual(oversized.json()["error"], "too_many_rows")

        self.client.post(PREFIX + f"imports/{record['id']}/finish/", {}, format="json")
        closed = self.client.post(url, {"start_row": 2, "rows": rows[2:]}, format="json")
        self.assertEqual((closed.status_code, closed.json()["error"]), (409, "import_not_running"))
        self.assertEqual(
            self.client.post(PREFIX + f"imports/{record['id']}/finish/", {}, format="json").status_code, 409
        )
        command = ContactImport.objects.create(file_name="cli.csv", consent="none")
        self.assertEqual(
            self.client.post(
                PREFIX + f"imports/{command.pk}/rows/", {"start_row": 0, "rows": rows[:1]}, format="json"
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(PREFIX + f"imports/{uuid.uuid4()}/rows/", {"start_row": 0, "rows": rows[:1]}, format="json").status_code,
            404,
        )

        listed = self.client.get(PREFIX + "imports/").json()["results"]
        self.assertEqual([item["id"] for item in listed], [str(command.pk), record["id"]])
        self.assertEqual(self.client.get(PREFIX + f"imports/{record['id']}/").json()["status"], "completed")

    def test_invalid_mappings_are_refused(self):
        def create(mapping):
            return self.client.post(
                PREFIX + "imports/",
                {"file_name": "a.csv", "consent": "none", "mapping": mapping},
                format="json",
            ).json()["error"]

        self.assertEqual(create({"Name": "display_name"}), "no_email_column")
        self.assertEqual(create({"Email": "email", "Work email": "email"}), "duplicate_target")
        self.assertEqual(create({"Email": "email", "Secret": "password"}), "unknown_target")
        self.assertEqual(create({"Email": "email", "Notes": "attributes.Notes"}), "unknown_target")
        self.assertFalse(ContactImport.objects.exists())
        plan = self.client.post(PREFIX + "imports/plan/", {"headers": ["Name", "Company"]}, format="json")
        self.assertEqual(plan.json()["error"], "no_email_column")
