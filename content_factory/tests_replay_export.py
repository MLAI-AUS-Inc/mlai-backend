"""Plan F5: the read-only export writes a redacted replay record from an allowlist.

The fixture run is stuffed with everything that must never leave the
database: repository and provider tokens, e-mail addresses, the founder's
name, domain and repository, private source code and free-text errors.
"""
import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from workflow_runs.models import ContentFactoryRun, ContentFactoryRunStep

from .replay_records import DRAFT_HANDLER, EXPORT_SITE, RecordError, export_record, validate

GITHUB_TOKEN = "ghs_" + "Q" * 36
PROVIDER_KEY = "sk-proj-" + "z" * 32
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmb3VuZGVyIn0.c2lnbmF0dXJlLXZhbHVl"
EMAIL = "avery.placeholder@founder-shop.com.au"
NAME = "Avery Placeholder"
DOMAIN = "founder-shop.com.au"
REPO = "avery-goods/private-storefront"
PRIVATE_SOURCE = "export const STRIPE_SECRET = loadSecret();\nfunction checkoutTotal(cart) { return cart.sum(); }"
SECRETS = [GITHUB_TOKEN, PROVIDER_KEY, JWT, EMAIL, NAME, "Avery", "Placeholder", DOMAIN, "founder-shop", REPO,
           "avery-goods", "private-storefront", "STRIPE_SECRET", "checkoutTotal", "x-access-token", "Bearer "]


def make_run(**overrides):
    fields = {
        "run_id": "export-fixture-run", "workflow": "direct_generate", "domain": DOMAIN, "github_repo": REPO,
        "status": "failed", "current_step": "plan_article", "approval_state": "not_required",
        "slack_user_id": "U0FOUNDER",
        "error": f"Unverified response 308 for https://{DOMAIN}/sitemap.xml (contact {EMAIL})",
        "run_request": {
            "request_source": "roo_slackbot", "delivery_mode": "content_only", "connection_generation": 3,
            "website_connection_id": "connection-uuid", "github_token": GITHUB_TOKEN, "installation_token": GITHUB_TOKEN,
            "openai_api_key": PROVIDER_KEY, "requested_by_email": EMAIL, "founder_name": NAME,
            "editorial_brief": {"audience": "Busy parents", "reader_task": NAME}, "framework": "next",
            "hosting_provider": "vercel", "topic": f"{NAME}'s guide", "roo_points_billing_status": "charged",
            "roo_points_cost": 12, "authorization": f"Bearer {JWT}",
        },
        "result": {
            "error_code": "CORPUS_INCOMPLETE",
            "failure": {"code": "CORPUS_INCOMPLETE", "retryable": False, "requires_user_action": True,
                        "failure_class": "unsupported", "stage": "plan_article", "message": f"Hi {NAME}, it failed",
                        "details": {"url": f"https://{DOMAIN}/sitemap.xml?token={GITHUB_TOKEN}", "http_status": 308,
                                    "location": f"https://user:{GITHUB_TOKEN}@{DOMAIN}/sitemap_index.xml"}},
            "callback_rejection": {"code": "website_operation_terminal", "raise_site": "website_operations.validate_operation",
                                   "message": EMAIL},
            "authority_refusals": [{"code": "website_connection_required", "raise_site": EMAIL},
                                   {"code": GITHUB_TOKEN}],
            "cancellation_reason": "system_stop", "cancelled_by": "system", "cancellation_transition": "reset",
            "article_preview_quality": {"status": "blocking_findings", "self_heal_exhausted": True,
                                        "escalation": "quality_review", "findings": ["style:2_mismatches", f"Ask {NAME}", EMAIL]},
            "generated_files": {"src/checkout.ts": PRIVATE_SOURCE}, "article_markdown": f"# {NAME}\n{PRIVATE_SOURCE}",
            "author": {"name": NAME, "email": EMAIL, "avatarUrl": f"https://{DOMAIN}/me.jpg"},
            "framework": DOMAIN,  # A code-shaped value that is the founder's domain is still dropped.
        },
    }
    fields.update(overrides)
    run = ContentFactoryRun.objects.create(**fields)
    ContentFactoryRunStep.objects.create(run=run, step_key="collect_research_bundle", display_order=0, status="completed", attempts=1)
    ContentFactoryRunStep.objects.create(run=run, step_key="plan_article", display_order=1, status="failed", attempts=2,
                                         error=f"Failed for {EMAIL} with {GITHUB_TOKEN}", message=PRIVATE_SOURCE)
    return run


@override_settings(IS_PRODUCTION_ENV=False)
class ExportReplayRecordTests(TestCase):
    def export(self, run, *extra):
        out = StringIO()
        call_command("export_replay_record", "--run-pk", str(run.pk), *extra, stdout=out, stderr=StringIO())
        return out.getvalue()

    def test_secrets_personal_data_and_private_content_are_removed(self):
        text = self.export(make_run())
        lowered = text.lower()
        for value in SECRETS:
            with self.subTest(value=value):
                self.assertNotIn(value.lower(), lowered)
        record = json.loads(text)
        self.assertEqual(record["code"], "CORPUS_INCOMPLETE")
        self.assertEqual(record["step"], "plan_article")
        self.assertEqual(record["workflow"], "direct_generate")
        self.assertEqual(record["http"], [{"method": "GET", "url": f"https://{EXPORT_SITE}/sitemap.xml", "status": 308,
                                           "headers": {}, "body": ""}])
        self.assertEqual(record["framework_fingerprint"], {"framework": "next", "host": "vercel"})
        inputs = record["inputs"]
        self.assertEqual(inputs["failure"], {"code": "CORPUS_INCOMPLETE", "retryable": False, "requires_user_action": True,
                                             "failure_class": "unsupported", "stage": "plan_article"})
        self.assertEqual(inputs["callback_rejection"], {"code": "website_operation_terminal",
                                                        "raise_site": "website_operations.validate_operation"})
        self.assertEqual(inputs["authority_refusals"], [{"code": "website_connection_required"}])
        self.assertEqual(inputs["article_preview_quality"]["findings"], ["style:2_mismatches"])
        self.assertEqual(inputs["cancellation"], {"cancellation_reason": "system_stop", "cancelled_by": "system",
                                                  "cancellation_transition": "reset"})
        self.assertEqual(inputs["request"], {"request_source": "roo_slackbot", "delivery_mode": "content_only",
                                             "connection_generation": 3, "roo_points_billing_status": "charged",
                                             "roo_points_cost": 12, "has_website_connection": True,
                                             "has_editorial_brief": True})
        self.assertEqual(inputs["steps"], [{"step_key": "collect_research_bundle", "status": "completed", "attempts": 1},
                                           {"step_key": "plan_article", "status": "failed", "attempts": 2}])
        self.assertIs(inputs["error_present"], True)
        self.assertNotIn("generated_files", text)
        self.assertNotIn("article_markdown", text)

    def test_the_export_is_an_allowlist_of_named_fields(self):
        record = json.loads(self.export(make_run()))
        self.assertEqual(set(record), {"schema", "id", "source", "workflow", "step", "code", "framework_fingerprint",
                                       "http", "inputs", "replay", "expected", "notes"})
        self.assertEqual(set(record["inputs"]), {"status", "current_step", "approval_state", "steps", "failure",
                                                 "callback_rejection", "authority_refusals", "cancellation",
                                                 "article_preview_quality", "request", "error_present"})

    def test_an_export_is_a_draft_until_its_redaction_is_reviewed(self):
        record = json.loads(self.export(make_run()))
        self.assertIs(record["source"]["synthetic"], False)
        self.assertIsNone(record["expected"])
        self.assertEqual(record["replay"]["handler"], DRAFT_HANDLER)
        validate(record, draft=True)
        with self.assertRaisesRegex(RecordError, "redaction_reviewed_by"):
            validate(record)
        record["source"]["redaction_reviewed_by"] = "reviewer-handle"
        with self.assertRaisesRegex(RecordError, "expected must be"):
            validate(record)
        record.update(expected="pass", replay={"repo": "cf", "handler": "corpus.load_existing_articles"})
        self.assertIs(validate(record), record)

    def test_the_command_writes_nothing_to_the_database(self):
        run = make_run()
        before = ContentFactoryRun.objects.filter(pk=run.pk).values().get()
        with CaptureQueriesContext(connection) as queries:
            self.export(run)
        writes = [query["sql"] for query in queries.captured_queries
                  if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(writes, [])
        self.assertEqual(ContentFactoryRun.objects.filter(pk=run.pk).values().get(), before)

    def test_it_writes_a_new_file_and_never_overwrites_one(self):
        run = make_run()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"
            self.export(run, "--output", str(path))
            self.assertEqual(json.loads(path.read_text())["source"]["run_pk"], run.pk)
            with self.assertRaisesRegex(CommandError, "already exists"):
                self.export(run, "--output", str(path))

    def test_an_unknown_run_is_an_error(self):
        with self.assertRaisesRegex(CommandError, "Run not found"):
            call_command("export_replay_record", "--run-pk", "999999", stdout=StringIO())

    @override_settings(IS_PRODUCTION_ENV=True)
    def test_outside_local_and_test_it_needs_the_explicit_flag(self):
        run = make_run()
        with self.assertRaisesRegex(CommandError, "allow-production-read"):
            self.export(run)
        self.assertIn('"schema": "replay-record-v1"', self.export(run, "--allow-production-read"))

    def test_a_forbidden_shape_that_slips_into_a_code_field_fails_closed(self):
        run = make_run(workflow="direct_generate")
        run.result = {**run.result, "error_code": "founder-shop.com.au"}
        with self.assertRaisesRegex(RecordError, "identifying"):
            export_record(run)

    def test_another_sites_url_is_not_exported(self):
        run = make_run()
        run.result["failure"]["details"]["url"] = "https://cdn.other-company.com/sitemap.xml"
        self.assertEqual(export_record(run)["http"], [])

    def test_a_url_shaped_domain_or_repository_is_still_caught(self):
        run = make_run(domain=f"https://www.{DOMAIN}/", github_repo=f"https://github.com/{REPO}.git")
        for leaked in ("private-storefront", "avery-goods", "founder-shop"):
            run.result = {**run.result, "error_code": leaked}
            with self.subTest(leaked=leaked), self.assertRaisesRegex(RecordError, "identifying"):
                export_record(run)
        run.result = {**run.result, "error_code": "CORPUS_INCOMPLETE"}
        self.assertEqual(export_record(run)["http"][0]["url"], f"https://{EXPORT_SITE}/sitemap.xml")
