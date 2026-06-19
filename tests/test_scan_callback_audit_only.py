"""P1: an audit-only inventory scan must not hard-block on `manual_blocked` readiness.

A repo scan run with scan_purpose=inventory never proposes a scaffold, so a
`manual_blocked` article-system readiness there only means "no articles route
exists yet" — it should complete, not surface as a BLOCKED "needs attention"
run carrying the misleading audit-only message. A genuine setup attempt, or an
explicit `scaffold_status == "manual_blocked"` (hint unmatched), still blocks.
"""
from django.test import TestCase

from content_factory.service_views import _sync_scan_callback_to_run
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStatus

REPO = "The-Product-Bus/tpbnewsite"
DOMAIN = "theproductbus.com"


def _scan_data(run_id, **overrides):
    data = {
        "run_id": run_id,
        "job_id": run_id,
        "workflow": "repo_scan",
        "domain": DOMAIN,
        "github_repo": REPO,
        "scan_purpose": "inventory",
        "scaffold_status": "not_needed",
        "article_system_readiness": {
            "status": "manual_blocked",
            "reason": "No safe articles route in this React Router SPA.",
        },
        "scaffold_reason": "Scan completed in audit-only mode; scaffold approval was not requested.",
    }
    data.update(overrides)
    return data


class ScanCallbackAuditOnlyTests(TestCase):
    def test_inventory_scan_with_manual_blocked_readiness_completes(self):
        run = _sync_scan_callback_to_run(data=_scan_data("inv-1"), approval_required=False)
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
        # The misleading audit-only line must NOT surface as a blocking error.
        self.assertEqual(run.error or "", "")

    def test_setup_scan_with_manual_blocked_readiness_still_blocks(self):
        run = _sync_scan_callback_to_run(
            data=_scan_data("setup-1", scan_purpose="setup", scaffold_status=""),
            approval_required=False,
        )
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)

    def test_explicit_manual_blocked_scaffold_status_always_blocks(self):
        # Explicit hint-unmatched block applies even on an inventory scan.
        run = _sync_scan_callback_to_run(
            data=_scan_data("hint-1", scaffold_status="manual_blocked"),
            approval_required=False,
        )
        self.assertEqual(run.status, ContentFactoryRunStatus.BLOCKED)

    def test_clean_inventory_scan_completes(self):
        run = _sync_scan_callback_to_run(
            data=_scan_data("inv-clean", article_system_readiness={"status": "ready"}),
            approval_required=False,
        )
        self.assertEqual(run.status, ContentFactoryRunStatus.COMPLETED)
