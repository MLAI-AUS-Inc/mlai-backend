"""Slack shared-conversation classification without database access or migrations."""

import os
import unittest
from unittest.mock import MagicMock, patch


class SlackSharedConversationPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mlai.settings")
        import django

        django.setup()
        from integrations.services import slack_dm_mirror

        cls.service = slack_dm_mirror
        cls.is_external = staticmethod(slack_dm_mirror._is_external_shared_conversation)

    def setUp(self):
        connection = patch(
            "django.db.backends.base.base.BaseDatabaseWrapper.ensure_connection",
            side_effect=AssertionError("Policy tests must not open a database"),
        )
        connection.start()
        self.addCleanup(connection.stop)

    def test_internal_enterprise_shared_conversation_is_eligible(self):
        self.assertFalse(
            self.is_external(
                {
                    "is_shared": True,
                    "is_org_shared": True,
                    "is_ext_shared": False,
                }
            )
        )

    def test_external_and_pending_external_flags_override_internal_sharing(self):
        for flag in (
            "is_ext_shared",
            "is_ext_ws_shared",
            "is_ext_shared_channel",
            "is_external_shared",
            "is_pending_ext_shared",
        ):
            with self.subTest(flag=flag):
                self.assertTrue(
                    self.is_external(
                        {"is_shared": True, "is_org_shared": True, flag: True}
                    )
                )

    def test_unclassified_shared_conversation_stays_ineligible(self):
        self.assertTrue(self.is_external({"is_shared": True}))
        self.assertTrue(self.is_external({"is_shared": True, "is_org_shared": False}))

    def test_unshared_conversation_remains_eligible(self):
        self.assertFalse(self.is_external({"is_shared": False}))
        self.assertFalse(self.is_external({}))

    def test_outer_event_external_flag_retires_existing_mirror(self):
        conversations = MagicMock()
        conversations.filter.return_value.values_list.return_value.distinct.return_value = [
            17
        ]
        with (
            patch.object(
                self.service.SlackDmMirrorConversation, "objects", conversations
            ),
            patch.object(self.service, "_retire_ineligible_conversation") as retire,
        ):
            result = self.service.ingest_slack_dm_event(
                {
                    "team_id": "TONE",
                    "is_ext_shared_channel": True,
                    "event": {"type": "message", "channel": "DONE"},
                }
            )
        self.assertEqual(result, {"status": "ignored"})
        retire.assert_called_once_with(
            17,
            "DONE",
            reason=self.service.SLACK_CONNECT_INELIGIBLE_REASON,
        )

    def test_internal_shared_event_reaches_normal_delivery_classification(self):
        with (
            patch.object(
                self.service, "_normalize_private_slack_event", return_value=None
            ) as normalize,
            patch.object(self.service, "_retire_ineligible_conversation") as retire,
        ):
            result = self.service.ingest_slack_dm_event(
                {
                    "team_id": "TONE",
                    "event": {
                        "type": "message",
                        "channel": "DONE",
                        "is_shared": True,
                        "is_org_shared": True,
                    },
                }
            )
        self.assertEqual(result, {"status": "ignored"})
        normalize.assert_called_once()
        retire.assert_not_called()


if __name__ == "__main__":
    unittest.main()
