"""Contacts API rules that need no database: access, inputs, wire shapes and filters."""

import uuid
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from corsheaders.middleware import CorsMiddleware
from django.db.models import Q
from django.test import RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.crm import serializers as wire
from community_chat.crm import views
from community_chat.crm.errors import CrmError
from community_chat.throttles import CommunityChatScopedThrottle
from core.middleware import DesktopAuthCorsMiddleware
from core.models import User
from crm.access import channel_availability, crm_access, is_crm_admin
from crm.contact_import import (
    ImportChunkError,
    ImportFileError,
    apply_upload_chunk,
    parse_rows,
    plan_columns,
    validate_column_plan,
)
from crm.models import Blast, Contact, ContactImport, CrmEvent, EventRegistration, MessageTemplate
from crm.queries import (
    contact_search_q,
    event_has_ended,
    guest_status_q,
    sms_allowed_prefixes,
)
from crm.segments import SegmentError

NOW = datetime(2026, 10, 23, 1, 0, tzinfo=timezone.utc)
PREFIX = "/api/v1/community-chat/crm/"


def lookups(q):
    """Every (lookup, value) pair inside a Q, however deeply nested."""

    found = []
    for child in q.children:
        if isinstance(child, Q):
            found.extend(lookups(child))
        elif isinstance(child, tuple):
            found.append(child)
    return found


def signed_in(method="get", path=PREFIX, data=None, **extra):
    factory = APIRequestFactory()
    request = getattr(factory, method)(path, data=data, format="json", **extra)
    user = User(email="admin@example.test", is_active=True)
    force_authenticate(request, user=user)
    request.community_chat_public_key = "a" * 64
    request.community_chat_installation_id = uuid.uuid4()
    return request


def admin_gate(is_admin=True, *, verified=True):
    """Patch the MLAI admin gate's two lookups, for the views and ``crm_access`` alike."""

    stack = ExitStack()
    stack.enter_context(
        patch("community_chat.admin_access.has_verified_installation", return_value=verified)
    )
    stack.enter_context(patch("community_chat.admin_access.is_mlai_admin_user", return_value=is_admin))
    return stack


class ChannelAvailabilityTests(SimpleTestCase):
    @override_settings(CRM_SENDING_ENABLED=False, CRM_SMS_ENABLED=False, CRM_WHATSAPP_ENABLED=False)
    def test_channels_are_disabled_by_default(self):
        self.assertEqual(
            channel_availability(),
            {
                "email": {"available": False, "reason": "disabled"},
                "sms": {"available": False, "reason": "disabled"},
                "whatsapp": {"available": False, "reason": "disabled"},
            },
        )

    @override_settings(
        CRM_SENDING_ENABLED=True,
        LISTMONK_BASE_URL="",
        CRM_SMS_ENABLED=True,
        TWILIO_ACCOUNT_SID="AC-synthetic",
        TWILIO_AUTH_TOKEN="synthetic-token",
        TWILIO_MESSAGING_SERVICE_SID="",
        CRM_WHATSAPP_ENABLED=True,
        TWILIO_WHATSAPP_FROM="+15550000000",
    )
    def test_an_enabled_channel_without_credentials_is_not_configured(self):
        channels = channel_availability()
        self.assertEqual(channels["email"], {"available": False, "reason": "not_configured"})
        self.assertEqual(channels["sms"], {"available": False, "reason": "not_configured"})
        self.assertEqual(channels["whatsapp"], {"available": True, "reason": None})

    @override_settings(
        CRM_SENDING_ENABLED=True,
        LISTMONK_BASE_URL="https://lists.example.test",
        LISTMONK_API_USER="mlai-backend",
        LISTMONK_API_TOKEN="synthetic-token",
    )
    def test_email_is_available_with_listmonk(self):
        self.assertEqual(channel_availability()["email"], {"available": True, "reason": None})
        with override_settings(LISTMONK_API_TOKEN=""):
            self.assertEqual(
                channel_availability()["email"], {"available": False, "reason": "not_configured"}
            )


class AccessTests(SimpleTestCase):
    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=False)
    def test_flag_off_denies_everyone_without_reading_roles(self):
        with patch("community_chat.admin_access.is_mlai_admin_user") as role:
            self.assertEqual(
                crm_access(signed_in()), {"enabled": False, "can_view": False, "can_send": False}
            )
        role.assert_not_called()

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_only_mlai_admins_on_the_session_installation_can_view(self):
        request = signed_in()
        request.user = request._force_auth_user
        for verified, is_admin, allowed in ((True, True, True), (True, False, False), (False, True, False)):
            with (
                self.subTest(verified=verified, is_admin=is_admin),
                patch(
                    "community_chat.admin_access.has_verified_installation", return_value=verified
                ) as device,
                patch("community_chat.admin_access.is_mlai_admin_user", return_value=is_admin),
            ):
                self.assertEqual(
                    crm_access(request), {"enabled": True, "can_view": allowed, "can_send": allowed}
                )
                device.assert_called_once_with(
                    request.user,
                    request.community_chat_public_key,
                    request.community_chat_installation_id,
                )

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_sessions_without_a_device_installation_are_members(self):
        request = RequestFactory().get(PREFIX)
        request.user = User(email="jwt@example.test", is_active=True, is_superuser=True)
        with patch("community_chat.admin_access.is_mlai_admin_user", return_value=True) as role:
            self.assertFalse(is_crm_admin(request))
        role.assert_not_called()


@patch.object(CommunityChatScopedThrottle, "allow_request", return_value=True)
class GateTests(SimpleTestCase):
    def call(self, view, request, **kwargs):
        response = view.as_view()(request, **kwargs)
        response.render()
        return response

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=False)
    def test_flag_off_is_404_and_access_still_answers(self, _throttle):
        with admin_gate():
            response = self.call(views.TagsView, signed_in())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data, {"error": "crm_disabled", "outcome": "crm_disabled"})
        self.assertEqual(response["Cache-Control"], "no-store")

        # The admin check comes first, so a non-admin never learns the flag.
        with admin_gate(False):
            response = self.call(views.TagsView, signed_in())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data, {"error": "not_authorised", "outcome": "not_authorised"})
        self.assertEqual(response["Cache-Control"], "no-store")

        response = self.call(views.AccessView, signed_in())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data,
            {
                "enabled": False,
                "can_view": False,
                "can_send": False,
                "luma_sync": None,
                "channels": None,
            },
        )

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_members_get_403_and_a_quiet_access_answer(self, _throttle):
        with admin_gate(False):
            response = self.call(views.ContactsView, signed_in())
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.data, {"error": "not_authorised", "outcome": "not_authorised"})
            access = self.call(views.AccessView, signed_in())
        self.assertEqual(access.status_code, 200)
        self.assertFalse(access.data["can_view"])
        self.assertIsNone(access.data["channels"])

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True, CRM_LUMA_SYNC_ENABLED=True)
    def test_admins_see_sync_and_channel_state(self, _throttle):
        sync = {"enabled": True, "last_synced_at": NOW, "status": "completed"}
        with (
            admin_gate(),
            patch("crm.queries.luma_sync_status", return_value=sync),
        ):
            response = self.call(views.AccessView, signed_in())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["can_send"])
        self.assertEqual(
            response.data["luma_sync"],
            {"enabled": True, "last_synced_at": "2026-10-23T01:00:00Z", "status": "completed"},
        )
        self.assertEqual(set(response.data["channels"]), {"email", "sms", "whatsapp"})

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_inputs_are_rejected_before_any_database_work(self, _throttle):
        cases = [
            (views.ContactsView, signed_in(path=PREFIX + "contacts/?sort=oldest"), {}, 400, "invalid_filter"),
            (views.ContactsView, signed_in(path=PREFIX + "contacts/?limit=x"), {}, 400, "invalid_pagination"),
            (
                views.ContactDetailView,
                signed_in("patch", data={"email_status": "active"}),
                {"contact_id": uuid.uuid4()},
                400,
                "invalid_request",
            ),
            (
                views.ImportRowsView,
                signed_in("post", data={"start_row": 0, "rows": [{"email": "a@example.test"}] * 1001}),
                {"import_id": uuid.uuid4()},
                400,
                "too_many_rows",
            ),
            (
                views.SegmentPreviewView,
                signed_in("post", data={"definition": {"rules": [{"type": "tag", "op": "nope"}]}}),
                {},
                400,
                "invalid_definition",
            ),
        ]
        with admin_gate():
            for view, request, kwargs, status, code in cases:
                with self.subTest(view=view.__name__, code=code):
                    response = self.call(view, request, **kwargs)
                    self.assertEqual(response.status_code, status, response.data)
                    self.assertEqual(response.data["error"], code)

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_preview_reports_the_json_pointer(self, _throttle):
        request = signed_in(
            "post", data={"definition": {"rules": [{"type": "tag", "op": "has_any", "values": []}]}}
        )
        with admin_gate():
            response = self.call(views.SegmentPreviewView, request)
        self.assertEqual(
            response.data,
            {
                "error": "invalid_definition",
                "outcome": "invalid_definition",
                "pointer": "/rules/0/values",
                "reason": "required",
            },
        )

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_import_plan_without_email_still_shows_the_columns(self, _throttle):
        request = signed_in("post", data={"headers": ["Name", "Company"]})
        with admin_gate():
            response = self.call(views.ImportPlanView, request)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["has_email"])
        self.assertEqual([column["target"] for column in response.data["columns"]], ["display_name", "company"])

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_import_plan_maps_headers_in_order(self, _throttle):
        request = signed_in("post", data={"headers": ["Email Address", "First Name", "Favourite model"]})
        with admin_gate():
            response = self.call(views.ImportPlanView, request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.data["columns"],
            [
                {"header": "Email Address", "target": "email"},
                {"header": "First Name", "target": "first_name"},
                {"header": "Favourite model", "target": "attributes.favourite_model"},
            ],
        )
        self.assertIn("whatsapp_opt_in", response.data["targets"])


class ErrorShapeTests(SimpleTestCase):
    def handle(self, exc):
        return views.CrmView().handle_exception(exc)

    def test_typed_errors_keep_the_volunteer_shape(self):
        cases = [
            (CrmError("contact_exists", 409, id="abc"), 409, {"error": "contact_exists", "outcome": "contact_exists", "id": "abc"}),
            (SegmentError("rule_not_available", "/rules/2", "blast_engagement"), 400, {
                "error": "rule_not_available",
                "outcome": "rule_not_available",
                "pointer": "/rules/2",
                "reason": "blast_engagement",
            }),
            (SegmentError("event_required"), 400, {"error": "event_required", "outcome": "event_required"}),
            (ImportFileError("no email", "no_email_column"), 400, {"error": "no_email_column", "outcome": "no_email_column"}),
            (ImportChunkError("start_row_mismatch", expected_start_row=1000), 409, {
                "error": "start_row_mismatch",
                "outcome": "start_row_mismatch",
                "expected_start_row": 1000,
            }),
            (ImportChunkError("import_not_running"), 409, {"error": "import_not_running", "outcome": "import_not_running"}),
            (ImportChunkError("row_count_exceeded", row_count=5), 400, {
                "error": "row_count_exceeded",
                "outcome": "row_count_exceeded",
                "row_count": 5,
            }),
            (Contact.DoesNotExist(), 404, {"error": "not_found", "outcome": "not_found"}),
        ]
        for exc, status, body in cases:
            with self.subTest(exc=type(exc).__name__):
                response = self.handle(exc)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.data, body)

    def test_validation_errors_report_codes_not_values(self):
        serializer = wire.ContactFieldsInput(data={"linkedin_url": "http://example.test/ada"}, partial=True)
        with self.assertRaises(ValidationError) as caught:
            serializer.is_valid(raise_exception=True)
        response = self.handle(caught.exception)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"], "invalid_request")
        self.assertEqual(response.data["fields"], {"linkedin_url": ["https_required"]})


class InputTests(SimpleTestCase):
    def errors(self, serializer_class, data, partial=False):
        serializer = serializer_class(data=data, partial=partial)
        with self.assertRaises(ValidationError) as caught:
            serializer.is_valid(raise_exception=True)
        return caught.exception.get_codes()

    def test_contact_edits_accept_only_known_fields(self):
        self.assertEqual(
            self.errors(wire.ContactFieldsInput, {"email_status": "active", "first_name": "Ada"}, True),
            {"email_status": ["unknown_field"]},
        )

    def test_contact_edit_rules(self):
        self.assertEqual(self.errors(wire.ContactFieldsInput, {"phone": "12"}, True), {"phone": ["invalid_phone"]})
        self.assertEqual(
            self.errors(wire.ContactFieldsInput, {"tags": ["x" * 65]}, True), {"tags": {0: ["max_length"]}}
        )
        self.assertEqual(
            self.errors(wire.ContactFieldsInput, {"tags": ["t"] * 51}, True), {"tags": ["max_length"]}
        )
        self.assertEqual(
            self.errors(wire.ContactFieldsInput, {"attributes": {"Bad Key": "x"}}, True),
            {"attributes": ["invalid_attribute_key"]},
        )
        self.assertEqual(
            self.errors(wire.ContactFieldsInput, {"attributes": {f"k{i}": "x" for i in range(51)}}, True),
            {"attributes": ["too_many_attributes"]},
        )

    def test_contact_edits_are_cleaned(self):
        serializer = wire.ContactFieldsInput(
            data={
                "phone": " 0412  345 678 ",
                "tags": ["founder", " founder ", "big   speaker"],
                "attributes": {"favourite_model": "  claude   opus "},
                "linkedin_url": "https://www.linkedin.com/in/ada",
            },
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["phone"], "0412 345 678")
        self.assertEqual(serializer.validated_data["tags"], ["founder", "big speaker"])
        self.assertEqual(serializer.validated_data["attributes"], {"favourite_model": "claude opus"})

    def test_new_contacts_need_a_valid_email(self):
        self.assertEqual(self.errors(wire.ContactCreateInput, {"email": "not-an-email"}), {"email": ["invalid_email"]})
        serializer = wire.ContactCreateInput(data={"email": "Ada@Example.COM"})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["email"], "ada@example.com")

    def test_stop_messaging_channels(self):
        self.assertIn("channels", self.errors(wire.StopMessagingInput, {"channels": []}))
        self.assertIn("channels", self.errors(wire.StopMessagingInput, {"channels": ["fax"]}))
        serializer = wire.StopMessagingInput(data={"channels": ["sms", "email", "sms"]})
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data, {"channels": ["sms", "email"], "reason": ""})

    def test_import_inputs(self):
        self.assertEqual(
            self.errors(wire.ImportPlanInput, {"headers": ["Email", "Email"]}), {"headers": ["duplicate_header"]}
        )
        self.assertEqual(
            self.errors(wire.ImportRowsInput, {"start_row": 0, "rows": [{"email": {"nested": 1}}]}),
            {"rows": ["invalid_rows"]},
        )
        serializer = wire.ImportCreateInput(
            data={"file_name": "optins.csv", "consent": "granted", "mapping": {"Email": "email"}}
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["row_count"], 0)
        self.assertFalse(serializer.validated_data["dry_run"])
        self.assertIn("consent", self.errors(wire.ImportCreateInput, {"file_name": "a.csv", "consent": "maybe", "mapping": {"e": "email"}}))

    def test_contact_list_filters(self):
        self.assertEqual(
            wire.contact_list_filters({"q": "ada", "tag": " founder ", "eligible": "marketing", "has_mobile": "1"}),
            {
                "text": "ada",
                "tag": "founder",
                "email_status": "",
                "marketing_only": True,
                "has_mobile": True,
                "whatsapp": "",
                "sort": "recent",
                "segment_id": None,
            },
        )
        for params in (
            {"sort": "oldest"},
            {"email_status": "sleeping"},
            {"eligible": "everyone"},
            {"has_mobile": "maybe"},
            {"whatsapp": "yes"},
            {"segment_id": "not-a-uuid"},
        ):
            with self.subTest(params=params), self.assertRaises(CrmError) as caught:
                wire.contact_list_filters(params)
            self.assertEqual(caught.exception.code, "invalid_filter")
            self.assertEqual(caught.exception.extra["field"], next(iter(params)))
        self.assertEqual(wire.guest_status({}), "all")
        self.assertEqual(wire.event_scope({}), "upcoming")
        with self.assertRaises(CrmError):
            wire.guest_status({"status": "declined"})


class WireShapeTests(SimpleTestCase):
    def contact(self, **fields):
        values = {
            "email": "ada@example.com",
            "first_name": "Ada",
            "last_name": "Lovelace",
            "company": "Analytical Engines",
            "tags": ["founder"],
            "phone_e164": "+61412345678",
            "source": Contact.Source.LUMA,
            "events_registered_count": 4,
            "events_attended_count": 3,
            "last_attended_at": NOW - timedelta(days=10),
        }
        values.update(fields)
        return Contact(**values)

    def test_times_are_utc_with_z(self):
        self.assertEqual(wire.iso(datetime(2026, 10, 30, 18, 30, tzinfo=timezone(timedelta(hours=11)))), "2026-10-30T07:30:00Z")
        self.assertIsNone(wire.iso(None))
        self.assertEqual(wire.money("-1.234", "usd"), {"amount": "1.23", "currency": "USD"})
        self.assertEqual(wire.money(None), {"amount": "0.00", "currency": "AUD"})

    def test_contact_row_matches_the_contract(self):
        contact = self.contact()
        contact.marketing_eligible = True
        row = wire.contact_row(contact)
        self.assertEqual(
            set(row),
            {
                "id", "email", "display_name", "first_name", "last_name", "company", "job_title",
                "tags", "phone_e164", "events_registered_count", "events_attended_count",
                "last_event_at", "last_attended_at", "email_status", "marketing_consent",
                "marketing_eligible", "sms_status", "whatsapp_opt_in",
            },
        )
        self.assertTrue(row["marketing_eligible"])
        self.assertEqual(row["last_attended_at"], "2026-10-13T01:00:00Z")

    def test_contact_detail_shows_when_inferred_consent_lapses(self):
        contact = self.contact()
        detail = wire.contact_detail(contact, now=NOW, registrations=[], deliveries=[], replies=[], history=[])
        self.assertEqual(detail["consent"]["email"]["inferred_until"], "2028-10-12T01:00:00Z")
        self.assertIsNone(detail["user"])
        self.assertEqual(detail["source"], "luma")
        granted = self.contact(marketing_consent=Contact.MarketingConsent.GRANTED)
        detail = wire.contact_detail(granted, now=NOW, registrations=[], deliveries=[], replies=[], history=[])
        self.assertIsNone(detail["consent"]["email"]["inferred_until"])

    def test_guest_row_falls_back_to_first_and_last_name(self):
        event = CrmEvent(name="MLAI Meetup", luma_event_id="evt-1")
        registration = EventRegistration(
            event=event, contact=self.contact(), approval_status="approved", ticket_names=["General"]
        )
        row = wire.guest_row(registration)
        self.assertEqual(row["display_name"], "Ada Lovelace")
        self.assertEqual(row["ticket_names"], ["General"])
        self.assertEqual(row["phone_e164"], "+61412345678")

    def test_blast_titles(self):
        self.assertEqual(wire.blast_title(Blast(subject="Doors open at 6", sms_body="ignored")), "Doors open at 6")
        long_sms = "Doors open at six tonight so please arrive early because seating is limited and popular"
        title = wire.blast_title(Blast(sms_body=long_sms))
        self.assertTrue(title.endswith("…"))
        self.assertLessEqual(len(title), 61)
        template = MessageTemplate(name="mlai_event_reminder", content_sid="HX1")
        self.assertEqual(wire.blast_title(Blast(whatsapp_template=template)), "mlai_event_reminder")

    def test_blast_summary_lists_only_its_channels(self):
        event = CrmEvent(name="MLAI Meetup", luma_event_id="evt-1")
        blast = Blast(
            purpose="event_update",
            event=event,
            quick_segment="going",
            channels=["email", "sms"],
            subject="Doors open at 6",
            recipient_count=3,
        )
        stats = {
            "channels": {"sms": {"recipients": 2, "sent": 2, "delivered": 1, "segments": 4, "cost": "-0.32", "price_unit": "AUD"}},
            "replies": 1,
        }
        summary = wire.blast_summary(blast, stats)
        self.assertEqual(summary["audience"], {"kind": "quick_segment", "key": "going", "label": "Going · MLAI Meetup"})
        self.assertEqual(set(summary["per_channel"]), {"email", "sms"})
        self.assertEqual(summary["per_channel"]["email"]["recipients"], 0)
        self.assertEqual(summary["per_channel"]["sms"]["cost"], {"amount": "0.32", "currency": "AUD"})
        self.assertEqual(summary["replies"], 1)
        self.assertEqual(summary["event"]["name"], "MLAI Meetup")

    def test_import_record_keeps_counts_and_row_numbers_only(self):
        record = ContactImport(
            file_name="optins.csv",
            consent="granted",
            origin=ContactImport.Origin.UPLOAD,
            row_count=10,
            rows_received=4,
            counts={"rows": 4, "created": 3, "invalid": 1},
            error_sample=[{"row": 3, "reason": "invalid_email"}],
        )
        body = wire.import_record(record)
        self.assertEqual(body["counts"], {"rows": 4, "created": 3, "invalid": 1})
        self.assertEqual(body["status"], "running")
        self.assertNotIn("@", str(body))


class QueryBuilderTests(SimpleTestCase):
    def test_search_matches_every_word_across_names_email_and_company(self):
        pairs = lookups(contact_search_q("Ada  Lovelace"))
        self.assertIn(("first_name__icontains", "Ada"), pairs)
        self.assertIn(("company__icontains", "Lovelace"), pairs)
        self.assertNotIn("phone_e164__contains", {name for name, _ in pairs})
        self.assertIsNone(contact_search_q("   "))

    def test_phone_like_queries_match_stored_digits(self):
        pairs = lookups(contact_search_q("0412 345", prefix="contact__"))
        self.assertIn(("contact__phone_e164__contains", "0412345"), pairs)
        self.assertIn(("contact__phone_e164__contains", "61412345"), pairs)
        self.assertNotIn("contact__phone_e164__contains", {name for name, _ in lookups(contact_search_q("ada2024"))})
        self.assertNotIn("phone_e164__contains", {name for name, _ in lookups(contact_search_q("412"))})

    def test_guest_status_filters(self):
        self.assertEqual(lookups(guest_status_q("going", event_ended=False)), [("approval_status", "approved")])
        self.assertEqual(lookups(guest_status_q("pending", event_ended=False)), [("approval_status", "pending_approval")])
        self.assertEqual(lookups(guest_status_q("checked_in", event_ended=False)), [("checked_in_at__isnull", False)])
        self.assertEqual(lookups(guest_status_q("no_show", event_ended=False)), [("pk__isnull", True)])
        self.assertEqual(
            sorted(lookups(guest_status_q("no_show", event_ended=True))),
            [("approval_status", "approved"), ("checked_in_at__isnull", True)],
        )
        self.assertEqual(lookups(guest_status_q("all", event_ended=True)), [])
        with self.assertRaises(ValueError):
            guest_status_q("declined", event_ended=True)

    def test_event_end_falls_back_to_start(self):
        self.assertTrue(event_has_ended(CrmEvent(end_at=NOW - timedelta(minutes=1)), NOW))
        self.assertFalse(event_has_ended(CrmEvent(end_at=NOW + timedelta(minutes=1), start_at=NOW - timedelta(hours=1)), NOW))
        self.assertTrue(event_has_ended(CrmEvent(start_at=NOW - timedelta(hours=1)), NOW))
        self.assertFalse(event_has_ended(CrmEvent(), NOW))

    def test_sms_prefixes_default_to_australian_mobiles(self):
        self.assertEqual(sms_allowed_prefixes(), ["+614"])
        with override_settings(CRM_SMS_ALLOWED_PREFIXES="+614, +64 "):
            self.assertEqual(sms_allowed_prefixes(), ["+614", "+64"])
        with override_settings(CRM_SMS_ALLOWED_PREFIXES=[]):
            self.assertEqual(sms_allowed_prefixes(), [])


@override_settings(
    CORS_ALLOWED_ORIGINS=["https://chat.mlai.au"],
    CORS_ALLOWED_ORIGIN_REGEXES=[],
    CORS_ALLOW_ALL_ORIGINS=False,
)
class RoutingTests(SimpleTestCase):
    def test_every_route_resolves_under_the_community_chat_prefix(self):
        some_id = str(uuid.uuid4())
        expected = {
            "access/": views.AccessView,
            "overview/": views.OverviewView,
            "events/": views.EventsView,
            f"events/{some_id}/": views.EventDetailView,
            f"events/{some_id}/guests/": views.EventGuestsView,
            "contacts/": views.ContactsView,
            f"contacts/{some_id}/": views.ContactDetailView,
            f"contacts/{some_id}/stop-messaging/": views.StopMessagingView,
            "tags/": views.TagsView,
            "imports/plan/": views.ImportPlanView,
            "imports/": views.ImportsView,
            f"imports/{some_id}/": views.ImportsView,
            f"imports/{some_id}/rows/": views.ImportRowsView,
            f"imports/{some_id}/finish/": views.ImportFinishView,
            "segments/": views.SegmentsView,
            "segments/preview/": views.SegmentPreviewView,
            "segments/catalog/": views.SegmentCatalogView,
            f"segments/{some_id}/": views.SegmentsView,
        }
        for path, view in expected.items():
            with self.subTest(path=path):
                self.assertIs(resolve(PREFIX + path).func.view_class, view)

    def test_desktop_origin_preflight_is_allowed_on_crm_routes(self):
        endpoint = Mock(side_effect=AssertionError("Preflight must not call the endpoint"))
        middleware = DesktopAuthCorsMiddleware(CorsMiddleware(endpoint))
        request = RequestFactory().options(
            PREFIX + "contacts/",
            HTTP_ORIGIN="tauri://localhost",
            HTTP_ACCESS_CONTROL_REQUEST_METHOD="PATCH",
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS="authorization, content-type",
        )
        response = middleware(request)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response["Access-Control-Allow-Origin"], "tauri://localhost")
        self.assertIn("PATCH", response["Access-Control-Allow-Methods"])
        self.assertNotIn("Access-Control-Allow-Credentials", response)


class UploadImportRuleTests(SimpleTestCase):
    def test_a_plan_from_plan_columns_is_accepted_even_with_long_headers(self):
        header = "What is your favourite large language model and why do you like it so much these days"
        plan = plan_columns(["Email", "First Name", header])
        self.assertEqual(validate_column_plan(plan), plan)

    def test_edited_plans_are_checked(self):
        cases = [
            ({}, "invalid_mapping"),
            ([("Email", "email")], "invalid_mapping"),
            ({"Name": "display_name"}, "no_email_column"),
            ({"Email": "email", "Work": "email"}, "duplicate_target"),
            ({"Email": "email", "Secret": "password"}, "unknown_target"),
            ({"Email": "email", "Notes": "attributes.Notes"}, "unknown_target"),
            ({"Email": "email", "Notes": "attributes."}, "unknown_target"),
            ({f"c{i}": "ignore" for i in range(201)}, "too_many_columns"),
        ]
        for mapping, code in cases:
            with self.subTest(code=code), self.assertRaises(ImportFileError) as caught:
                validate_column_plan(mapping)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(
            validate_column_plan({"Email": "email", "A": "attributes.a", "B": "attributes.a", "C": "ignore", "D": "ignore"}),
            {"Email": "email", "A": "attributes.a", "B": "attributes.a", "C": "ignore", "D": "ignore"},
        )

    def test_chunk_row_numbers_continue_from_the_chunk_start(self):
        parsed = parse_rows(
            [{"Email": "ada@example.com"}, {"Email": "nope"}],
            {"Email": "email"},
            first_row_number=1002,
        )
        self.assertEqual(parsed.errors, [{"row": 1003, "reason": "invalid_email"}])

    def test_chunks_are_refused_before_any_database_work(self):
        cases = [
            ([], 0, "no_rows"),
            ([{"Email": "a@example.com"}] * 1001, 0, "too_many_rows"),
            (["a@example.com"], 0, "invalid_rows"),
            ([{"Email": "a@example.com"}], -1, "invalid_start_row"),
            ([{"Email": "a@example.com"}], True, "invalid_start_row"),
        ]
        for rows, start_row, code in cases:
            with self.subTest(code=code), self.assertRaises(ImportChunkError) as caught:
                apply_upload_chunk(uuid.uuid4(), start_row=start_row, rows=rows, actor=None)
            self.assertEqual(caught.exception.code, code)
