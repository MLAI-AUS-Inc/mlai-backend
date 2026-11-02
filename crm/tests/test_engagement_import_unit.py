import csv
import hashlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from crm.engagement_import import load_snapshot, merge_snapshot


class EngagementImportTests(SimpleTestCase):
    def source(self, **changes):
        return {
            "Email": "person@example.com", "Name": "A Person",
            "MLAI stars provisional": "5", "MLAI score provisional": "60.0",
            "MLAI score version": "test-v1", "MLAI scored as of": "2026-11-02",
            "MLAI score reasons": "Three event check-ins",
            "MLAI source coverage": "Provisional", "MLAI marketing consent": "unknown_not_audited",
            **changes,
        }

    def load(self, **changes):
        row = self.source(**changes)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.csv"
            with path.open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)
            return load_snapshot(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())[0][0]

    def contact(self, **changes):
        return SimpleNamespace(
            attributes={"manual_note": "Keep"}, display_name="Existing name",
            marketing_consent="granted", email_status="unsubscribed",
            marketing_consent_source="original", marketing_consent_at="2026-01-01",
            email_status_changed_at="2026-02-01", **changes,
        )

    def merge(self, contact, row):
        return merge_snapshot(contact, row, digest="a" * 64, file_name="snapshot.csv", evidence=[])

    def test_suppression_and_permission_fields_are_preserved_while_rating_is_imported(self):
        contact = self.contact()
        before = vars(contact).copy()
        self.assertTrue(self.merge(contact, self.load()))
        for key in before.keys() - {"attributes"}:
            self.assertEqual(getattr(contact, key), before[key])
        self.assertEqual(contact.attributes["manual_note"], "Keep")
        self.assertEqual(contact.attributes["engagement_rating"]["stars"], 5)
        self.assertNotIn("marketing_review_hold", contact.attributes)

    def test_unknown_permission_is_held_without_being_granted_or_withdrawn(self):
        contact = self.contact()
        contact.marketing_consent = "unknown"
        self.merge(contact, self.load())
        self.assertTrue(contact.attributes["marketing_review_hold"])
        self.assertEqual(contact.marketing_consent, "unknown")

    def test_decline_is_held_for_scope_review_without_inventing_an_unsubscribe(self):
        contact = self.contact()
        self.merge(contact, self.load(**{"MLAI marketing consent": "luma_event_updates_declined_scope_review"}))
        self.assertTrue(contact.attributes["marketing_review_hold"])
        self.assertEqual(contact.marketing_consent, "granted")
        self.assertEqual(contact.email_status, "unsubscribed")

    def test_reimport_is_idempotent_and_preserves_existing_hold(self):
        contact = self.contact()
        contact.attributes["marketing_review_hold"] = True
        row = self.load()
        self.assertTrue(self.merge(contact, row))
        self.assertFalse(self.merge(contact, row))
        self.assertTrue(contact.attributes["marketing_review_hold"])

    def test_older_snapshot_cannot_replace_newer_rating_or_evidence(self):
        contact = self.contact()
        row = self.load()
        self.merge(contact, row)
        contact.attributes["engagement_rating"]["as_of"] = "2026-11-03"
        old_import = contact.attributes["engagement_import"]
        self.merge(contact, self.load(**{"MLAI score reasons": "Older description"}))
        self.assertEqual(contact.attributes["engagement_rating"]["as_of"], "2026-11-03")
        self.assertEqual(contact.attributes["engagement_import"], old_import)

    def test_invalid_or_nonfinite_ratings_fail_before_database_access(self):
        for value in ("nan", "inf", "-1", "101"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(**{"MLAI score provisional": value})
        for value in ("0", "6", "3.5", "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(**{"MLAI stars provisional": value})

    def test_checksum_mismatch_and_duplicate_email_are_refused(self):
        row = self.source()
        text = io.StringIO()
        writer = csv.DictWriter(text, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
        writer.writerow(row)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.csv"
            path.write_text(text.getvalue())
            with self.assertRaisesRegex(ValueError, "checksum"):
                load_snapshot(path, expected_sha256="0" * 64)
            with self.assertRaisesRegex(ValueError, "duplicate"):
                load_snapshot(path, expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
