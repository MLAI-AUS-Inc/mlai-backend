"""No-database integration coverage for the origin web handoff."""

import http.client
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
SWITCH = ROOT / "ops/backend-api/switch-web-upstream.sh"
TEMPLATE = ROOT / "ops/backend-api/nginx-api.conf.template"
NGINX = shutil.which("nginx")
DEPLOY = (ROOT / "deploy.sh").read_text()


class DatabaseContinuityTests(unittest.TestCase):
    def test_application_release_does_not_replace_database_dependency(self):
        # APP_RELEASE and many feature flags share the database's env_file.
        # Compose otherwise recreates a healthy Postgres container on each
        # code release, even before the web handoff begins.
        commands = [
            line.strip()
            for line in DEPLOY.splitlines()
            if line.strip().startswith("docker compose up -d ")
        ]
        self.assertEqual(len(commands), 6)
        database = [line for line in commands if line.endswith(" db")]
        self.assertEqual(database, ["docker compose up -d --no-recreate db"])
        for command in commands:
            if command not in database:
                self.assertIn("--no-deps", command)
        self.assertLess(
            DEPLOY.index("docker compose up -d --no-recreate db"),
            DEPLOY.index("compose_run_web python manage.py migrate --check --noinput"),
        )
        self.assertIn("APPROVED_MIGRATION_PLAN_SHA256", DEPLOY)
        self.assertIn("compose_run_web python manage.py migrate --noinput", DEPLOY)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/slow":
            self.server.slow_started.set()
            self.server.release_slow.wait(15)
        self.send_json(
            {
                "status": "ok",
                "release": self.server.release,
                "forwarded_proto": self.headers.get("X-Forwarded-Proto"),
                "forwarded_for": self.headers.get("X-Forwarded-For"),
                "cf_connecting_ip": self.headers.get("CF-Connecting-IP"),
                "host": self.headers.get("Host"),
            }
        )

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        self.send_json({"size": len(body)})


@unittest.skipUnless(NGINX, "nginx is required for the live route-flip test")
class WebHandoffIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config_dir = self.root / "conf.d"
        self.config_dir.mkdir()
        self.vhost = self.config_dir / "mlai-backend-api.conf"
        real_ip_config = self.root / "cloudflare-real-ip.conf"
        real_ip_config.write_text("set_real_ip_from 203.0.113.1;\n")
        self.web_port = free_port()
        self.candidate_port = free_port()
        self.proxy_port = free_port()
        self.servers = []
        self.nginx_config = self.root / "nginx.conf"
        self.nginx_config.write_text(
            "worker_processes 2;\n"
            f"pid {self.root / 'nginx.pid'};\n"
            f"error_log {self.root / 'error.log'} notice;\n"
            "events { worker_connections 1024; }\n"
            "http { access_log off; "
            f"include {self.config_dir}/*.conf; }}\n"
        )
        wrapper = self.root / "nginx-wrapper"
        wrapper.write_text(
            "#!/bin/sh\n"
            'case " $* " in *" -c "*) '
            f'exec "{NGINX}" -p "{self.root}/" "$@" ;; '
            f'*) exec "{NGINX}" -p "{self.root}/" -c "{self.nginx_config}" "$@" ;; '
            'esac\n'
        )
        wrapper.chmod(0o755)
        self.env = {
            **os.environ,
            "MLAI_API_NGINX_CONFIG_PATH": str(self.vhost),
            "MLAI_API_NGINX_TEMPLATE_PATH": str(TEMPLATE),
            "MLAI_API_NGINX_BIN": str(wrapper),
            "MLAI_API_NGINX_LISTEN_PORT": str(self.proxy_port),
            "MLAI_API_WEB_PORT": str(self.web_port),
            "MLAI_API_CANDIDATE_PORT": str(self.candidate_port),
            "MLAI_API_REAL_IP_CONFIG_PATH": str(real_ip_config),
        }
        self.nginx(["-t"])
        self.nginx([])
        self.addCleanup(self.stop_nginx)

    def nginx(self, args):
        return subprocess.run(
            [self.env["MLAI_API_NGINX_BIN"], *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def stop_nginx(self):
        try:
            self.nginx(["-s", "quit"])
        except subprocess.SubprocessError:
            pass

    def switch(self, operation, target=None, *, check=True, env=None):
        result = subprocess.run(
            ["bash", str(SWITCH), operation, *([target] if target else [])],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if check and result.returncode:
            self.fail(f"route helper failed: {result.stderr}")
        return result

    def start_web(self, port, release):
        server = ThreadingHTTPServer(("127.0.0.1", port), WebHandler)
        server.release = release
        server.slow_started = threading.Event()
        server.release_slow = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append((server, thread))
        self.addCleanup(self.stop_web, server, thread)
        return server

    @staticmethod
    def stop_web(server, thread):
        server.release_slow.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def request(self, path="/healthz/ready", method="GET", body=None, *, forwarded_proto="https", extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.proxy_port, timeout=5)
        headers = {"Host": "api.mlai.au"}
        if forwarded_proto is not None:
            headers["X-Forwarded-Proto"] = forwarded_proto
        headers.update(extra_headers or {})
        connection.request(
            method,
            path,
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        payload = json.loads(response.read())
        headers = dict(response.getheaders())
        status = response.status
        connection.close()
        return status, payload, headers

    def wait_route(self, expected_release, expected_slot):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                status, payload, headers = self.request()
            except (OSError, http.client.HTTPException, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if (
                status == 200
                and payload["release"] == expected_release
                and headers.get("X-MLAI-Origin-Web-Slot") == expected_slot
            ):
                return payload
            time.sleep(0.05)
        self.fail(f"Proxy never routed to {expected_slot}/{expected_release}")

    def test_two_live_slots_flip_and_rollback_on_bad_config(self):
        old = self.start_web(self.web_port, "old")
        candidate = self.start_web(self.candidate_port, "new")
        self.switch("validate", "web")
        self.switch("switch", "web")
        payload = self.wait_route("old", "web")
        self.assertEqual(payload["forwarded_proto"], "https")
        self.assertEqual(payload["host"], "api.mlai.au")
        self.assertEqual(payload["forwarded_for"], "127.0.0.1")
        self.assertEqual(self.request(forwarded_proto=None)[1]["forwarded_proto"], "http")
        spoofed = self.request(extra_headers={"X-Forwarded-For": "1.2.3.4", "CF-Connecting-IP": "8.8.8.8"})[1]
        self.assertEqual(spoofed["forwarded_for"], "127.0.0.1")
        self.assertEqual(spoofed["cf_connecting_ip"], "127.0.0.1")

        slow_result = []
        slow_thread = threading.Thread(target=lambda: slow_result.append(self.request("/slow")))
        slow_thread.start()
        self.assertTrue(old.slow_started.wait(5))
        self.switch("switch", "candidate")
        self.wait_route("new", "candidate")
        old.release_slow.set()
        slow_thread.join(timeout=5)
        self.assertEqual(slow_result[0][1]["release"], "old")

        self.stop_web(old, next(thread for server, thread in self.servers if server is old))
        self.start_web(self.web_port, "new")
        self.switch("switch", "web")
        self.wait_route("new", "web")
        self.stop_web(candidate, next(thread for server, thread in self.servers if server is candidate))
        self.wait_route("new", "web")

        status, body, _headers = self.request("/upload", "POST", b"x" * (2 * 1024 * 1024))
        self.assertEqual(status, 200)
        self.assertEqual(body["size"], 2 * 1024 * 1024)

        broken = self.root / "broken-template"
        broken.write_text("server { this-is-invalid; }\n")
        bad_env = {**self.env, "MLAI_API_NGINX_TEMPLATE_PATH": str(broken)}
        result = self.switch("switch", "candidate", check=False, env=bad_env)
        self.assertNotEqual(result.returncode, 0)
        self.wait_route("new", "web")
        self.assertIn("target=web", self.vhost.read_text())

    def test_first_adoption_stages_without_stealing_direct_port(self):
        direct = self.start_web(self.proxy_port, "old")
        self.start_web(self.candidate_port, "new")
        self.switch("validate", "candidate")
        self.assertFalse(self.vhost.exists())
        self.assertEqual(self.request()[1]["release"], "old")
        self.stop_web(direct, next(thread for server, thread in self.servers if server is direct))
        self.switch("switch", "candidate")
        self.wait_route("new", "candidate")
        self.switch("remove")
        self.assertFalse(self.vhost.exists())
        # Old Nginx workers must exit before Docker can reclaim the port.
        deadline = time.monotonic() + 8
        while True:
            try:
                restored = self.start_web(self.proxy_port, "old")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)
        self.assertEqual(self.request()[1]["release"], restored.release)


class WebHandoffRollbackTests(unittest.TestCase):
    def run_rollback(self, *, prior_proxy, replacement_started):
        begin = DEPLOY.index("    restore_runtime_on_error() {")
        end = DEPLOY.index("\n    # A code-only release", begin)
        function = DEPLOY[begin:end].replace("\\$", "$")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "api.conf"
            config.write_text(
                "# managed-mlai-backend-api target="
                + ("web" if prior_proxy else "candidate")
                + "\n"
            )
            manifest = root / "manifest"
            manifest.write_text(
                "web|old-image|mlai-backend-web|rollback-web\n"
                "scheduler|old-scheduler|mlai-backend-scheduler|rollback-scheduler\n"
            )
            events = root / "events"
            script = "\n".join(
                [
                    "set -euo pipefail",
                    f"web_proxy_config={shlex.quote(str(config))}",
                    f"rollback_manifest={shlex.quote(str(manifest))}",
                    f"events={shlex.quote(str(events))}",
                    "web_proxy_script=/unused",
                    "APP_RELEASE=" + "b" * 40,
                    "previous_app_release=" + "a" * 40,
                    "migrations_pending=0",
                    "migration_started=0",
                    "runtime_pause_started=0",
                    "runtime_restore_attempted=0",
                    "web_only_rollback=0",
                    "web_candidate_started=1",
                    f"web_proxy_preexisting={int(prior_proxy)}",
                    f"web_proxy_candidate_verified={int(not prior_proxy)}",
                    f"new_runtime_replacement_started={int(replacement_started)}",
                    "web_proxy_switch_attempted=1",
                    "web_proxy_staged=1",
                    "web_direct_stopped=1",
                    "previous_scheduler_container_id=",
                    "previous_scheduler_image_id=",
                    "previous_scheduler_tick_mtime=0",
                    "previous_runtime_container_ids=()",
                    "runtime_services=(web scheduler)",
                    "all_runtime_writer_services=(web scheduler)",
                    "nginx_worker_snapshot() { echo 999999; }",
                    'wait_for_nginx_workers_to_drain() { echo "drain" >> "$events"; }',
                    'wait_for_origin_web_health() { echo "health $1 $2 ${3:-}" >> "$events"; }',
                    'upsert_env_value() { echo "env $1 $2" >> "$events"; }',
                    'docker() { echo "docker $*" >> "$events"; }',
                    'bash() { echo "route $3" >> "$events"; printf "# managed-mlai-backend-api target=%s\\n" "$3" > "$web_proxy_config"; }',
                    function,
                    "restore_runtime_on_error",
                    'cat "$events"',
                ]
            )
            result = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=5
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_failed_replacement_routes_candidate_before_recreating_old_web(self):
        events = self.run_rollback(prior_proxy=True, replacement_started=True)
        self.assertLess(events.index("route candidate"), events.index("docker image tag"))
        self.assertLess(events.index("docker compose up"), events.index("health 8001"))
        self.assertLess(events.index("health 8001"), events.index("route web"))
        self.assertIn("env APP_RELEASE " + "a" * 40, events)

    def test_failed_first_adoption_recreates_only_old_web(self):
        events = self.run_rollback(prior_proxy=False, replacement_started=False)
        self.assertIn("docker compose up -d --no-deps --force-recreate web", events)
        self.assertNotIn("docker compose up -d --no-deps --force-recreate web scheduler", events)
        self.assertNotIn("old-scheduler", events)
        self.assertLess(events.index("health 8001"), events.index("route web"))


if __name__ == "__main__":
    unittest.main()
