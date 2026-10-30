"""Snapshot merge by meaning: rails still refuse, spelling never does.

Run with scripts/test_without_database.py. No model is imported or queried.
"""
from copy import deepcopy
from django.test import SimpleTestCase, override_settings


class MeaningTests(SimpleTestCase):
    def test_empty_string_null_and_missing_are_one_value(self):
        from .snapshot_meaning import meaning, same_meaning
        self.assertIsNone(meaning(""))
        self.assertIsNone(meaning(None))
        self.assertTrue(same_meaning({"a": "", "b": 1}, {"b": 1}))
        self.assertTrue(same_meaning({"a": None}, {}))
        self.assertFalse(same_meaning({"a": "x"}, {}))

    def test_numbers_and_numeric_strings_are_equal_but_flags_are_not_numbers(self):
        from .snapshot_meaning import same_meaning
        for left, right in ((2, "2"), ("2.0", 2), (6, 6.0), ("123", 123), (" 7 ", 7)):
            with self.subTest(left=left, right=right):
                self.assertTrue(same_meaning(left, right))
        self.assertFalse(same_meaning(True, 1))
        self.assertFalse(same_meaning(False, 0))
        self.assertFalse(same_meaning("2", 3))

    def test_case_is_ignored_only_for_identifiers_compared_without_case_elsewhere(self):
        from .snapshot_meaning import same_meaning
        self.assertTrue(same_meaning("Owner/Site", "owner/site", "github_repo"))
        self.assertTrue(same_meaning("Example.TEST ", "example.test", "domain"))
        self.assertTrue(same_meaning("11111111-AAAA-1111-1111-111111111111", "11111111-aaaa-1111-1111-111111111111",
                                     "website_connection_id"))
        self.assertTrue(same_meaning("ABCDEF" * 6 + "abcd", "abcdef" * 6 + "ABCD", "expected_source_sha"))
        # Git branch names and dispatch keys are case-sensitive.
        self.assertFalse(same_meaning("Main", "main", "branch"))
        self.assertFalse(same_meaning("Dispatch-Key", "dispatch-key", "client_request_id"))

    def test_set_like_lists_ignore_order_and_ordered_lists_do_not(self):
        from .snapshot_meaning import same_meaning
        self.assertTrue(same_meaning({"acceptance_criteria": ["b", "a"]}, {"acceptance_criteria": ["a", "b"]}))
        self.assertFalse(same_meaning({"step_order": ["b", "a"]}, {"step_order": ["a", "b"]}))

    def test_rails_refuse_only_a_real_mismatch_and_other_fields_record_drift(self):
        from .snapshot_meaning import refuses
        drift = []
        self.assertFalse(refuses("roo_points_ledger_id", "123", 123, drift))
        self.assertTrue(refuses("roo_points_ledger_id", "123", "forged", drift))
        self.assertTrue(refuses("github_repo", "owner/site", None, drift))
        self.assertTrue(refuses("connection_generation", 2, "3", drift))
        self.assertEqual(drift, [])
        self.assertFalse(refuses("requested_delivery_mode", "review_draft", "content_only", drift, path="run_request."))
        self.assertEqual(drift, ["run_request.requested_delivery_mode"])

    def test_drift_counter_counts_names_and_never_values(self):
        from .snapshot_meaning import DRIFT_RESULT_KEY, record_drift
        self.assertEqual(record_drift({"x": 1}, []), {"x": 1})
        with self.assertLogs("content_factory.snapshot_meaning", "WARNING") as logs:
            result = record_drift({"x": 1}, ["run_request.editorial_brief"], run_id="run-1")
        result = record_drift(result, ["run_request.editorial_brief", "run_request.requested_delivery_mode"])
        drift = result[DRIFT_RESULT_KEY]
        self.assertEqual(result["x"], 1)
        self.assertEqual(drift["count"], 2)
        self.assertEqual(drift["fields"], {"run_request.editorial_brief": 2, "run_request.requested_delivery_mode": 1})
        self.assertIn("run_request.editorial_brief", logs.output[0])
        many = record_drift({}, [f"field_{index}" for index in range(80)])
        self.assertEqual(len(many[DRIFT_RESULT_KEY]["fields"]), 50)

    def test_flag_defaults_off(self):
        from .snapshot_meaning import merge_by_meaning_enabled
        self.assertFalse(merge_by_meaning_enabled())
        with override_settings(ARTICLES_SNAPSHOT_MERGE_BY_MEANING_ENABLED=True):
            self.assertTrue(merge_by_meaning_enabled())


class RunRequestMergeTests(SimpleTestCase):
    ORIGINAL = {"github_repo": "Owner/Site", "website_connection_id": "11111111-1111-1111-1111-111111111111",
                "connection_generation": 2, "repository_id": 42, "requested_delivery_mode": "review_draft",
                "roo_points_ledger_id": "123", "roo_points_cost": 6, "client_request_id": "dispatch-1"}

    def merge(self, incoming, drift=None):
        from .service_views import _merge_django_owned_run_request
        return _merge_django_owned_run_request(deepcopy(self.ORIGINAL), incoming, drift=drift)

    def test_flag_off_keeps_the_exact_comparison(self):
        from .editorial_run_state import EditorialRunConflict
        for change in ({"connection_generation": "2"}, {"github_repo": "owner/site"},
                       {"roo_points_ledger_id": 123}, {"requested_delivery_mode": "content_only"}):
            with self.subTest(change=change), self.assertRaises(EditorialRunConflict):
                self.merge(change)
        self.assertEqual(self.merge({"github_repo": "Owner/Site"}), self.ORIGINAL)

    def test_by_meaning_accepts_spelling_and_keeps_the_saved_values(self):
        drift = []
        merged = self.merge({"connection_generation": "2", "github_repo": "owner/site", "repository_id": "42",
                             "roo_points_ledger_id": 123, "roo_points_cost": "6", "worker_only": True}, drift)
        self.assertEqual({key: merged[key] for key in self.ORIGINAL}, self.ORIGINAL)
        self.assertTrue(merged["worker_only"])
        self.assertEqual(drift, [])

    def test_by_meaning_still_refuses_every_real_rail_change(self):
        from .editorial_run_state import EditorialRunConflict
        for change in ({"connection_generation": 3}, {"github_repo": "other/site"}, {"github_repo": None},
                       {"repository_id": 7}, {"roo_points_ledger_id": "forged"}, {"roo_points_ledger_id": None},
                       {"roo_points_cost": 0}, {"client_request_id": "other"}):
            with self.subTest(change=change), self.assertRaises(EditorialRunConflict):
                self.merge(change, [])

    def test_by_meaning_keeps_a_non_rail_saved_field_and_records_the_drift(self):
        drift = []
        merged = self.merge({"requested_delivery_mode": "content_only"}, drift)
        self.assertEqual(merged["requested_delivery_mode"], "review_draft")
        self.assertEqual(drift, ["run_request.requested_delivery_mode"])


class EditorialBriefMergeTests(SimpleTestCase):
    def snapshot(self, **brief):
        from .tests_editorial_dispatch_unit import selected_brief
        return {"workflow": "article_generation", "domain": "example.test", "status": "running",
                "run_request": {"client_request_id": "dispatch-key", "editorial_brief": {**selected_brief(), **brief}}}

    def test_flag_off_refuses_a_changed_brief(self):
        from .editorial_run_state import EditorialRunConflict, merge_editorial_run_snapshot
        with self.assertRaises(EditorialRunConflict):
            merge_editorial_run_snapshot(self.snapshot(), self.snapshot(country="NZ"))

    def test_by_meaning_keeps_the_stored_brief_and_records_drift(self):
        from .editorial_run_state import merge_editorial_run_snapshot
        original = self.snapshot()
        for incoming in (self.snapshot(country="NZ"), self.snapshot(reader_task="Private task"),
                         {**self.snapshot(), "run_request": {"editorial_brief": None}},
                         {**self.snapshot(), "run_request": {"editorial_brief": {"country": "AU"}}},
                         {**self.snapshot(), "run_request": {"editorial_brief": original["run_request"]["editorial_brief"],
                                                             "editorialBrief": {"country": "NZ"}}}):
            with self.subTest(incoming=incoming):
                drift = []
                merged = merge_editorial_run_snapshot(original, incoming, drift=drift)
                self.assertEqual(merged["run_request"]["editorial_brief"], original["run_request"]["editorial_brief"])
                self.assertEqual(drift, ["run_request.editorial_brief"])
                self.assertNotIn("Private task", str(drift))

    def test_by_meaning_same_brief_with_reordered_criteria_is_not_drift(self):
        from .editorial_run_state import merge_editorial_run_snapshot
        original = self.snapshot(acceptance_criteria=["Run the example", "Show the output"])
        drift = []
        merge_editorial_run_snapshot(original, self.snapshot(acceptance_criteria=["Show the output", "Run the example"]),
                                     drift=drift)
        self.assertEqual(drift, [])

    def test_by_meaning_still_refuses_organisation_and_dispatch_key_changes(self):
        from .editorial_run_state import EditorialRunConflict, merge_editorial_run_snapshot
        original = self.snapshot()
        moved = {**self.snapshot(), "domain": "other.test"}
        rekeyed = self.snapshot()
        rekeyed["run_request"]["client_request_id"] = "other-key"
        for incoming in (moved, rekeyed):
            with self.subTest(incoming=incoming), self.assertRaises(EditorialRunConflict):
                merge_editorial_run_snapshot(original, incoming, drift=[])

    def test_by_meaning_still_refuses_a_changed_admission(self):
        from .editorial_run_state import EditorialRunConflict, merge_editorial_run_snapshot
        from .tests_editorial_snapshot_unit import admitted_snapshot
        original = admitted_snapshot()
        incoming = deepcopy(original)
        incoming["run_request"]["editorial_admission"]["selection_sha256"] = "0" * 64
        with self.assertRaises(EditorialRunConflict):
            merge_editorial_run_snapshot(original, incoming, drift=[])
        # A worker brief that disagrees with the unchanged admission is drift:
        # the stored brief and admission both stay exactly as admitted.
        incoming = deepcopy(original)
        incoming["run_request"]["editorial_brief"]["country"] = "NZ"
        drift = []
        merged = merge_editorial_run_snapshot(original, incoming, drift=drift)
        self.assertEqual(merged["run_request"]["editorial_brief"], original["run_request"]["editorial_brief"])
        self.assertEqual(merged["run_request"]["editorial_admission"], original["run_request"]["editorial_admission"])
        self.assertEqual(drift, ["run_request.editorial_brief"])

    def test_first_seen_invalid_brief_still_refuses_with_the_flag_on(self):
        from .editorial_run_state import EditorialRunConflict, merge_editorial_run_snapshot
        incoming = self.snapshot()
        incoming["run_request"]["editorial_brief"] = {"country": "AU"}
        for existing in (None, {"workflow": "article_generation", "domain": "example.test", "run_request": {}}):
            with self.subTest(existing=existing), self.assertRaises(EditorialRunConflict):
                merge_editorial_run_snapshot(existing, incoming, drift=[])
