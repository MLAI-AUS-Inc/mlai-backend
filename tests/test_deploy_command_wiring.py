from io import StringIO
from pathlib import Path

from django.core.management import get_commands, load_command_class
from django.test import SimpleTestCase

from core.management.commands.deploy_postmigrate import Command as PostMigrateCommand
from core.management.commands.deploy_preflight import PREFLIGHT_STEPS


# deploy_preflight and deploy_postmigrate exist to collapse a dozen cold Django
# starts into two, but that means every sub-command they invoke is now called
# through call_command with keyword options instead of a shell flag. A renamed
# command or a retired flag would no longer fail at review time — it would fail
# mid-deploy before runtime replacement. These tests
# check the wiring against the real command registry.
class DeployCommandWiringTests(SimpleTestCase):
    def test_workflows_cannot_call_retired_slack_replay_operators(self):
        retired = {
            "backfill_community_bridge_slack_avatars", "backfill_community_bridge_slack_mentions",
            "backfill_community_bridge_thread_references", "repair_community_bridge_slack_thread",
            "reconcile_community_bridge_slack_threads", "repair_community_bridge_threads",
            "reconcile_recent_community_bridge_slack",
        }
        commands = get_commands()
        self.assertTrue(retired.isdisjoint(commands))
        self.assertIn("message_sync_repair", commands)
        for workflow in (Path(__file__).resolve().parents[1] / ".github/workflows").glob("*.yml"):
            for command in retired:
                self.assertNotIn(command, workflow.read_text(), workflow.name)

    def _assert_step_is_callable(self, name, kwargs):
        commands = get_commands()
        self.assertIn(name, commands, f"deploy step calls unknown command {name!r}")

        parser = load_command_class(commands[name], name).create_parser("manage.py", name)
        accepted = {action.dest for action in parser._actions}
        unknown = sorted(set(kwargs) - accepted)
        self.assertEqual(unknown, [], f"{name} does not accept {unknown}")

    def _recorded_postmigrate_steps(self):
        """Run deploy_postmigrate with its sub-commands stubbed out.

        Its final step reads a PostgreSQL index through a cursor, which
        SimpleTestCase blocks, so the run always ends in an exception — by
        which point every sub-command call has been recorded.
        """
        recorded = []
        command = PostMigrateCommand(stdout=StringIO(), stderr=StringIO())
        command._step = lambda label, name, kwargs: recorded.append((name, kwargs))
        try:
            command.handle()
        except Exception:
            pass
        return recorded

    def test_preflight_steps_call_real_commands_with_accepted_options(self):
        self.assertTrue(PREFLIGHT_STEPS)
        for _, name, kwargs in PREFLIGHT_STEPS:
            with self.subTest(command=name):
                self._assert_step_is_callable(name, kwargs)

    def test_postmigrate_steps_call_real_commands_with_accepted_options(self):
        recorded = self._recorded_postmigrate_steps()

        self.assertTrue(recorded, "deploy_postmigrate recorded no sub-commands")
        for name, kwargs in recorded:
            with self.subTest(command=name):
                self._assert_step_is_callable(name, kwargs)

    def test_postmigrate_verifies_migrations_before_anything_else(self):
        recorded = self._recorded_postmigrate_steps()

        self.assertEqual(recorded[0][0], "migrate")
        self.assertTrue(recorded[0][1]["check_unapplied"])

    def test_postmigrate_reaches_its_final_sub_command(self):
        # Guards the assumption above: if an earlier step started raising, the
        # option checks would silently cover only part of the sequence.
        recorded = self._recorded_postmigrate_steps()

        self.assertEqual(
            [name for name, _ in recorded],
            ["migrate", "check_org_memory_search", "validate_startup_update_schema"],
        )
