"""B22: the 18 Nov 2026 connection states each raise exactly one staff alert; every authority code has a class."""
import ast
from datetime import timedelta
from pathlib import Path

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from workflow_runs.models import ContentFactoryRun

from . import founder_journey_alerts as alerts
from . import website_connection_invariants as invariants
from .founder_journey_testing import make_history_free_org
from .tests_live_url_verification import MERGED, _connect, _next_js_target
from .website_connections import contract_for
from .website_models import WebsiteConnectionOperation, WebsiteScanSnapshot

MOVED = "f" * 40


class Site:
    """One synthetic website, Ready on MLAI's build proof unless told otherwise."""

    def __init__(self, domain, *, ready=True):
        self.built = make_history_free_org(domain=domain)
        self.connection = _connect(self.built, verified_sha=MERGED if ready else "")
        if ready:
            self.connection.capabilities = {**self.connection.capabilities, "publishingReady": True}
            self.connection.save(update_fields=["capabilities"])
            _next_js_target(self.connection)
            self.built.config.refresh_from_db()
            self.built.config.publish_targets = [{"target_id": "articles", "verification": {"source_sha": MERGED}}]
            self.built.config.save(update_fields=["publish_targets"])

    def blocker(self, code):
        self.connection.blockers = [*self.connection.blockers, {"code": code, "message": "Synthetic."}]
        self.connection.save(update_fields=["blockers"])

    def operation(self, action, state, *, age=timedelta(0), receipt=None):
        op = WebsiteConnectionOperation.objects.create(connection=self.connection, generation=self.connection.generation,
            idempotency_key=f"{self.connection.pk}:{action}:{state}:{age}", action=action, state=state,
            payload={"attempt": 1}, receipt=receipt or {})
        WebsiteConnectionOperation.objects.filter(pk=op.pk).update(created_at=timezone.now() - age)
        return op

    def head(self, sha, *, age):
        snapshot = WebsiteScanSnapshot.objects.create(connection=self.connection, generation=self.connection.generation,
            run_id=f"github-head:{sha}", fingerprint=sha[:64], source_sha=sha, detector_version="github_head", evidence={})
        WebsiteScanSnapshot.objects.filter(pk=snapshot.pk).update(created_at=timezone.now() - age)

    def run(self, run_id, *, status, result, workflow="confirmed_topic", age=timedelta(hours=3)):
        run = ContentFactoryRun.objects.create(run_id=run_id, workflow=workflow, domain=self.built.organization.domain,
            organization=self.built.organization, github_repo=self.connection.github_repo,
            run_request=contract_for(self.connection), status=status, result=result)
        ContentFactoryRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - age)
        return run


def tala_thrive():
    site = Site("tala-thrive.example.test")
    site.blocker("scan_required")
    return site


def la_vie():
    site = Site("la-vie.example.test")
    site.run("la-vie-5396", status="approval_required", result={"authority_refusals": [
        {"at": timezone.now().isoformat(), "code": "website_connection_required"}]})
    return site


def mlai():
    site = Site("mlai.example.test")
    site.head(MOVED, age=timedelta(hours=5))
    site.operation("prepare", "needs_attention", receipt={"reason_code": "website_source_changed"})
    return site


def never_verified(domain):
    site = Site(domain, ready=False)
    site.blocker("repository_source_changed")
    return site


PROD_STATES = {
    "tala_thrive": (tala_thrive, "ready_with_scan_or_source_blocker"),
    "la_vie": (la_vie, "connection_required_failure_while_connected"),
    "mlai": (mlai, "prepare_stopped_on_platform_code"),
    "studio_narrm": (lambda: never_verified("studio-narrm.example.test"), "source_blocker_never_verified"),
    "pawshare": (lambda: never_verified("pawshare.example.test"), "source_blocker_never_verified"),
    "examon": (lambda: never_verified("examon.example.test"), "source_blocker_never_verified"),
}


@override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=True, ARTICLES_READY_ON_MERGE_ENABLED=True)
class InvariantSweepTests(TestCase):
    def setUp(self):
        cache.delete(invariants.THROTTLE_KEY)

    def receipts(self, site):
        return [receipt for _, receipts in alerts._receipt_index({site.built.organization.pk})
                for receipt in receipts.values() if receipt.get("alert_reason") == invariants.ALERT_REASON]

    def sweep(self):
        cache.delete(invariants.THROTTLE_KEY)
        return invariants.run_connection_invariant_sweep()

    def test_a_healthy_ready_site_raises_nothing(self):
        site = Site("healthy.example.test")
        self.assertEqual(invariants.find_violations(), [])
        self.sweep()
        self.assertEqual(self.receipts(site), [])

    def test_each_18_nov_prod_state_raises_exactly_one_alert(self):
        for name, (build, expected) in PROD_STATES.items():
            with self.subTest(name):
                site = build()
                outcome = self.sweep()
                self.assertEqual(outcome["status"], "completed")
                receipts = self.receipts(site)
                self.assertEqual(len(receipts), 1, receipts)
                self.assertEqual(receipts[0]["details"]["invariant"], expected)
                self.sweep()
                self.assertEqual(len(self.receipts(site)), 1)

    def test_mlai_lists_its_head_drift_inside_its_one_alert(self):
        site = mlai()
        self.sweep()
        details = self.receipts(site)[0]["details"]
        self.assertIn("head_drift_without_job", details["also"])

    def test_an_old_open_operation_on_a_ready_connection_raises_one_alert(self):
        site = Site("la-vie-ops.example.test")
        site.operation("workflow", "blocked", age=timedelta(days=4))
        self.sweep()
        receipts = self.receipts(site)
        self.assertEqual([item["details"]["invariant"] for item in receipts], ["old_open_operation_on_ready"])

    def test_a_system_cancelled_run_with_no_notice_raises_one_alert(self):
        site = Site("tala-runs.example.test")
        for index in range(6):  # Six cancelled confirmed_topic runs: one alert for the site.
            site.run(f"tala-{index}", status="cancelled", result={"cancellation_reason": "system_stop",
                "cancelled_by": "system", "cancellation_transition": "reset"})
        self.sweep()
        self.assertEqual([item["details"]["invariant"] for item in self.receipts(site)], ["system_stopped_run_without_notice"])

    def test_a_founder_cancel_or_an_announced_stop_raises_nothing(self):
        site = Site("owner-cancel.example.test")
        site.run("owner", status="cancelled", result={"cancellation_reason": "owner_cancelled", "cancelled_by": "owner"})
        site.run("announced", status="cancelled", result={"cancellation_reason": "system_stop", "cancelled_by": "system",
            "cancellation_transition": "reset", "system_stop_followup": {"decision": "notice"}})
        self.assertEqual(invariants.find_violations(), [])

    def test_a_drift_with_an_open_check_is_not_an_alert(self):
        site = Site("drift-open.example.test")
        site.head(MOVED, age=timedelta(hours=1))
        site.operation("source-reverify", "pending")
        self.assertEqual(invariants.find_violations(), [])
        WebsiteConnectionOperation.objects.filter(connection=site.connection).update(state="cancelled")
        self.assertEqual([item.invariant for item in invariants.find_violations()], ["head_drift_without_job"])

    @override_settings(ARTICLES_FOUNDER_STUCK_ALERTS_ENABLED=False)
    def test_the_kill_switch_stops_the_sweep(self):
        tala_thrive()
        self.assertEqual(self.sweep(), {"status": "disabled"})


class AuthorityCodeRegistryTests(SimpleTestCase):
    """CI: every code a WebsiteAuthorityError is raised with has a registry class."""

    # Raised today without a registry row; the registry is vendored from
    # Content Factory, so this one waits for its row there. Remove it from
    # this set when the row lands (the test fails until you do).
    # The two repo_health codes are raised only behind the default-off
    # ARTICLES_REPO_HEALTH_ENABLED switch; their rows go into Content
    # Factory's failure-codes.json before that switch is turned on.
    AWAITING_REGISTRY_ROW = frozenset({
        "company_billing_founder_unavailable",
        "repo_health_build_not_green",
        "repo_health_blocked",
    })

    def raised_codes(self):
        root = Path(__file__).resolve().parents[1]
        found = {}
        for path in root.rglob("*.py"):
            parts = path.relative_to(root).parts
            if ("migrations" in parts or "tests" in parts or path.name.startswith("tests") or parts[0] in {".venv", "node_modules"}):
                continue
            try:
                tree = ast.parse(path.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "WebsiteAuthorityError"
                        and node.args):
                    for code in self.literal_codes(node.args[0]):
                        found.setdefault(code, f"{path.relative_to(root)}:{node.lineno}")
        return found

    def literal_codes(self, node):
        """The literal codes an argument can evaluate to: a string, either side of ``a if c else b``, an ``or`` default."""
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) and node.value else []
        if isinstance(node, ast.IfExp):
            return self.literal_codes(node.body) + self.literal_codes(node.orelse)
        if isinstance(node, ast.BoolOp):
            return [code for value in node.values for code in self.literal_codes(value)]
        return []

    def test_every_raised_authority_code_has_a_registry_class(self):
        from .website_failure_contract import CLASSES, load_registry
        registry = load_registry()["by_code_v2"]
        found = self.raised_codes()
        self.assertGreater(len(found), 100)
        missing = {code: where for code, where in found.items() if code not in registry and code not in self.AWAITING_REGISTRY_ROW}
        self.assertEqual(missing, {})
        for code in set(found) - self.AWAITING_REGISTRY_ROW:
            self.assertIn(registry[code]["failure_class"], CLASSES, code)
        self.assertEqual({code for code in self.AWAITING_REGISTRY_ROW if code in registry}, set(),
            "A code waiting for its registry row now has one; drop it from AWAITING_REGISTRY_ROW.")
