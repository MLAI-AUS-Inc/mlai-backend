"""No-I/O telemetry regressions: foreground never waits for a cache sink."""
from queue import Queue
from threading import Event, Lock, Thread, get_ident
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings

from integrations.services.message_sync import telemetry
from integrations.services.message_sync.slack_client import budgeted_client


class AsyncTelemetryTests(SimpleTestCase):
    def test_concurrent_first_record_drops_without_waiting_for_startup_lock(self):
        startup_lock, returned = Lock(), Event()
        startup_lock.acquire()

        def record():
            telemetry.record("synthetic", "admitted")
            returned.set()

        with patch.multiple(telemetry, _startup_lock=startup_lock, _queue=None,
                            _worker_thread=None), patch.object(telemetry, "Thread") as worker:
            caller = Thread(target=record, daemon=True)
            caller.start()
            try:
                self.assertTrue(returned.wait(5), "counter waited for concurrent daemon startup")
                worker.assert_not_called()
                self.assertIsNone(telemetry._queue)
            finally:
                startup_lock.release()
                caller.join(timeout=5)
        self.assertFalse(caller.is_alive())

    def test_stalled_cache_sink_does_not_block_callers_and_queue_has_a_hard_limit(self):
        queue = Queue(maxsize=2)
        blocked, release = Event(), Event()
        written, thread_ids = [], []

        def stalled_sink(batch):
            thread_ids.append(get_ident())
            blocked.set()
            if not release.wait(5):
                raise AssertionError("Test did not release telemetry sink")
            written.extend(batch)

        worker = Thread(target=telemetry._drain, args=(queue,), daemon=True)
        with patch.object(telemetry, "_write_batch", side_effect=stalled_sink), patch.object(
            telemetry, "_get_queue", return_value=queue
        ):
            worker.start()
            try:
                telemetry.record("synthetic", "admitted")
                self.assertTrue(blocked.wait(5))
                # These calls return while the sink is still blocked. The fourth
                # increment is discarded instead of waiting or growing memory.
                for _ in range(3):
                    telemetry.record("synthetic", "admitted")
                self.assertEqual(queue.qsize(), 2)
                self.assertFalse(release.is_set())
            finally:
                release.set()
                queue.put(None, timeout=5)
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(written), 3)
        self.assertTrue(all(thread_id != get_ident() for thread_id in thread_ids))

    def test_failed_sink_discards_batch_and_next_batch_can_continue(self):
        queue = Queue()
        for _ in range(telemetry.MAX_BATCH_COUNTERS + 1):
            queue.put(("synthetic", "admitted", 1, 1, 0))
        queue.put(None)
        with patch.object(telemetry, "_write_batch", side_effect=[RuntimeError("cache unavailable"), None]) as sink:
            telemetry._drain(queue)
        self.assertEqual([len(call.args[0]) for call in sink.call_args_list],
                         [telemetry.MAX_BATCH_COUNTERS, 1])
        self.assertEqual(queue.unfinished_tasks, 0)

    def test_batch_coalesces_repeated_counters_before_cache_io(self):
        sink = MagicMock()
        deltas = [("synthetic", "admitted", 1, 61, 1)] * 100
        with patch.object(telemetry, "cache", sink):
            telemetry._write_batch(deltas)
        self.assertEqual(sink.add.call_count, 2)
        self.assertEqual(sink.incr.call_count, 2)
        self.assertEqual([call.args[1] for call in sink.incr.call_args_list], [100, 100])

    def test_fork_pid_change_replaces_inherited_locked_state(self):
        inherited_lock, inherited_queue = Lock(), Queue()
        inherited_lock.acquire()
        returned, finished = [], Event()

        def get_queue():
            returned.append(telemetry._get_queue())
            finished.set()

        with patch.multiple(telemetry, _pid=-1, _startup_lock=inherited_lock,
                            _queue=inherited_queue, _worker_thread=MagicMock()):
            caller = Thread(target=get_queue, daemon=True)
            caller.start()
            try:
                self.assertTrue(finished.wait(5), "child attempted to acquire inherited startup lock")
            finally:
                inherited_lock.release()
                caller.join(timeout=5)
                if returned:
                    returned[0].put(None, timeout=5)
                    telemetry._worker_thread.join(timeout=5)
            self.assertIsNot(returned[0], inherited_queue)
            self.assertEqual(returned[0].maxsize, telemetry.MAX_PENDING_COUNTERS)
            self.assertIs(telemetry._get_queue(), returned[0])

    def test_request_timestamp_is_captured_before_deferred_worker_write(self):
        queued = []
        with patch.object(telemetry, "time", return_value=3661), patch.object(telemetry, "_enqueue", side_effect=queued.append):
            telemetry.record("synthetic", "admitted")
        self.assertEqual(queued, [("synthetic", "admitted", 1, 61, 1)])

    @override_settings(MESSAGE_SYNC_ENABLED=True, MESSAGE_SYNC_SLACK_APP_ID="ATEST")
    def test_request_duration_excludes_finished_telemetry_work(self):
        clock = [10.0]
        values = {}
        client = MagicMock()

        def provider(*_args, **_kwargs):
            clock[0] += .2
            return {"ok": True}

        def record(_scope, counter, amount=1):
            if counter == "finished":
                clock[0] += 10  # Intentionally exaggerated instrumentation delay.
            values[counter] = amount

        client.api_call.side_effect = provider
        with patch("integrations.services.message_sync.slack_client.admit_request"), patch(
            "integrations.services.message_sync.slack_client.time.monotonic", side_effect=lambda: clock[0]
        ), patch.object(telemetry, "record", side_effect=record):
            result = budgeted_client(client, workspace_id="TTEST").api_call("conversations.history")
        self.assertTrue(result["ok"])
        self.assertEqual(values["request_ms"], 200)


class MinuteCoverageTests(SimpleTestCase):
    def setUp(self):
        telemetry.cache.clear()

    def test_partial_minute_window_has_explicit_missing_coverage(self):
        telemetry._write_batch([("synthetic", "admitted", 10, 1, 0)])
        result = telemetry.snapshot(["synthetic", "missing"], minutes=3, now=240)
        self.assertEqual(result["observed_minutes"], {"synthetic": 1, "missing": 0})
        self.assertEqual(result["scopes"]["synthetic"]["admitted"], 10)
        self.assertIsNone(result["scopes"]["missing"])
        self.assertIsNone(telemetry.snapshot(["synthetic"], minutes=1, now=61)["scopes"]["synthetic"])


class OpenLatencyTests(SimpleTestCase):
    def setUp(self):
        telemetry.cache.clear()

    def test_latency_records_minute_and_hour_buckets_without_identifiers(self):
        queued = []
        with patch.object(telemetry, "time", return_value=3661), patch.object(telemetry, "_enqueue", side_effect=queued.append):
            telemetry.record_latency("open_presented", 120001)
            telemetry.record_latency("open_presented", 30000)
        telemetry._write_batch(queued)
        result = telemetry.latency_snapshot(minutes=1, now=3720)
        self.assertEqual(result["kinds"]["open_presented"], {
            "count": 2, "sum_ms": 150001, "over_30s": 1, "over_120s": 1,
        })
        hourly = telemetry.latency_snapshot(hours=1, now=7200)
        self.assertEqual(hourly["kinds"], result["kinds"])
        self.assertIsNone(result["kinds"]["open_ready"])
        self.assertIsNone(telemetry.latency_snapshot(minutes=1, now=3661)["kinds"]["open_presented"])

    def test_latency_rejects_unknown_kind_and_sink_failure_never_stalls(self):
        with self.assertRaises(ValueError):
            telemetry.record_latency("private source", 1)
        with patch.object(telemetry, "_enqueue", side_effect=RuntimeError()):
            telemetry.record_latency("open_ready", 100)


class OpenStatusTests(SimpleTestCase):
    def test_status_sections_are_content_free_and_expired_requests_are_ignored(self):
        from integrations.management.commands.message_sync_status import summarize_open_discovery
        from integrations.services.message_sync.discovery import KEY as DISCOVERY_KEY
        from integrations.services.slack_open_requests import KEY as OPEN_KEY
        cursors = [{DISCOVERY_KEY: {"expires": 110, "error": "BudgetDeferred"}, OPEN_KEY: {
            "device:private": {"state": "pending", "until": 200, "requested_at": 60},
            "other": {"state": "importing", "until": 200, "requested_at": 80},
            "expired": {"state": "pending", "until": 99, "requested_at": 1},
        }}, {DISCOVERY_KEY: {"error": "TimeoutError"}}, {DISCOVERY_KEY: {"error": "private body"}}]
        discovery, opens = summarize_open_discovery(cursors, now=100)
        self.assertEqual(discovery, {"leased_grants": 1, "budget_deferred_grants": 1, "error_codes": {"TimeoutError": 1, "sync_error": 1}})
        self.assertEqual(opens, {"pending": 1, "importing": 1, "error": 0, "oldest_pending_age_seconds": 40, "oldest_importing_age_seconds": 20})
        self.assertNotIn("private", str((discovery, opens)))
