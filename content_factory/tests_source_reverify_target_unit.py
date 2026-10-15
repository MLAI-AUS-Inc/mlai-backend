"""Source re-verification follows the publishing target selected after its rescan.

TalaThrive (14 Oct): the push after its setup merge pinned the configured
default, a React fallback id that no row of generation 3 ever carried. Every
hourly attempt then raised ``verified_target_required``, even though a rescan
could re-register the setup's real target under the config's default.
"""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
import uuid

from django.test import SimpleTestCase
from django.utils import timezone

PINNED = "react_article_system_stories_{slug}_tsx__tsx"
SELECTED = "stack_native_collection_data_articles_json_static"
SOURCE = "c" * 40


class SourceReverifyTargetTests(SimpleTestCase):
    def setUp(self):
        self.rows = {}
        self.website = SimpleNamespace(pk=uuid.uuid4(), generation=3, github_repo="synthetic/site", repository_id=42,
            app_root="", branch="main", organization=SimpleNamespace(domain="synthetic.test"),
            targets=SimpleNamespace(filter=self._targets))
        self.config = SimpleNamespace(default_publish_target_id=SELECTED)
        self.lookups = []

    def _targets(self, *, generation, target_key):
        self.lookups.append(target_key)
        return SimpleNamespace(first=lambda: self.rows.get((generation, target_key)))

    def _row(self, key, *, generation=3):
        self.rows[(generation, key)] = SimpleNamespace(target_key=key, generation=generation,
            contract={"contract_digest": "d" * 64})
        return self.rows[(generation, key)]

    def _process(self, *, payload, receipt):
        from integrations import http_client
        from . import vibe_marketing_views as views
        from . import website_connections as authority
        from . import website_reconciliation as reconciliation
        from . import website_verification as verification
        op = SimpleNamespace(pk=uuid.uuid4(), connection=self.website, generation=3, attempts=0, next_attempt_at=None,
            payload={"source_sha": SOURCE, "owned_merge_run_id": "", **payload}, receipt=dict(receipt), save=Mock())
        proof = {"source_sha": SOURCE, "provider_verified": True}
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as update, \
                patch.object(authority, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(self.website)), \
                patch.object(authority, "require_unlocked_remote_call"), \
                patch.object(verification, "discover_source_attestation", return_value=proof) as discover, \
                patch.object(verification, "record_ci_attestation", return_value=SimpleNamespace(pk=uuid.uuid4(), receipt=proof)) as ci, \
                patch.object(verification, "verify_live_deployment", return_value=SimpleNamespace(pk=uuid.uuid4())) as deployment, \
                patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=self.config), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": False}), \
                patch.object(http_client, "post", side_effect=AssertionError("The rescan was already dispatched")):
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            state = reconciliation._process_source_reverification(op.pk, timezone.now())
        written = update.return_value.update.call_args.kwargs
        return state, written, discover, ci, deployment

    def test_rescanned_source_verifies_the_selected_target_when_the_pinned_one_never_existed(self):
        selected = self._row(SELECTED)

        state, written, discover, ci, deployment = self._process(
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"})

        self.assertEqual(state, "completed")
        self.assertEqual(self.lookups[:2], [PINNED, SELECTED])
        discover.assert_called_once_with(self.website, selected, SOURCE)
        ci.assert_called_once()
        deployment.assert_called_once()
        self.assertEqual(written["state"], "completed")
        self.assertEqual(written["receipt"]["status"], "verified")
        self.assertEqual(written["receipt"]["target_id"], SELECTED)

    def test_an_existing_pinned_target_is_still_the_one_verified(self):
        pinned = self._row(PINNED)
        self._row(SELECTED)

        state, written, discover, _, _ = self._process(
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"})

        self.assertEqual(state, "completed")
        discover.assert_called_once_with(self.website, pinned, SOURCE)
        self.assertEqual(written["receipt"]["target_id"], PINNED)

    def test_without_any_selected_target_row_it_keeps_waiting(self):
        # TalaThrive today: the config default is the pinned fallback and no row exists.
        self.config.default_publish_target_id = PINNED

        state, written, discover, _, _ = self._process(
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"})

        self.assertEqual(state, "pending")
        self.assertEqual(written["receipt"]["code"], "verified_target_required")
        self.assertTrue(written["receipt"]["retryable"])
        self.assertEqual(self.lookups, [PINNED])
        discover.assert_not_called()

    def test_the_selected_target_is_not_used_before_the_rescan_is_dispatched(self):
        # A missing target needs the inventory scan first, even after an owned merge.
        self._row(SELECTED)

        state, written, discover, _, _ = self._process(
            payload={"target_id": PINNED, "scan_required": False}, receipt={})

        self.assertEqual(state, "pending")
        self.assertEqual(written["receipt"]["code"], "source_reverification_unavailable")
        self.assertEqual(self.lookups, [PINNED])
        discover.assert_not_called()

    def test_a_target_of_another_generation_is_never_selected(self):
        self._row(SELECTED, generation=2)

        state, written, discover, _, _ = self._process(
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"})

        self.assertEqual(state, "pending")
        self.assertEqual(written["receipt"]["code"], "verified_target_required")
        discover.assert_not_called()
