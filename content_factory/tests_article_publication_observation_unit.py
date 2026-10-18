"""Consume actual CF intent/stop fixtures without DB or provider access."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from . import article_publication_observation as observer
from .article_publication_evidence import cancellation_result, refund_reconciliation_reason
from .article_publication_intent import validate
from .website_contract import WebsiteAuthorityError


class PublicationObservationTests(SimpleTestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / "test_fixtures/article-publication-observation-v1.json").read_text())
        self.intent = deepcopy(self.fixture["intent"])
        self.now = timezone.now()
        scope = self.intent["scope"]
        self.website = SimpleNamespace(pk=scope["website_connection_id"], organization_id=7,
            organization=SimpleNamespace(domain="fixture.test"), generation=2, repository_id=17,
            github_repo="fixture/site", branch="main", app_root="", configuration_version=3, state="connected", blockers=[])
        self.run = SimpleNamespace(run_id=self.intent["run_id"], organization_id=7, domain="fixture.test",
            github_repo="fixture/site", workflow="direct_generate", run_request=deepcopy(self.fixture["original_request"]),
            result={}, status="failed", acceptance_summary={}, save=Mock())
        self.run.result = cancellation_result(self.run, self.fixture["cancelled"])
        self.run.status = "cancelled"
        self.op = SimpleNamespace(pk="observer", connection=self.website, connection_id=self.website.pk,
            action=observer.ACTION, payload={"intent": self.intent}, state="pending", attempts=0,
            next_attempt_at=None, receipt={}, created_at=self.now, save=Mock())

    def rows(self):
        return self.website, self.op, self.intent, self.run

    def advance(self, *, probe=None, reply=None):
        with patch.object(observer.transaction, "atomic", side_effect=lambda: nullcontext()), \
                patch.object(observer, "_locked", side_effect=lambda _: self.rows()), \
                patch.object(observer, "_observation", side_effect=probe or (lambda *_: deepcopy(self.fixture["observation"]))) as read, \
                patch.object(observer, "_send_observation", side_effect=reply or (lambda *_: deepcopy(self.fixture["acknowledged"]))) as send:
            result = observer.advance("observer", now=self.now)
            return result, read, send

    def test_actual_writer_fixture_registers_exact_article_files_and_original_payment_scope(self):
        from workflow_runs.models import ContentFactoryRun
        from .website_models import WebsiteConnectionOperation
        data = deepcopy(self.fixture["mutation"])
        mutation = SimpleNamespace(run_id=self.run.run_id, generation=2,
            operation_id=f"{self.website.pk}:{self.intent['mutation_id']}",
            files=data["files"], base_sha=data["base_sha"], branch=data["branch"])
        with patch.object(ContentFactoryRun.objects, "select_for_update") as runs, \
                patch.object(WebsiteConnectionOperation.objects, "get_or_create", return_value=(self.op, True)) as reserve:
            runs.return_value.filter.return_value.first.return_value = self.run
            self.assertEqual(observer.register_intent(self.website, mutation, data), self.intent["intent_id"])
            saved = reserve.call_args.kwargs["defaults"]["payload"]["intent"]
            self.assertEqual(saved["scope"]["client_request_id"], self.run.run_request["client_request_id"])
            mutation.files[0]["kind"] = "setup_scaffolding"
            with self.assertRaises(WebsiteAuthorityError):
                observer.register_intent(self.website, mutation, data)
            self.assertEqual(reserve.call_count, 1)

    def test_positive_content_observation_records_effect_without_changing_stop_or_refund(self):
        previous = deepcopy(self.run.result["article_cancellation_v1"])
        result, read, send = self.advance()
        self.assertEqual(result, "completed")
        self.assertEqual(self.op.attempts, 1)
        self.assertEqual(self.run.result["publication_evidence_v1"], self.fixture["acknowledged"]["publication_evidence_v1"])
        self.assertEqual(self.run.result["article_cancellation_v1"], previous)
        self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")
        self.assertNotIn("refunded", self.run.result)
        read.assert_called_once(); send.assert_called_once()

    def test_original_push_must_be_stopped_before_provider_attempt_budget_starts(self):
        self.run.status = "running"
        self.run.result.pop("article_cancellation_v1")
        result, read, send = self.advance()
        self.assertEqual((result, self.op.attempts), ("pending", 0))
        read.assert_not_called(); send.assert_not_called()

    def test_absence_or_transport_failure_never_becomes_no_effect_and_stops_after_three(self):
        for attempt in range(3):
            self.op.next_attempt_at = None
            result, _, send = self.advance(probe=lambda *_: (_ for _ in ()).throw(ValueError("404")))
            send.assert_not_called()
            self.assertEqual(self.op.attempts, attempt + 1)
        self.assertEqual(result, "needs_attention")
        self.assertIsNone(self.op.next_attempt_at)
        self.assertEqual(self.run.result["article_cancellation_v1"]["outcome"], "unknown")
        self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")

    def test_deadline_retires_even_an_unconfirmed_original_worker_without_reading_or_refunding(self):
        self.op.created_at = self.now - observer.DEADLINE
        result, read, send = self.advance()
        self.assertEqual(result, "needs_attention")
        read.assert_not_called(); send.assert_not_called()
        self.assertEqual(refund_reconciliation_reason(self.run), "needs_reconciliation")

    def test_same_operation_rebind_during_provider_read_cannot_apply_old_observation(self):
        def rebind(*_):
            self.run.run_request["operation_attempt"] = 2
            return deepcopy(self.fixture["observation"])
        result, _, send = self.advance(probe=rebind)
        self.assertEqual(result, "pending")
        send.assert_not_called(); self.run.save.assert_not_called()

    def test_consent_changes_during_worker_acknowledgement_cannot_commit_result(self):
        def revoke(*_):
            self.website.generation += 1
            return deepcopy(self.fixture["acknowledged"])
        result, _, send = self.advance(reply=revoke)
        self.assertEqual(result, "pending")
        send.assert_called_once(); self.run.save.assert_not_called()

    def test_scope_source_payment_repository_and_epoch_must_stay_original(self):
        for key, value in (("expected_source_sha", "e" * 40), ("client_request_id", "new-payment"),
                ("repository_id", 99), ("deletion_epoch", 1), ("connection_generation", 3)):
            with self.subTest(key=key):
                original = self.run.run_request[key]
                self.run.run_request[key] = value
                self.assertFalse(observer._same_scope(self.run, self.intent, self.website))
                self.run.run_request[key] = original

    def provider_reads(self, change=None):
        repo = {"id": 17, "full_name": "fixture/site"}
        ref = {"ref": "refs/heads/" + self.intent["branch_name"], "object": {"type": "commit", "sha": "b" * 40}}
        commit = {"sha": "b" * 40, "tree": {"sha": "c" * 40}, "parents": [{"sha": "a" * 40}]}
        bodies = [repo, ref, commit]
        if change:
            change(bodies)
        def get(url, **kwargs):
            body = json.dumps(bodies.pop(0)).encode()
            return SimpleNamespace(status_code=200, iter_content=lambda **_: iter([body]), close=Mock())
        return get

    def observe(self, change=None):
        with patch("content_factory.website_connections.authority_guard", side_effect=lambda *a, **kw: nullcontext()), \
                patch("content_factory.website_tokens.mint_website_token", return_value={"github_token": "synthetic"}) as token, \
                patch("integrations.http_client.get", side_effect=self.provider_reads(change)) as get, \
                patch("integrations.http_client.delete"):
            result = observer._observation(self.website, self.intent)
            self.assertEqual(get.call_count, 3)
            self.assertEqual(token.call_args.kwargs, {"permission_mode": "read", "action": "read"})
            self.assertNotIn("operation_id", token.call_args.args[0])
            return result

    def test_actual_provider_reader_requires_repo_ref_commit_tree_and_parent(self):
        self.assertEqual(self.observe(), self.fixture["observation"])
        for change in (lambda b: b[0].update(id=99), lambda b: b[1]["object"].update(sha="d" * 40),
                lambda b: b[2]["tree"].update(sha="d" * 40), lambda b: b[2]["parents"][0].update(sha="d" * 40)):
            with self.assertRaises(ValueError):
                self.observe(change)

    def test_provider_response_body_is_bounded_and_redirects_are_refused(self):
        for code, body in ((302, b"{}"), (404, b"{}"), (200, b"x" * 65537)):
            response = SimpleNamespace(status_code=code, iter_content=lambda **_: iter([body]), close=Mock())
            with patch("integrations.http_client.get", return_value=response) as get, self.assertRaises(ValueError):
                observer._provider_json("https://api.github.com/repos/fixture/site", headers={})
            self.assertFalse(get.call_args.kwargs["allow_redirects"])

    def test_contract_rejects_extra_fields_bool_id_and_digest_tampering(self):
        self.assertEqual(validate(self.intent), self.intent)
        for change in (lambda v: v.update(token="secret"), lambda v: v["scope"].update(operation_attempt=True),
                lambda v: v.update(head_sha="f" * 40)):
            value = deepcopy(self.intent); change(value)
            with self.assertRaises(ValueError):
                validate(value)

    def test_new_observed_callback_between_read_or_ack_and_commit_wins(self):
        for boundary in ("probe", "reply"):
            with self.subTest(boundary=boundary):
                self.setUp()
                current = deepcopy(self.fixture["acknowledged"]["publication_evidence_v1"])
                current["head_sha"] = "d" * 40
                def callback(*_):
                    self.run.result["publication_evidence_v1"] = deepcopy(current)
                    return deepcopy(self.fixture["observation" if boundary == "probe" else "acknowledged"])
                result, _, send = self.advance(**{boundary: callback})
                self.assertEqual(result, "pending")
                self.assertEqual(self.run.result["publication_evidence_v1"], current)
                self.run.save.assert_not_called()
                self.assertEqual(send.call_count, 0 if boundary == "probe" else 1)

    def test_changed_stop_receipt_after_provider_read_stays_unmodified(self):
        def callback(*_):
            self.run.result["article_cancellation_v1"]["state_version"] += 1
            return deepcopy(self.fixture["observation"])
        result, _, send = self.advance(probe=callback)
        self.assertEqual(result, "pending")
        send.assert_not_called(); self.run.save.assert_not_called()

    def test_private_acknowledgement_body_is_bounded_and_closed(self):
        for body in (b"x" * 65537, b"[]"):
            response = SimpleNamespace(status_code=200, iter_content=lambda **_: iter([body]), close=Mock())
            with patch("content_factory.vibe_marketing_views._content_factory_remote_config",
                    return_value={"enabled": True, "base_url": "https://worker.example.org"}), \
                    patch("content_factory.vibe_marketing_views._content_factory_headers", return_value={}), \
                    patch("integrations.http_client.post", return_value=response) as post, self.assertRaises(ValueError):
                observer._send_observation(self.run, self.intent, self.fixture["observation"])
            self.assertTrue(post.call_args.kwargs["stream"])
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
            response.close.assert_called_once()
