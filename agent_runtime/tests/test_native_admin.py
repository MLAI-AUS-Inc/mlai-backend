"""Chat admin controls for Dots (list, suspend, restore, remove, activity), without a database."""

from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.test import SimpleTestCase
from django.urls import resolve
from rest_framework.exceptions import PermissionDenied, ValidationError

from agent_runtime import native_admin as na
from agent_runtime import policy

NOW = datetime(2026, 11, 15, tzinfo=dt_timezone.utc)


def dot(state="active"):
    installation = SimpleNamespace(pk=uuid4(), name="Maek", handle="sam-donegan-maek@chat.mlai.au",
        owner=SimpleNamespace(first_name="Sam", last_name="Donegan"), app_id="openai-dot", state=state,
        public_key="b" * 64, created_at=NOW, suspended_at=None, native_cursor={"epoch": "x"}, is_active=True,
        community_id=uuid4())
    installation.grants = MagicMock()
    installation.grants.order_by.return_value.first.return_value = None
    installation.save = MagicMock()
    return installation


admin = SimpleNamespace(pk=1, is_active=True)


def raw(function):
    """Run the body without opening a database transaction."""
    return getattr(function, "__wrapped__", function)


@patch.object(na, "is_chat_admin", return_value=True)
@patch.object(na.runtime, "audit")
class ActionTests(SimpleTestCase):
    def test_suspend_then_restore_returns_previous_state(self, audit, _):
        installation = dot("paused")
        with patch.object(na, "_locked", return_value=installation):
            self.assertEqual(raw(na.suspend)(admin, installation.pk)["state"], "suspended")
            self.assertIsNotNone(installation.suspended_at)
            self.assertEqual(raw(na.restore)(admin, installation.pk)["state"], "paused")
        self.assertIsNone(installation.suspended_at)
        self.assertEqual(installation.native_cursor, {"epoch": "x"})
        self.assertEqual([call.args[0] for call in audit.call_args_list],
                         ["native.admin.suspended", "native.admin.restored"])

    def test_restore_requires_suspension(self, *_):
        with patch.object(na, "_locked", return_value=dot()):
            with self.assertRaises(ValidationError):
                raw(na.restore)(admin, uuid4())

    def test_remove_disconnects_as_the_owner_would(self, audit, _):
        installation = dot()
        with patch.object(na, "_locked", return_value=installation), \
                patch.object(na.runtime, "revoke_installation") as revoke:
            raw(na.remove)(admin, installation.pk)
        revoke.assert_called_once_with(installation.pk, owner=installation.owner)
        self.assertEqual(audit.call_args.args[0], "native.admin.removed")


class AuthorityTests(SimpleTestCase):
    def test_members_cannot_act(self):
        with patch.object(na, "is_chat_admin", return_value=False):
            for action in (raw(na.suspend), raw(na.restore), raw(na.remove), na.activity):
                with self.assertRaises(PermissionDenied):
                    action(admin, uuid4())

    def test_suspended_dot_fails_every_grant_check(self):
        grant = SimpleNamespace(installation=SimpleNamespace(is_active=True, owner=SimpleNamespace(is_active=True),
                                                             community_id=uuid4(), state="suspended"))
        with patch.object(policy, "feature_enabled", return_value=True):
            with self.assertRaises(policy.AgentPermissionDenied):
                policy.validate_grant_base(grant)


class ActivityTests(SimpleTestCase):
    def test_only_ids_and_types_are_shown(self):
        event = SimpleNamespace(event_type="native.dot.message_sent", created_at=NOW,
                                metadata={"channel_id": "c", "event_id": "e", "key_digest": "secret", "text": "hi"})
        query = MagicMock()
        query.__or__.return_value.order_by.return_value = [event]
        with patch.object(na, "is_chat_admin", return_value=True), \
                patch.object(na.AgentInstallation.objects, "get", return_value=dot()), \
                patch.object(na.AgentAuditEvent.objects, "filter", return_value=query):
            rows = na.activity(admin, uuid4())
        self.assertEqual(rows, [{"type": "native.dot.message_sent", "at": NOW.isoformat(),
                                 "channel_id": "c", "event_id": "e"}])


class RouteTests(SimpleTestCase):
    def test_admin_routes(self):
        identifier = uuid4()
        self.assertEqual(resolve("/api/v1/agent-runtime/admin/installations/").func.view_class.__name__,
                         "NativeAdminInstallationsView")
        self.assertEqual(resolve(f"/api/v1/agent-runtime/admin/installations/{identifier}/activity/").func.view_class.__name__,
                         "NativeAdminActivityView")
        match = resolve(f"/api/v1/agent-runtime/admin/installations/{identifier}/suspend/")
        self.assertEqual((match.func.view_class.__name__, match.kwargs["action"]),
                         ("NativeAdminInstallationActionView", "suspend"))
