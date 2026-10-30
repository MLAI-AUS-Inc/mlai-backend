"""Append-only run events: idempotent by (run_id, seq), bounded, observation only.

Run with scripts/test_without_database.py. Row locks and saves are replaced by
fixtures here; the database path is covered in
tests.test_content_factory_callback_idempotency.
"""
from contextlib import nullcontext
from datetime import datetime, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

CONNECTION = {"website_connection_id": "11111111-1111-1111-1111-111111111111",
              "connection_generation": 2, "repository_id": 42}


def started(seq, step, attempt=1):
    return {"seq": seq, "type": "step_started", "step": step, "attempt": attempt, "at": "2026-10-30T00:00:00+00:00"}


def done(seq, step, attempt=1, status="completed"):
    return {"seq": seq, "type": "step_done", "step": step, "attempt": attempt, "status": status}


class CleanEventTests(SimpleTestCase):
    def test_keeps_typed_facts_and_drops_worker_text(self):
        from .run_events import clean_event
        event = clean_event({**done(4, "write_article"), "message": "Private draft text", "error": "stack"})
        self.assertEqual(event, {"seq": 4, "type": "step_done", "step": "write_article", "attempt": 1, "status": "completed"})
        failure = clean_event({"seq": 5, "type": "failure", "step": "write_article", "code": "ARTICLE_DIRECTORY_AGENT_OUTPUT_INVALID",
                               "failure_class": "transient", "retryable": True, "message": "raw"})
        # The class is the registry's, never the worker's.
        self.assertEqual(failure, {"seq": 5, "type": "failure", "step": "write_article",
                                   "code": "ARTICLE_DIRECTORY_AGENT_OUTPUT_INVALID", "failure_class": "repairable",
                                   "retryable": True})
        cost = clean_event({"seq": 6, "type": "cost", "step": "write_article", "calls": 2, "tokens": 900,
                            "usd": "0.0123", "model": "gpt-5"})
        self.assertEqual(cost["usd"], "0.0123")

    def test_rejects_malformed_events(self):
        from .run_events import clean_event
        for raw in ({"seq": True, "type": "step_done", "step": "a"}, {"seq": 0, "type": "step_done", "step": "a"},
                    {"seq": "3", "type": "step_done", "step": "a"}, {"seq": 3, "type": "deleted", "step": "a"},
                    {"seq": 3, "type": "step_done", "step": "../a"}, {"seq": 3, "type": "step_started"},
                    {"seq": 3, "type": "step_done", "step": "a", "status": "published"},
                    {"seq": 3, "type": "failure", "code": "bad code"}, "event"):
            with self.subTest(raw=raw):
                self.assertIsNone(clean_event(raw))
        self.assertNotIn("usd", clean_event({"seq": 3, "type": "cost", "usd": "-1"}))

    def test_a_code_the_registry_does_not_know_keeps_the_failure_without_it(self):
        from .run_events import clean_event
        self.assertEqual(clean_event({"seq": 3, "type": "failure", "code": "NOT_YET_REGISTERED", "failure_class": "waiting"}),
                         {"seq": 3, "type": "failure", "unregistered_code": True, "failure_class": "waiting"})
        self.assertEqual(clean_event({"seq": 3, "type": "failure", "code": "NOT_YET_REGISTERED", "failure_class": "made_up"}),
                         {"seq": 3, "type": "failure", "unregistered_code": True})


class FoldTests(SimpleTestCase):
    def fold(self, log, raw):
        from .run_events import clean_event, fold_events
        return fold_events(log, [clean_event(item) for item in raw],
                           now=datetime(2026, 10, 30, tzinfo=dt_timezone.utc))

    def test_each_seq_applies_once(self):
        batch = [started(1, "plan"), done(2, "plan"), {"seq": 3, "type": "artifact", "step": "plan"}]
        log, accepted, duplicates = self.fold(None, batch)
        self.assertEqual((accepted, duplicates), (3, 0))
        log, accepted, duplicates = self.fold(log, batch + [started(4, "write")])
        self.assertEqual((accepted, duplicates), (1, 3))
        self.assertEqual(log["steps"]["plan"]["artifacts"], 1)
        self.assertEqual((log["last_seq"], log["applied"]), (4, [[1, 4]]))
        self.assertEqual(log["current_step"], {"step": "write", "seq": 4})

    def test_late_events_never_move_a_step_backwards(self):
        log, _, _ = self.fold(None, [done(3, "plan")])
        log, accepted, _ = self.fold(log, [started(2, "plan")])
        self.assertEqual(accepted, 1)
        self.assertEqual(log["steps"]["plan"]["status"], "completed")
        self.assertEqual(log["applied"], [[2, 3]])

    def test_failures_and_cost_fold_into_bounded_summaries(self):
        from .website_failure_contract import load_registry
        codes = sorted(entry["code"] for entry in load_registry()["codes"])[:30]
        raw = [{"seq": index, "type": "failure", "step": "review", "code": codes[index - 1]} for index in range(1, 31)]
        raw += [{"seq": 40, "type": "cost", "step": "review", "calls": 2, "tokens": 100, "usd": "0.5"},
                {"seq": 41, "type": "cost", "step": "review", "calls": 3, "tokens": 150, "usd": "0.75"},
                {"seq": 42, "type": "cost", "step": "write", "calls": 1, "tokens": 50, "usd": "0.25"}]
        log, _, _ = self.fold(None, raw)
        self.assertEqual(len(log["failures"]), 20)
        self.assertEqual(log["failures"][-1]["code"], codes[29])
        self.assertEqual(log["steps"]["review"]["failure"]["code"], codes[29])
        self.assertEqual(log["cost"]["totals"], {"calls": 4, "tokens": 200, "input_tokens": 0, "output_tokens": 0, "usd": "1.00"})

    def test_log_stays_bounded(self):
        from .run_events import INTERVAL_LIMIT, RECENT_LIMIT, STEP_LIMIT
        log, _, _ = self.fold(None, [started(index, f"step_{index}") for index in range(1, 400, 2)])
        self.assertEqual(len(log["recent"]), RECENT_LIMIT)
        self.assertEqual(len(log["steps"]), STEP_LIMIT)
        self.assertEqual(log["dropped_steps"], 200 - STEP_LIMIT)
        self.assertEqual(len(log["applied"]), INTERVAL_LIMIT)
        self.assertEqual(log["last_seq"], 399)


class ProgressTests(SimpleTestCase):
    def run_with(self, raw, current_step="plan"):
        from .run_events import EVENTS_RESULT_KEY, clean_event, fold_events
        log, _, _ = fold_events(None, [clean_event(item) for item in raw])
        return SimpleNamespace(result={EVENTS_RESULT_KEY: log}, current_step=current_step)

    def test_events_ahead_of_a_lagging_snapshot_are_reported(self):
        from .run_events import event_progress
        run = self.run_with([started(1, "plan"), done(2, "plan"), started(3, "write")])
        progress = event_progress(run, {"plan": {"status": "running", "attempts": 1}})
        self.assertTrue(progress["snapshot_lagging"])
        self.assertEqual(set(progress["steps_ahead"]), {"plan", "write"})
        self.assertEqual(progress["current_step"], "write")

    def test_a_current_snapshot_wins_and_a_new_attempt_is_ahead(self):
        from .run_events import event_progress
        run = self.run_with([started(1, "plan"), done(2, "plan")])
        progress = event_progress(run, {"plan": {"status": "completed", "attempts": 1}})
        self.assertEqual((progress["snapshot_lagging"], progress["current_step"]), (False, "plan"))
        run = self.run_with([started(1, "plan"), done(2, "plan"), started(3, "plan", attempt=2)])
        progress = event_progress(run, {"plan": {"status": "completed", "attempts": 1}})
        self.assertEqual(progress["steps_ahead"]["plan"]["status"], "running")
        self.assertIsNone(event_progress(SimpleNamespace(result={}, current_step=""), {}))

    def test_serializer_adds_progress_only_with_the_flag(self):
        from .service_views import _serialize_content_factory_run
        run = self.run_with([started(1, "plan"), done(2, "plan")])
        step = SimpleNamespace(step_key="plan", required=True, status="running", attempts=1, message="", started_at=None,
                               completed_at=None, artifacts=[], error="", latest_attempt_path="",
                               attempt_history=SimpleNamespace(order_by=lambda *args: []))
        stamp = datetime(2026, 10, 30, tzinfo=dt_timezone.utc)
        run.__dict__.update(run_id="run-1", workflow="direct_generate", domain="example.test", github_repo="", slack_user_id="",
                            status="running", artifact_root="", step_order=["plan"], acceptance_summary={},
                            verification_summary={}, approval_state="not_required", resume_available=False, error="",
                            run_request={}, created_at=stamp, updated_at=stamp,
                            steps=SimpleNamespace(order_by=lambda *args: [step]))
        self.assertNotIn("event_progress", _serialize_content_factory_run(run))
        with override_settings(ARTICLES_RUN_EVENTS_ENABLED=True):
            payload = _serialize_content_factory_run(run)
        self.assertEqual(payload["event_progress"]["steps_ahead"]["plan"]["status"], "completed")
        self.assertEqual(payload["step_states"]["plan"]["status"], "running")


class ReceiveTests(SimpleTestCase):
    def setUp(self):
        self.run = SimpleNamespace(run_id="run-1", status="running", domain="example.test", result={},
                                   run_request=dict(CONNECTION), save=Mock())
        query = Mock()
        query.filter.side_effect = lambda **kwargs: SimpleNamespace(first=lambda: self.run if kwargs["run_id"] == "run-1" else None)
        locks = patch("workflow_runs.models.ContentFactoryRun.objects.select_for_update", return_value=query)
        atomic = patch("django.db.transaction.atomic", side_effect=lambda *args, **kwargs: nullcontext())
        for item in (locks, atomic):
            item.start()
            self.addCleanup(item.stop)

    def receive(self, events, **extra):
        from .run_events import receive_run_events
        return receive_run_events({"event": "run_events", "run_id": "run-1", "domain": "example.test",
                                   **CONNECTION, "events": events, **extra})

    def test_accepts_once_and_acknowledges_a_resend_without_writing(self):
        from .run_events import EVENTS_RESULT_KEY
        batch = [started(1, "plan"), done(2, "plan")]
        response = self.receive(batch)
        self.assertEqual((response.status_code, response.data["accepted"]), (200, 2))
        self.run.save.assert_called_once_with(update_fields=["result"])
        self.assertEqual(self.run.result[EVENTS_RESULT_KEY]["last_seq"], 2)
        self.assertEqual(self.run.status, "running")
        response = self.receive(batch)
        self.assertEqual((response.status_code, response.data["accepted"], response.data["duplicates"]), (200, 0, 2))
        self.run.save.assert_called_once()

    def test_unknown_run_is_retryable_and_a_foreign_binding_is_not(self):
        response = self.receive([started(1, "plan")], run_id="missing")
        self.assertEqual((response.status_code, response.data["retryable"]), (409, True))
        for change in ({"connection_generation": 3}, {"domain": "other.test"},
                       {"website_connection_id": "22222222-2222-2222-2222-222222222222"}):
            with self.subTest(change=change):
                response = self.receive([started(1, "plan")], **change)
                self.assertEqual((response.status_code, response.data["retryable"]), (409, False))
                # No ``code``: a deployed sender must not treat this as a run refusal.
                self.assertNotIn("code", response.data)
        self.run.save.assert_not_called()

    def test_spelling_of_the_binding_does_not_matter(self):
        response = self.receive([started(1, "plan")], connection_generation="2", repository_id="42",
                                website_connection_id=CONNECTION["website_connection_id"].upper(), domain="Example.TEST")
        self.assertEqual(response.status_code, 200)

    def test_closed_runs_and_invalid_batches_write_nothing(self):
        self.run.status = "cancelled"
        self.assertEqual(self.receive([started(1, "plan")]).data["reason"], "run_closed")
        self.run.status = "running"
        for events in ([], "x", [started(1, "plan")] * 101):
            with self.subTest(size=len(events)):
                self.assertEqual(self.receive(events).status_code, 400)
        self.run.save.assert_not_called()


class CallbackRoutingTests(SimpleTestCase):
    def post(self, payload):
        from .service_views import ContentFactoryCallbackView
        view = ContentFactoryCallbackView()
        view._post_legacy = Mock(return_value="legacy")
        with patch("content_factory.run_events.receive_run_events", return_value="events") as receive:
            return view.post(SimpleNamespace(data=payload)), view._post_legacy, receive

    def test_flag_off_keeps_the_legacy_callback_path(self):
        result, legacy, receive = self.post({"event": "run_events", "run_id": "run-1", "events": []})
        self.assertEqual(result, "legacy")
        receive.assert_not_called()

    def test_flag_on_routes_only_run_events(self):
        with override_settings(ARTICLES_RUN_EVENTS_ENABLED=True):
            result, legacy, receive = self.post({"event": "run_events", "run_id": "run-1", "events": []})
            self.assertEqual(result, "events")
            legacy.assert_not_called()
            result, legacy, receive = self.post({"event": "article_progress", "run_id": "run-1"})
            self.assertEqual(result, "legacy")
            receive.assert_not_called()
