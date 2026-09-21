"""Database-free unread coverage regressions."""
from unittest import TestCase

from integrations.services.message_sync.read_coverage import read_coverage


class ReadCoverageTests(TestCase):
    def test_cache_occupancy_does_not_prove_unread_availability(self):
        for snapshot in (None, {"available": False}, {"available": True}):
            value = read_coverage({"dm": snapshot}, discovery_complete=True, now=200)
            self.assertFalse(value["complete"])
            self.assertFalse(value["fresh"])

    def test_source_freshness_discovery_and_device_provisioning_are_independent(self):
        snapshots = {"dm": {"available": True, "is_unread": False, "fetched_at": 100}}
        self.assertFalse(read_coverage(snapshots, discovery_complete=False, now=200)["complete"])
        self.assertFalse(read_coverage(snapshots, discovery_complete=True, pending_channels=1, now=200)["complete"])
        fresh = read_coverage(snapshots, discovery_complete=True, now=200)
        self.assertTrue(fresh["complete"])
        self.assertTrue(fresh["fresh"])
        stale = read_coverage(snapshots, discovery_complete=True, now=300)
        self.assertTrue(stale["complete"])
        self.assertFalse(stale["fresh"])

    def test_unknown_zero_channels_does_not_claim_caught_up(self):
        self.assertFalse(read_coverage({}, discovery_complete=False)["complete"])
        self.assertTrue(read_coverage({}, discovery_complete=True)["complete"])

    def test_explicit_source_exclusion_is_resolved_but_must_stay_fresh(self):
        snapshots = {"not-a-member": {"available": False, "excluded": True, "fetched_at": 100}}
        coverage = read_coverage(snapshots, discovery_complete=True, now=200)
        self.assertTrue(coverage["complete"])
        self.assertTrue(coverage["fresh"])
        self.assertEqual(coverage["expected_channels"], 0)
        self.assertEqual(coverage["excluded_channels"], 1)
        self.assertFalse(read_coverage(snapshots, discovery_complete=True, now=300)["fresh"])
