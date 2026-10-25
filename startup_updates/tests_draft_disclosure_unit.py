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

    def test_generated_financial_sharing_uses_only_the_pinned_founder_choice(self):
        self.run.run_request["financial_history_selection"] = {"enabled": True, "shareAmounts": False}
        item = self.item(structured_memo={**self.memo,
            "_financial_history_selection": {"enabled": True, "shareAmounts": True},
            "financial_history": {"snapshot": {"revenue": 99999}},
        })
        with ExitStack() as stack:
            self.seams(stack)
            save = stack.enter_context(patch("startup_updates.revisions.save_revision", side_effect=self.save_revision))
            generated = self.submit(item)
        self.assertEqual(generated.status_code, 200)
        memo = save.call_args.args[1]
        self.assertEqual(memo["_financial_history_selection"], {"enabled": True, "shareAmounts": False, "refresh": False})
        self.assertNotIn("financial_history", memo)


class LegacyDraftAudienceSelectionTests(SimpleTestCase):
    """New legacy drafts inherit saved owner choices; existing drafts keep theirs."""
    def setUp(self):
        from django.contrib.auth import get_user_model
        from founder_tools.models import VibeRaisingCompany, VibeRaisingProfile
        from organizations.models import Organization
        from startup_updates.models import MonthlyUpdateDraft, UserStartupBinding
        from workflow_runs.models import ContentFactoryRun
        self.organization = Organization(pk=16, domain='example.test')
        self.user = get_user_model()(pk=22)
        self.profile = VibeRaisingProfile(pk=23, user=self.user)
        self.company = VibeRaisingCompany(pk=24, organization=self.organization,
            profile=self.profile, domain=self.organization.domain, default_audience_visibility=['just_me'])
        self.profile.active_company = self.company
        self.binding = UserStartupBinding(pk=25, user=self.user, organization=self.organization)
        self.run = ContentFactoryRun(pk=26, run_id='legacy-owned-generation', domain=self.organization.domain,
            organization=self.organization, run_request={'binding_id': 25, 'audience_visibility': ['community']})
        self.draft = MonthlyUpdateDraft(pk=27, organization=self.organization,
            month=date(2026, 10, 1), audience_visibility=['just_me'], structured_memo={'_month_sequence': 1})

    def seams(self, stack):
        binding = stack.enter_context(patch('startup_updates.services.UserStartupBinding.objects'))
        binding.select_related.return_value.filter.return_value.first.return_value = self.binding
        profile = stack.enter_context(patch('founder_tools.models.VibeRaisingProfile.objects'))
        profile.select_related.return_value.prefetch_related.return_value.filter.return_value.first.return_value = self.profile
        return binding

    def test_new_draft_uses_saved_run_choice_but_an_existing_private_draft_is_preserved(self):
        from startup_updates.services import upsert_monthly_update_draft
        for created, expected in ((True, ['community']), (False, ['just_me'])):
            with self.subTest(created=created), ExitStack() as stack:
                self.seams(stack)
                stack.enter_context(patch('startup_updates.update_identity.resolve_update', return_value=(self.draft, created)))
                save = stack.enter_context(patch('startup_updates.services.MonthlyUpdateDraft.objects.update_or_create',
                    return_value=(self.draft, False)))
                # Exercise the actual body with ORM seams; transaction execution
                # remains covered by the separately assigned database suite.
                upsert_monthly_update_draft.__wrapped__(organization=self.organization, month=self.draft.month,
                    run=self.run, structured_memo={'highlights': ['Saved choice']}, model_name='synthetic')
                self.assertEqual(save.call_args.kwargs['defaults']['audience_visibility'], expected)

    def test_saved_private_choice_is_not_expanded_by_later_company_defaults(self):
        from startup_updates.services import _default_audience_visibility_for_monthly_update
        self.run.run_request['audience_visibility'] = ['just_me']
        self.company.default_audience_visibility = ['public']
        with ExitStack() as stack:
            self.seams(stack)
            self.assertEqual(_default_audience_visibility_for_monthly_update(
                organization=self.organization, run=self.run), ['just_me'])

    def test_a_choice_without_an_organization_binding_stays_private(self):
        from startup_updates.services import _default_audience_visibility_for_monthly_update
        with ExitStack() as stack:
            binding = self.seams(stack)
            binding.select_related.return_value.filter.return_value.first.return_value = None
            self.assertEqual(_default_audience_visibility_for_monthly_update(
                organization=self.organization, run=self.run), ['just_me'])
            binding.select_related.return_value.filter.assert_called_once_with(
                id=25, organization=self.organization)
