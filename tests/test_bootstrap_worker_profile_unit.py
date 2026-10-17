"""Fixture admission, closed metrics and teardown without workers or services."""

from copy import deepcopy
from contextlib import ExitStack
import ast
import gc
from io import StringIO
import json
from pathlib import Path
import runpy
import subprocess
import weakref
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from scripts import bootstrap_worker_profile as profile


DATABASE = {"ENGINE": "django.db.backends.postgresql", "NAME": "test_mlai_test", "HOST": "127.0.0.1", "PORT": "5432",
    "USER": "mlai", "PASSWORD": "mlai_test_password"}


def events(counts=(300, 300)):
    rows = []
    for pid, completed in enumerate(counts, 100):
        for phase, count in [("start", 0), *[("sample", index) for index in range(100, completed + 1, 100)], ("exit", completed)]:
            rows.append({"schema_version": 1, "phase": phase, "worker_pid": pid, "completed_requests": count,
                "baseline_completed_requests": 0, "rss_kib": 10000 + count, "peak_rss_kib": 11000 + count, "rss_delta_kib": count})
    return rows


def log(rows):
    return "\n".join("worker_memory_profile " + json.dumps(row) for row in rows)


class BootstrapWorkerProfileTests(TestCase):
    def test_fixture_memory_guard_accepts_measured_post_fix_and_rejects_pre_fix_windows(self):
        def measured(peaks, growth):
            return [{"max_requests": limit, "jitter": jitter, "max_peak_rss_kib": peak,
                "worker_lifetimes": [{"requests": requests, "growth_after_100_kib": delta}]}
                for (limit, jitter, requests), peak, delta in zip(((300, 50, 304), (1000, 100, 1054)), peaks, growth)]
        post = measured((347216, 347248), (5188, 5260))
        self.assertIsNone(profile.validate_application_memory(post))
        with self.assertRaises(ValueError):
            profile.validate_application_memory(measured((2934652, 3051384), (685236, 803208)))
        changed = deepcopy(post)
        changed[1]["max_peak_rss_kib"] = post[0]["max_peak_rss_kib"] + profile.MAX_WARM_GROWTH_KIB + 1
        with self.assertRaises(ValueError): profile.validate_application_memory(changed)
        changed = deepcopy(post)
        changed[1]["worker_lifetimes"][0]["growth_after_100_kib"] = profile.MAX_WARM_GROWTH_KIB + 1
        with self.assertRaises(ValueError): profile.validate_application_memory(changed)

    def test_fixture_memory_guard_never_treats_missing_or_boolean_metrics_as_zero(self):
        valid = [{"max_requests": limit, "jitter": jitter, "max_peak_rss_kib": 350000,
            "worker_lifetimes": [{"requests": 100, "growth_after_100_kib": 1000}]}
            for limit, jitter in ((300, 50), (1000, 100))]
        for field, value in (("max_peak_rss_kib", None), ("max_peak_rss_kib", True),
                ("worker_lifetimes", []), ("worker_lifetimes", [{"requests": 100, "growth_after_100_kib": None}]),
                ("worker_lifetimes", [{"requests": 100, "growth_after_100_kib": True}])):
            changed = deepcopy(valid)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError): profile.validate_application_memory(changed)
        for changed in (None, [], valid[:1], list(reversed(valid))):
            with self.assertRaises(ValueError): profile.validate_application_memory(changed)

    def test_local_or_unowned_runs_fail_before_starting_any_process(self):
        with patch.object(profile, "require_ci", side_effect=ValueError("CI only")), patch.object(profile.subprocess, "Popen") as process:
            with self.assertRaises(ValueError): profile.profile_window(database=DATABASE, user_id=1, limit=300, jitter=50)
            process.assert_not_called()

    def test_child_receives_only_explicit_synthetic_environment_and_existing_test_database(self):
        with TemporaryDirectory() as scratch, patch.object(profile, "require_ci", return_value=Path(scratch)), patch.dict(profile.os.environ,
            {"ARTICLES_BOOTSTRAP_MEMORY_CI": "1", "GITHUB_SHA": "a" * 40, "OPENAI_API_KEY": "synthetic-forbidden-provider", "GH_TOKEN": "synthetic-forbidden-token"}, clear=True):
            value = profile.child_environment(database=DATABASE, user_id=4)
            self.assertNotIn("OPENAI_API_KEY", value)
            self.assertNotIn("GH_TOKEN", value)
            self.assertEqual(value["BOOTSTRAP_MEMORY_USER_ID"], "4")
            for changes in ({"NAME": "mlai_test"}, {"NAME": "production"}, {"HOST": "outside.test"}, {"USER": "other"}):
                with self.subTest(fields=sorted(changes)), self.assertRaises(ValueError):
                    profile.child_environment(database={**DATABASE, **changes}, user_id=4)
            with self.assertRaises(ValueError): profile.child_environment(database=DATABASE, user_id=True)

    def test_realistic_sample_windows_export_numeric_lifetimes_without_worker_ids_or_raw_data(self):
        value = profile.summarize(log(events()), completed=600, attempted=600, limit=300, jitter=50)
        self.assertEqual(value["completed_requests"], 600)
        self.assertEqual([row["requests"] for row in value["worker_lifetimes"]], [300, 300])
        self.assertNotIn("worker_pid", json.dumps(value))
        self.assertEqual(value["worker_lifetimes"][0]["growth_after_100_kib"], 200)
        self.assertEqual([row["completed_requests"] for row in value["worker_lifetimes"][0]["samples"]], [100, 200, 300])
        self.assertTrue(all(set(row) == {"completed_requests", "rss_kib", "peak_rss_kib"}
            for life in value["worker_lifetimes"] for row in life["samples"]))

    def test_incomplete_or_duplicated_lifecycle_samples_and_changed_metrics_are_rejected(self):
        original = events()
        variants = [original[:-1], original[1:], [row for row in original if not (row["phase"] == "sample" and row["completed_requests"] == 100)],
            [*original, original[-1]], [{**original[0], "private": "private"}, *original[1:]]]
        for field, value in (("rss_kib", True), ("rss_delta_kib", 99), ("worker_pid", 0), ("baseline_completed_requests", 1)):
            changed = deepcopy(original)
            changed[0][field] = value
            variants.append(changed)
        for rows in variants:
            with self.subTest(rows=len(rows)), self.assertRaises(ValueError):
                profile.summarize(log(rows), completed=600, attempted=600, limit=300, jitter=50)
        with self.assertRaises(ValueError): profile.summarize(log(original) + profile.CANARY, completed=600, attempted=600, limit=300, jitter=50)

    def test_nonrecycled_or_unaccounted_windows_are_refused(self):
        for rows, completed in ((events((200, 200)), 400), (events(), 601)):
            with self.assertRaises(ValueError): profile.summarize(log(rows), completed=completed, attempted=completed, limit=300, jitter=50)

    def test_teardown_stops_owned_group_even_if_master_exited_early_and_kills_on_timeout(self):
        process = Mock(pid=4321)
        with patch.object(profile.os, "killpg") as kill:
            profile.stop_group(process)
            kill.assert_called_once_with(4321, profile.signal.SIGTERM)
        process.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
        with patch.object(profile.os, "killpg") as kill:
            profile.stop_group(process)
            self.assertEqual([call.args for call in kill.call_args_list], [(4321, profile.signal.SIGTERM), (4321, profile.signal.SIGKILL)])

    def test_memory_ceiling_uses_known_bounds_and_retains_unavailable(self):
        def read(path, *args, **kwargs):
            return StringIO("1048576" if str(path).endswith("memory.max") else "MemTotal: 4096 kB\n" if str(path) == "/proc/meminfo" else "max")
        with patch.object(profile.Path, "open", read): self.assertEqual(profile.memory_ceiling_bytes(), 1048576)
        with patch.object(profile.Path, "open", side_effect=OSError): self.assertIsNone(profile.memory_ceiling_bytes())

    def test_test_only_wsgi_entry_point_refuses_local_import_before_django_or_database(self):
        with patch.dict(profile.os.environ, {}, clear=True), patch("django.setup", side_effect=AssertionError("Must refuse before setup")), self.assertRaises(ValueError):
            runpy.run_path(str(profile.ROOT / "tests/fixtures/gunicorn_bootstrap_memory_app.py"))

    def test_driver_owns_process_group_uses_private_unix_socket_and_cleans_up_after_success_or_failure(self):
        for success in (True, False):
            with TemporaryDirectory() as scratch:
                process = Mock(pid=4321)
                process.poll.return_value = None
                response = Mock(status=200 if success else 500)
                response.read.return_value = b'{"runs":24,"bytes":200000}'
                client = Mock()
                client.getresponse.return_value = response
                env = {"RUNNER_TEMP": scratch, "ONLY_SYNTHETIC": "fixture"}
                with (patch.object(profile, "child_environment", return_value=env), patch.object(profile, "REQUESTS", 2),
                    patch.object(profile.subprocess, "Popen", return_value=process) as create,
                    patch.object(profile.socket, "socket"), patch.object(profile.http.client, "HTTPConnection", return_value=client),
                    patch.object(profile, "stop_group") as stop, patch.object(profile, "summarize", return_value={"fixture": "complete"}) as summarize):
                    if success:
                        result = profile.profile_window(database=DATABASE, user_id=1, limit=300, jitter=50)
                        self.assertEqual(result["fixture"], "complete")
                        self.assertGreaterEqual(result["elapsed_ms"], 0)
                        self.assertEqual(summarize.call_args.kwargs["completed"], 2)
                    else:
                        with self.assertRaisesRegex(ValueError, "^Bootstrap worker response failed$"):
                            profile.profile_window(database=DATABASE, user_id=1, limit=300, jitter=50)
                        summarize.assert_not_called()
                    stop.assert_called_once_with(process)
                    command = create.call_args.args[0]
                    self.assertIn("unix:", next(item for item in command if item.startswith("unix:")))
                    self.assertEqual(create.call_args.kwargs["env"], env)
                    self.assertIs(create.call_args.kwargs["start_new_session"], True)
                    self.assertEqual(command[command.index("--workers") + 1], "1")
                self.assertEqual(list(Path(scratch).iterdir()), [])

    def test_application_profile_is_a_required_deployment_gate_with_only_synthetic_fixture_configuration(self):
        import yaml
        workflow = yaml.safe_load((profile.ROOT / ".github/workflows/deploy.yml").read_text())
        jobs = workflow["jobs"]
        self.assertIn("bootstrap-worker-profile", jobs["deploy"]["needs"])
        self.assertIn("needs.bootstrap-worker-profile.result == 'success'", jobs["deploy"]["if"])
        fixture = jobs["bootstrap-worker-profile"]
        self.assertNotIn("secrets.", json.dumps(fixture))
        self.assertEqual(fixture["env"]["ARTICLES_BOOTSTRAP_MEMORY_CI"], "1")
        commands = [step.get("run", "") for step in fixture["steps"]]
        self.assertTrue(any("scripts/test_without_database.py tests.test_bootstrap_worker_profile_unit" in command for command in commands))
        self.assertTrue(any("manage.py test tests.test_bootstrap_worker_memory_profile" in command for command in commands))

    def test_actual_fixture_throttle_override_does_not_retain_completed_requests(self):
        source = profile.ROOT / "tests/fixtures/gunicorn_bootstrap_memory_app.py"
        tree = ast.parse(source.read_text())
        helpers = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "skip_fixture_throttles"]
        overrides = [node for node in tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
            and any(isinstance(child, ast.Name) and child.id == "APIView" for child in ast.walk(node))]
        self.assertEqual(len(overrides), 1)
        class View:
            def check_throttles(self, request): raise AssertionError("Fixture must bypass throttling")
        class Request:
            pass
        with ExitStack() as boundaries:
            namespace = {"_boundaries": boundaries, "patch": patch, "APIView": View}
            exec(compile(ast.Module(body=[*helpers, *overrides], type_ignores=[]), str(source), "exec"), namespace)
            view = View()
            request = Request()
            reference = weakref.ref(request)
            self.assertIsNone(view.check_throttles(request))
            del request
            gc.collect()
            self.assertIsNone(reference(), "Benchmark hooks must not retain request/response object graphs")
