"""Robustness Phase 3: fold ContentFactoryJob into ContentFactoryRun (R7 5.1-5.3).

Dual write mirrors every Job write onto the run's backend columns, the
backfill fills older runs, and Job reads can be shadowed or served from the
run. Every switch defaults off, and the flag-off tests pin today's behaviour.
"""
import ast
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from content_factory.dispatch_binding import bind_dispatch_token_run
from content_factory.job_mirror import RUN_JOB_COLUMN_DEFAULTS, RUN_JOB_COLUMNS, mirror_jobs_to_runs
from content_factory.job_view import RunJobView, job_field_mismatches, job_view
from content_factory.models import ContentFactoryJob, WrittenArticle
from content_factory.run_job_backfill import (
    BACKFILL_RESULT_KEY, backfill_run_job_fields, compare_run_job_fields, infer_workflow, undo_backfill,
)
from organizations.models import Organization
from workflow_runs.models import CONTENT_FACTORY_DELIVERY_PHASE_CHOICES, ContentFactoryRun, ContentFactoryRunStatus

User = get_user_model()
ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "example.com"
DUAL_WRITE = override_settings(ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED=True)
SHADOW = override_settings(ARTICLES_RUN_JOB_SHADOW_READ_ENABLED=True)
READ = override_settings(ARTICLES_RUN_JOB_READ_ENABLED=True)


def _run(run_id, **fields):
    defaults = {"workflow": "direct_generate", "domain": DOMAIN, "slack_user_id": "U_FOUNDER",
                "status": ContentFactoryRunStatus.RUNNING, "run_request": {"client_request_id": run_id}}
    defaults.update(fields)
    return ContentFactoryRun.objects.create(run_id=run_id, **defaults)


def _job(job_id, **fields):
    defaults = {"domain": DOMAIN, "slack_user_id": "U_FOUNDER", "status": "generating",
                "request_meta": {"topic": "Synthetic topic", "requested_by_slack_user_id": "U_FOUNDER"},
                "client_request_id": "key-" + job_id, "billing_source_job_id": job_id, "billing_amount": 6,
                "billing_status": "charged", "selected_keyword": "synthetic keyword",
                "selection_reason": "Synthetic reason", "selection_data": {"options": [1, 2]},
                "slack_channel_id": "C_SYNTH", "slack_thread_ts": "100.1", "slack_root_message_ts": "100.0",
                "progress_message_ts": "100.2", "posted_progress_ids": ["p1"], "last_progress_milestone_index": 2,
                "last_progress_milestone_key": "drafting", "last_progress_updated_at": timezone.now(),
                "article_url": "https://example.com/blog/synthetic", "pr_url": "https://example.com/acme/site/pull/1",
                "error_message": ""}
    defaults.update(fields)
    return ContentFactoryJob.objects.create(job_id=job_id, **defaults)


def _run_columns(run):
    run.refresh_from_db()
    return {column: getattr(run, column) for column in RUN_JOB_COLUMNS}


def _blank():
    return {column: (type(value)() if isinstance(value, (dict, list)) else value)
            for column, value in RUN_JOB_COLUMN_DEFAULTS.items()}


class DeliveryPhaseChoicesTest(SimpleTestCase):
    def test_delivery_phase_uses_the_job_vocabulary_unchanged(self):
        self.assertEqual(CONTENT_FACTORY_DELIVERY_PHASE_CHOICES, ContentFactoryJob.STATUS_CHOICES)


class FlagOffTest(TestCase):
    """With every switch off, Job writes and reads behave exactly as before."""

    def test_settings_default_off(self):
        from django.conf import settings
        for name in ("ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED", "ARTICLES_RUN_JOB_SHADOW_READ_ENABLED",
                     "ARTICLES_RUN_JOB_READ_ENABLED"):
            self.assertIs(getattr(settings, name), False, name)

    def test_job_writes_leave_run_columns_blank(self):
        run = _run("run-off-1")
        job = _job("run-off-1")
        job.status = "completed"
        job.save(update_fields=["status", "updated_at"])
        ContentFactoryJob.objects.update_or_create(job_id="run-off-1", defaults={"status": "error"})
        self.assertEqual(_run_columns(run), _blank())
        self.assertIsNone(job_view("run-off-1"))

    def test_signal_skipping_mirror_costs_no_query(self):
        _job("run-off-2")
        with self.assertNumQueries(0):
            self.assertEqual(mirror_jobs_to_runs(ContentFactoryJob.objects.filter(job_id="run-off-2")), 0)

    def test_token_rebind_is_unchanged(self):
        token = "vibe-dispatch:article_generation:off"
        run = _run(token, status=ContentFactoryRunStatus.BLOCKED)
        _job(token, client_request_id=token)
        bound = bind_dispatch_token_run(client_request_id=token, remote_run_id="cf-real-off")
        self.assertEqual(bound.pk, run.pk)
        self.assertTrue(ContentFactoryJob.objects.filter(job_id="cf-real-off").exists())
        self.assertEqual(_run_columns(bound), _blank())

    def test_thread_and_billing_reads_never_touch_runs(self):
        from content_factory.content_views import _resolve_confirm_job_for_thread
        from content_factory.billing_lineage import _same_company_charge
        from content_factory.first_article_entitlement import is_established_publisher
        _job("run-off-3", status="blocked")
        organization = Organization.objects.create(domain=DOMAIN, name="Synthetic")
        with patch("content_factory.job_view.job_runs", side_effect=AssertionError("run read with flags off")):
            payload = _resolve_confirm_job_for_thread(slack_user_id="U_FOUNDER", slack_channel_id="C_SYNTH",
                                                      slack_thread_ts="100.1")
            self.assertEqual(payload["job_id"], "run-off-3")
            self.assertFalse(_same_company_charge(type("L", (), {"pk": 999, "user_id": 1})(),
                                                  organization_id=organization.pk, domain=DOMAIN, key="missing"))
            self.assertTrue(is_established_publisher(organization))


@DUAL_WRITE
class DualWriteMirrorTest(TestCase):
    def assertParity(self, job_id):
        job = ContentFactoryJob.objects.get(job_id=job_id)
        view = job_view(job_id)
        self.assertIsNotNone(view)
        self.assertEqual(job_field_mismatches(job, view), [])
        return job, view

    def test_create_copies_every_field_without_touching_cf_owned_columns(self):
        run = _run("run-dw-1", error="cf error", result={"cf": True})
        before = ContentFactoryRun.objects.filter(pk=run.pk).values("status", "updated_at", "run_request", "error",
                                                                      "result", "slack_user_id", "domain").get()
        _job("run-dw-1")
        self.assertParity("run-dw-1")
        after = ContentFactoryRun.objects.filter(pk=run.pk).values("status", "updated_at", "run_request", "error",
                                                                     "result", "slack_user_id", "domain").get()
        self.assertEqual(after, before)

    def test_partial_save_update_or_create_and_full_save_keep_parity(self):
        _run("run-dw-2")
        job = _job("run-dw-2")
        ContentFactoryRun.objects.filter(run_id="run-dw-2").update(job_meta={"stale": True})
        job.status = "awaiting_approval"
        job.save(update_fields=["status", "updated_at"])
        run = ContentFactoryRun.objects.get(run_id="run-dw-2")
        self.assertEqual(run.delivery_phase, "awaiting_approval")
        self.assertEqual(run.job_meta, {"stale": True})  # a partial save copies only what it wrote
        ContentFactoryJob.objects.update_or_create(job_id="run-dw-2", defaults={"status": "completed",
                                                                                 "request_meta": {"topic": "B"}})
        job.refresh_from_db()
        job.error_message = "Synthetic failure"
        job.save()
        self.assertParity("run-dw-2")

    def test_partial_save_on_a_never_mirrored_run_copies_the_whole_job(self):
        with override_settings(ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED=False):
            job = _job("run-dw-3")
        _run("run-dw-3")
        job.status = "blocked"
        job.save(update_fields=["status", "updated_at"])
        self.assertParity("run-dw-3")

    def test_run_created_after_its_job_is_filled_from_it(self):
        _job("run-dw-4")
        _run("run-dw-4")
        self.assertParity("run-dw-4")

    def test_missing_run_is_a_no_op(self):
        job = _job("run-dw-missing")
        job.status = "completed"
        job.save(update_fields=["status"])
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="run-dw-missing").exists())

    def test_service_write_paths_keep_parity(self):
        from integrations.services.article_generation import _store_job_tracking_record, attach_progress_message
        _run("run-dw-5")
        _store_job_tracking_record("run-dw-5", domain=DOMAIN, slack_user_id="U_FOUNDER",
                                   request_meta={"topic": "A"}, slack_channel_id="C_SYNTH", slack_thread_ts="5.1",
                                   client_request_id="key-5", billing_source_job_id="run-dw-5", billing_amount=6,
                                   billing_status="charged")
        self.assertParity("run-dw-5")
        _store_job_tracking_record("run-dw-5", domain=DOMAIN, slack_user_id="U_FOUNDER",
                                   request_meta={"publish_stage": "ready"}, billing_status="reused")
        attach_progress_message("run-dw-5", progress_message_ts="5.2", slack_channel_id="C_SYNTH",
                                slack_thread_ts="5.1")
        self.assertParity("run-dw-5")

    def test_research_ledger_stamp_becomes_the_run_carrier(self):
        from roo.services import PointsService
        user = User.objects.create_user(email="research@example.com", password="password")
        PointsService.credit_purchased_topup(user=user, delta=10, description="Synthetic funding",
                                             idempotency_key="synthetic-funding-research")
        ledger, _ = PointsService.spend(user=user, delta=4, source="CONTENT_FACTORY", description="Synthetic research",
                                        created_by_slack_id="U_FOUNDER",
                                        idempotency_key="content_factory:topic_generation:charge:key-research",
                                        reference_type="CONTENT_FACTORY", reference_id="key-research")
        _run("run-dw-research", workflow="auto_discovery")
        _job("run-dw-research", billing_ledger=None,
             request_meta={"roo_points_action": "content_island_topic_generation", "roo_points_ledger_id": str(ledger.pk)})
        run = ContentFactoryRun.objects.get(run_id="run-dw-research")
        self.assertEqual(run.billing_ledger_id, ledger.pk)
        self.assertParity("run-dw-research")

    def test_mirror_failure_never_fails_the_job_write(self):
        run = _run("run-dw-6")
        with patch("content_factory.job_mirror.run_values_for_job", side_effect=RuntimeError("synthetic")), \
                self.assertLogs("content_factory.job_mirror", level="WARNING"):
            _job("run-dw-6")
        self.assertTrue(ContentFactoryJob.objects.filter(job_id="run-dw-6").exists())
        self.assertEqual(_run_columns(run), _blank())

    def test_bulk_refund_mirrors_the_source_and_every_child(self):
        from integrations.services.article_generation import _ledger_reference_id, _refund_content_factory_request
        from roo.services import PointsService
        user = User.objects.create_user(email="refund@example.com", password="password")
        PointsService.credit_purchased_topup(user=user, delta=20, description="Synthetic funding",
                                             idempotency_key="synthetic-funding-refund")
        key = "key-refund-family"
        spend, _ = PointsService.spend(user=user, delta=6, source="CONTENT_FACTORY", description="Synthetic article",
                                       created_by_slack_id="U_FOUNDER", idempotency_key=f"content_factory:charge:{key}",
                                       reference_type="CONTENT_FACTORY", reference_id=_ledger_reference_id(key))
        for job_id in ("run-refund-source", "run-refund-child"):
            _run(job_id, run_request={"client_request_id": key})
            _job(job_id, client_request_id=key, billing_ledger=spend, billing_status="charged",
                 request_meta={"source_run_id": "run-refund-source"} if job_id.endswith("child") else {})
        refund = _refund_content_factory_request(user=user, slack_user_id="U_FOUNDER",
                                                 article_request={"client_request_id": key},
                                                 resolved_domain=DOMAIN, reason="Synthetic start failure")
        self.assertIsNotNone(refund)
        for job_id in ("run-refund-source", "run-refund-child"):
            run = ContentFactoryRun.objects.get(run_id=job_id)
            self.assertEqual((run.billing_status, run.billing_amount, run.billing_ledger_id), ("refunded", 6, refund.pk))
            self.assertParity(job_id)

    def test_token_rebind_moves_the_carrier_to_the_real_run(self):
        token = "vibe-dispatch:article_generation:dw"
        _run(token, status=ContentFactoryRunStatus.BLOCKED)
        _job(token, client_request_id=token)
        self.assertEqual(ContentFactoryRun.objects.get(run_id=token).delivery_phase, "generating")
        bound = bind_dispatch_token_run(client_request_id=token, remote_run_id="cf-real-dw")
        self.assertEqual(bound.run_id, "cf-real-dw")
        _, view = self.assertParity("cf-real-dw")
        self.assertEqual(view.job_id, "cf-real-dw")
        self.assertEqual(ContentFactoryRun.objects.get(run_id="cf-real-dw").legacy_job_id, "")
        self.assertIsNone(job_view(token))  # as ContentFactoryJob.objects.filter(job_id=token) is now empty

    def test_token_rebind_merge_fills_the_existing_real_run(self):
        token = "vibe-dispatch:article_generation:merge"
        _run(token, status=ContentFactoryRunStatus.BLOCKED)
        _run("cf-real-merge", run_request={})
        _job(token, client_request_id=token)
        merged = bind_dispatch_token_run(client_request_id=token, remote_run_id="cf-real-merge")
        self.assertEqual(merged.run_id, "cf-real-merge")
        self.assertParity("cf-real-merge")

    def test_orphan_token_job_never_overwrites_the_real_carrier(self):
        token = "vibe-dispatch:article_generation:orphan"
        _run(token, status=ContentFactoryRunStatus.BLOCKED)
        _job(token, client_request_id=token, billing_status="charged", billing_amount=12)
        _job("cf-real-orphan", client_request_id=token, billing_status="reused", billing_amount=0)
        bind_dispatch_token_run(client_request_id=token, remote_run_id="cf-real-orphan")
        self.assertTrue(ContentFactoryJob.objects.filter(job_id=token).exists())  # orphaned, as today
        self.assertParity("cf-real-orphan")
        orphan = ContentFactoryJob.objects.get(job_id=token)
        orphan.billing_status = "refunded"
        orphan.save(update_fields=["billing_status"])
        self.assertEqual(ContentFactoryRun.objects.get(run_id="cf-real-orphan").billing_status, "reused")


class JobUpdateCallSiteGuardTest(SimpleTestCase):
    """Queryset writes skip post_save, so each must mirror explicitly."""

    MIRRORED_SITES = {
        ("integrations/services/article_generation.py", "_refund_content_factory_request"),
        ("content_factory/dispatch_binding.py", "_rebind_billing_job"),
    }
    SIGNAL_SKIPPING = {"update", "bulk_update", "bulk_create", "_update"}
    SKIP_PARTS = {".git", "node_modules", "venv", ".venv", "site-packages", "__pycache__", "migrations", "tests"}

    @classmethod
    def _chain_root(cls, node):
        names = []
        while True:
            if isinstance(node, ast.Call):
                node = node.func
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Subscript):
                node = node.value
            else:
                return node, names

    @classmethod
    def job_write_sites(cls, source, path="<source>"):
        """(path, function, line) for every signal-skipping write on a Job queryset in ``source``."""
        tree = ast.parse(source)
        aliases = {"ContentFactoryJob"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                aliases.update(alias.asname for alias in node.names if alias.name == "ContentFactoryJob" and alias.asname)
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "attr", "") == "get_model"
                    and any(getattr(arg, "value", None) == "ContentFactoryJob" for arg in node.value.args)):
                aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        sites = []

        def visit(scope, name):
            querysets = set()
            for node in ast.walk(scope):
                if isinstance(node, ast.Assign):
                    root, _ = cls._chain_root(node.value)
                    if isinstance(root, ast.Name) and root.id in aliases and isinstance(node.value, ast.Call):
                        querysets.update(t.id for t in node.targets if isinstance(t, ast.Name))
            for node in ast.walk(scope):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in cls.SIGNAL_SKIPPING):
                    continue
                root, names = cls._chain_root(node.func.value)
                job_chain = isinstance(root, ast.Name) and (
                    (root.id in aliases and "objects" in names) or root.id in querysets)
                if job_chain or "content_factory_jobs" in names:
                    sites.append((path, name, node.lineno))

        functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for function in functions:
            visit(function, function.name)
        return sites

    def _source_files(self):
        for path in sorted(ROOT.rglob("*.py")):
            relative = path.relative_to(ROOT)
            if self.SKIP_PARTS.intersection(relative.parts) or path.name.startswith("test"):
                continue
            yield relative.as_posix(), path

    def test_every_job_queryset_write_mirrors(self):
        found = {}
        for relative, path in self._source_files():
            for site_path, function, line in self.job_write_sites(path.read_text(), relative):
                found.setdefault((site_path, function), []).append(line)
        self.assertEqual(set(found), self.MIRRORED_SITES,
                         "A ContentFactoryJob queryset write skips post_save: call "
                         "content_factory.job_mirror.mirror_jobs_to_runs after it and list it here.")
        for relative, function in self.MIRRORED_SITES:
            tree = ast.parse((ROOT / relative).read_text())
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == function)
            calls = {getattr(n.func, "id", getattr(n.func, "attr", "")) for n in ast.walk(node) if isinstance(n, ast.Call)}
            self.assertTrue(calls & {"mirror_jobs_to_runs", "mirror_job_to_run"}, (relative, function))

    def test_guard_catches_new_write_shapes(self):
        source = '''
from content_factory.models import ContentFactoryJob as Job

def bulk_flip(key):
    Job.objects.filter(client_request_id=key).update(status="error")

def by_variable(key):
    jobs = ContentFactoryJob.objects.filter(client_request_id=key)
    jobs.update(status="error")

def by_ledger(ledger):
    ledger.content_factory_jobs.update(billing_status="refunded")

def harmless(job):
    job.request_meta.update({"a": 1})
    ContentFactoryJob.objects.update_or_create(job_id="x", defaults={})
'''
        functions = {function for _, function, _ in self.job_write_sites(source)}
        self.assertEqual(functions, {"bulk_flip", "by_variable", "by_ledger"})


class BackfillTest(TestCase):
    def setUp(self):
        with override_settings(ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED=False):
            self.organization = Organization.objects.create(domain=DOMAIN, name="Synthetic Startup")
            self.matched_run = _run("run-bf-matched")
            self.matched_job = _job("run-bf-matched")
            self.orphan_job = _job("legacy-slack-job", status="completed",
                                   request_meta={"type": "scan"}, client_request_id=None)
            ContentFactoryJob.objects.filter(pk=self.orphan_job.pk).update(
                created_at=timezone.now() - timedelta(days=200))
            self.orphan_job.refresh_from_db()
            self.article = WrittenArticle.objects.create(organization=self.organization, title="Synthetic",
                                                         slug="synthetic", category="guide",
                                                         primary_keyword="synthetic", job=self.matched_job)

    def _call(self, *args):
        out = StringIO()
        call_command("backfill_run_job_fields", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_is_the_default_and_reports_counts_only(self):
        output = self._call()
        self.assertIn("Mode: dry run", output)
        self.assertIn("Would create runs: 1", output)
        self.assertIn("by workflow: repo_scan=1", output)
        self.assertEqual(_run_columns(self.matched_run), _blank())
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="legacy-slack-job").exists())
        for private in (DOMAIN, "Synthetic Startup", "@", "U_FOUNDER", "run-bf-matched"):
            self.assertNotIn(private, output)

    def test_apply_fills_creates_tagged_runs_links_articles_and_is_idempotent(self):
        at = timezone.now()
        first = backfill_run_job_fields(apply=True, now=at)
        self.assertEqual(first.counts["runs_created"], 1)
        self.assertEqual(first.counts["runs_filled"], 1)
        self.assertEqual(job_field_mismatches(self.matched_job, job_view("run-bf-matched")), [])
        created = ContentFactoryRun.objects.get(run_id="legacy-slack-job")
        self.assertEqual(created.workflow, "repo_scan")
        self.assertEqual(created.status, "completed")
        self.assertEqual(created.run_request, {})
        self.assertEqual(created.result[BACKFILL_RESULT_KEY]["job_pk"], self.orphan_job.pk)
        self.assertEqual(created.created_at, self.orphan_job.created_at)
        self.assertEqual(created.updated_at, at)
        self.assertEqual(created.organization_id, self.organization.pk)
        self.article.refresh_from_db()
        self.assertEqual(self.article.run_id, self.matched_run.pk)
        state = list(ContentFactoryRun.objects.order_by("pk").values())
        second = backfill_run_job_fields(apply=True)
        self.assertEqual((second.counts["runs_created"], second.counts["runs_filled"],
                          second.counts["articles_linked"]), (0, 0, 0))
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), state)
        self.assertEqual(ContentFactoryJob.objects.count(), 2)  # never deletes a Job

    def test_never_overwrites_and_reports_conflicts_by_field(self):
        ContentFactoryRun.objects.filter(pk=self.matched_run.pk).update(billing_status="refunded",
                                                                         selected_keyword="other keyword")
        report = backfill_run_job_fields(apply=True, create=False)
        self.matched_run.refresh_from_db()
        self.assertEqual((self.matched_run.billing_status, self.matched_run.selected_keyword),
                         ("refunded", "other keyword"))
        self.assertEqual(report.conflicts["billing_status"], 1)
        self.assertEqual(report.conflicts["selected_keyword"], 1)
        self.assertEqual(self.matched_run.delivery_phase, "generating")  # empty columns are still filled
        self.assertFalse(ContentFactoryRun.objects.filter(run_id="legacy-slack-job").exists())

    def test_creates_only_for_jobs_without_a_run_and_honours_the_cutoff(self):
        report = backfill_run_job_fields(apply=True, create_since=timezone.now() - timedelta(days=30))
        self.assertEqual(report.counts["create_skipped"], 1)
        self.assertEqual(ContentFactoryRun.objects.filter(result__has_key=BACKFILL_RESULT_KEY).count(), 0)
        self.assertEqual(ContentFactoryRun.objects.count(), 1)

    def test_token_job_matches_its_bound_run_and_orphans_keep_the_real_carrier(self):
        with override_settings(ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED=False):
            bound_token = "vibe-dispatch:article_generation:bf"
            _run("cf-real-bf", run_request={"client_request_id": bound_token})
            token_job = _job(bound_token, client_request_id=bound_token)
            orphan_token = "vibe-dispatch:article_generation:bf-orphan"
            _run("cf-real-bf-owned", run_request={"client_request_id": orphan_token})
            _job(orphan_token, client_request_id=orphan_token, billing_status="charged")
            _job("cf-real-bf-owned", client_request_id=orphan_token, billing_status="reused")
        report = backfill_run_job_fields(apply=True, create=False)
        bound = ContentFactoryRun.objects.get(run_id="cf-real-bf")
        self.assertEqual(bound.legacy_job_id, bound_token)
        self.assertEqual(job_field_mismatches(token_job, job_view(bound_token)), [])
        self.assertIsNone(job_view("cf-real-bf"))  # no Job has the real id yet, as on the Job table
        self.assertEqual(report.counts["orphan_token_jobs"], 1)
        self.assertEqual(report.conflicts["billing_status"], 1)
        self.assertEqual(ContentFactoryRun.objects.get(run_id="cf-real-bf-owned").billing_status, "reused")

    def test_undo_restores_the_previous_state(self):
        before = list(ContentFactoryRun.objects.order_by("pk").values())
        backfill_run_job_fields(apply=True)
        output = self._call("--undo")
        self.assertIn("created_runs_deleted: 1", output)
        self.assertTrue(ContentFactoryRun.objects.filter(run_id="legacy-slack-job").exists())  # dry run
        self._call("--undo", "--apply")
        self.assertEqual(list(ContentFactoryRun.objects.order_by("pk").values()), before)
        self.article.refresh_from_db()
        self.assertIsNone(self.article.run_id)
        self.assertEqual(ContentFactoryJob.objects.count(), 2)

    def test_undo_keeps_a_created_run_something_updated(self):
        backfill_run_job_fields(apply=True)
        created = ContentFactoryRun.objects.get(run_id="legacy-slack-job")
        created.current_step = "callback"
        created.save(update_fields=["current_step", "updated_at"])
        counts = undo_backfill(apply=True)
        self.assertEqual(counts["created_runs_kept_updated"], 1)
        created.refresh_from_db()
        self.assertEqual(_run_columns(created), _blank())

    @READ
    def test_undo_refuses_while_reads_come_from_runs(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            self._call("--undo", "--apply")

    def test_compare_counts_mismatches_by_field_only(self):
        backfill_run_job_fields(apply=True)
        ContentFactoryJob.objects.filter(pk=self.matched_job.pk).update(status="completed", billing_amount=12)
        report = compare_run_job_fields()
        self.assertEqual(report.counts["jobs_with_mismatch"], 1)
        self.assertEqual(dict(report.mismatches), {"status": 1, "billing_amount": 1})
        out = StringIO()
        call_command("compare_run_job_fields", stdout=out)
        self.assertIn("Mismatches by field: billing_amount=1, status=1", out.getvalue())
        self.assertNotIn(DOMAIN, out.getvalue())

    def test_workflow_inference(self):
        cases = {
            "repo_scan": {"type": "scan"},
            "article_system_setup": {"type": "scaffold_articles"},
            "confirmed_topic": {"source_run_id": "src", "topic": "A"},
            "auto_discovery": {"roo_points_action": "content_island_topic_generation"},
            "direct_generate": {"topic": "A"},
        }
        for expected, meta in cases.items():
            job = ContentFactoryJob(job_id="x", request_meta=meta)
            self.assertEqual(infer_workflow(job), expected, meta)


@DUAL_WRITE
class JobViewAndShadowReadTest(TestCase):
    def setUp(self):
        now = timezone.now()
        # Run timestamps run opposite to the Jobs', so only the rank decides.
        specs = (
            ("run-tr-source", "generating", {}, now - timedelta(minutes=5)),
            ("run-tr-child-blocked", "blocked", {"source_run_id": "run-tr-source"}, now - timedelta(minutes=4)),
            ("run-tr-child-mode", "awaiting_delivery_mode", {"source_run_id": "run-tr-source"}, now - timedelta(minutes=3)),
            ("run-tr-other", "awaiting_delivery_mode", {}, now - timedelta(minutes=2)),
            ("run-tr-done", "completed", {"source_run_id": "run-tr-source"}, now - timedelta(minutes=1)),
        )
        for job_id, status, meta, stamp in specs:
            run = _run(job_id)
            _job(job_id, status=status, request_meta={**meta, "requested_by_slack_user_id": "U_FOUNDER"},
                 slack_thread_ts="200.1", slack_root_message_ts="200.0")
            ContentFactoryJob.objects.filter(job_id=job_id).update(created_at=stamp, updated_at=stamp)
            ContentFactoryRun.objects.filter(pk=run.pk).update(created_at=now - (stamp - now),
                                                               updated_at=now - (stamp - now))

    def _confirm(self, **kwargs):
        from content_factory.content_views import _resolve_confirm_job_for_thread
        return _resolve_confirm_job_for_thread(slack_user_id="U_FOUNDER", slack_channel_id="C_SYNTH",
                                               slack_thread_ts=kwargs.pop("thread", "200.1"), **kwargs)

    def test_view_has_job_parity_for_every_field_and_is_read_only(self):
        for job in ContentFactoryJob.objects.all():
            view = job_view(job.job_id)
            self.assertEqual(job_field_mismatches(job, view), [], job.job_id)
            self.assertEqual(view.status, job.status)
            self.assertNotEqual(view.status, view.run.status)  # delivery phase, not the CF status
        view = job_view("run-tr-source")
        with self.assertRaises(AttributeError):
            view.status = "completed"
        with self.assertRaises(TypeError):
            view.save()
        _run("run-without-job")
        self.assertIsNone(job_view("run-without-job"))
        self.assertIsNone(job_view(ContentFactoryRun.objects.get(run_id="run-without-job")))
        self.assertIsInstance(job_view(ContentFactoryRun.objects.get(run_id="run-tr-source")), RunJobView)

    def test_confirm_thread_routing_picks_the_same_job_in_every_mode(self):
        cases = ({}, {"source_job_id": "run-tr-source"}, {"source_job_id": "run-tr-other"}, {"domain": DOMAIN},
                 {"thread": "200.0"})
        for kwargs in cases:
            baseline = self._confirm(**dict(kwargs))
            with SHADOW, self.assertNoLogs("content_factory.job_view", level="WARNING"):
                self.assertEqual(self._confirm(**dict(kwargs)), baseline, kwargs)
            with READ:
                self.assertEqual(self._confirm(**dict(kwargs)), baseline, kwargs)
        self.assertEqual(self._confirm()["job_id"], "run-tr-child-mode")  # child first, then the status rank
        self.assertEqual(self._confirm(source_job_id="run-tr-other")["job_id"], "run-tr-other")

    def test_publishable_thread_routing_matches_in_every_mode(self):
        from content_factory.content_views import _resolve_publishable_job_for_thread
        source = ContentFactoryJob.objects.get(job_id="run-tr-source")
        source.request_meta = {**source.request_meta, "publish_stage": "promotion_requested",
                               "promoted_publish_job_id": "run-tr-done"}
        source.save(update_fields=["request_meta"])
        done = ContentFactoryJob.objects.get(job_id="run-tr-done")
        done.request_meta = {**done.request_meta, "publish_stage": "pr_opened"}
        done.save(update_fields=["request_meta"])

        def resolve():
            return _resolve_publishable_job_for_thread(slack_user_id="U_FOUNDER", slack_channel_id="C_SYNTH",
                                                       slack_thread_ts="200.1", domain=DOMAIN)
        baseline = resolve()
        self.assertEqual(baseline["resolution"], "in_progress")
        with SHADOW, self.assertNoLogs("content_factory.job_view", level="WARNING"):
            self.assertEqual(resolve(), baseline)
        with READ:
            self.assertEqual(resolve(), baseline)

    def test_shadow_serves_the_job_and_logs_field_names_only(self):
        ContentFactoryRun.objects.filter(run_id="run-tr-child-mode").update(delivery_phase="completed",
                                                                             job_error_message="founder@example.com")
        baseline_job = "run-tr-child-mode"
        with SHADOW, self.assertLogs("content_factory.job_view", level="WARNING") as logs:
            self.assertEqual(self._confirm()["job_id"], baseline_job)
        message = "\n".join(logs.output)
        self.assertIn("site=confirm_thread", message)
        self.assertIn("fields=", message)
        for private in ("founder@example.com", DOMAIN, "U_FOUNDER", "C_SYNTH"):
            self.assertNotIn(private, message)
        with READ:
            self.assertNotEqual(self._confirm()["job_id"], baseline_job)  # read mode serves the run

    def test_shadow_read_failure_never_changes_the_served_value(self):
        baseline = self._confirm()
        with SHADOW, patch("content_factory.job_view.thread_runs", side_effect=RuntimeError("synthetic")), \
                self.assertLogs("content_factory.job_view", level="WARNING"):
            self.assertEqual(self._confirm(), baseline)

    def test_billing_flag_reads_follow_the_switches(self):
        from content_factory.job_view import read_job_flag
        from integrations.services.article_generation import CONTENT_FACTORY_BILLING_STATUS_REFUNDED
        ContentFactoryJob.objects.filter(job_id="run-tr-other").update(billing_status=CONTENT_FACTORY_BILLING_STATUS_REFUNDED)

        def refunded():
            return read_job_flag(
                "billing_key_refunded",
                from_job=lambda: ContentFactoryJob.objects.filter(client_request_id="key-run-tr-other",
                                                                  billing_status="refunded").first(),
                from_run=lambda: ContentFactoryRun.objects.filter(client_request_id="key-run-tr-other",
                                                                  billing_status="refunded").exists())
        self.assertTrue(refunded())
        with SHADOW, self.assertLogs("content_factory.job_view", level="WARNING") as logs:
            self.assertTrue(refunded())
        self.assertIn("fields=value count=1", logs.output[0])
        with READ:
            self.assertFalse(refunded())  # the queryset update was never mirrored

    def test_refunded_key_guard_reads_the_run_in_read_mode(self):
        from integrations.services import article_generation
        from integrations.services.article_generation import ArticleGenerationError
        ContentFactoryRun.objects.filter(run_id="run-tr-other").update(billing_status="refunded")
        user = User.objects.create_user(email="guard@example.com", password="password")
        request = {"client_request_id": "key-run-tr-other", "expected_cost_points": 6}
        with READ, patch.object(article_generation, "_original_article_admission_points", return_value=6), \
                patch.object(article_generation, "_article_billing_user", return_value=user):
            with self.assertRaisesRegex(ArticleGenerationError, "already refunded"):
                article_generation._charge_content_factory_user(
                    user=user, created_by_slack_id="U_FOUNDER", article_request=dict(request),
                    resolved_domain=DOMAIN, prevalidated_admission=True)


class JobViewLogPrivacyTest(TestCase):
    def test_mismatch_log_has_names_and_counts_only(self):
        from content_factory.job_view import read_job_facts
        with SHADOW, self.assertLogs("content_factory.job_view", level="WARNING") as logs:
            value = read_job_facts("synthetic", from_job=lambda: {"email": "founder@example.com", "a": 1},
                                   from_run=lambda: {"email": "other@example.com", "a": 1})
        self.assertEqual(value["email"], "founder@example.com")
        self.assertEqual(logs.output, ["WARNING:content_factory.job_view:content_factory_run_job_shadow_mismatch "
                                       "site=synthetic fields=email count=1"])

    def test_flags_off_reads_only_the_job(self):
        from content_factory.job_view import read_job_facts
        calls = []
        self.assertEqual(read_job_facts("synthetic", from_job=lambda: "job",
                                        from_run=lambda: calls.append(1) or "run"), "job")
        self.assertEqual(calls, [])

