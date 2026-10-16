"""Source re-verification follows the publishing target selected after its rescan.

TalaThrive (14 Oct): the push after its setup merge pinned the configured
default, a React fallback id that no row of generation 3 ever carried. Every
hourly attempt then raised ``verified_target_required``, even though a rescan
could re-register the setup's real target under the config's default.

TalaThrive (16 Oct): its config default was that same fallback, so no target was
selected either. Check 229160e7's rescan had finished on 14 Oct, yet the check
stayed pending for 49 attempts. Once its rescan has finished without recording
a target, the check now settles as superseded and leaves the source to setup.
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

    def _process(self, *, payload, receipt, scan_state="completed"):
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
            # The rescan this check reserved under its idempotency key.
            update.return_value.first.return_value = SimpleNamespace(state=scan_state)
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

    def test_without_any_selected_target_row_it_waits_while_the_rescan_runs(self):
        # The config default is the pinned fallback and no row exists yet.
        self.config.default_publish_target_id = PINNED

        state, written, discover, _, _ = self._process(
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"},
            scan_state="running")

        self.assertEqual(state, "pending")
        self.assertEqual(written["receipt"]["code"], "verified_target_required")
        self.assertTrue(written["receipt"]["retryable"])
        self.assertEqual(self.lookups, [PINNED])
        discover.assert_not_called()

    def test_a_finished_rescan_without_any_target_settles_the_check_as_superseded(self):
        # TalaThrive 229160e7: the config default is the pinned fallback, no row
        # carries it and the check's rescan finished two days earlier.
        self.config.default_publish_target_id = PINNED
        for scan_state in ("completed", "failed", "blocked", "cancelled", "denied", "deleted"):
            with self.subTest(scan_state=scan_state):
                self.lookups.clear()

                state, written, discover, ci, deployment = self._process(
                    payload={"target_id": PINNED, "scan_required": False, "owned_merge_run_id": "setup-1"},
                    receipt={"scan_run_id": "scan-1", "status": "verification_pending",
                        "code": "verified_target_required", "retryable": True},
                    scan_state=scan_state)

                self.assertEqual(state, "cancelled")
                self.assertEqual(written["state"], "cancelled")
                receipt = written["receipt"]
                self.assertEqual(receipt["status"], "superseded")
                self.assertEqual(receipt["code"], "verified_target_required")
                self.assertFalse(receipt["retryable"])
                self.assertEqual(receipt["message"], "No publishing target exists at this source. Setting up articles verifies it.")
                self.assertEqual(receipt["scan_run_id"], "scan-1")
                self.assertEqual(self.lookups, [PINNED])
                discover.assert_not_called()
                ci.assert_not_called()
                deployment.assert_not_called()

    def test_a_finished_rescan_still_verifies_a_selected_target_that_exists(self):
        selected = self._row(SELECTED)

        state, written, discover, _, _ = self._process(
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"})

        self.assertEqual(state, "completed")
        discover.assert_called_once_with(self.website, selected, SOURCE)
        self.assertEqual(written["receipt"]["status"], "verified")

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
            payload={"target_id": PINNED, "scan_required": True}, receipt={"scan_run_id": "scan-1"},
            scan_state="running")

        self.assertEqual(state, "pending")
        self.assertEqual(written["receipt"]["code"], "verified_target_required")
        discover.assert_not_called()

    def test_a_rescan_dispatched_by_this_attempt_does_not_settle_the_check(self):
        # A short-circuited scan can report completed in its dispatch response,
        # before its targets are recorded. Settling waits for a later attempt.
        from integrations import http_client
        from . import vibe_marketing_views as views
        from . import website_connections as authority
        from . import website_operations as operations
        from . import website_reconciliation as reconciliation
        from . import website_verification as verification
        self.config.default_publish_target_id = PINNED
        self.website.authorized_by = SimpleNamespace(pk=7)
        op = SimpleNamespace(pk=uuid.uuid4(), connection=self.website, generation=3, attempts=0, next_attempt_at=None,
            payload={"source_sha": SOURCE, "owned_merge_run_id": "", "target_id": PINNED, "scan_required": True},
            receipt={}, save=Mock())
        response = Mock(status_code=202, json=lambda: {"run_id": "scan-1"})
        with patch.object(reconciliation.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "select_for_update") as claimed, \
                patch.object(reconciliation.WebsiteConnectionOperation.objects, "filter") as update, \
                patch.object(authority, "authority_guard", side_effect=lambda *args, **kwargs: nullcontext(self.website)), \
                patch.object(authority, "require_unlocked_remote_call"), \
                patch.object(verification, "discover_source_attestation") as discover, \
                patch.object(reconciliation.OrganizationContentConfig.objects, "get", return_value=self.config), \
                patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://cf.test"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views, "_require_roo_points_for_ai_agent", return_value=(None, 100)), \
                patch.object(views, "_mark_roo_points_gate_authorized"), \
                patch.object(views, "founder_actor_id_for_user", return_value="founder-7"), \
                patch.object(views, "_create_local_run", return_value=SimpleNamespace(run_id="scan-1")), \
                patch.object(operations, "reserve_workflow_operation", return_value=SimpleNamespace(pk=uuid.uuid4())), \
                patch.object(operations, "bind_operation_run"), \
                patch.object(http_client, "post", return_value=response) as post:
            claimed.return_value.select_related.return_value.filter.return_value.first.return_value = op
            update.return_value.first.return_value = SimpleNamespace(state="completed")
            state = reconciliation._process_source_reverification(op.pk, timezone.now())
        written = update.return_value.update.call_args.kwargs

        post.assert_called_once()
        self.assertEqual(state, "pending")
        self.assertEqual(written["receipt"]["scan_run_id"], "scan-1")
        self.assertEqual(written["receipt"]["code"], "verified_target_required")
        self.assertTrue(written["receipt"]["retryable"])
        discover.assert_not_called()
