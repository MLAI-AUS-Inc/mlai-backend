"""Exercise the worker disclosure contract without a database or providers."""

import copy
from contextlib import ExitStack
from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from startup_updates.api_views import StartupUpdateDraftResultsView
from startup_updates.revisions import RevisionConflict
from startup_updates.serializers import DraftResultsSerializer


class DraftDisclosureContractTests(SimpleTestCase):
    def setUp(self):
        self.month = date(2026, 10, 1)
        self.snapshot = SimpleNamespace(pk=52, payload={"metrics": [], "events": []})
        self.memo = {"topline": "We shipped the pilot.", "_month_sequence": 1}
        now = datetime(2026, 10, 12, tzinfo=timezone.utc)
        self.draft = SimpleNamespace(
            pk=111, id=111, organization_id=16, month=self.month,
            current_revision_id=None, current_revision=None,
            creation_key=None, update_date=None, title="October update",
            structured_memo=copy.deepcopy(self.memo), rendered_markdown="We shipped the pilot.",
            audience_visibility=["just_me"], published_at=None, first_published_at=None,
            status="draft", groundedness_status="pending", groundedness_notes="",
            model_name="", evidence_event_ids=[], evidence_metric_ids=[],
            carry_forward_event_ids=[], created_at=now, updated_at=now,
            save=MagicMock(), refresh_from_db=MagicMock(),
        )
        self.run = SimpleNamespace(
            run_id="synthetic-disclosure-run", save=MagicMock(),
            run_request={"evidence_snapshots": {"2026-10-01": {"snapshot_id": 52, "expected_revision": None}}},
        )

    def item(self, **overrides):
        return {"month": "2026-10-01", "audience": "private",
            "snapshot_id": 52, "expected_revision": None,
            "structured_memo": copy.deepcopy(self.memo), **overrides}

    def submit(self, item):
        request = SimpleNamespace(data={"drafts": [item]})
        return StartupUpdateDraftResultsView.post.__wrapped__(
            StartupUpdateDraftResultsView(), request, self.run.run_id,
        )

    def seams(self, stack):
        organization = SimpleNamespace(pk=16)
        mocks = {
            "startup_updates.api_views._locked_pipeline_run_context": {"return_value": (self.run, organization, None, None, None)},
            "startup_updates.api_views._reject_if_run_cancelled": {"return_value": None},
            "startup_updates.api_views.require_month_source_contract": {},
            "startup_updates.api_views._update_run_step": {},
            "startup_updates.api_views.get_startup_update_run_cancel_backups": {"return_value": {}},
            "startup_updates.api_views._backup_draft_if_needed": {"return_value": False},
            "startup_updates.api_views.get_object_or_404": {"return_value": self.snapshot},
            "startup_updates.api_views._serialize_run": {"return_value": {}},
            "startup_updates.update_identity.run_update": {"return_value": self.draft},
            "startup_updates.cover_images.generated_cover_image": {"side_effect": lambda memo, request: memo},
            "startup_updates.api_views.Organization.objects": {},
            "startup_updates.api_views.MonthlyUpdateDraft.objects": {},
            "startup_updates.models.MonthlyUpdateRevision.objects": {},
            "startup_updates.revisions.approve_and_publish": {"side_effect": AssertionError("Worker results must not publish")},
        }
        result = {path: stack.enter_context(patch(path, **kwargs)) for path, kwargs in mocks.items()}
        result["startup_updates.api_views.MonthlyUpdateDraft.objects"].select_for_update.return_value.get.return_value = self.draft
        return result

    def save_revision(self, draft, memo, *, snapshot, audience, **kwargs):
        revision = SimpleNamespace(
            pk=64, number=1, content_hash="synthetic-revision-hash", snapshot_id=snapshot.pk,
            snapshot=snapshot, audience=audience, structured_memo=copy.deepcopy(memo),
        )
        draft.current_revision = revision
        draft.current_revision_id = revision.pk
        draft.structured_memo = revision.structured_memo
        return revision

    def test_generation_retry_and_review_preserve_each_disclosure_without_publication(self):
        for visibility, audience in [(["just_me"], "private"), (["community"], "community"), (["public"], "public")]:
            with self.subTest(audience=audience), ExitStack() as stack:
                self.setUp()
                self.run.run_request["audience_visibility"] = visibility
                seams = self.seams(stack)
                save = stack.enter_context(patch("startup_updates.revisions.save_revision", side_effect=self.save_revision))
                generated = self.submit(self.item())
                self.assertEqual(generated.status_code, 200)
                receipt = generated.data["drafts"][0]
                self.assertEqual(receipt["audience"], audience)
                self.assertEqual(receipt["revisionId"], 64)
                self.assertEqual(receipt["evidenceSnapshot"], self.snapshot.payload)
                repeated = self.submit(self.item())
                self.assertEqual(repeated.data["drafts"][0], receipt)
                self.assertEqual(save.call_count, 1)
                review = self.submit(self.item(
                    audience=audience, revision_id=receipt["revisionId"],
                    revision_hash=receipt["revisionHash"], groundedness_status="passed",
                ))
                self.assertEqual(review.status_code, 200)
                self.assertEqual(review.data["drafts"][0]["revisionHash"], receipt["revisionHash"])
                self.assertEqual(review.data["drafts"][0]["audience"], audience)
                self.assertEqual(self.draft.current_revision.structured_memo, receipt["structured_memo"])
                self.assertEqual(self.draft.groundedness_status, "passed")
                self.assertIsNone(self.draft.published_at)
                seams["startup_updates.revisions.approve_and_publish"].assert_not_called()

    def test_review_cannot_change_saved_revision_disclosure(self):
        for audience in ("private", "community", "public"):
            with self.subTest(audience=audience), ExitStack() as stack:
                self.setUp()
                self.save_revision(self.draft, self.memo, snapshot=self.snapshot, audience=audience)
                seams = self.seams(stack)
                wrong = "public" if audience != "public" else "community"
                with self.assertRaises(RevisionConflict):
                    self.submit(self.item(audience=wrong, revision_id=64, revision_hash="synthetic-revision-hash", groundedness_status="passed"))
                seams["startup_updates.models.MonthlyUpdateRevision.objects"].filter.assert_not_called()
                self.draft.save.assert_not_called()

    def test_unknown_review_audience_is_rejected(self):
        serializer = DraftResultsSerializer(data={"drafts": [self.item(audience="everyone")]})
        self.assertFalse(serializer.is_valid())
        self.assertIn("audience", serializer.errors["drafts"][0])
