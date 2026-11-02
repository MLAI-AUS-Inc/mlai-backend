"""Closed typed history transport contract, without network or a database."""
from copy import deepcopy
from pathlib import Path
import json
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from . import run_reconciliation as reconcile

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "docs/run-event-authority-v1.fixture.json").read_text())


def page():
    return {"schema_version": 1, "run_id": FIXTURE["run_id"], "batches": [deepcopy(FIXTURE)],
            "next_cursor": FIXTURE["events"][-1]["seq"], "has_more": False}


class HistoryContractTests(SimpleTestCase):
    def test_exact_retained_fixture_and_empty_end_of_journal_are_accepted(self):
        raw = page()
        validated = reconcile.validate_history_page(raw, run_id=FIXTURE["run_id"], after_seq=0)
        self.assertEqual(validated[0][1].events[0]["digest"], FIXTURE["events"][0]["digest"])
        cursor = raw["next_cursor"]
        self.assertEqual(reconcile.validate_history_page({**raw, "batches": []},
            run_id=FIXTURE["run_id"], after_seq=cursor), [])

    def test_no_snapshot_extra_field_cursor_invention_or_type_coercion_is_allowed(self):
        for changes in ({"schema_version": True}, {"run_id": "another-run"}, {"status": "completed"},
                        {"next_cursor": True}, {"has_more": 1}, {"next_cursor": 1000},
                        {"batches": [], "has_more": True}, {"batches": [], "next_cursor": 7},
                        {"batches": {}}, {"next_cursor": -1}):
            with self.subTest(changes=changes), self.assertRaises(reconcile.RunReconciliationError):
                reconcile.validate_history_page({**page(), **changes}, run_id=FIXTURE["run_id"], after_seq=0)

    def test_nonincreasing_page_sequences_and_wrong_digest_cannot_advance(self):
        raw = page()
        for batches in ([deepcopy(FIXTURE), deepcopy(FIXTURE)], [{**deepcopy(FIXTURE), "run_id": "foreign"}]):
            with self.assertRaises(reconcile.RunReconciliationError):
                reconcile.validate_history_page({**raw, "batches": batches}, run_id=FIXTURE["run_id"], after_seq=0)
        raw["batches"][0]["events"][0]["digest"] = "b" * 64
        with self.assertRaises(reconcile.RunReconciliationError):
            reconcile.validate_history_page(raw, run_id=FIXTURE["run_id"], after_seq=0)

    def test_bounded_http_refuses_redirects_malformed_and_oversized_pages_and_closes(self):
        claim = {"run_id": "worker/id", "cursor": 0}
        for status, content, maximum in ((302, [b"{}"], 100), (200, [b"not-json"], 100),
                                         (200, [b"01234567890"], 10)):
            remote = Mock(status_code=status, iter_content=Mock(return_value=content))
            with self.subTest(status=status, content=content), \
                    patch.object(reconcile.requests, "get", return_value=remote) as get, \
                    patch.object(reconcile, "MAX_PAGE_BYTES", maximum):
                with self.assertRaises(reconcile.RunReconciliationError):
                    reconcile._fetch_page("https://fixture.test", {"X-API-KEY": "synthetic"}, claim)
                self.assertEqual(get.call_args.args[0], "https://fixture.test/api/runs/worker%2Fid/events")
                self.assertFalse(get.call_args.kwargs["allow_redirects"])
                remote.close.assert_called_once()

    def test_missing_service_configuration_is_read_only_and_never_contacts_any_provider(self):
        with patch("content_factory.reconciliation._remote_base_url", return_value=""), \
                patch("content_factory.reconciliation._remote_headers", return_value={}), \
                patch.object(reconcile.requests, "get") as get:
            result = reconcile.run_authoritative_reconciliation_sweep()
        self.assertEqual((result["status"], result["checked"], result["skipped"]),
                         ("completed", 0, "remote_not_configured"))
        get.assert_not_called()

    def test_slow_stream_exceeding_read_deadline_is_closed_without_using_its_body(self):
        remote = Mock(status_code=200, iter_content=Mock(return_value=[b"{}"]))
        with patch.object(reconcile.requests, "get", return_value=remote), \
                patch.object(reconcile, "monotonic", side_effect=[0, 16]):
            with self.assertRaisesRegex(reconcile.RunReconciliationError, "history_read_deadline"):
                reconcile._fetch_page("https://fixture.test", {}, {"run_id": "worker", "cursor": 0})
        remote.close.assert_called_once()
