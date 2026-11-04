"""Framework and historical tools cannot regain admitted Run authority."""
from copy import deepcopy
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from organizations.models import Organization
from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep, ContentFactoryRunStepAttempt
from .models import ContentFactoryJob, WrittenArticle
from .run_backend_facts import create_job_facts
from .run_ownership import bind_admitted_run, reserve_admitted_run, user_principal
from .tests_run_backend_facts import FLAGS


@override_settings(**FLAGS)
class FactFrameworkTests(TransactionTestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Framework owner", domain="framework.example")
        payload = {"domain": self.org.domain, "client_request_id": "framework-key"}
        self.run = reserve_admitted_run(organization=self.org, principal=user_principal(123, "user:123"),
            workflow="article_generation", payload=payload)
        self.run = bind_admitted_run(self.run, "framework-worker")
        self.job = create_job_facts(job_id=self.run.run_id, domain=self.org.domain,
            slack_user_id="U_FRAMEWORK", status="generating")
        self.request = SimpleNamespace(user=SimpleNamespace(has_perm=lambda *args: True))

    def state(self):
        return deepcopy(ContentFactoryRun.objects.values().get(pk=self.run.pk))

    def test_legacy_mirror_and_new_run_fill_never_reverse_owned_facts(self):
        from .job_mirror import mirror_job_to_run, mirror_jobs_to_runs
        before = self.state()
        ContentFactoryJob.objects.filter(pk=self.job.pk).update(status="error", progress_message_ts="foreign-output")
        self.job.refresh_from_db()
        with override_settings(ARTICLES_RUN_JOB_DUAL_WRITE_ENABLED=True):
            self.assertFalse(mirror_job_to_run(self.job))
            self.assertEqual(mirror_jobs_to_runs(ContentFactoryJob.objects.filter(pk=self.job.pk)), 0)
        self.assertEqual(self.state(), before)

    def test_backfill_and_undo_exclude_owned_fields_and_article_links(self):
        from .run_job_backfill import backfill_run_job_fields, undo_backfill, BACKFILL_RESULT_KEY
        before = self.state()
        ContentFactoryJob.objects.filter(pk=self.job.pk).update(progress_message_ts="not-authority")
        report = backfill_run_job_fields(apply=True, create=True)
        self.assertEqual(report.counts["owned_runs_excluded"], 1)
        self.assertEqual(self.state(), before)
        article = WrittenArticle.objects.create(organization=self.org, title="Retained body", slug="retained-body",
            category="guides", primary_keyword="retained", run=self.run)
        result = {**before["result"], BACKFILL_RESULT_KEY: {"at": before["updated_at"].isoformat(), "job_pk": self.job.pk}}
        ContentFactoryRun.objects.filter(pk=self.run.pk).update(result=result)
        before = self.state()
        undo_backfill(apply=True)
        self.assertEqual(self.state(), before)
        article.refresh_from_db()
        self.assertEqual(article.run_id, self.run.pk)

    def test_job_admin_denies_edit_delete_create_and_identity_evasion(self):
        from .admin import ContentFactoryJobAdmin
        model_admin = ContentFactoryJobAdmin(ContentFactoryJob, admin.site)
        self.assertFalse(model_admin.has_change_permission(self.request, self.job))
        self.assertFalse(model_admin.has_delete_permission(self.request, self.job))
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, self.job, None, True)
        with self.assertRaises(PermissionDenied):
            model_admin.delete_queryset(self.request, ContentFactoryJob.objects.filter(pk=self.job.pk))
        self.job.job_id = "renamed-to-evade-owner"
        with self.assertRaises(PermissionDenied):
            model_admin.save_model(self.request, self.job, None, True)
        self.assertTrue(ContentFactoryJob.objects.filter(pk=self.job.pk, job_id="framework-worker").exists())

    def test_run_step_and_attempt_admin_cannot_mutate_or_delete_owned_history(self):
        from workflow_runs.admin import ContentFactoryRunAdmin, ContentFactoryRunStepAdmin, ContentFactoryRunStepAttemptAdmin
        step = ContentFactoryRunStep.objects.create(run=self.run, step_key="write", display_order=1)
        attempt = ContentFactoryRunStepAttempt.objects.create(step=step, attempt=1, status="running")
        for obj, model_admin in ((self.run, ContentFactoryRunAdmin(ContentFactoryRun, admin.site)),
            (step, ContentFactoryRunStepAdmin(ContentFactoryRunStep, admin.site)),
            (attempt, ContentFactoryRunStepAttemptAdmin(ContentFactoryRunStepAttempt, admin.site))):
            with self.subTest(model=type(obj).__name__):
                self.assertFalse(model_admin.has_change_permission(self.request, obj))
                self.assertFalse(model_admin.has_delete_permission(self.request, obj))
                with self.assertRaises(PermissionDenied):
                    model_admin.save_model(self.request, obj, None, True)
                with self.assertRaises(PermissionDenied):
                    model_admin.delete_model(self.request, obj)

    def test_teardown_preflights_before_external_cleanup_or_any_database_scrub(self):
        from core.management.commands.teardown_company import Command
        before = self.state()
        with patch.object(Command, "_external_cleanup") as external, \
                patch.object(Command, "_scrub_startup_data") as scrub, self.assertRaises(CommandError):
            call_command("teardown_company", org_id=self.org.pk, apply=True, no_storage=True, stdout=StringIO())
        external.assert_not_called()
        scrub.assert_not_called()
        self.assertEqual(self.state(), before)
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
