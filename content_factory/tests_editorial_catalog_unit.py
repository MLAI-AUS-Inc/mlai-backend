"""Pure contract tests; run with python -m unittest, without Django/DB setup."""
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from content_factory.editorial_catalog import (
    CatalogConflict, approve_catalog, catalog_payload, merge_strategy, review_payload, update_catalog,
)
from content_factory.editorial_contract import ArticleEditorialBrief, AudienceOption, normalize_cta_options, resolve_editorial_brief


class EditorialCatalogTests(unittest.TestCase):
    def audience(self):
        return {"id": "BUILDER", "reader_task": "Show tested work", "status": "draft"}

    def test_roundtrip_and_generated_pillars_cannot_clobber_catalog(self):
        strategy = update_catalog({"pillars": ["Delivery"]}, {"expected_editorial_catalog_version": 0, "audience_options": [self.audience()]})
        payload = catalog_payload(strategy)
        self.assertEqual(payload["audience_options"][0]["id"], "BUILDER")
        self.assertEqual(payload["editorial_catalog_version"], 1)
        rescan = merge_strategy(strategy, {"pillars": ["Evidence"], "editorial_catalog": {"audience_options": []}})
        self.assertEqual(catalog_payload(rescan), payload)
        self.assertEqual(rescan["pillars"], ["Evidence"])

    def test_stale_and_changed_unversioned_updates_fail(self):
        strategy = update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [self.audience()]})
        with self.assertRaises(ValueError):
            update_catalog(strategy, {"expected_editorial_catalog_version": 0, "audience_options": []})
        with self.assertRaises(ValueError):
            update_catalog(strategy, {"expected_editorial_catalog_version": 1, "audience_options": [{**self.audience(), "reader_task": "Changed task"}]})

    def test_service_cannot_create_approval(self):
        with self.assertRaises(ValueError):
            update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [{**self.audience(), "status": "approved"}]})

    def test_no_op_is_idempotent(self):
        strategy = update_catalog({}, {"expected_editorial_catalog_version": 0, "audience_options": [self.audience()]})
        self.assertEqual(update_catalog(strategy, edit_payload(strategy)), strategy)


NOW = datetime(2026, 9, 10, 3, 0, tzinfo=timezone.utc)


def edit_payload(strategy):
    payload = catalog_payload(strategy)
    payload["expected_editorial_catalog_version"] = payload.pop("editorial_catalog_version")
    return payload


def approval_payload(strategy):
    review = review_payload(strategy)
    return {"expected_editorial_catalog_version": review["editorial_catalog_version"],
            "entries": list(reversed(review["review_entries"]))}


def draft_catalog():
    return update_catalog({"pillars": ["Delivery"]}, {
        "expected_editorial_catalog_version": 0,
        "audience_options": [{"id": "BUILDER", "reader_task": "Show tested work", "allow_no_offer": True}],
        "cta_options": [{"id": "studio", "title": "Apply", "body": "Apply for consideration", "button_text": "Apply",
                         "button_href": "/studio#apply", "audience_ids": ["BUILDER"], "countries": ["AU"]}],
    })


def approved_catalog():
    strategy = draft_catalog()
    return approve_catalog(strategy, approval_payload(strategy), actor_id="user:fixture-owner", approved_at=NOW)


class ContentBoundApprovalTests(unittest.TestCase):
    def test_draft_review_approval_roundtrip_in_any_selection_order(self):
        original = draft_catalog()
        approved = approve_catalog(original, approval_payload(original), actor_id="user:fixture-owner", approved_at=NOW)
        self.assertEqual(catalog_payload(original)["audience_options"][0]["status"], "draft")
        payload = catalog_payload(approved)
        self.assertEqual(payload["editorial_catalog_version"], 2)
        for field in ("audience_options", "cta_options"):
            self.assertEqual(payload[field][0]["approved_by"], "user:fixture-owner")
            self.assertEqual(payload[field][0]["approved_at"], NOW.isoformat())
            self.assertEqual(payload[field][0]["status"], "approved")
        self.assertEqual(len(approved["editorial_catalog"]["approval_history"]), 2)
        self.assertEqual(approved["editorial_catalog"]["approval_history"][1]["entry"], payload["cta_options"][0])
        self.assertEqual(approved["pillars"], ["Delivery"])

    def test_expected_revision_is_required_strict_and_current_for_both_operations(self):
        for value in (None, True, False, 1.0, "1", -1, [], {}):
            for operation in ("edit", "approve"):
                with self.subTest(value=value, operation=operation), self.assertRaises(ValueError):
                    strategy = draft_catalog()
                    payload = edit_payload(strategy) if operation == "edit" else approval_payload(strategy)
                    payload["expected_editorial_catalog_version"] = value
                    if operation == "edit":
                        update_catalog(strategy, payload)
                    else:
                        approve_catalog(strategy, payload, actor_id="user:owner", approved_at=NOW)
        with self.assertRaises(CatalogConflict):
            approve_catalog(approved_catalog(), approval_payload(draft_catalog()), actor_id="user:owner", approved_at=NOW)

    def test_service_cannot_forge_new_or_renewed_approval(self):
        for existing in ({}, approved_catalog()):
            for field in ("audience_options", "cta_options"):
                with self.subTest(existing=bool(existing), field=field), self.assertRaises(ValueError):
                    payload = edit_payload(existing or draft_catalog())
                    payload["expected_editorial_catalog_version"] = catalog_payload(existing)["editorial_catalog_version"]
                    item = payload[field][0]
                    item.update(version=2, status="approved", approved_by="copied-reviewer", approved_at=NOW.isoformat())
                    update_catalog(existing, payload)
        for key in ("approved_by", "approved_at"):
            for value in ("", " ", "copied-provenance"):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    payload = edit_payload(draft_catalog())
                    payload["cta_options"][0].update(version=2, **{key: value})
                    update_catalog(draft_catalog(), payload)

    def test_changed_offer_requires_draft_higher_version_then_exact_owner_review(self):
        original = approved_catalog()
        payload = edit_payload(original)
        offer = payload["cta_options"][0]
        offer.update(version=2, button_href="/changed", status="draft", approved_by=None, approved_at=None)
        draft = update_catalog(original, payload)
        self.assertEqual(catalog_payload(draft)["cta_options"][0]["status"], "draft")
        with self.assertRaises(CatalogConflict):
            stale = approval_payload(original)
            stale["expected_editorial_catalog_version"] = catalog_payload(draft)["editorial_catalog_version"]
            approve_catalog(draft, stale, actor_id="user:owner", approved_at=NOW)
        approved = approve_catalog(draft, approval_payload(draft), actor_id="user:second-owner", approved_at=NOW)
        self.assertEqual(catalog_payload(approved)["cta_options"][0]["approved_by"], "user:second-owner")
        self.assertEqual(catalog_payload(approved)["audience_options"][0]["approved_by"], "user:fixture-owner")
        self.assertEqual(len(approved["editorial_catalog"]["approval_history"]), 3)
        self.assertEqual(approved["editorial_catalog"]["approval_history"][1]["entry"]["button_href"], "/studio#apply")
        self.assertEqual(approved["editorial_catalog"]["approval_history"][2]["entry"]["button_href"], "/changed")

    def test_audience_edit_must_explicitly_invalidate_dependent_offers(self):
        original = approved_catalog()
        payload = edit_payload(original)
        payload["audience_options"][0].update(version=2, reader_task="A different task", status="draft", approved_by=None, approved_at=None)
        with self.assertRaisesRegex(ValueError, "dependent offers"):
            update_catalog(original, payload)
        payload["cta_options"][0].update(version=2, status="draft", approved_by=None, approved_at=None)
        draft = update_catalog(original, payload)
        offer_only = approval_payload(draft)
        offer_only["entries"] = [entry for entry in offer_only["entries"] if entry["kind"] == "offer"]
        with self.assertRaisesRegex(ValueError, "approved, known audiences"):
            approve_catalog(draft, offer_only, actor_id="user:owner", approved_at=NOW)
        accepted = approve_catalog(draft, approval_payload(draft), actor_id="user:owner", approved_at=NOW)
        self.assertEqual(catalog_payload(accepted)["cta_options"][0]["status"], "approved")

    def test_fingerprint_protects_content_even_if_storage_version_was_not_incremented(self):
        for field, key, value in (("audience_options", "reader_task", "Different"),
                                  ("audience_options", "allow_no_offer", False),
                                  ("cta_options", "countries", ["NZ"]),
                                  ("cta_options", "button_href", "/different"),
                                  ("cta_options", "body", "New promise")):
            with self.subTest(field=field, key=key):
                original = approved_catalog()
                tampered = deepcopy(original)
                tampered["editorial_catalog"][field][0][key] = value
                payload = catalog_payload(tampered)
                self.assertEqual(payload[field][0]["status"], "draft")
                self.assertIsNone(payload[field][0]["approved_by"])
                with self.assertRaises(CatalogConflict):
                    approve_catalog(tampered, approval_payload(original), actor_id="user:owner", approved_at=NOW)

    def test_legacy_approval_strings_are_drafts_until_explicitly_reviewed(self):
        old = approved_catalog()
        old["editorial_catalog"].pop("approval_receipts")
        old["editorial_catalog"]["schema_version"] = 1
        self.assertTrue(all(a["status"] == "draft" for field in ("audience_options", "cta_options") for a in catalog_payload(old)[field]))
        self.assertEqual(old["editorial_catalog"]["audience_options"][0]["status"], "approved")  # Pure read, no mutation.
        accepted = approve_catalog(old, approval_payload(old), actor_id="user:owner", approved_at=NOW)
        self.assertEqual(catalog_payload(accepted)["cta_options"][0]["status"], "approved")

    def test_no_deletion_or_version_reuse_or_boolean_entry_versions(self):
        original = approved_catalog()
        for field in ("audience_options", "cta_options"):
            payload = edit_payload(original)
            payload[field] = []
            with self.assertRaisesRegex(ValueError, "Retire"):
                update_catalog(original, payload)
            for version in (0, 1, True, "2", 2.0):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    payload = edit_payload(original)
                    payload[field][0].update(version=version, status="retired", approved_by=None, approved_at=None)
                    update_catalog(original, payload)

    def test_null_or_dictionary_lists_do_not_silently_erase_entries(self):
        for field in ("audience_options", "cta_options"):
            for value in (None, {}, "", 0):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    update_catalog(draft_catalog(), {"expected_editorial_catalog_version": 1, field: value})

    def test_approval_rejects_forged_identity_duplicates_retirements_and_bad_selection(self):
        strategy = draft_catalog()
        for extra in ({"approved_by": "spoof"}, {"approved_at": NOW.isoformat()}, {"domain": "other.example"}):
            with self.assertRaises(ValueError):
                approve_catalog(strategy, {**approval_payload(strategy), **extra}, actor_id="user:owner", approved_at=NOW)
        for entries in ([], None, {}, [None], [{"kind": "audience"}], [approval_payload(strategy)["entries"][0]] * 2):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                approve_catalog(strategy, {**approval_payload(strategy), "entries": entries}, actor_id="user:owner", approved_at=NOW)
        payload = edit_payload(strategy)
        payload["cta_options"][0].update(status="retired", version=2)
        retired = update_catalog(strategy, payload)
        with self.assertRaises(ValueError):
            approve_catalog(retired, approval_payload(retired), actor_id="user:owner", approved_at=NOW)

    def test_approval_requires_known_audiences_and_explicit_markets(self):
        for patch in ({"audience_ids": []}, {"audience_ids": ["unknown"]}, {"countries": []},
                      {"countries": ["au"]}, {"countries": ["Australia"]}, {"countries": ["AU", "AU"]}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                original = draft_catalog()
                payload = edit_payload(original)
                payload["cta_options"][0].update(version=2, **patch)
                draft = update_catalog(original, payload)
                approve_catalog(draft, approval_payload(draft), actor_id="user:owner", approved_at=NOW)

    def test_noop_approval_and_scan_do_not_reissue_or_erase_history(self):
        original = approved_catalog()
        self.assertEqual(approve_catalog(original, approval_payload(original), actor_id="user:other", approved_at=NOW), original)
        self.assertEqual(update_catalog(original, edit_payload(original)), original)
        scan = merge_strategy(original, {"pillars": ["Different"], "editorial_catalog": {}})
        self.assertEqual(scan["editorial_catalog"], original["editorial_catalog"])

    def test_dispatch_resolves_only_current_approved_offer_and_no_offer_permissions(self):
        brief = ArticleEditorialBrief(audience_id="BUILDER", audience_version=1, offer_id="studio", offer_version=1,
                                      country="AU", reader_task="Show tested work", distinct_contribution="A tested example", acceptance_criteria=["Run the example"])
        original = approved_catalog()
        def resolve(strategy, selected=brief):
            current = catalog_payload(strategy)
            return resolve_editorial_brief(selected, [AudienceOption.model_validate(a) for a in current["audience_options"]], normalize_cta_options(current["cta_options"]))
        self.assertEqual(resolve(original)[1].id, "studio")
        self.assertIsNone(resolve(original, brief.model_copy(update={"conversion_intent": "none", "offer_id": None, "offer_version": None, "no_offer_reason": "Learning"}))[1])
        with self.assertRaises(ValueError):
            resolve(draft_catalog())
        payload = edit_payload(original)
        payload["cta_options"][0].update(version=2, status="retired", approved_by=None, approved_at=None)
        with self.assertRaises(ValueError):
            resolve(update_catalog(original, payload))
