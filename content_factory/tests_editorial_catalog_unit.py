"""Pure contract tests; run with python -m unittest, without Django/DB setup."""
import unittest
from content_factory.editorial_catalog import catalog_payload, merge_strategy, update_catalog


class EditorialCatalogTests(unittest.TestCase):
    def audience(self):
        return {"id": "BUILDER", "reader_task": "Show tested work", "status": "approved", "approved_by": "fixture-editor", "approved_at": "2026-09-09T00:00:00Z"}

    def test_roundtrip_and_generated_pillars_cannot_clobber_catalog(self):
        strategy = update_catalog({"pillars": ["Delivery"]}, {"audience_options": [self.audience()]})
        payload = catalog_payload(strategy)
        self.assertEqual(payload["audience_options"][0]["id"], "BUILDER")
        self.assertEqual(payload["editorial_catalog_version"], 1)
        rescan = merge_strategy(strategy, {"pillars": ["Evidence"], "editorial_catalog": {"audience_options": []}})
        self.assertEqual(catalog_payload(rescan), payload)
        self.assertEqual(rescan["pillars"], ["Evidence"])

    def test_stale_and_changed_unversioned_updates_fail(self):
        strategy = update_catalog({}, {"audience_options": [self.audience()]})
        with self.assertRaises(ValueError):
            update_catalog(strategy, {"expected_editorial_catalog_version": 0, "audience_options": []})
        with self.assertRaises(ValueError):
            update_catalog(strategy, {"audience_options": [{**self.audience(), "reader_task": "Changed task"}]})

    def test_approval_requires_provenance(self):
        with self.assertRaises(ValueError):
            update_catalog({}, {"audience_options": [{**self.audience(), "approved_by": None}]})

    def test_no_op_is_idempotent(self):
        strategy = update_catalog({}, {"audience_options": [self.audience()]})
        self.assertEqual(update_catalog(strategy, catalog_payload(strategy)), strategy)
