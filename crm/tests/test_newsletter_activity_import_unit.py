"""Synthetic, database-free checks for consent-neutral newsletter ingestion."""

import ast
import csv
import hashlib
import inspect
import tempfile
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from crm import newsletter_activity_import as source
from crm.management.commands.crm_import_newsletter_activity import Command

EXPORTED = "2026-11-02T05:20:55+00:00"
LATER = "2026-11-03T05:20:55+00:00"
NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)
DIGEST = "a" * 64
EMAIL = "member@sample.au"


class NewsletterParsingTests(SimpleTestCase):
    def load(self, rows, fields=None, *, digest=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.csv"
            with path.open("w", newline="", encoding="utf-8") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields or list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            return source.load_newsletter_activity(path,
                expected_sha256=digest or hashlib.sha256(path.read_bytes()).hexdigest(), exported_at=EXPORTED)

    def test_retains_only_email_and_explicit_click_ignoring_opens_and_subscription_type(self):
        rows, _ = self.load([{"Email": " Member@sample.au ", "Last clicked at": "2026-11-01T04:00:00.123Z",
            "Type": "comp", "Last email open": "tomorrow", "Emails opened (30d)": "9000", "Cancel reason": "private"}])
        self.assertEqual(rows, [source.NewsletterActivityRow(EMAIL, "2026-11-01T04:00:00.123000+00:00")])
        self.assertEqual(set(vars(rows[0])), {"email", "last_clicked_at"})

    def test_blank_click_is_unknown_and_date_only_is_kept_as_date(self):
        rows, _ = self.load([{"Email": EMAIL, "Last clicked at": ""}, {"Email": "second@sample.au", "Last clicked at": "2026-11-01"}])
        self.assertIsNone(rows[0].last_clicked_at)
        self.assertEqual(rows[1].last_clicked_at, "2026-11-01")

    def test_checksum_mismatch_rejects_source(self):
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.load([{"Email": EMAIL, "Last clicked at": ""}], digest="0" * 64)

    def test_normalized_duplicates_and_invalid_emails_reject_whole_export(self):
        for rows in ([{"Email": EMAIL, "Last clicked at": ""}, {"Email": EMAIL.upper(), "Last clicked at": ""}],
                     [{"Email": "not-an-address", "Last clicked at": ""}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "Invalid or duplicate"):
                self.load(rows)

    def test_missing_columns_and_duplicate_headers_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "columns"):
            self.load([{"Email": EMAIL}])
        with self.assertRaisesRegex(ValueError, "columns"):
            self.load([{"Email": EMAIL, "Last clicked at": ""}], fields=["Email", "Email", "Last clicked at"])

    def test_invalid_naive_and_future_click_dates_are_rejected_without_email_in_error(self):
        for stamp in ("yesterday", "2026-02-30", "2026-11-01T12:00:00", "2026-11-03", "2026-11-02T06:00:00Z"):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError) as error:
                self.load([{"Email": EMAIL, "Last clicked at": stamp}])
            self.assertNotIn(EMAIL, str(error.exception))

    def test_export_timestamp_requires_timezone_and_normalizes_utc(self):
        for value in (None, "2026-11-02", "2026-11-02T12:00:00", datetime(2026, 11, 2)):
            with self.subTest(value=value), self.assertRaises(ValueError):
                source.export_time(value)
        self.assertEqual(source.export_time("2026-11-02T16:20:55+11:00").isoformat(), EXPORTED)


class NewsletterMergeTests(SimpleTestCase):
    def merge(self, attributes, click=None, exported=LATER):
        return source.merge_newsletter_activity(attributes, source.NewsletterActivityRow(EMAIL, click), digest=DIGEST, exported_at=exported)

    def previous(self, click="2026-11-01T01:00:00+00:00"):
        return {"source": "substack_export", "exported_at": EXPORTED, "sha256": "b" * 64, "last_clicked_at": click}

    def test_only_activity_namespace_changes_and_original_attributes_are_untouched(self):
        attributes = {"marketing_review_hold": True, "engagement_rating": {"stars": 5}, "private_note": ["keep"]}
        before = deepcopy(attributes)
        result, status = self.merge(attributes, "2026-11-01T01:00:00+00:00")
        self.assertEqual(status, "updated")
        self.assertEqual({key: result[key] for key in before}, before)
        self.assertEqual(attributes, before)
        self.assertEqual(result[source.ATTRIBUTE_KEY]["sha256"], DIGEST)

    def test_blank_or_older_click_never_erases_real_prior_click(self):
        for click in (None, "2026-10-01T00:00:00+00:00"):
            with self.subTest(click=click):
                result, status = self.merge({source.ATTRIBUTE_KEY: self.previous()}, click)
                self.assertEqual(status, "updated")
                self.assertEqual(result[source.ATTRIBUTE_KEY]["last_clicked_at"], self.previous()["last_clicked_at"])
                self.assertEqual(result[source.ATTRIBUTE_KEY]["exported_at"], LATER)

    def test_newer_actual_click_replaces_older_evidence(self):
        result, _ = self.merge({source.ATTRIBUTE_KEY: self.previous()}, "2026-11-02T02:00:00+00:00")
        self.assertEqual(result[source.ATTRIBUTE_KEY]["last_clicked_at"], "2026-11-02T02:00:00+00:00")

    def test_equal_or_older_export_is_idempotent_even_with_new_click_claim(self):
        attributes = {source.ATTRIBUTE_KEY: self.previous(), "marketing_review_hold": None}
        for exported in (EXPORTED, "2026-11-01T00:00:00+00:00"):
            with self.subTest(exported=exported):
                result, status = self.merge(attributes, "2026-11-02T01:00:00+00:00", exported)
                self.assertEqual(status, "newer_or_equal_skipped")
                self.assertEqual(result, attributes)

    def test_malformed_existing_attributes_or_source_are_skipped_unchanged(self):
        for original in (None, [], "old", {source.ATTRIBUTE_KEY: []}, {source.ATTRIBUTE_KEY: {"source": "other"}}):
            with self.subTest(original=original):
                before = deepcopy(original)
                result, status = self.merge(original)
                self.assertIn(status, {"malformed_attributes_skipped", "malformed_source_skipped"})
                self.assertEqual(result, before)


class NewsletterImportTests(SimpleTestCase):
    def setUp(self):
        self.actor = object()
        self.person = SimpleNamespace(email=EMAIL, attributes={"marketing_review_hold": True},
            marketing_consent="withdrawn", email_status="unsubscribed", display_name="Existing profile", updated_at="unchanged")
        self.admin = self.enterContext(patch.object(source, "require_mlai_admin"))
        self.loader = self.enterContext(patch.object(source, "load_newsletter_activity", return_value=(
            [source.NewsletterActivityRow(EMAIL, "2026-11-01T01:00:00+00:00"), source.NewsletterActivityRow("missing@sample.au", None)], DIGEST)))
        self.enterContext(patch.object(source.timezone, "now", return_value=NOW))
        self.atomic = self.enterContext(patch.object(source.transaction, "atomic", side_effect=lambda: nullcontext()))
        self.rollback = self.enterContext(patch.object(source.transaction, "set_rollback"))
        self.on_commit = self.enterContext(patch.object(source.transaction, "on_commit"))
        self.enterContext(patch.object(source, "_lock_import"))
        self.latest = self.enterContext(patch.object(source, "_latest_export", return_value=None))
        self.select = self.enterContext(patch.object(source.Contact.objects, "select_for_update"))
        self.select.return_value.filter.return_value = [self.person]
        self.bans = self.enterContext(patch.object(source.AccountBan.objects, "annotate"))
        self.bans.return_value.filter.return_value.values_list.return_value = []
        self.write = self.enterContext(patch.object(source.Contact.objects, "bulk_update"))
        self.audit = self.enterContext(patch.object(source.CrmAuditEvent.objects, "create"))
        self.cache = self.enterContext(patch.object(source.cache, "set"))

    def run_import(self, **options):
        return source.import_newsletter_activity("/synthetic/export.csv", expected_sha256=DIGEST,
            exported_at=options.pop("exported_at", EXPORTED), actor=self.actor, **options)

    def test_default_preview_rolls_back_without_audit_or_cache_side_effects(self):
        result = self.run_import()
        self.assertEqual(result["status"], "dry_run")
        self.rollback.assert_called_once_with(True)
        self.audit.assert_not_called()
        self.on_commit.assert_not_called()
        self.cache.assert_not_called()

    def test_apply_preserves_status_and_profile_skips_missing_and_publishes_after_commit(self):
        before = vars(self.person).copy()
        result = self.run_import(dry_run=False)
        self.assertEqual(result["counts"], {"rows": 2, "updated": 1, "missing_skipped": 1})
        self.assertEqual({key: getattr(self.person, key) for key in before if key != "attributes"}, {key: value for key, value in before.items() if key != "attributes"})
        self.assertTrue(self.person.attributes["marketing_review_hold"])
        self.assertEqual(self.write.call_args.args[1], ["attributes"])
        self.assertEqual(self.audit.call_args.kwargs["actor"], self.actor)
        self.assertEqual(self.audit.call_args.kwargs["target_type"], "crm_substack_activity")
        self.assertEqual(self.audit.call_args.kwargs["detail"]["exported_at"], EXPORTED)
        self.on_commit.assert_called_once_with(source._publish_cursor)
        self.cache.assert_not_called()

    def test_banned_contact_is_not_mutated(self):
        self.bans.return_value.filter.return_value.values_list.return_value = [EMAIL]
        before = deepcopy(vars(self.person))
        result = self.run_import(dry_run=False)
        self.assertEqual(result["counts"]["banned_skipped"], 1)
        self.assertEqual(vars(self.person), before)
        self.write.assert_not_called()

    def test_admin_is_checked_before_reading_export_or_starting_transaction(self):
        self.admin.side_effect = PermissionError("admin required")
        with self.assertRaises(PermissionError):
            self.run_import()
        self.loader.assert_not_called()
        self.atomic.assert_not_called()

    def test_invalid_source_aborts_before_transaction(self):
        self.loader.side_effect = ValueError("Invalid source")
        with self.assertRaises(ValueError):
            self.run_import()
        self.atomic.assert_not_called()
        self.write.assert_not_called()

    def test_future_export_is_rejected_before_source_read(self):
        with self.assertRaisesRegex(ValueError, "future"):
            self.run_import(exported_at="2026-11-05T00:00:00Z")
        self.loader.assert_not_called()

    def test_global_newer_export_prevents_older_run_from_changing_contacts_or_freshness(self):
        self.latest.return_value = source.export_time(LATER)
        result = self.run_import(dry_run=False)
        self.assertEqual(result["status"], "newer_or_equal_skipped")
        self.select.assert_not_called()
        self.write.assert_not_called()
        self.audit.assert_not_called()
        self.on_commit.assert_not_called()

    def test_audit_failure_aborts_without_publishing_freshness(self):
        self.audit.side_effect = RuntimeError("audit failure")
        with self.assertRaises(RuntimeError):
            self.run_import(dry_run=False)
        self.atomic.assert_called_once()
        self.on_commit.assert_not_called()
        self.cache.assert_not_called()

    def test_cache_callback_reads_latest_committed_export(self):
        self.latest.return_value = source.export_time(LATER)
        source._publish_cursor()
        self.cache.assert_called_once_with(source.CURSOR_KEY, LATER, timeout=None)

    def test_failed_cache_publication_keeps_durable_audit_import_successful(self):
        self.latest.return_value = source.export_time(LATER)
        self.cache.side_effect = RuntimeError("cache unavailable")
        source._publish_cursor()


class NewsletterCommandTests(SimpleTestCase):
    def test_cli_defaults_to_preview(self):
        parsed = Command().create_parser("manage.py", "crm_import_newsletter_activity").parse_args([
            "synthetic.csv", "--expected-sha256", DIGEST, "--exported-at", EXPORTED, "--actor-email", EMAIL,
        ])
        self.assertFalse(parsed.apply)

    def test_source_has_no_sending_or_contact_creation_dependency(self):
        tree = ast.parse(inspect.getsource(source))
        imports = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any(term in module for module in imports for term in ("blast", "warmup", "resend", "community_optins", "luma_sync", "listmonk_sync")))
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("bulk_create", calls)
        self.assertNotIn("save", calls)
