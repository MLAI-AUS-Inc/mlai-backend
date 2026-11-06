"""No database: releasing held monthly reminders needs change permission, not view."""

from unittest.mock import MagicMock, patch

from django.contrib import admin
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, SimpleTestCase

from startup_updates.models import MonthlyUpdateReminderDelivery

VIEW = "startup_updates.view_monthlyupdatereminderdelivery"
CHANGE = "startup_updates.change_monthlyupdatereminderdelivery"


class StaffUser(AnonymousUser):
    """A staff user holding exactly ``perms``; no database lookups."""

    is_active = True
    is_staff = True
    is_superuser = False
    is_anonymous = False
    is_authenticated = True

    def __init__(self, *perms):
        self.perms = set(perms)

    def has_perm(self, perm, obj=None):
        return perm in self.perms

    def has_perms(self, perm_list, obj=None):
        return all(perm in self.perms for perm in perm_list)

    def has_module_perms(self, app_label):
        return any(perm.startswith(app_label + ".") for perm in self.perms)

    def get_username(self):
        return "staff"


class HeldReminderActionPermissionTests(SimpleTestCase):
    def setUp(self):
        self.model_admin = admin.site._registry[MonthlyUpdateReminderDelivery]
        self.factory = RequestFactory()

    def request(self, *perms, method="get", data=None):
        request = getattr(self.factory, method)("/admin/startup_updates/monthlyupdatereminderdelivery/", data or {})
        request.user = StaffUser(*perms)
        return request

    def queryset(self, pks=(1,)):
        queryset = MagicMock()
        queryset.filter.return_value = queryset
        queryset.values_list.return_value = list(pks)
        return queryset

    def test_view_only_staff_cannot_see_the_action(self):
        self.assertNotIn("send_held_reminders", self.model_admin.get_actions(self.request(VIEW)))
        self.assertIn("send_held_reminders", self.model_admin.get_actions(self.request(VIEW, CHANGE)))

    def test_forms_stay_read_only_for_change_holders(self):
        request = self.request(VIEW, CHANGE)
        self.assertFalse(self.model_admin.has_change_permission(request))
        self.assertFalse(self.model_admin.has_add_permission(request))

    @patch("startup_updates.monthly_update_reminders.release_held_reminder")
    def test_forged_post_from_view_only_staff_sends_nothing(self, release):
        request = self.request(VIEW, method="post", data={
            "action": "send_held_reminders", "_selected_action": ["1"], "index": "0",
        })
        with patch.object(self.model_admin, "message_user"):
            self.model_admin.response_action(request, self.queryset())
        release.assert_not_called()

    @patch("startup_updates.monthly_update_reminders.release_held_reminder")
    def test_direct_call_from_view_only_staff_is_denied(self, release):
        with self.assertRaises(PermissionDenied):
            self.model_admin.send_held_reminders(self.request(VIEW), self.queryset())
        release.assert_not_called()

    @patch("startup_updates.monthly_update_reminders.release_held_reminder", return_value={"status": "sent"})
    def test_change_permission_releases_selected_rows(self, release):
        request = self.request(VIEW, CHANGE, method="post", data={
            "action": "send_held_reminders", "_selected_action": ["1", "2"], "index": "0",
        })
        with patch.object(self.model_admin, "message_user") as message:
            self.model_admin.response_action(request, self.queryset(pks=(1, 2)))
        self.assertEqual([call.args[0] for call in release.call_args_list], [1, 2])
        self.assertIn("Sent 2", message.call_args.args[1])
