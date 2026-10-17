"""CI fixture boundaries are enforced without Docker, a database or a socket."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import cms_provider_fixture as fixture


class CmsProviderFixtureTests(unittest.TestCase):
    def test_ci_containers_have_pinned_images_internal_network_and_only_loopback_ports(self):
        import re
        import yaml
        compose = yaml.safe_load((fixture.ROOT / "ops/cms-provider-ci/compose.yml").read_text())
        self.assertIs(compose["networks"]["default"]["internal"], True)
        self.assertEqual(compose["services"]["loopback"]["networks"], ["default", "access"])
        for name in ("wordpress", "ghost", "mysql", "wpcli"):
            self.assertNotIn("networks", compose["services"][name])
            self.assertNotIn("ports", compose["services"][name])
        for service in compose["services"].values():
            self.assertTrue(re.fullmatch(r"[a-z0-9:./-]+@sha256:[a-f0-9]{64}", service["image"]))
            for port in service.get("ports", []):
                self.assertTrue(port.startswith("127.0.0.1:"))
        proxy = (fixture.ROOT / "ops/cms-provider-ci/loopback.conf").read_text()
        self.assertEqual(proxy.count("proxy_pass http://$cms_backend$request_uri;"), 2)
        self.assertIn("set $cms_backend wordpress:80;", proxy)
        self.assertIn("set $cms_backend ghost:2368;", proxy)
        self.assertIn("access_log off;", proxy)

    def test_ci_has_both_providers_without_secrets_and_always_erases_keys_and_volumes(self):
        import yaml
        workflow = yaml.safe_load((fixture.ROOT / ".github/workflows/deploy.yml").read_text())
        job = workflow["jobs"]["cms-provider-acceptance"]
        self.assertEqual(set(job["strategy"]["matrix"]["provider"]), {"wordpress", "ghost"})
        self.assertTrue(job["timeout-minutes"] <= 25)
        self.assertNotIn("secrets.", json.dumps(job))
        cleanup = job["steps"][-1]
        self.assertIn("always()", cleanup["if"])
        self.assertIn("down --volumes --remove-orphans", cleanup["run"])
        self.assertIn('"$RUNNER_TEMP/mlai-cms-provider/wordpress.json"', cleanup["run"])
        self.assertIn('"$RUNNER_TEMP/mlai-cms-provider/ghost.json"', cleanup["run"])

    def test_only_exact_repository_remote_context_and_fixed_private_file_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "RUNNER_TEMP": directory}
            path = fixture.fixture_path("wordpress", env)
            self.assertEqual(path, Path(directory) / "mlai-cms-provider/wordpress.json")
            for change in ({"GITHUB_ACTIONS": "false"}, {"GITHUB_REPOSITORY": "other/repo"}, {"RUNNER_TEMP": "relative"}):
                with self.assertRaises(ValueError):
                    fixture.fixture_path("wordpress", {**env, **change})
            path.parent.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(ValueError):
                fixture.fixture_path("wordpress", env)

    def test_credential_file_requires_private_mode_and_exact_closed_provider(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "RUNNER_TEMP": directory}, clear=True):
            path = fixture.fixture_path("ghost")
            path.parent.mkdir(mode=0o700)
            value = {"version": 1, "provider": "ghost", "username": "", "secret": "a" * 24 + ":" + "b" * 64}
            path.write_text(json.dumps(value)); path.chmod(0o600)
            self.assertEqual(fixture.load_fixture("ghost"), value)
            path.chmod(0o644)
            with self.assertRaises(ValueError): fixture.load_fixture("ghost")
            path.chmod(0o600); path.write_text(json.dumps({**value, "provider": "wordpress"}))
            with self.assertRaises(ValueError): fixture.load_fixture("ghost")

    def test_no_other_host_scheme_port_or_user_info_can_reach_fixture_transport(self):
        host = fixture.HOSTS["wordpress"]
        self.assertEqual(fixture.addresses("https://" + host + "/", host)[1], ["127.0.0.1"])
        for url, expected in (("https://other.test/", host), ("http://" + host, host),
                ("https://" + host + ":444/", host), ("https://key@" + host, host), ("https://other.test/", "other.test")):
            with self.assertRaises(AssertionError): fixture.addresses(url, expected)
        for actual, address in (("other.test", "127.0.0.1"), (host, "8.8.8.8")):
            with self.assertRaises(AssertionError): fixture.FixtureConnection(actual, address)

    @patch.object(fixture.http.client, "HTTPConnection")
    def test_test_proxy_uses_numeric_loopback_and_preserves_request_without_other_destination(self, http):
        client = fixture.FixtureConnection(fixture.HOSTS["wordpress"], "127.0.0.1")
        client.request("GET", "/wp-json/", headers={"Accept": "application/json"})
        http.assert_called_once_with("127.0.0.1", 18880, timeout=15)
        sent = http.return_value.request.call_args.kwargs["headers"]
        self.assertEqual(sent["X-Forwarded-Proto"], "https")
        self.assertNotIn("Authorization", sent)
        with self.assertRaises(AssertionError): client.request("GET", "//other.test/")
        with self.assertRaises(AssertionError): client.request("GET", "/", headers={"Host": "other.test"})

    def test_ghost_bootstrap_uses_actual_setup_route_and_never_emits_credentials(self):
        from contextlib import redirect_stdout
        from io import StringIO
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "RUNNER_TEMP": directory}, clear=True):
            secret = "b" * 64
            replies = [({}, ""), ({}, "synthetic-session"),
                ({"integrations": [{"api_keys": [{"type": "admin", "id": "a" * 24, "secret": secret}]}]}, "")]
            output = StringIO()
            with patch.object(fixture, "_ghost_call", side_effect=replies) as api, redirect_stdout(output):
                fixture.bootstrap("ghost")
            self.assertEqual(api.call_args_list[0].args[1], "authentication/setup/")
            self.assertEqual(api.call_args_list[2].args[1], "integrations/?include=api_keys")
            self.assertTrue(secret not in output.getvalue())
            self.assertTrue("synthetic-session" not in output.getvalue())
            self.assertEqual(fixture.fixture_path("ghost").stat().st_mode & 0o777, 0o600)

    @patch.object(fixture.http.client, "HTTPConnection")
    def test_ghost_session_plain_created_body_uses_cookie_without_json_decoding(self, http):
        response = http.return_value.getresponse.return_value
        response.status = 201
        response.read.return_value = b"Created"
        response.getheaders.return_value = [("Set-Cookie", "ghost-admin-api-session=synthetic-fixture-session; Secure; HttpOnly")]
        value, cookie = fixture._ghost_call("POST", "session/", {"username": "fixture@example.test", "password": "synthetic"})
        self.assertEqual(value, {})
        self.assertTrue(cookie.startswith("ghost-admin-api-session="))
        http.return_value.close.assert_called_once()

    def test_wordpress_bootstrap_explicitly_invokes_wp_in_the_cli_image(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,
                {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "RUNNER_TEMP": directory}, clear=True):
            replies = [SimpleNamespace(returncode=0, stdout=b""), SimpleNamespace(returncode=0, stdout=b""),
                SimpleNamespace(returncode=0, stdout=b"synthetic-application-password")]
            with patch.object(fixture.subprocess, "run", side_effect=replies) as run, redirect_stdout(StringIO()):
                fixture.bootstrap("wordpress")
            command = run.call_args_list[0].args[0]
            self.assertEqual(command[command.index("wpcli") + 1:][:3], ["wp", "core", "install"])
            self.assertEqual(fixture.fixture_path("wordpress").stat().st_mode & 0o777, 0o600)
