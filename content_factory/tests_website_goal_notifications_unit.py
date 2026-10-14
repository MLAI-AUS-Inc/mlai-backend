"""Fixed notice content and state identity; no transport or database access."""
from datetime import timedelta
from django.test import SimpleTestCase,override_settings
from django.utils import timezone
from . import website_goal_notifications as notifications, website_goal as goals, tests_website_goal_unit as fixtures

@override_settings(COMMUNITY_CHAT_FRONTEND_URL="https://chat.mlai.au")
class NoticeTests(SimpleTestCase):
    def setUp(self):
        self.now=timezone.now();self.op=fixtures.GoalRunnerRegressions().operation()
        self.op.payload["company_id"]="9"
        self.op.receipt["goal"]["current_source_sha"]="a"*40

    def test_repeated_owner_state_ignores_raw_messages_and_changed_action_has_new_identity(self):
        self.op.state="needs_user";self.op.receipt.update(user_action={"id":"merge_pr","run_id":"setup-one","pr_number":4,"url":"https://evil.test/?secret=canary"},message="token=credential-canary")
        first=notifications.notice_for(self.op,now=self.now)
        self.assertEqual(first,notifications.notice_for(self.op,now=self.now+timedelta(hours=2)))
        self.assertNotIn("canary",str(first));self.assertNotIn("evil.test",str(first))
        self.assertIn("company_id=9",first["link"])
        self.op.receipt["goal"]["current_source_sha"]="b"*40
        self.op.connection.configuration_version=2
        self.op.receipt["goal"]["scope"]["configuration_revision"]=2
        self.assertEqual(first["id"],notifications.notice_for(self.op,now=self.now)["id"])
        self.op.receipt["user_action"]["run_id"]="setup-two"
        self.assertNotEqual(first["id"],notifications.notice_for(self.op,now=self.now)["id"])
        self.op.receipt["user_action"]={"id":"deploy_site"}
        self.assertNotEqual(first["id"],notifications.notice_for(self.op,now=self.now)["id"])

    def test_working_or_foreign_cancelled_or_unknown_action_is_silent(self):
        for state in ("pending","running","failed","cancelled","needs_attention"):
            self.op.state=state;self.assertIsNone(notifications.notice_for(self.op,now=self.now))
        self.op.state="needs_user";self.op.receipt["user_action"]={"id":"invented"}
        self.assertIsNone(notifications.notice_for(self.op,now=self.now))
        self.op.receipt["user_action"]={"id":"review_setup"};self.op.connection.state="disconnected"
        self.assertIsNone(notifications.notice_for(self.op,now=self.now))

    def test_completion_needs_current_fresh_proof_and_one_completion_identity(self):
        goal=self.op.receipt["goal"]
        self.op.receipt["goal"]=goals.refresh_goal(goal,fixtures.proof_set(self.now),scope=goal["scope"],source_sha="a"*40,now=self.now)
        self.op.receipt["goal"]["current_source_sha"]="a"*40;self.op.state="completed"
        first=notifications.notice_for(self.op,now=self.now)
        self.assertEqual(first["state"],"done")
        self.assertEqual(first["id"],notifications.notice_for(self.op,now=self.now+timedelta(hours=1))["id"])
        self.assertIsNone(notifications.notice_for(self.op,now=self.now+timedelta(days=8)))
        self.op.receipt["goal"]["current_source_sha"]="b"*40
        self.assertIsNone(notifications.notice_for(self.op,now=self.now))

    def test_forged_completion_ticks_and_foreign_frontend_are_silent(self):
        self.op.state="completed"
        for item in self.op.receipt["goal"]["milestones"]:
            if item["required"]:item["state"]="passed"
        self.assertIsNone(notifications.notice_for(self.op,now=self.now))
        self.op.state="needs_user";self.op.receipt["user_action"]={"id":"review_setup"}
        for origin in ("https://evil.test","https://chat.mlai.au?token=secret","https://user:secret@chat.mlai.au","https://chat.mlai.au:invalid"):
            with override_settings(COMMUNITY_CHAT_FRONTEND_URL=origin):self.assertIsNone(notifications.notice_for(self.op,now=self.now))
