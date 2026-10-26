"""MLAI Chat's Admin gate without a database: the route audit, ``admin/access/``,
the service guards and the overview's summaries and rates."""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.urls import URLPattern, URLResolver, resolve
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from community_chat.admin_access import AdminError, require_mlai_admin
from community_chat.admin_views import AdminAccessView, MlaiAdminView
from community_chat.crm import serializers as wire
from community_chat.crm import services, urls as crm_urls, views
from community_chat.throttles import CommunityChatScopedThrottle
from core.models import User
from crm import blasts, contact_import
from crm.models import Blast, CrmAuditEvent, Segment
from crm.queries import delivery_rate
from roo.permissions import is_mlai_admin_user, mlai_admin_role

NOW = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc)
ADMIN_ACCESS_PATH = "/api/v1/community-chat/admin/access/"
# The only Admin views a non-admin may reach; both reveal nothing to them.
OPEN_VIEWS = {views.AccessView, AdminAccessView}


def view_classes(patterns):
    """Every class-based view under ``patterns``, includes followed."""

    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            yield from view_classes(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            yield pattern.pattern, getattr(pattern.callback, "view_class", None)


def admin_gate(is_admin=True, *, verified=True):
    return (
        patch("community_chat.admin_access.has_verified_installation", return_value=verified),
        patch("community_chat.admin_access.is_mlai_admin_user", return_value=is_admin),
    )


def signed_in(path=ADMIN_ACCESS_PATH, *, installation=True, **user_fields):
    request = APIRequestFactory().get(path)
    user = User(email="someone@example.test", is_active=True, **user_fields)
    force_authenticate(request, user=user)
    if installation:
        request.community_chat_public_key = "a" * 64
        request.community_chat_installation_id = uuid.uuid4()
    return request


class RouteAuditTests(SimpleTestCase):
    """A CRM endpoint added without the MLAI admin gate fails here."""

    def test_every_crm_route_subclasses_the_admin_view_and_is_admin_only(self):
        seen = set()
        for pattern, view in view_classes(crm_urls.urlpatterns):
            with self.subTest(route=str(pattern)):
                self.assertIsNotNone(view, "CRM routes must be class-based MlaiAdminView views")
                self.assertTrue(issubclass(view, MlaiAdminView), view)
                seen.add(view)
                if view not in OPEN_VIEWS:
                    self.assertIs(view.admin_only, True, view)
        self.assertIn(views.AccessView, seen)
        self.assertGreater(len(seen), 20)

    def test_admin_access_is_mounted_beside_the_crm_routes(self):
        view = resolve(ADMIN_ACCESS_PATH).func.view_class
        self.assertIs(view, AdminAccessView)
        self.assertTrue(issubclass(view, MlaiAdminView))
        self.assertIs(view.admin_only, False)
        self.assertEqual(view.community_chat_throttle_scope, "community_chat_admin")

    def test_only_the_access_views_open_the_gate(self):
        for view in OPEN_VIEWS:
            with self.subTest(view=view.__name__):
                self.assertIs(view.admin_only, False)
        self.assertIs(MlaiAdminView.admin_only, True)
        self.assertIs(views.CrmView.admin_only, True)


class Probe(MlaiAdminView):
    def get(self, request):
        return Response({"ok": True})


@patch.object(CommunityChatScopedThrottle, "allow_request", return_value=True)
class AdminGateTests(SimpleTestCase):
    def call(self, view, request):
        response = view.as_view()(request)
        response.render()
        return response

    def test_admins_pass_and_everyone_else_gets_403(self, _throttle):
        for is_admin, verified, status in ((True, True, 200), (False, True, 403), (True, False, 403)):
            device, role = admin_gate(is_admin, verified=verified)
            with self.subTest(is_admin=is_admin, verified=verified), device, role:
                response = self.call(Probe, signed_in())
                self.assertEqual(response.status_code, status)
                self.assertEqual(response["Cache-Control"], "no-store")
                if status == 403:
                    self.assertEqual(response.data, {"error": "not_authorised", "outcome": "not_authorised"})

    def test_a_jwt_session_is_refused_even_for_a_superuser(self, _throttle):
        with patch("community_chat.admin_access.is_mlai_admin_user") as role:
            response = self.call(Probe, signed_in(installation=False, is_superuser=True))
        self.assertEqual(response.status_code, 403)
        role.assert_not_called()

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=True)
    def test_admin_access_lists_sections_for_admins_only(self, _throttle):
        device, role = admin_gate()
        with device, role:
            response = self.call(AdminAccessView, signed_in())
        self.assertEqual((response.status_code, response.data), (200, {"is_admin": True, "sections": ["crm"]}))
        self.assertEqual(response["Cache-Control"], "no-store")

        device, role = admin_gate(False)
        with device, role:
            response = self.call(AdminAccessView, signed_in())
        self.assertEqual((response.status_code, response.data), (200, {"is_admin": False, "sections": []}))

        response = self.call(AdminAccessView, signed_in(installation=False, is_superuser=True))
        self.assertEqual(response.data, {"is_admin": False, "sections": []})

    @override_settings(COMMUNITY_CHAT_CRM_ENABLED=False)
    def test_admin_access_has_no_sections_while_the_crm_is_off(self, _throttle):
        device, role = admin_gate()
        with device, role:
            response = self.call(AdminAccessView, signed_in())
        self.assertEqual(response.data, {"is_admin": True, "sections": []})


class MlaiAdminRoleUnitTests(SimpleTestCase):
    """The paths that need no ``PointsAdmin`` lookup."""

    def test_anonymous_inactive_and_superuser(self):
        self.assertIsNone(mlai_admin_role(None))
        self.assertIsNone(mlai_admin_role(User(email="gone@example.test", is_active=False, is_superuser=True)))
        self.assertEqual(mlai_admin_role(User(email="root@example.test", is_superuser=True)), "superuser")
        self.assertTrue(is_mlai_admin_user(User(email="root@example.test", is_superuser=True)))


class ServiceGuardTests(SimpleTestCase):
    """Each write a view calls refuses a non-admin actor before any database work."""

    def entry_points(self, actor):
        some_id = uuid.uuid4()
        return {
            "create_contact": lambda: services.create_contact({"email": "a@example.test"}, actor=actor, now=NOW),
            "update_contact": lambda: services.update_contact(some_id, {"city": "Melbourne"}, actor=actor, now=NOW),
            "stop_messaging": lambda: services.stop_messaging(
                some_id, ["email"], reason="", actor=actor, now=NOW
            ),
            "save_segment": lambda: services.save_segment(
                None, {"name": "Founders", "definition": {"rules": []}}, actor=actor, now=NOW
            ),
            "delete_segment": lambda: services.delete_segment(Segment(name="Founders"), actor=actor),
            "save_blast_draft": lambda: services.save_blast_draft(
                None, {"purpose": "marketing"}, actor=actor, now=NOW
            ),
            "start_upload_import": lambda: contact_import.start_upload_import(
                file_name="people.csv", consent="none", column_plan={"Email": "email"}, actor=actor
            ),
            "apply_upload_chunk": lambda: contact_import.apply_upload_chunk(
                some_id, start_row=0, rows=[{"Email": "a@example.test"}], actor=actor
            ),
            "finish_upload_import": lambda: contact_import.finish_upload_import(some_id, actor=actor),
            "cancel_upload_import": lambda: contact_import.cancel_upload_import(some_id, actor=actor),
            "queue_blast": lambda: blasts.queue_blast(
                some_id, confirm={"email": 1}, idempotency_key="key-1", actor=actor
            ),
            "cancel_blast": lambda: blasts.cancel_blast(some_id, actor=actor),
            "send_tests": lambda: blasts.send_tests(some_id, ["email"], user=actor),
        }

    def test_non_admin_actors_are_refused(self):
        actor = User(email="member@example.test", is_active=True)
        with patch("community_chat.admin_access.is_mlai_admin_user", return_value=False) as role:
            for name, call in self.entry_points(actor).items():
                with self.subTest(entry_point=name), self.assertRaises(AdminError) as caught:
                    call()
                self.assertEqual((caught.exception.code, caught.exception.status), ("not_authorised", 403))
        self.assertTrue(all(args == ((actor,), {}) for args in role.call_args_list))
        self.assertEqual(role.call_count, len(self.entry_points(actor)))

    def test_a_missing_actor_is_refused(self):
        for name, call in self.entry_points(None).items():
            with self.subTest(entry_point=name), self.assertRaises(AdminError):
                call()

    def test_the_guard_itself(self):
        with self.assertRaises(AdminError):
            require_mlai_admin(None)
        require_mlai_admin(User(email="root@example.test", is_superuser=True))


class OverviewRateTests(SimpleTestCase):
    def test_delivery_rate(self):
        self.assertIsNone(delivery_rate(0, 0))
        self.assertIsNone(delivery_rate(0, 3))
        self.assertEqual(delivery_rate(12842, 90), 0.993)
        self.assertEqual(delivery_rate(4, 0), 1.0)
        self.assertEqual(delivery_rate(3, 1), 0.6667)
        self.assertEqual(delivery_rate(2, 5), 0.0)


def audit_row(action, target_type="", target_id="", detail=None, actor=None):
    return CrmAuditEvent(
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail if detail is not None else {},
        actor=actor,
        created_at=NOW,
    )


class ActivitySummaryTests(SimpleTestCase):
    def setUp(self):
        self.blast = Blast(subject="October update")
        self.blasts = {str(self.blast.pk): self.blast}

    def summary(self, *args, **kwargs):
        return wire.activity_summary(audit_row(*args, **kwargs), self.blasts)

    def test_imports(self):
        upload = {"operation": "finish", "dry_run": False, "counts": {"created": 20, "updated": 4, "unchanged": 9}}
        self.assertEqual(self.summary("import", "contact_import", detail=upload), "24 contacts imported")
        self.assertEqual(
            self.summary("import", "contact_import", detail={"created": 1, "rows": 3}), "1 contact imported"
        )
        dry_run = {**upload, "dry_run": True}
        self.assertEqual(self.summary("import", "contact_import", detail=dry_run), "Import checked: 24 contacts")
        self.assertEqual(self.summary("import", "contact_import", detail={"operation": "cancel"}), "Import stopped")
        self.assertEqual(
            self.summary("import", "contact_import", detail={"counts": {"created": "lots", "updated": True}}),
            "0 contacts imported",
        )

    def test_campaigns_are_named_from_the_blast(self):
        blast_id = str(self.blast.pk)
        cases = {
            ("send", "queue"): 'Campaign "October update" sent',
            ("send", "cancel"): 'Campaign "October update" cancelled',
            ("send", "test"): 'Test of campaign "October update" sent',
            ("send", "refuse"): 'Campaign "October update" stopped: its creator is no longer an admin',
            ("edit", "create"): 'Campaign "October update" drafted',
        }
        for (action, operation), expected in cases.items():
            with self.subTest(operation=operation):
                self.assertEqual(self.summary(action, "blast", blast_id, {"operation": operation}), expected)
        self.assertEqual(self.summary("send", "blast", str(uuid.uuid4()), {"operation": "queue"}), "Campaign sent")

    def test_long_titles_are_shortened(self):
        self.blast.subject = "A very long subject line " * 5
        line = self.summary("send", "blast", str(self.blast.pk), {"operation": "queue"})
        self.assertLess(len(line), 90)
        self.assertIn("…", line)

    def test_everything_else(self):
        self.assertEqual(self.summary("sync", "luma_calendar", detail={"events_synced": 3}), "Luma sync")
        self.assertEqual(self.summary("sync", "listmonk"), "Email feedback synced")
        self.assertEqual(self.summary("sync", "somewhere_new"), "Sync")
        self.assertEqual(self.summary("edit", "contact", detail={"operation": "create"}), "Contact added")
        self.assertEqual(self.summary("edit", "contact", detail={"operation": "update"}), "Contact edited")
        self.assertEqual(self.summary("edit", "segment", detail={"operation": "delete"}), "Segment deleted")
        self.assertEqual(self.summary("opt_out", "contact", detail={"channels": ["sms"]}), "Contact opted out")
        self.assertEqual(self.summary("export"), "Export")

    def test_detail_text_never_reaches_the_line(self):
        detail = {"operation": "queue", "reason": "ada@example.com asked", "fields": ["email"]}
        for action, target in (("send", "blast"), ("edit", "contact"), ("opt_out", "contact"), ("import", "")):
            with self.subTest(action=action):
                self.assertNotIn("ada@", self.summary(action, target, str(self.blast.pk), detail))

    def test_rows_are_whitelisted(self):
        actor = User(pk=7, email="sam@example.test", first_name="Sam", last_name="Donegan")
        row = wire.activity_row(
            audit_row("opt_out", "contact", detail={"channels": ["email"], "reason": "secret"}, actor=actor),
            self.blasts,
        )
        self.assertEqual(
            row,
            {
                "action": "opt_out",
                "created_at": "2026-10-25T01:00:00Z",
                "actor_name": "Sam Donegan",
                "summary": "Contact opted out",
            },
        )
        self.assertIsNone(wire.activity_row(audit_row("sync", "luma_calendar"), {})["actor_name"])
