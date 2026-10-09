"""Curation endpoint composition tests; never open a database connection."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from startup_updates.api_views import StartupUpdateCurationContextView
from startup_updates.tests_curation_context_unit import Revision


class CurationContextApiTests(SimpleTestCase):
    def test_publication_history_does_not_call_full_draft_serializer(self):
        organization = object()
        run = SimpleNamespace(
            run_request={
                "update_date": "2026-10-08",
                "update_id": 11,
                "draft_months": ["2026-10-01"],
                "startup_context": {},
                "external_context": {},
            }
        )
        draft = SimpleNamespace(
            pk=7,
            month=date(2026, 9, 1),
            published_at=None,
            title="Current edit",
            published_revision=Revision(),
        )
        with (
            patch(
                "startup_updates.api_views._locked_pipeline_run_context",
                return_value=(run, organization, None, None, None),
            ),
            patch(
                "startup_updates.api_views._reject_if_run_cancelled", return_value=None
            ),
            patch("startup_updates.api_views._update_run_step"),
            patch(
                "startup_updates.api_views.get_startup_update_run_target_month",
                return_value=date(2026, 10, 1),
            ),
            patch("startup_updates.api_views._serialize_run", return_value={}),
            patch("startup_updates.api_views.build_timeline_payload", return_value={}),
            patch(
                "startup_updates.api_views._serialize_draft",
                side_effect=AssertionError("Full evidence serializer must not run"),
            ),
            patch(
                "startup_updates.update_identity.previous_publications",
                return_value=[(draft, Revision.structured_memo, "2026-09-30")],
            ) as prior,
        ):
            response = StartupUpdateCurationContextView.get.__wrapped__(
                StartupUpdateCurationContextView(), object(), "run"
            )
        prior.assert_called_once_with(organization, 11, date(2026, 10, 8))
        self.assertEqual(
            response.data["prior_updates"][0]["revisionHash"], "published-hash"
        )
        self.assertNotIn("evidenceSnapshot", response.data["prior_updates"][0])
