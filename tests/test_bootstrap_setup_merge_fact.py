"""Database-free checks for merged setup recovery in the real bootstrap overlay."""

from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import django
from django.apps import apps
from django.test import SimpleTestCase

if not apps.ready:
    django.setup()

from content_factory import vibe_marketing_views as views
from content_factory.website_journey import project_journey


class BootstrapSetupMergeFactTests(SimpleTestCase):
    def overlay(self, state, *, ready=False, access=True):
        context = SimpleNamespace(
            organization=SimpleNamespace(id=1, domain="example.test"),
            company=SimpleNamespace(pk="company-1"),
            profile=SimpleNamespace(user=SimpleNamespace(pk=2)),
        )
        website = {
            "status": "connected", "repositoryId": "repository-1",
            "writePolicy": {"allowed": access},
        }
        capabilities = {
            "canGenerateArticle": ready, "canPublishArticle": ready,
            "repositoryAccessVerified": access,
            "repositoryWriteVerified": access,
            "reason": "Verify the current source.", "reasonCode": "source_changed",
        }
        journey = project_journey(
            company_id=context.company.pk, domain=context.organization.domain,
            website=website, capabilities=capabilities,
            discovery={"complete": True},
        )
        payload = {
            "articleSetupState": deepcopy(state), "article_setup_state": deepcopy(state),
            "checks": {"scaffold": {"passed": True, "published": True}},
        }
        stubs = {
            "_get_config": SimpleNamespace(), "website_summary": website,
            "google_baseline_connection_status": {}, "_google_baseline_connect_url": "",
            "_article_capabilities_for_context": capabilities, "_guided_steps": ([], None),
            "_recommended_next_action": {}, "_latest_runs_for_org": [],
            "_workflow_progress": {},
        }
        with ExitStack() as stack:
            for name, value in stubs.items():
                stack.enter_context(patch.object(views, name, Mock(return_value=value)))
            stack.enter_context(patch("content_factory.website_journey.journey_for_context", return_value=journey))
            return views._overlay_live_bootstrap_fields(payload, context=context, request=None)

    def test_merged_setup_survives_stale_proof_without_granting_article_authority(self):
        for access in (True, False):
            with self.subTest(access=access):
                result = self.overlay({"setupMerged": True, "setupRunId": "merged-setup"}, access=access)
                for alias in ("articleSetupState", "article_setup_state"):
                    self.assertIs(result[alias]["setupMerged"], True)
                    self.assertEqual(result[alias]["setupRunId"], "merged-setup")
                    for flag in ("generationReady", "generation_ready", "scaffoldConnected", "published"):
                        self.assertIs(result[alias][flag], False)
                self.assertIs(result["articleCapabilities"]["canGenerateArticle"], False)
                self.assertIs(result["articleCapabilities"]["canPublishArticle"], False)
                self.assertIs(result["websiteJourney"]["capabilities"]["canGenerateArticle"], False)
                self.assertIs(result["websiteJourney"]["capabilities"]["canPublishArticle"], False)
                self.assertIs(result["checks"]["scaffold"]["passed"], False)

    def test_readiness_cannot_invent_a_merge_fact(self):
        for state in ({}, {"setupMerged": False}, {"setupMerged": "true"}):
            with self.subTest(state=state):
                result = self.overlay(state, ready=True)
                for alias in ("articleSetupState", "article_setup_state"):
                    self.assertIs(result[alias]["setupMerged"], False)
                    self.assertIs(result[alias]["generationReady"], True)

    def test_verified_merged_setup_retains_both_facts(self):
        result = self.overlay({"setupMerged": True}, ready=True)
        for alias in ("articleSetupState", "article_setup_state"):
            self.assertIs(result[alias]["setupMerged"], True)
            self.assertIs(result[alias]["generationReady"], True)
