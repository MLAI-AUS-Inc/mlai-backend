"""Execute deployed validators without credentials, services, or a database."""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts.validate_website_deploy_config import canonical_migration_plan, validate as validate_website, validate_single


DEPLOY_SCRIPT = (Path(__file__).resolve().parents[1] / "deploy.sh").read_text()
REMOTE_HEREDOC_START = 'ssh "$DEPLOY_SSH_TARGET" <<EOF | tee "$remote_deploy_log"\n'


def _remote_block(start, end):
    begin = DEPLOY_SCRIPT.index(start)
    finish = DEPLOY_SCRIPT.index(end, begin) + len(end)
    return DEPLOY_SCRIPT[begin:finish].replace("\\$", "$")


def _rendered_remote_script(release):
    # Let bash expand the unquoted heredoc as the runner does, so the text
    # under test is exactly what SSH sends to the host.
    body = DEPLOY_SCRIPT.split(REMOTE_HEREDOC_START, 1)[1].split("\nEOF\n", 1)[0]
    return subprocess.run(
        ["bash", "-c", "cat <<EOF\n" + body + "\nEOF\n"],
        env={
            "PATH": os.environ["PATH"],
            "APP_RELEASE": release,
            "remote_deploy_completed": f"Remote deployment script completed for {release}.",
        },
        text=True, capture_output=True, check=True, timeout=5,
    ).stdout


class DeploymentRuntimeChecksTests(unittest.TestCase):

    def test_migration_free_release_keeps_runtime_online_during_checks(self):
        # Execute the deployment's actual decision block with stubbed Django
        # commands. No database or production service is involved.
        start = '    echo "🔎 Checking pending migrations before pausing runtime services..."'
        decision = DEPLOY_SCRIPT.split(start, 1)[1].split(
            "    # Recovery disables errexit", 1
        )[0]
        decision = (start + decision).replace("\\$", "$")
        plan = "Planned operations:\napp.0001_example\n    Example"
        approved_hash = hashlib.sha256(plan.encode()).hexdigest()
        for check_result, plan_result, approval, expected_exit, expected_pending, expected_calls in (
            (0, 0, "", 0, "0", "check,"),
            (1, 0, "", 1, None, "check,plan,"),
            (1, 0, "wrong-plan-hash", 1, None, "check,plan,"),
            (1, 0, hashlib.sha256((plan + " changed").encode()).hexdigest(), 1, None, "check,plan,"),
            (1, 0, approved_hash, 0, "1", "check,plan,"),
            (1, 2, approved_hash, 2, None, "check,plan,"),
        ):
            with self.subTest(check_result=check_result, approval=approval, plan_result=plan_result):
                script = "\n".join([
                    "set -euo pipefail",
                    # This test exercises the steady-state migration gate;
                    # first proxy adoption has its own fail-closed check.
                    "web_proxy_preexisting=1",
                    'calls_file="$(mktemp)"',
                    "compose_run_web() {",
                    "  case \" $* \" in",
                    "    *' --check '*) printf check, >> \"$calls_file\"; return " + str(check_result) + ";;",
                    "    *' --plan '*) printf plan, >> \"$calls_file\"; printf '%s\\n' " + shlex.quote(plan) + "; return " + str(plan_result) + ";;",
                    "  esac",
                    "}",
                    # Production uses GNU sha256sum; use Python in the shell
                    # harness so macOS exercises the same hash/approval gate.
                    'sha256sum() { ' + shlex.quote(sys.executable)
                    + ' -c "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"; }',
                    'read_env_value() { echo "' + approval + '"; }',
                    'trap \'printf "calls=%s\\n" "$(cat "$calls_file")"; rm -f "$calls_file"\' EXIT',
                    decision,
                    'printf "pending=%s\\n" "$migrations_pending"',
                ])
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                self.assertIn(f"calls={expected_calls}", result.stdout)
                if expected_pending is not None:
                    self.assertIn(f"pending={expected_pending}", result.stdout)
                else:
                    self.assertNotIn("pending=", result.stdout)

    def test_reviewed_multiline_migration_plan_uses_exact_shell_digest(self):
        start = '    echo "🔎 Checking pending migrations before pausing runtime services..."'
        decision = (start + DEPLOY_SCRIPT.split(start, 1)[1].split("    # Recovery disables errexit", 1)[0]).replace("\\$", "$")
        plan = "Planned operations:\ncontent_factory.0042_website_connection_lifecycle\n    Create model WebsiteConnection"
        for reviewed_plan, expected in ((plan, 0), (plan + "\n    Create model Unreviewed", 1)):
            approved = hashlib.sha256(reviewed_plan.encode()).hexdigest()
            script = "\n".join([
                "set -euo pipefail", "web_proxy_preexisting=1",
                "compose_run_web() { case \" $* \" in *' --check '*) return 1;; *' --plan '*) printf '%s\\n' " + shlex.quote(plan) + ";; esac; }",
                'sha256sum() { ' + shlex.quote(sys.executable) + ' -c "import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())"; }',
                'read_env_value() { printf "%s" "' + approved + '"; }', decision,
            ])
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, expected, result.stderr)

    def test_container_probes_do_not_consume_remaining_ssh_script(self):
        release_probe = next(
            line for line in DEPLOY_SCRIPT.splitlines()
            if line.strip().startswith("running_release=")
        )
        sync_probes = [
            line for line in DEPLOY_SCRIPT.splitlines()
            if "docker compose exec -T bridge-worker" in line
            and "message_sync_status" in line
        ]
        probes = [release_probe, *[
            probe + "\n:; fi" if probe.strip().startswith("if ") else probe
            for probe in sync_probes
        ]]
        self.assertEqual(len(sync_probes), 2)
        for probe in probes:
            with self.subTest(probe=probe):
                # SSH feeds bash via stdin. Simulate Docker attaching to that
                # stream: later deployment checks must still be executed.
                script = "\n".join([
                    "set -euo pipefail",
                    "docker() { cat >/dev/null; }",
                    probe.replace("\\$", "$"),
                    "echo subsequent-health-check-executed",
                ])
                result = subprocess.run(
                    ["bash"], input=script, text=True, capture_output=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.strip(), "subsequent-health-check-executed"
                )

    def test_final_sync_check_preserves_later_release_checks_and_candidate_cleanup(self):
        start = '    if [ "\\$bridge_worker_enabled" = "1" ]; then\n        echo "🌉 Verifying the current member sync worker..."'
        tail = (start + DEPLOY_SCRIPT.split(start, 1)[1].split("\n} </dev/null\nEOF", 1)[0]).replace("\\$", "$")
        for sync_status in (0, 23):
            with self.subTest(sync_status=sync_status), tempfile.TemporaryDirectory() as directory:
                sandbox = Path(directory)
                calls_file = sandbox / "calls"
                manifest = sandbox / "rollback-manifest"
                manifest.write_text("synthetic prior image")
                script = "\n".join([
                    "set -euo pipefail",
                    "bridge_worker_enabled=1; migrations_pending=0; web_candidate_started=1",
                    "web_candidate_drain_workers=synthetic-worker; rollback_tags=(synthetic-image)",
                    "calls_file=" + shlex.quote(str(calls_file)),
                    "rollback_manifest=" + shlex.quote(str(manifest)),
                    "remote_deploy_completed=remote-release-tail-completed",
                    "docker() {",
                    "  printf 'docker %s\\n' \"$*\" >> \"$calls_file\"",
                    "  case \" $* \" in",
                    "    *' message_sync_status '*) cat >/dev/null; return " + str(sync_status) + ";;",
                    "    *' compose exec '*) cat >/dev/null; printf '/static/admin/base.css\\n';;",
                    "  esac",
                    "}",
                    "curl() {",
                    "  printf 'curl %s\\n' \"$*\" >> \"$calls_file\"",
                    "  while [ \"$#\" -gt 0 ]; do",
                    "    if [ \"$1\" = '-D' ]; then printf 'access-control-allow-origin: https://mlai.au\\n' > \"$2\"; shift; fi",
                    "    shift",
                    "  done",
                    "}",
                    "wait_for_nginx_workers_to_drain() { echo drained >> \"$calls_file\"; }",
                    # Execute the actual remote tail through stdin, as SSH does.
                    # Docker deliberately drains any inherited command stream.
                    # The tail's own last line reports completion.
                    tail,
                ])
                result = subprocess.run(["bash"], input=script, text=True,
                    capture_output=True, timeout=5)
                self.assertEqual(result.returncode, sync_status, result.stderr)
                calls = calls_file.read_text()
                if sync_status:
                    self.assertNotIn("curl ", calls)
                    self.assertTrue(manifest.exists())
                    self.assertNotIn("remote-release-tail-completed", result.stdout)
                else:
                    self.assertIn("/admin/login/", calls)
                    self.assertIn("/static/admin/base.css", calls)
                    self.assertIn("/uploads/video/session/", calls)
                    self.assertIn("drained", calls)
                    self.assertIn("docker compose stop web-candidate", calls)
                    self.assertIn("docker compose rm -f web-candidate", calls)
                    self.assertFalse(manifest.exists())
                    self.assertIn("remote-release-tail-completed", result.stdout)

    def test_host_commands_cannot_read_the_remote_script_from_ssh_stdin(self):
        # On 25 Oct 2026 the bridge-worker probe read the rest of this script
        # from SSH stdin. The remote shell exited 0 without the release tail,
        # so the migration writer pause marker was never removed. Feed the
        # whole rendered script to bash on stdin, as SSH does. Its first host
        # command records what it can read; the stub-only PATH then stops the
        # script before any other host command can run.
        remote = _rendered_remote_script("a" * 40)
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            bin_dir = sandbox / "bin"
            bin_dir.mkdir()
            captured = sandbox / "git-stdin"
            git = bin_dir / "git"
            git.write_text('#!/bin/sh\n/bin/cat > "$CAPTURED_STDIN"\n')
            git.chmod(0o755)
            result = subprocess.run(
                [shutil.which("bash")], input=remote, text=True, capture_output=True,
                cwd=sandbox, timeout=10,
                env={"PATH": str(bin_dir), "HOME": str(sandbox), "CAPTURED_STDIN": str(captured)},
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("Cannot verify current main from the host", result.stderr)
            self.assertTrue(captured.exists(), "The host main check is no longer the first host command.")
            self.assertEqual(captured.read_text(), "")
            self.assertNotIn("Remote deployment script completed", result.stdout)

    def test_runner_fails_when_the_remote_script_stops_before_its_last_line(self):
        runner = DEPLOY_SCRIPT[
            DEPLOY_SCRIPT.index("remote_deploy_log=$(mktemp)"):
            DEPLOY_SCRIPT.index("# Independently verify the release outside SSH")
        ]
        release = "a" * 40
        completed = f"Remote deployment script completed for {release}."
        probe = "🌉 Verifying the current member sync worker...\n"
        for remote_output, remote_status, expected_status in (
            (probe + completed + "\n", 0, 0),
            # The 25 Oct 2026 run: the probe swallowed the script tail and the
            # remote shell exited 0 with no further output.
            (probe + '{"enabled": true, "stale_lanes": []}\n', 0, 1),
            (f"Remote deployment script completed for {'b' * 40}.\n", 0, 1),
            ("+ " + completed + "\n", 0, 1),
            (probe, 7, 7),
        ):
            with self.subTest(remote_output=remote_output, remote_status=remote_status), \
                    tempfile.TemporaryDirectory() as directory:
                sent = Path(directory) / "sent-script"
                script = "\n".join([
                    # deploy.sh also sets -u; this harness leaves the runner
                    # settings the heredoc expands undefined.
                    "set -eo pipefail",
                    f"APP_RELEASE={release}",
                    "DEPLOY_SSH_TARGET=synthetic-host",
                    "ssh() { cat > " + shlex.quote(str(sent)) + '; printf "%s" "$REMOTE_OUTPUT"; return "$REMOTE_STATUS"; }',
                    runner,
                    "echo runner-reached-release-check",
                ])
                result = subprocess.run(
                    ["bash", "-c", script], text=True, capture_output=True, timeout=10,
                    env={**os.environ, "TMPDIR": directory,
                         "REMOTE_OUTPUT": remote_output, "REMOTE_STATUS": str(remote_status)},
                )
                self.assertEqual(result.returncode, expected_status, result.stderr)
                self.assertEqual("runner-reached-release-check" in result.stdout, expected_status == 0)
                self.assertEqual("ended before its final step" in result.stderr, expected_status == 1)
                # The output log is removed on every exit path.
                self.assertEqual([path.name for path in Path(directory).iterdir()], ["sent-script"])
                sent_lines = sent.read_text().splitlines()
                self.assertEqual(sent_lines[:2], ["{", "    set -euo pipefail"])
                self.assertEqual(sent_lines[-2:], [f'    echo "{completed}"', "} </dev/null"])

    def test_stale_writer_pause_is_reported_with_its_release_and_time(self):
        describe = _remote_block("    describe_writer_pause_sentinel() {", "\n    }")
        pause = _remote_block("    pause_runtime_writers_for_migration() {", "\n    }")
        transition = _remote_block(
            '    if [ "\\$migrations_pending" = "1" ]; then\n        verify_current_main_release_on_host',
            "\n    fi\n",
        )
        stale_release = "3cfbea590f9a102ebc1b7092c3fb8e1b04469810"
        paused_at = datetime(2026, 10, 25, 2, 33, 46, tzinfo=timezone.utc).timestamp()
        for pending, marker_present in ((0, True), (0, False), (1, True)):
            with self.subTest(pending=pending, marker_present=marker_present), \
                    tempfile.TemporaryDirectory() as directory:
                sandbox = Path(directory)
                marker = sandbox / "writers-paused"
                calls = sandbox / "calls"
                if marker_present:
                    marker.write_text(stale_release + "\n")
                    os.utime(marker, (paused_at, paused_at))
                script = "\n".join([
                    "set -euo pipefail",
                    "APP_RELEASE=" + "a" * 40,
                    f"migrations_pending={pending}",
                    "writer_pause_sentinel=" + shlex.quote(str(marker)),
                    "calls_file=" + shlex.quote(str(calls)),
                    "verify_current_main_release_on_host() { :; }",
                    'compose_run_web() { printf "compose_run_web %s\\n" "$*" >> "$calls_file"; }',
                    'docker() { printf "docker %s\\n" "$*" >> "$calls_file"; }',
                    'systemctl() { printf "systemctl %s\\n" "$*" >> "$calls_file"; }',
                    describe,
                    pause,
                    transition,
                ])
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, timeout=5)
                details = f"{marker} (release {stale_release}, written 2026-10-25 02:33:46 UTC)"
                recorded = calls.read_text() if calls.exists() else ""
                if pending:
                    # The migration deploy refuses before it touches a writer
                    # or overwrites the earlier release's marker.
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn("A prior migration writer pause is still active", result.stderr)
                    self.assertIn(details, result.stderr)
                    self.assertNotIn("Running migrations", result.stdout)
                    self.assertEqual(recorded, "")
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(recorded, "compose_run_web python manage.py migrate --check --noinput\n")
                    warning = f"::warning title=Stale migration writer pause::{details} is still present."
                    self.assertEqual(warning in result.stdout, marker_present, result.stdout)
                    self.assertEqual("::warning" in result.stdout, marker_present)
                self.assertEqual(marker.read_text() if marker.exists() else None,
                                 stale_release + "\n" if marker_present else None)

    def test_recovery_preserves_failure_and_stops_deployment(self):
        # Use the actual trap after the outer SSH heredoc removes its escapes.
        trap = re.search(r"^    trap .* ERR$", DEPLOY_SCRIPT, re.MULTILINE)
        self.assertIsNotNone(trap)
        remote_trap = trap.group(0).replace("\\$", "$")
        for failing_step in ("false", "bash -c 'exit 23'"):
            with self.subTest(failing_step=failing_step):
                result = subprocess.run(
                    ["bash", "-c", "\n".join([
                        "set -euo pipefail",
                        "restore_runtime_on_error() { trap - ERR; set +e; echo recovered; }",
                        remote_trap,
                        failing_step,
                        "echo incorrectly-continued",
                    ])],
                    text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 1 if failing_step == "false" else 23)
                self.assertEqual(result.stdout.strip(), "recovered")






class WebsiteDeploymentConfigTests(unittest.TestCase):
    def test_plan_canonicalization_retains_every_operation_and_rejects_ambiguous_output(self):
        plan = "Planned operations:\ncontent_factory.0042_website_connection_lifecycle\n    Create model WebsiteConnection"
        for prefix in ("", "Initializing Firebase with project ID: synthetic\nDEBUG: ESAFETY VIEWS MODULE LOADED\n"):
            self.assertEqual(canonical_migration_plan(prefix + plan + "\n"), plan)
        self.assertNotEqual(canonical_migration_plan(plan + "\n    Unreviewed operation"), plan)
        for invalid in ("", "Import failed", plan + "\nPlanned operations:\nUnexpected"):
            with self.assertRaises(ValueError):
                canonical_migration_plan(invalid)

    def test_paused_default_and_each_rollout_mode_are_explicit(self):
        validate_website({})
        validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "disabled"})
        validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "enabled"})
        validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "canary", "WEBSITE_CONNECTION_CANARY_DOMAINS": "talathrive.com,site.example.test"})
        for mode in ("", "true", "DISABLED", "typo", "disabled\nenabled"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_website({"WEBSITE_CONNECTION_WRITE_MODE": mode})
        with self.assertRaises(ValueError):
            validate_website({"WEBSITE_CONNECTION_WRITE_MODE": "canary"})

    def test_canary_domains_reject_noncanonical_or_ambiguous_authority(self):
        for domains in ("https://site.test", "*.site.test", "site.test.", "SITE.TEST", "site.test, site2.test", "site.test,site.test", "site.test,", "127.0.0.1", "localhost", "-bad.site.test", "site.test\nother.test", "site.test/path"):
            with self.subTest(domains=domains), self.assertRaises(ValueError):
                validate_single("WEBSITE_CONNECTION_CANARY_DOMAINS", domains)
        for value in ("a" * 63, "A" * 64, "a" * 65, "a" * 64 + "\n", "not-a-hash"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_single("APPROVED_MIGRATION_PLAN_SHA256", value)

    def test_workflow_and_remote_writer_install_approved_values_before_runtime_mutation(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/deploy.yml").read_text()
        for key, default in (("WEBSITE_CONNECTION_WRITE_MODE", "disabled"), ("WEBSITE_CONNECTION_CANARY_DOMAINS", ""), ("APPROVED_MIGRATION_PLAN_SHA256", "")):
            self.assertIn(key + ": ${{ vars." + key + " || '" + default + "' }}", workflow)
            self.assertIn('install_remote_env_value ' + key + ' "$' + key + '"', DEPLOY_SCRIPT)
            self.assertLess(DEPLOY_SCRIPT.index('install_remote_env_value ' + key), DEPLOY_SCRIPT.index('    migrations_pending=1'))
        self.assertLess(DEPLOY_SCRIPT.index('python3 scripts/validate_website_deploy_config.py'), DEPLOY_SCRIPT.index('rsync -avz'))

    def test_stdin_writer_clears_old_approval_and_canary_without_touching_other_environment(self):
        root = Path(__file__).resolve().parents[1]
        values = {
            "WEBSITE_CONNECTION_WRITE_MODE": ("disabled", "canary", "enabled", "", "true", "disabled\nenabled"),
            "WEBSITE_CONNECTION_CANARY_DOMAINS": ("site.example.test", "", "https://site.test", "*.site.test"),
            "APPROVED_MIGRATION_PLAN_SHA256": ("a" * 64, "", "A" * 64, "a" * 63, "a" * 64 + "\nnot-a-hash"),
        }
        with tempfile.TemporaryDirectory() as directory:
            sandbox = Path(directory)
            (sandbox / "scripts").mkdir()
            for name in ("upsert_env_value_from_stdin.sh", "validate_website_deploy_config.py"):
                shutil.copy(root / "scripts" / name, sandbox / "scripts")
            for key, candidates in values.items():
                for value in candidates:
                    with self.subTest(key=key, value=value):
                        env_file = sandbox / ".env"
                        original = "KEEP_ME=yes\n" + key + "=old-value\n"
                        env_file.write_text(original)
                        result = subprocess.run(["bash", "scripts/upsert_env_value_from_stdin.sh", key], cwd=sandbox,
                                                input=value, text=True, capture_output=True, timeout=5)
                        try:
                            validate_single(key, value)
                            valid = True
                        except ValueError:
                            valid = False
                        self.assertEqual(result.returncode == 0, valid, result.stderr)
                        self.assertEqual(env_file.read_text(), "KEEP_ME=yes\n" + key + "=" + value + "\n" if valid else original)
                        self.assertEqual(result.stdout, "")
                        if valid:
                            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
