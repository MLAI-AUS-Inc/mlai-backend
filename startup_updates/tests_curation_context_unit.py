"""Database-free checks: python -m unittest startup_updates.tests_curation_context_unit."""

from datetime import date
from types import SimpleNamespace
from unittest import TestCase

from startup_updates.curation_context import prior_update_context


class Revision:
    pk = 12
    content_hash = "published-hash"
    structured_memo = {"title": "Published title", "update_date": "2026-09-30"}
    rendered_markdown = "The September launch shipped."

    @property
    def snapshot(self):
        raise AssertionError("Curation must not load archived evidence")


class CurationContextTests(TestCase):
    def test_uses_published_revision_without_snapshot_or_current_edits(self):
        draft = SimpleNamespace(
            pk=7,
            month=date(2026, 9, 1),
            published_at=None,
            title="Unsaved edit",
            structured_memo={"raw": "x" * 4_000_000},
            rendered_markdown="Changed text",
            published_revision=Revision(),
            current_revision=None,
        )
        result = prior_update_context(draft, published=True)
        self.assertEqual(result["rendered_markdown"], Revision.rendered_markdown)
        self.assertEqual(result["title"], "Published title")
        self.assertEqual(result["revisionHash"], "published-hash")
        self.assertNotIn("evidenceSnapshot", result)
        self.assertNotIn("structured_memo", result)
        self.assertLess(len(str(result)), 1000)

    def test_legacy_context_keeps_text_without_inventing_revision(self):
        draft = SimpleNamespace(
            pk=3,
            month=date(2026, 8, 1),
            published_at=None,
            title="August",
            structured_memo={},
            rendered_markdown="A milestone.",
            current_revision=None,
        )
        result = prior_update_context(draft)
        self.assertEqual(result["rendered_markdown"], "A milestone.")
        self.assertIsNone(result["revisionId"])

    def test_nested_content_cannot_hide_in_continuity_fields(self):
        draft = SimpleNamespace(
            pk=3,
            month=date(2026, 8, 1),
            published_at=None,
            title="August",
            structured_memo={
                "title": {"raw": "x" * 4_000_000},
                "update_date": {"raw": "hidden"},
                "narrative_period": {"start": "2026-08-01", "end": {"raw": "hidden"}},
            },
            rendered_markdown="A milestone.",
            current_revision=None,
        )
        result = prior_update_context(draft)
        self.assertEqual(result["title"], "August")
        self.assertIsNone(result["updateDate"])
        self.assertEqual(result["narrativePeriod"], {"start": "2026-08-01"})
        self.assertLess(len(str(result)), 1000)
