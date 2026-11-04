"""PostgreSQL-backed checks that only MLAI admins reach the Admin area; runs in CI."""

import re
import uuid
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from community_chat.admin_access import AdminError
from community_chat.admin_views import AdminAccessView
from community_chat.crm import services, urls as crm_urls, views
from community_chat.models import ChatRole, CommunityChatDevice
from crm import blasts, contact_import
from crm.models import Blast, BlastDelivery, Contact, CrmAuditEvent, Segment
from roo.models import PointsAdmin

from .api_fixtures import PREFIX, CrmApiMixin, crm_enabled, make_contact

ADMIN_ACCESS = "/api/v1/community-chat/admin/access/"
METHODS = ("get", "post", "put", "patch", "delete")
OPEN_VIEWS = {views.AccessView, AdminAccessView}


def crm_routes():
    """(path, view) for every CRM route, with each URL parameter filled by a fresh UUID."""

    for pattern in crm_urls.urlpatterns:
        path = re.sub(r"<uuid:\w+>", lambda _match: str(uuid.uuid4()), str(pattern.pattern))
        unfilled = re.search(r"<[^>]+>", path)
        assert unfilled is None, f"fill the {unfilled.group()} parameter in {pattern.pattern}"
        yield path, pattern.callback.view_class


@crm_enabled
class AdminAccessMatrixDatabaseTests(CrmApiMixin, TestCase):
    """One read and one write, for every kind of account the plan names."""

    def requests(self, client):
        overview = client.get(PREFIX + "overview/")
        preview = client.post(PREFIX + "segments/preview/", {"definition": {}}, format="json")
        access = client.get(ADMIN_ACCESS)
        return overview, preview, access

    def assert_admin(self, client):
        overview, preview, access = self.requests(client)
        self.assertEqual((overview.status_code, preview.status_code), (200, 200))
        self.assertEqual((access.status_code, access.json()), (200, {"is_admin": True, "sections": ["crm"]}))

    def assert_refused(self, client):
        overview, preview, access = self.requests(client)
        for response in (overview, preview):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"error": "not_authorised", "outcome": "not_authorised"})
        self.assertEqual((access.status_code, access.json()), (200, {"is_admin": False, "sections": []}))

    def test_committee_admin_and_superusers_get_in(self):
        self.assert_admin(self.client)
        self.assert_admin(self.account_client(self.chat_user("role-admin@example.test", "c", role="admin")))
        root = self.chat_user("root@example.test", "d")
        root.is_superuser = True
        root.save(update_fields=["is_superuser"])
        self.assert_admin(self.account_client(root))

    def test_other_points_admin_roles_are_refused(self):
        for key, role in (("e", "portfolio_lead"), ("f", "partner")):
            with self.subTest(role=role):
                self.assert_refused(self.account_client(self.chat_user(f"{role}@example.test", key, role=role)))

    def test_inactive_and_unlinked_points_admins_are_refused(self):
        inactive = self.chat_user("inactive@example.test", "g", role="committee")
        PointsAdmin.objects.filter(user=inactive).update(is_active=False)
        self.assert_refused(self.account_client(inactive))

        unlinked = self.chat_user("unlinked@example.test", "h")
        unlinked.slack_id = "UHHHHHHHH"
        unlinked.save(update_fields=["slack_id"])
        PointsAdmin.objects.create(slack_user_id="UHHHHHHHH", user=None, role="committee", is_active=True)
        self.assert_refused(self.account_client(unlinked))

    def test_chat_owners_and_admins_without_points_admin_are_refused(self):
        # The intended change: Chat appointments no longer open the CRM.
        for key, role in (("i", "owner"), ("j", "admin")):
            with self.subTest(role=role):
                user = self.chat_user(f"chat-{role}@example.test", key)
                ChatRole.objects.create(user=user, role=role)
                self.assert_refused(self.account_client(user))

    def test_committee_sessions_off_their_own_verified_installation_are_refused(self):
        pending = self.chat_user("pending@example.test", "k", role="committee", device_status="pending")
        self.assert_refused(self.account_client(pending))

        revoked = self.chat_user("revoked@example.test", "l", role="committee")
        revoked_client = self.account_client(revoked)
        CommunityChatDevice.objects.filter(user=revoked).update(revoked_at=timezone.now())
        self.assert_refused(revoked_client)

        borrowed = self.chat_user("borrowed@example.test", "m", role="committee")
        member_installation = CommunityChatDevice.objects.get(user=self.member).installation_id
        self.assert_refused(self.account_client(borrowed, installation_id=member_installation))

    def test_jwt_only_sessions_are_refused(self):
        client = APIClient()
        client.force_authenticate(user=self.admin)
        self.assert_refused(client)

    def test_signed_out_requests_are_refused(self):
        for path in ("overview/", "access/"):
            self.assertIn(APIClient().get(PREFIX + path).status_code, (401, 403))
        self.assertIn(APIClient().get(ADMIN_ACCESS).status_code, (401, 403))

    def test_with_the_flag_off_admins_get_404_and_everyone_else_403(self):
        with override_settings(COMMUNITY_CHAT_CRM_ENABLED=False):
            admin = self.client.get(PREFIX + "overview/")
            member = self.member_client.get(PREFIX + "overview/")
            admin_access = self.client.get(ADMIN_ACCESS).json()
            member_access = self.member_client.get(ADMIN_ACCESS).json()
        self.assertEqual((admin.status_code, admin.json()["error"]), (404, "crm_disabled"))
        self.assertEqual((member.status_code, member.json()["error"]), (403, "not_authorised"))
        self.assertEqual(admin_access, {"is_admin": True, "sections": []})
        self.assertEqual(member_access, {"is_admin": False, "sections": []})


@crm_enabled
class RouteAuditDatabaseTests(CrmApiMixin, TestCase):
    """Every method of every CRM route refuses a signed-in member, flag on or off."""

    def test_members_get_403_from_every_route_and_method(self):
        checked = 0
        for enabled in (True, False):
            with override_settings(COMMUNITY_CHAT_CRM_ENABLED=enabled):
                for path, view in crm_routes():
                    if view in OPEN_VIEWS:
                        continue
                    for method in METHODS:
                        if not hasattr(view, method):
                            continue
                        with self.subTest(enabled=enabled, method=method, path=path):
                            response = getattr(self.member_client, method)(PREFIX + path, {}, format="json")
                            self.assertEqual(response.status_code, 403)
                            self.assertEqual(response.json()["error"], "not_authorised")
                        checked += 1
        self.assertGreater(checked, 60)

    def test_the_open_views_reveal_nothing_to_members(self):
        self.assertEqual(
            self.member_client.get(PREFIX + "access/").json(),
            {"enabled": True, "can_view": False, "can_send": False, "luma_sync": None, "channels": None},
        )
        self.assertEqual(self.member_client.get(ADMIN_ACCESS).json(), {"is_admin": False, "sections": []})


@crm_enabled
class ServiceGuardDatabaseTests(CrmApiMixin, TestCase):
    """The services refuse a real non-admin account and write nothing."""

    def test_non_admin_actors_change_nothing(self):
        now = timezone.now()
        contact = make_contact("ada@example.com", first_name="Ada")
        segment = Segment.objects.create(name="Founders", definition={"match": "all", "rules": [], "exclude": []})
        blast = Blast.objects.create(purpose=Blast.Purpose.MARKETING, channels=["email"], created_by=self.admin)
        calls = {
            "create_contact": lambda: services.create_contact({"email": "new@example.com"}, actor=self.member, now=now),
            "update_contact": lambda: services.update_contact(
                contact.pk, {"city": "Melbourne"}, actor=self.member, now=now
            ),
            "stop_messaging": lambda: services.stop_messaging(
                contact.pk, ["email"], reason="", actor=self.member, now=now
            ),
            "save_segment": lambda: services.save_segment(segment, {"name": "Renamed"}, actor=self.member, now=now),
            "delete_segment": lambda: services.delete_segment(segment, actor=self.member),
            "save_blast_draft": lambda: services.save_blast_draft(
                blast, {"email": {"subject": "Hi"}}, actor=self.member, now=now
            ),
            "start_upload_import": lambda: contact_import.start_upload_import(
                file_name="people.csv", consent="none", column_plan={"Email": "email"}, actor=self.member
            ),
            "queue_blast": lambda: blasts.queue_blast(
                blast.pk, confirm={"email": 0}, idempotency_key="key-member", actor=self.member
            ),
            "cancel_blast": lambda: blasts.cancel_blast(blast.pk, actor=self.member),
            "send_tests": lambda: blasts.send_tests(blast.pk, ["email"], user=self.member),
        }
        for name, call in calls.items():
            with self.subTest(entry_point=name), self.assertRaises(AdminError):
                call()
        self.assertFalse(Contact.objects.filter(email="new@example.com").exists())
        contact.refresh_from_db()
        self.assertEqual((contact.city, contact.email_status), ("", Contact.EmailStatus.ACTIVE))
        self.assertEqual(Segment.objects.get(pk=segment.pk).name, "Founders")
        blast.refresh_from_db()
        self.assertEqual((blast.subject, blast.status), ("", Blast.Status.DRAFT))
        self.assertFalse(CrmAuditEvent.objects.exists())


@crm_enabled
class OverviewDatabaseTests(CrmApiMixin, TestCase):
    def test_segments_email_and_recent_activity(self):
        now = timezone.now()
        big = Segment.objects.create(name="Founders", definition={}, last_count=842)
        Segment.objects.create(name="Never counted", definition={})
        Segment.objects.create(name="Speakers", definition={}, last_count=12)
        small = Segment.objects.create(name="Volunteers", definition={}, last_count=3)
        Segment.objects.filter(pk=small.pk).update(updated_at=now - timedelta(days=10))

        blast = Blast.objects.create(
            purpose=Blast.Purpose.MARKETING, channels=["email", "sms"], subject="October update",
            status=Blast.Status.SENT, created_by=self.admin,
        )
        statuses = ["sent", "delivered", "read", "bounced", "undelivered", "failed", "queued", "unknown"]
        for index, status in enumerate(statuses):
            person = make_contact(f"person{index}@example.com", phone_e164=f"+6141234567{index}")
            BlastDelivery.objects.create(
                blast=blast, contact=person, channel="email", address=person.email,
                consent_basis="explicit", status=status,
            )
            BlastDelivery.objects.create(
                blast=blast, contact=person, channel="sms", address=person.phone_e164,
                consent_basis="event_registration", status="sent",
            )
        old_person = make_contact("old@example.com")
        old_row = BlastDelivery.objects.create(
            blast=blast, contact=old_person, channel="email", address=old_person.email,
            consent_basis="explicit", status="sent",
        )
        BlastDelivery.objects.filter(pk=old_row.pk).update(created_at=now - timedelta(days=31))

        def logged(minutes_ago, **fields):
            row = CrmAuditEvent.objects.create(**fields)
            CrmAuditEvent.objects.filter(pk=row.pk).update(created_at=now - timedelta(minutes=minutes_ago))

        for minutes in (1, 31, 61):
            logged(minutes, action="sync", target_type="luma_calendar", detail={})
        logged(2, action="edit", target_type="blast", target_id=str(blast.pk), actor=self.admin,
               detail={"operation": "update", "fields": ["email"]})
        logged(3, action="import", target_type="contact_import", actor=self.admin,
               detail={"operation": "start", "counts": {}})
        logged(4, action="send", target_type="blast", target_id=str(blast.pk), actor=self.admin,
               detail={"operation": "queue", "channels": {"email": 5}})
        logged(5, action="opt_out", target_type="contact", actor=self.admin, detail={"channels": ["sms"]})
        logged(6, action="import", target_type="contact_import", actor=self.admin,
               detail={"operation": "finish", "dry_run": False, "counts": {"created": 20, "updated": 4}})
        logged(7, action="import", target_type="contact_import", detail={"created": 2, "rows": 2})
        logged(8, action="edit", target_type="segment", actor=self.admin, detail={"operation": "create"})

        body = self.client.get(PREFIX + "overview/").json()
        self.assertEqual(body["segments"]["total"], 4)
        self.assertEqual(body["segments"]["updated_last_7_days"], 3)
        self.assertEqual(
            body["segments"]["top"],
            [
                {"id": str(big.pk), "name": "Founders", "last_count": 842},
                {"id": str(Segment.objects.get(name="Speakers").pk), "name": "Speakers", "last_count": 12},
                {"id": str(small.pk), "name": "Volunteers", "last_count": 3},
            ],
        )
        # Five reached the provider (the 31-day-old row is outside the window); two never arrived.
        self.assertEqual(body["email_30d"], {"sent": 5, "delivery_rate": 0.6})
        self.assertEqual(
            [row["summary"] for row in body["recent_activity"]],
            [
                "Luma sync",
                'Campaign "October update" sent',
                "Contact opted out",
                "24 contacts imported",
                "2 contacts imported",
            ],
        )
        first = body["recent_activity"][1]
        self.assertEqual(set(first), {"action", "created_at", "actor_name", "summary"})
        self.assertEqual(first["action"], "send")
        self.assertIsNone(body["recent_activity"][0]["actor_name"])
        self.assertIn("contacts", body)

    def test_an_empty_crm(self):
        body = self.client.get(PREFIX + "overview/").json()
        self.assertEqual(body["segments"], {"total": 0, "updated_last_7_days": 0, "top": []})
        self.assertEqual(body["email_30d"], {"sent": 0, "delivery_rate": None})
        self.assertEqual(body["recent_activity"], [])
