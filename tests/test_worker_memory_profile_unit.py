"""Worker telemetry is bounded, private and never a request failure."""

import importlib.util
from io import StringIO
import json
import signal
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from scripts import worker_memory_profile as profile
from scripts import check_worker_memory_smoke as smoke


class WorkerMemoryProfileTests(SimpleTestCase):
    def worker(self):
        rows = []
        def record(message, *args):
            rows.append(message % args if args else message)
        return SimpleNamespace(log=SimpleNamespace(info=record)), rows

    def samples(self, rows):
        return [json.loads(row.split(" ", 1)[1]) for row in rows if row.startswith("worker_memory_profile ")]

    def test_disabled_profile_does_not_read_procfs_or_emit_samples(self):
        worker, rows = self.worker()
        with patch.dict("os.environ", {"GUNICORN_MEMORY_PROFILE_ENABLED": "false"}), patch.object(profile, "read_process_memory") as read:
            profile.start_worker_profile(worker)
            for _ in range(500): profile.record_worker_request(worker)
            profile.finish_worker_profile(worker)
        read.assert_not_called()
        self.assertEqual(rows, [])

    def test_real_hook_signatures_and_opaque_request_arguments(self):
        import gunicorn.config
        path = Path(__file__).parents[1] / "scripts/gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("memory_profile_gunicorn_config", path)
        config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(config)
        installed = gunicorn.config.Config()
        for name in ("post_worker_init", "post_request", "worker_exit"):
            installed.set(name, getattr(config, name))
        worker, rows = self.worker()
        class Opaque:
            def __repr__(self): raise AssertionError("Never inspect request, response, server or environment")
        with patch.dict("os.environ", {"GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": "1"}), patch.object(
            profile, "read_process_memory", return_value={"rss_kib": 1024, "peak_rss_kib": 2048}
        ), patch("django.urls.get_resolver", return_value=SimpleNamespace(url_patterns=[])):
            config.post_worker_init(worker)
            config.post_request(worker, Opaque(), Opaque(), Opaque())
            config.worker_exit(Opaque(), worker)
        self.assertEqual([row["phase"] for row in self.samples(rows)], ["start", "sample", "exit"])
        self.assertEqual([row["completed_requests"] for row in self.samples(rows)], [0, 1, 1])

    def test_interval_samples_original_baseline_and_exit_without_per_request_reads(self):
        worker, rows = self.worker()
        with patch.dict("os.environ", {"GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": "2"}), patch.object(
            profile, "read_process_memory", side_effect=[{"rss_kib": 1000, "peak_rss_kib": 1500},
                {"rss_kib": 3000, "peak_rss_kib": 4000}, {"rss_kib": 2000, "peak_rss_kib": 4000}]
        ) as read:
            profile.start_worker_profile(worker)
            for _ in range(3): profile.record_worker_request(worker)
            profile.finish_worker_profile(worker)
            profile.finish_worker_profile(worker)
        self.assertEqual(read.call_count, 3)
        values = self.samples(rows)
        self.assertEqual([row["completed_requests"] for row in values], [0, 2, 3])
        self.assertEqual([row["rss_delta_kib"] for row in values], [0, 2000, 1000])
        self.assertEqual([row["baseline_completed_requests"] for row in values], [0, 0, 0])
        self.assertTrue(all(set(row) == {"schema_version", "phase", "worker_pid", "completed_requests", "baseline_completed_requests",
            "rss_kib", "peak_rss_kib", "rss_delta_kib"} for row in values))

    def test_unavailable_is_not_zero_and_is_reported_once_without_exception_text(self):
        worker, rows = self.worker()
        with patch.dict("os.environ", {"GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": "1"}), patch.object(
            profile, "read_process_memory", side_effect=[OSError("synthetic-private-path"), None,
                {"rss_kib": 3000, "peak_rss_kib": 4000}]
        ):
            profile.start_worker_profile(worker)
            profile.record_worker_request(worker)
            profile.record_worker_request(worker)
        self.assertEqual(rows.count("worker_memory_profile_unavailable"), 1)
        self.assertNotIn("synthetic-private-path", str(rows))
        values = self.samples(rows)
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["baseline_completed_requests"], 2)
        self.assertEqual(values[0]["rss_kib"], 3000)

    def test_bad_intervals_are_bounded_and_worker_baseline_resets(self):
        for interval in ("0", "-1", "100001", "not-a-number"):
            with self.subTest(interval=interval):
                worker, rows = self.worker()
                with patch.dict("os.environ", {"GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": interval}), patch.object(
                    profile, "read_process_memory", return_value={"rss_kib": 1000, "peak_rss_kib": 2000}
                ) as read:
                    profile.start_worker_profile(worker)
                    for _ in range(99): profile.record_worker_request(worker)
                    self.assertEqual(read.call_count, 1)
                    profile.record_worker_request(worker)
                    self.assertEqual(read.call_count, 2)
                    profile.start_worker_profile(worker)
                    self.assertEqual(self.samples(rows)[-1]["completed_requests"], 0)

    def test_parser_rejects_missing_duplicate_wrong_unit_negative_and_oversized_data(self):
        valid = "Name:\tprivate-name\nVmRSS:\t20480 kB\nVmHWM:\t40960 kB\n"
        self.assertEqual(profile.parse_process_status(valid), {"rss_kib": 20480, "peak_rss_kib": 40960})
        for body in (None, "", valid + "VmRSS: 20480 kB\n", valid.replace("20480", "-1"),
                     valid.replace("kB", "bytes"), valid.replace("20480", "9" * 13),
                     valid.replace("VmRSS:\t", "VmRSS:\n"), valid + "x" * 16_384):
            with self.subTest(body_type=type(body).__name__):
                self.assertIsNone(profile.parse_process_status(body))
        with patch.object(Path, "open", return_value=StringIO(valid)):
            self.assertEqual(profile.read_process_memory(), {"rss_kib": 20480, "peak_rss_kib": 40960})
        with patch.object(Path, "open", side_effect=OSError("synthetic-private-path")):
            self.assertIsNone(profile.read_process_memory())

    def test_broken_optional_logger_does_not_fail_start_request_or_exit(self):
        worker = SimpleNamespace(log=SimpleNamespace(info=Mock(side_effect=RuntimeError("logger unavailable"))))
        with patch.dict("os.environ", {"GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": "1"}), patch.object(
            profile, "read_process_memory", return_value={"rss_kib": 1000, "peak_rss_kib": 2000}
        ):
            profile.start_worker_profile(worker)
            profile.record_worker_request(worker)
            profile.finish_worker_profile(worker)

    def test_smoke_rejects_local_or_unowned_execution_before_starting_a_process(self):
        with patch.dict("os.environ", {"GITHUB_ACTIONS": "false"}), patch.object(smoke.subprocess, "Popen") as process:
            with self.assertRaises(ValueError): smoke.main()
            process.assert_not_called()
        with tempfile.TemporaryDirectory() as root, patch.object(smoke.sys, "platform", "linux"):
            with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "unowned/repository", "RUNNER_TEMP": root}):
                with self.assertRaises(ValueError): smoke.require_ci()
            with patch.dict("os.environ", {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "RUNNER_TEMP": root}):
                self.assertEqual(smoke.require_ci(), Path(root))

    def test_smoke_child_environment_never_inherits_account_or_database_credentials(self):
        with patch.dict("os.environ", {"DATABASE_URL": "synthetic-private-db", "GITHUB_TOKEN": "synthetic-private-key", "OPENAI_API_KEY": "synthetic-model-key"}):
            value = smoke.child_environment()
        self.assertEqual(set(value), {"PATH", "PYTHON_DOTENV_DISABLED", "PYTHONUNBUFFERED",
            "GUNICORN_MEMORY_PROFILE_ENABLED", "GUNICORN_MEMORY_PROFILE_INTERVAL"})
        self.assertNotIn("synthetic-private", str(value))

    def test_smoke_accepts_only_closed_actual_recycle_events_without_request_data(self):
        rows = []
        for pid in range(1, 5):
            for phase, count in (("start", 0), ("sample", 2), ("exit", 8)):
                rows.append({"schema_version": 1, "phase": phase, "worker_pid": pid, "completed_requests": count,
                    "baseline_completed_requests": 0, "rss_kib": 1024 + count, "peak_rss_kib": 2048 + count, "rss_delta_kib": count})
        def encoded(values): return "\n".join("[INFO] worker_memory_profile " + json.dumps(row) for row in values)
        report = smoke.validate_samples(encoded(rows), completed=32, attempted=32)
        self.assertEqual(report["worker_starts"], 4)
        self.assertEqual(report["completed_requests"], 32)
        for invalid in (encoded(rows) + smoke.CANARY, encoded([{**row, "url": "/private"} for row in rows]),
                        encoded([row for row in rows if row["phase"] != "exit"]),
                        encoded([row for row in rows if row["phase"] != "sample"]),
                        encoded([{**row, "rss_kib": True} for row in rows])):
            with self.assertRaises(ValueError): smoke.validate_samples(invalid, completed=32, attempted=32)

    def test_smoke_timeout_cleans_only_its_owned_process_group(self):
        process = Mock(pid=123456)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), None]
        with patch.object(smoke.os, "killpg") as kill:
            smoke.stop(process)
        process.terminate.assert_called_once_with()
        kill.assert_called_once_with(123456, signal.SIGKILL)
        self.assertEqual(process.wait.call_count, 2)
