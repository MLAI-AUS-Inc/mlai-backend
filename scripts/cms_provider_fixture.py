"""Disposable CI-only CMS bootstrap and fixed loopback transport; never production."""

import argparse
import http.client
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import secrets
import subprocess
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOSTS = {"wordpress": "wordpress.cms-fixture.test", "ghost": "ghost.cms-fixture.test"}
PORTS = {"wordpress": 18880, "ghost": 18881}


class FixtureFailure(ValueError):
    """Closed bootstrap stage/status only; never provider text or credentials."""


def fixture_path(provider, environment=None):
    """Allow only this repository's disposable remote runner and fixed filename."""
    env = os.environ if environment is None else environment
    if (provider not in HOSTS or env.get("GITHUB_ACTIONS") != "true" or
            env.get("GITHUB_REPOSITORY") != "MLAI-AUS-Inc/mlai-backend"):
        raise ValueError("CMS acceptance requires the isolated GitHub CI job")
    temporary = Path(env.get("RUNNER_TEMP", ""))
    if not temporary.is_absolute() or not temporary.is_dir() or temporary.is_symlink():
        raise ValueError("CMS acceptance requires runner temporary storage")
    directory = temporary / "mlai-cms-provider"
    if directory.is_symlink():
        raise ValueError("CMS acceptance storage must not be redirected")
    path = directory / (provider + ".json")
    if path.is_symlink():
        raise ValueError("CMS acceptance credential must not be redirected")
    return path


def load_fixture(provider):
    """Read only the private synthetic fixture file; never an environment secret."""
    path = fixture_path(provider)
    if path.stat().st_mode & 0o077 or path.stat().st_size > 4096:
        raise ValueError("CMS acceptance credential permissions or size invalid")
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or set(value) != {"version", "provider", "username", "secret"} or
            type(value["version"]) is not int or value["version"] != 1 or value["provider"] != provider or
            value["username"] != ("fixture-owner" if provider == "wordpress" else "") or
            not isinstance(value["secret"], str) or not 16 <= len(value["secret"]) <= 200):
        raise ValueError("CMS acceptance credential shape invalid")
    return value


def addresses(url, expected_host):
    """Test-only resolver; reject every destination outside the two fixed fixtures."""
    parsed = urlsplit(url)
    if (expected_host not in HOSTS.values() or parsed.hostname != expected_host or
            parsed.scheme != "https" or parsed.port not in {None, 443} or
            parsed.username is not None or parsed.password is not None):
        raise AssertionError("Unexpected CMS acceptance destination")
    return parsed, ["127.0.0.1"]


class FixtureConnection:
    """Map one synthetic TLS origin to its isolated HTTP container, never DNS."""

    def __init__(self, hostname, address):
        if hostname not in HOSTS.values() or address != "127.0.0.1":
            raise AssertionError("Unexpected CMS acceptance destination")
        self.hostname = hostname
        provider = next(key for key, value in HOSTS.items() if value == hostname)
        self.connection = http.client.HTTPConnection("127.0.0.1", PORTS[provider], timeout=15)

    def request(self, method, target, body=None, headers=None):
        if not target.startswith("/") or target.startswith("//"):
            raise AssertionError("Unexpected CMS acceptance target")
        headers = dict(headers or {})
        if headers.get("Host", self.hostname) != self.hostname:
            raise AssertionError("Unexpected CMS acceptance host header")
        headers.update(Host=self.hostname, **{"X-Forwarded-Proto": "https"})
        return self.connection.request(method, target, body=body, headers=headers)

    def getresponse(self):
        return self.connection.getresponse()

    def close(self):
        self.connection.close()


def _ghost_call(method, path, payload=None, cookie=None):
    client = http.client.HTTPConnection("127.0.0.1", PORTS["ghost"], timeout=15)
    headers = {"Host": HOSTS["ghost"], "X-Forwarded-Proto": "https",
        "Origin": "https://" + HOSTS["ghost"], "Accept-Version": "v6.0", "Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        client.request(method, "/ghost/api/admin/" + path, body=json.dumps(payload) if payload is not None else None, headers=headers)
        response = client.getresponse()
        if not 200 <= response.status < 300:
            stage = {"authentication/setup/": "setup", "session/": "session",
                "integrations/?include=api_keys": "integration"}[path]
            raise FixtureFailure(f"Ghost fixture {stage} rejected with HTTP {response.status}")
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Ghost fixture bootstrap response too large")
        cookies = SimpleCookie()
        for key, value in response.getheaders():
            if key.lower() == "set-cookie":
                cookies.load(value)
        cookie_header = "; ".join(f"{key}={value.value}" for key, value in cookies.items())
        # Ghost's session middleware can acknowledge with a plain "Created"
        # body. Its cookie establishes this bootstrap session, not JSON content.
        if path == "session/":
            if not cookie_header:
                raise FixtureFailure("Ghost fixture session did not return a cookie")
            return {}, cookie_header
        try:
            value = json.loads(raw) if raw.strip() else {}
        except (ValueError, UnicodeError):
            stage = {"authentication/setup/": "setup", "integrations/?include=api_keys": "integration"}[path]
            raise FixtureFailure(f"Ghost fixture {stage} did not return JSON") from None
        return value, cookie_header
    finally:
        client.close()


def bootstrap(provider):
    """Create ephemeral test identity without printing keys, cookies or responses."""
    path = fixture_path(provider)
    password = secrets.token_urlsafe(40)
    if provider == "wordpress":
        base = ["docker", "compose", "-p", "mlai-cms-provider", "-f", str(ROOT / "ops/cms-provider-ci/compose.yml"),
            "run", "--rm", "-T", "wpcli", "wp"]

        def wp(*args):
            result = subprocess.run([*base, *args], capture_output=True, timeout=120)
            if result.returncode:
                stage = {"core": "install", "eval": "seed", "user": "application-password"}[args[0]]
                raise FixtureFailure(f"WordPress fixture {stage} exited {result.returncode}")
            return result.stdout.decode().strip()

        wp("core", "install", "--url=https://" + HOSTS[provider], "--title=MLAI disposable fixture",
            "--admin_user=fixture-owner", "--admin_password=" + password, "--admin_email=fixture@example.test", "--skip-email")
        wp("eval", "for ($i=0; $i<101; $i++) { wp_insert_post(['post_title'=>'Existing fixture '.$i, 'post_content'=>'<p>Existing native content.</p>', 'post_status'=>'publish', 'post_author'=>1]); }")
        secret = wp("user", "application-password", "create", "fixture-owner", "MLAI disposable CI", "--porcelain")
    else:
        _ghost_call("POST", "authentication/setup/", {"setup": [{"name": "Fixture Owner", "email": "fixture@example.test",
            "password": password, "blogTitle": "MLAI disposable fixture"}]})
        _, cookie = _ghost_call("POST", "session/", {"username": "fixture@example.test", "password": password})
        value, _ = _ghost_call("POST", "integrations/?include=api_keys", {"integrations": [{"name": "MLAI disposable CI"}]}, cookie)
        keys = value["integrations"][0]["api_keys"]
        admin = next(key for key in keys if key["type"] == "admin")
        secret = admin["id"] + ":" + admin["secret"]
    path.parent.mkdir(mode=0o700, exist_ok=True)
    # An existing file is an error; never overwrite/recover a persistent key.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"version": 1, "provider": provider, "username": "fixture-owner" if provider == "wordpress" else "", "secret": secret}, output)
    print("Disposable CMS credential stored privately; no provider secret emitted.")


def wait_ready(provider):
    """Wait a bounded time for the fixed container listener, without provider logs."""
    for _ in range(90):
        client = http.client.HTTPConnection("127.0.0.1", PORTS[provider], timeout=2)
        try:
            client.request("GET", "/", headers={"Host": HOSTS[provider], "X-Forwarded-Proto": "https"})
            status = client.getresponse().status
            if status < 500:
                return
        except OSError:
            pass
        finally:
            client.close()
        time.sleep(2)
    raise FixtureFailure("Disposable CMS loopback listener did not become ready")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=tuple(HOSTS))
    options = parser.parse_args()
    try:
        fixture_path(options.provider)
        wait_ready(options.provider)
        bootstrap(options.provider)
    except FixtureFailure as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        # Never print subprocess/provider bodies or exception context containing keys.
        raise SystemExit("Disposable CMS bootstrap failed (" + type(exc).__name__ + "); no provider response emitted.") from None
