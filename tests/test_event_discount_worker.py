"""Database/network-free worker supervision and rollout boundary checks."""

import argparse
from io import StringIO
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from django.core.management.base import CommandError
from django.db import OperationalError
import yaml

from core.management.commands.run_periodic_command import Command as PeriodicCommand
from roo.management.commands.reconcile_event_discounts import Command


ROOT = Path(__file__).resolve().parents[1]


class EventDiscountWorkerTests(unittest.TestCase):
    def command(self):
        return Command(stdout=StringIO(), stderr=StringIO())

    def options(self, **values):
        return {"limit": 1, "loop": False, "interval": 30, "status": False, **values}

    def test_single_batch_is_bounded_and_does_not_sleep(self):
        with patch("roo.management.commands.reconcile_event_discounts.process_pending", return_value=1) as work, \
             patch("roo.management.commands.reconcile_event_discounts.time.sleep") as sleep:
            self.command().handle(**self.options())
            work.assert_called_once_with(limit=1)
            sleep.assert_not_called()

    def test_database_failure_has_no_success_heartbeat(self):
        command = self.command()
        with patch("roo.management.commands.reconcile_event_discounts.process_pending", side_effect=OperationalError("sensitive sql")):
            with self.assertRaises(CommandError):
                command.handle(**self.options())
        self.assertNotIn("sensitive", command.stderr.getvalue())
        self.assertNotIn("attempts", command.stdout.getvalue())

    def test_status_is_read_only_and_contains_no_member_or_coupon_data(self):
        rows = Mock()
        rows.count.return_value = 3
        rows.filter.return_value.count.return_value = 1
        command = self.command()
        with patch("roo.management.commands.reconcile_event_discounts.EventDiscountRedemption.objects.filter", return_value=rows), \
             patch("roo.management.commands.reconcile_event_discounts.process_pending") as work:
            command.handle(**self.options(status=True))
            work.assert_not_called()
        self.assertEqual(command.stdout.getvalue().strip(), "pending=3 needs_review=1")

    def test_invalid_limits_cannot_start_work(self):
        for options in (self.options(limit=0), self.options(limit=101), self.options(interval=0)):
            with patch("roo.management.commands.reconcile_event_discounts.process_pending") as work:
                with self.assertRaises(CommandError):
                    self.command().handle(**options)
                work.assert_not_called()

    def test_periodic_runner_accepts_event_reconciler(self):
        parser = argparse.ArgumentParser()
        PeriodicCommand().add_arguments(parser)
        arguments = parser.parse_args(["reconcile_event_discounts", "--poll-seconds", "30"])
        self.assertEqual(arguments.command_name, "reconcile_event_discounts")

    def test_compose_worker_is_opt_in_and_checks_without_applying_schema(self):
        service = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["event-discount-worker"]
        self.assertEqual(service["profiles"], ["event-discounts"])
        self.assertEqual(service["environment"]["RUN_MIGRATIONS_ON_START"], "0")
        self.assertIn("manage.py migrate --check --noinput", service["command"])
        self.assertIn("reconcile_event_discounts", service["command"])
        self.assertIn("--health-file /tmp/mlai-event-discounts.ok", service["command"])
        self.assertIn("mlai-event-discounts.ok", " ".join(service["healthcheck"]["test"]))

    def test_worker_is_in_all_writer_stop_and_rollback_inventory(self):
        inventory = (ROOT / "scripts/runtime-services.sh").read_text()
        self.assertIn("event_discount_runtime_services=(event-discount-worker)", inventory)
        block = inventory.split("all_runtime_writer_services=(", 1)[1].split(")", 1)[0]
        self.assertIn('"${event_discount_runtime_services[@]}"', block)

    def test_deploy_rejects_issuance_without_worker_and_drains_independently(self):
        deploy = (ROOT / "deploy.sh").read_text()
        self.assertIn("read_env_value ROO_EVENT_DISCOUNT_WORKER_ENABLED", deploy)
        self.assertIn("read_env_value ROO_EVENT_DISCOUNTS_ENABLED", deploy)
        self.assertIn("issuance requires its supervised reconciliation worker", deploy)
        self.assertIn('runtime_services+=("\\${event_discount_runtime_services[@]}")', deploy)


if __name__ == "__main__":
    unittest.main()
