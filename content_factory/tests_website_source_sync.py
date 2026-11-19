"""B13b: source sync chases GitHub's head; MLAI's service rescans without the founder's points."""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from . import website_reconciliation as reconciliation
from . import website_source_carry as carry
from . import website_source_sync as source_sync
from .tests_website_source_carry import CarriedSite, _compare
from .website_contract import WebsiteAuthorityError
from .website_models import WebsiteConnectionOperation, WebsiteScanSnapshot

STORM = [f"{index:x}" * 40 for index in range(10, 15)]
SYNC_ON = dict(ARTICLES_READY_ON_MERGE_ENABLED=True, ARTICLES_PROOF_CARRY_FORWARD_ENABLED=True,
    ARTICLES_SOURCE_SYNC_ENABLED=True, ARTICLES_SOURCE_SYNC_DAILY_SCAN_CAP=2)


class SyncedSite(CarriedSite):
    def push_head(self, sha, comparison):
        with patch.object(carry, "compare_sources", return_value=comparison):
            result = reconciliation.handle_website_github_event("push", {
                "repository": {"id": 777}, "ref": "refs/heads/main", "after": sha})
        self.connection.refresh_from_db()
        self.target.refresh_from_db()
        return result

    def checks(self):
        return WebsiteConnectionOperation.objects.filter(connection=self.connection, action="source-reverify")

    def process(self, op, now):
        with patch("content_factory.website_reconciliation._renews_by_live_url", return_value=False):
            return reconciliation._process_source_reverification(op.pk, now)


@override_settings(**SYNC_ON)
class PushStormTests(SyncedSite, TestCase):
    def test_a_push_storm_ends_on_one_verified_head_with_no_needs_attention(self):
        start = timezone.now()
        for sha in STORM:  # Five pushes in two minutes, each leaving the setup files alone.
            self.push_head(sha, _compare("src/app/page.tsx"))
        pending = list(self.checks().filter(state="pending"))
        self.assertEqual([op.payload["source_sha"] for op in pending], [STORM[-1]])
        superseded = self.checks().filter(state="cancelled", receipt__status="superseded")
        self.assertEqual(superseded.count(), 4)
        # Each push hands the chase to the next one; only the last check is left.
        self.assertEqual(sorted(op.receipt["superseded_by_source_sha"] for op in superseded), sorted(STORM[1:]))
        last = pending[0]
        # The quiet period holds the one check back so the storm can settle.
        self.assertGreater(last.next_attempt_at, start + timedelta(seconds=55))
        self.assertIsNone(self.process(last, start))
        state = self.process(last, start + timedelta(minutes=3))
        self.assertEqual(state, "completed")
        self.assertEqual(self.connection.verified_sha, STORM[-1])
        self.assertFalse(self.checks().filter(state="needs_attention").exists())
        self.assertTrue(self.evidence()["verified"])

    def test_a_check_that_finds_the_branch_moved_records_the_new_head(self):
        self.push_head(STORM[0], _compare("src/app/page.tsx"))
        op = self.checks().get(payload__source_sha=STORM[0])
        moved = WebsiteAuthorityError("website_source_changed", "The branch moved.")
        with patch("content_factory.website_connections.verify_repository_head", side_effect=moved), \
                patch.object(source_sync, "read_head", return_value=STORM[1]), \
                patch.object(carry, "compare_sources", return_value=_compare("README.md")):
            state = self.process(op, timezone.now() + timedelta(minutes=2))
        self.assertEqual(state, "cancelled")
        # The webhook for STORM[1] never arrived; the converge read queued its check.
        self.assertTrue(self.checks().filter(payload__source_sha=STORM[1], state="pending").exists())

    @override_settings(ARTICLES_SOURCE_SYNC_ENABLED=False)
    def test_with_the_switch_off_every_push_keeps_its_own_check(self):
        for sha in STORM[:3]:
            self.push_head(sha, _compare("src/app/page.tsx"))
        self.assertEqual(self.checks().filter(state="pending").count(), 3)


@override_settings(**SYNC_ON)
class MissedWebhookTests(SyncedSite, TestCase):
    def setUp(self):
        super().setUp()
        cache.delete(source_sync.SWEEP_KEY.format(connection_id=self.connection.pk))

    def test_the_hourly_sweep_heals_a_missed_webhook(self):
        with patch.object(source_sync, "read_head", return_value=STORM[0]), \
                patch.object(carry, "compare_sources", return_value=_compare("src/app/page.tsx")):
            outcome = source_sync.sweep(now=timezone.now())
        self.assertEqual((outcome["read"], outcome["recorded"]), (1, 1))
        self.assertTrue(WebsiteScanSnapshot.objects.filter(connection=self.connection, source_sha=STORM[0],
            detector_version="github_head").exists())
        self.assertTrue(self.checks().filter(payload__source_sha=STORM[0]).exists())
        # The same hour reads nothing again.
        with patch.object(source_sync, "read_head", side_effect=AssertionError("read twice")):
            self.assertEqual(source_sync.sweep(now=timezone.now())["read"], 0)

    def test_a_current_head_records_nothing(self):
        with patch.object(source_sync, "read_head", return_value=self.connection.verified_sha):
            outcome = source_sync.sweep(now=timezone.now())
        self.assertEqual(outcome["recorded"], 0)
        self.assertFalse(self.checks().exists())


class _Response:
    status_code = 202

    def raise_for_status(self):
        return None

    def json(self):
        return {"run_id": "scan-remote-1"}


@override_settings(**SYNC_ON)
class ServiceRescanTests(SyncedSite, TestCase):
    """A push that changed the setup needs a rescan; MLAI's service runs it for free."""

    def rescan(self, *, owner=None, now=None):
        self.connection.authorized_by = owner
        self.connection.save(update_fields=["authorized_by"])
        self.push_head(STORM[0], _compare("src/app/blog/page.tsx"))
        op = self.checks().get(payload__source_sha=STORM[0])
        self.assertTrue(op.payload["scan_required"])
        posted = {}

        def post(url, json=None, **kwargs):
            posted.update(url=url, payload=json)
            return _Response()

        from . import vibe_marketing_views as views
        from . import website_operations as operations
        from integrations import http_client
        scan_op = SimpleNamespace(pk="scan-op")
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://cf.test"}), \
                patch.object(views, "_content_factory_headers", return_value={}), \
                patch.object(views, "_require_roo_points_for_ai_agent", side_effect=AssertionError("founder points read")), \
                patch.object(views, "_create_local_run", return_value=SimpleNamespace(run_id="scan-remote-1")), \
                patch("content_factory.dispatch_admission.reserve_user_dispatch", side_effect=AssertionError("founder admission")), \
                patch("content_factory.dispatch_admission.reserve_scheduler_dispatch", return_value=None) as scheduler, \
                patch.object(operations, "reserve_workflow_operation", return_value=scan_op), \
                patch.object(operations, "bind_operation_run"), \
                patch.object(http_client, "post", side_effect=post):
            state = self.process(op, now or timezone.now() + timedelta(minutes=2))
        op.refresh_from_db()
        return state, op, posted, scheduler

    def test_a_founder_with_no_account_or_points_still_reverifies(self):
        state, op, posted, scheduler = self.rescan(owner=None)
        # The rescan is dispatched; the check waits for its target as before.
        self.assertEqual(state, "pending")
        self.assertEqual(op.receipt["scan_run_id"], "scan-remote-1")
        self.assertIs(op.receipt[source_sync.SERVICE_RESCAN_KEY], True)
        self.assertNotEqual(op.receipt.get("code"), "roo_points_required")
        scheduler.assert_called_once()
        self.assertEqual(scheduler.call_args.kwargs["service"], "source_sync")
        self.assertEqual(scheduler.call_args.kwargs["workflow"], "repo_scan")
        payload = posted["payload"]
        self.assertTrue(posted["url"].endswith("/api/runs/scan"))
        # Read and scan only: never setup, preview, components or a charge.
        self.assertEqual((payload["scaffold_if_missing"], payload["auto_setup_preview"], payload["generate_components"]),
            (False, False, False))
        self.assertEqual((payload["roo_points_cost"], payload["roo_points_required"], payload["roo_points_billing_status"]),
            (0, 0, "free"))
        self.assertEqual(payload["slack_user_id"], "")
        self.assertEqual(payload["website_connection_id"], str(self.connection.pk))

    def test_the_daily_cap_holds_further_rescans(self):
        for index in range(2):
            WebsiteConnectionOperation.objects.create(connection=self.connection, generation=self.connection.generation,
                idempotency_key=f"capped-{index}", action="source-reverify", state="completed",
                payload={"source_sha": "9" * 40}, receipt={source_sync.SERVICE_RESCAN_KEY: True})
        state, op, posted, scheduler = self.rescan(owner=None)
        self.assertEqual(state, "pending")
        self.assertEqual(op.receipt["sync_wait"], "daily_scan_cap")
        self.assertNotIn("scan_run_id", op.receipt)
        scheduler.assert_not_called()
        self.assertEqual(posted, {})

    @override_settings(ARTICLES_SOURCE_SYNC_ENABLED=False)
    def test_with_the_switch_off_the_old_owner_requirement_stays(self):
        self.connection.authorized_by = None
        self.connection.save(update_fields=["authorized_by"])
        self.push_head(STORM[0], _compare("src/app/blog/page.tsx"))
        op = self.checks().get(payload__source_sha=STORM[0])
        from . import vibe_marketing_views as views
        with patch.object(views, "_content_factory_remote_config", return_value={"enabled": True, "base_url": "https://cf.test"}):
            state = self.process(op, timezone.now())
        op.refresh_from_db()
        self.assertEqual(state, "pending")
        self.assertEqual(op.receipt["code"], "website_owner_review_required")
