import json
from io import StringIO

from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, override_settings


class ImportCapacityTests(SimpleTestCase):
    def capacity(self, **kwargs):
        output = StringIO()
        call_command("message_sync_capacity", stdout=output, **kwargs)
        return json.loads(output.getvalue())

    @override_settings(MESSAGE_SYNC_SLACK_DISTRIBUTION="internal")
    def test_full_directory_cannot_claim_two_hour_completion(self):
        result = self.capacity(info_probes=6433)
        self.assertEqual(result["minimum_provider_minutes"], 128.66)
        self.assertEqual(result["target_assessment"], "impossible_with_this_budget")

    @override_settings(MESSAGE_SYNC_SLACK_DISTRIBUTION="internal")
    def test_concurrent_owners_share_capacity_not_multiply_it(self):
        result = self.capacity(owners=4, directory_pages=5, info_probes=100,
                               history_pages=500, reply_pages=200)
        self.assertEqual(result["minimum_provider_minutes"], 40)
        self.assertEqual(result["model_minutes_at_import_share"], 80)
        self.assertEqual(result["target_assessment"], "requires_measured_end_to_end_import")

    @override_settings(MESSAGE_SYNC_SLACK_DISTRIBUTION="restricted")
    def test_restricted_budget_and_invalid_workloads(self):
        self.assertEqual(self.capacity(history_pages=200)["minimum_provider_minutes"], 200)
        for kwargs in ({}, {"info_probes": -1}, {"owners": 0}, {"history_pages": 1, "import_share": 0}):
            with self.assertRaises(CommandError):
                self.capacity(**kwargs)
