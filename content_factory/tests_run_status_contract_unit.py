"""The generated run-status module and the call sites built on it; never create a database."""
import importlib.util
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]


def generator():
    spec = importlib.util.spec_from_file_location("generate_run_status_contract", ROOT / "scripts" / "generate_run_status_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GeneratedModuleTests(SimpleTestCase):
    def test_generated_module_is_current(self):
        script = generator()
        self.assertEqual(script.target(), ROOT / "content_factory" / "run_status_contract.py")
        self.assertEqual(script.target().read_text(encoding="utf-8"), script.render(script.load()))

    def test_lifecycle_partitions_every_run_status(self):
        from workflow_runs.models import ContentFactoryRunStatus
        from . import run_status_contract as contract
        values = set(ContentFactoryRunStatus.values)
        self.assertEqual(contract.ACTIVE | contract.AWAITING | contract.TERMINAL, values)
        self.assertEqual(contract.RUN_STATUSES | contract.PLATFORM_EXTENSIONS, values)
        for status in ContentFactoryRunStatus:
            with self.subTest(status=status):
                flags = [contract.is_active(status), contract.is_awaiting(status), contract.is_terminal(status)]
                self.assertEqual(flags.count(True), 1)
        self.assertEqual(contract.ACTIVE, {"queued", "running"})
        self.assertTrue(contract.is_terminal(ContentFactoryRunStatus.BLOCKED))
        self.assertFalse(contract.is_active(None))

    def test_check_fails_loudly_on_a_stale_module_or_an_inconsistent_contract(self):
        script = generator()
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "run_status_contract.py"
            stale.write_text("ACTIVE = frozenset()\n", encoding="utf-8")
            original_root, original_target = script.ROOT, script.target
            script.ROOT, script.target = Path(directory), (lambda: stale)
            try:
                self.assertEqual(script.main(["--check"]), 1)
            finally:
                script.ROOT, script.target = original_root, original_target
            broken = json.loads((ROOT / "contracts" / "run-statuses.json").read_text(encoding="utf-8"))
            broken["run_status_lifecycle"]["active"].append("blocked")
            path = Path(directory) / "run-statuses.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "'blocked' is in both 'active' and 'terminal'"):
                script.load(path)


class CallSiteTests(SimpleTestCase):
    """Each former ACTIVE_RUN_STATUSES copy keeps exactly the members it had."""

    def test_snapshot_merge_keeps_external_and_legacy_active_spellings(self):
        from .run_state import ACTIVE_RUN_STATUSES, ACTIVE_SNAPSHOT_STATUSES
        self.assertIs(ACTIVE_RUN_STATUSES, ACTIVE_SNAPSHOT_STATUSES)
        self.assertEqual(ACTIVE_SNAPSHOT_STATUSES, {"queued", "running", "processing", "pending", "starting", "in_progress"})

    def test_reconciliation_sweeps_only_contract_active_runs(self):
        from workflow_runs.models import ContentFactoryRunStatus
        from .reconciliation import ACTIVE_RUN_STATUSES
        self.assertEqual(set(ACTIVE_RUN_STATUSES), {ContentFactoryRunStatus.QUEUED, ContentFactoryRunStatus.RUNNING})

    def test_app_run_view_refreshes_active_awaiting_and_blocked_runs(self):
        from workflow_runs.models import ContentFactoryRunStatus as Status
        from integrations.api_views_content_factory_app import REMOTE_REFRESH_RUN_STATUSES
        self.assertEqual(REMOTE_REFRESH_RUN_STATUSES, {
            Status.QUEUED, Status.RUNNING, Status.BLOCKED, Status.AWAITING_CONFIRMATION,
            Status.AWAITING_DELIVERY_MODE, Status.AWAITING_APPROVAL, Status.APPROVAL_REQUIRED,
        })
        self.assertNotIn(Status.COMPLETED, REMOTE_REFRESH_RUN_STATUSES)
