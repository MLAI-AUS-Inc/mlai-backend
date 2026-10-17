"""Owned remote-CI application memory comparison; never a production runner."""

import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time

from scripts.check_worker_memory_smoke import require_ci, FIELDS, CANARY

ROOT = Path(__file__).resolve().parents[1]
REQUESTS = 1200
MAX_LOG_BYTES = 2 * 1024 * 1024


def memory_ceiling_bytes():
    """Read fixed Linux memory bounds; unknown remains None, never zero."""
    limits = []
    for name in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with Path(name).open("r", encoding="ascii") as handle:
                value = handle.read(64).strip()
            if value.isdigit() and 0 < int(value) < 2**60:
                limits.append(int(value))
        except (OSError, UnicodeError, ValueError):
            pass
    try:
        with Path("/proc/meminfo").open("r", encoding="ascii") as handle:
            text = handle.read(16384)
        found = re.search(r"^MemTotal:[ \t]+([0-9]{1,16})[ \t]+kB$", text, re.M)
        if found and int(found[1]) > 0:
            limits.append(int(found[1]) * 1024)
    except (OSError, UnicodeError, ValueError):
        pass
    return min(limits) if limits else None


def child_environment(*, database, user_id):
    """Admit only the existing synthetic loopback PostgreSQL test database."""
    temporary = require_ci()
    if (os.environ.get("ARTICLES_BOOTSTRAP_MEMORY_CI") != "1" or
            database.get("ENGINE") != "django.db.backends.postgresql" or database.get("NAME") != "test_mlai_test" or
            database.get("HOST") != "127.0.0.1" or str(database.get("PORT")) != "5432" or
            database.get("USER") != "mlai" or database.get("PASSWORD") != "mlai_test_password" or
            type(user_id) is not int or user_id <= 0 or
            re.fullmatch(r"[a-f0-9]{40}", os.environ.get("GITHUB_SHA", "")) is None):
        raise ValueError("Bootstrap worker profile requires its disposable CI fixture")
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHON_DOTENV_DISABLED": "1", "PYTHONUNBUFFERED": "1",
        "GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "MLAI-AUS-Inc/mlai-backend", "GITHUB_SHA": os.environ["GITHUB_SHA"],
        "RUNNER_TEMP": str(temporary), "ARTICLES_BOOTSTRAP_MEMORY_CI": "1", "BOOTSTRAP_MEMORY_USER_ID": str(user_id),
        "DATABASE_URL": "postgresql://mlai:mlai_test_password@127.0.0.1:5432/test_mlai_test",
        "DJANGO_SETTINGS_MODULE": "mlai.settings", "SECRET_KEY": "synthetic-memory-fixture-signing-key-not-for-deployment",
        "DEBUG": "true", "ALLOWED_HOSTS": "testserver,localhost", "VIBE_BOOTSTRAP_CACHE_SECONDS": "300",
        "GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": "100"}


def summarize(log, *, completed, attempted, limit, jitter):
    """Export only closed numeric lifecycle summaries, excluding private logs."""
    if not isinstance(log, str) or len(log.encode()) > MAX_LOG_BYTES or CANARY in log:
        raise ValueError("Bootstrap worker profile log boundary failed")
    workers = {}
    for line in log.splitlines():
        _, marker, payload = line.partition("worker_memory_profile ")
        if not marker:
            continue
        row = json.loads(payload)
        if (not isinstance(row, dict) or set(row) != FIELDS or row["phase"] not in {"start", "sample", "exit"} or
                any(type(row[key]) is not int for key in FIELDS - {"phase"}) or row["schema_version"] != 1 or
                row["worker_pid"] <= 0 or row["rss_kib"] <= 0 or row["peak_rss_kib"] < row["rss_kib"] or
                row["baseline_completed_requests"] != 0 or not 0 <= row["completed_requests"] <= limit + jitter):
            raise ValueError("Bootstrap worker profile event shape failed")
        workers.setdefault(row["worker_pid"], []).append(row)
    lives = []
    for rows in workers.values():
        counts = [row["completed_requests"] for row in rows]
        if (rows[0]["phase"] != "start" or counts[0] != 0 or rows[-1]["phase"] != "exit" or counts != sorted(counts) or
                sum(row["phase"] == "start" for row in rows) != 1 or sum(row["phase"] == "exit" for row in rows) != 1 or
                any(row["phase"] == "sample" and (not row["completed_requests"] or row["completed_requests"] % 100) for row in rows)):
            raise ValueError("Bootstrap worker profile lifecycle failed")
        samples = [row for row in rows if row["phase"] == "sample"]
        if ([row["completed_requests"] for row in samples] != list(range(100, counts[-1] + 1, 100)) or
                any(row["rss_delta_kib"] != row["rss_kib"] - rows[0]["rss_kib"] for row in rows)):
            raise ValueError("Bootstrap worker profile sample accounting failed")
        lives.append({"requests": counts[-1], "start_rss_kib": rows[0]["rss_kib"], "exit_rss_kib": rows[-1]["rss_kib"],
            "peak_rss_kib": max(row["peak_rss_kib"] for row in rows), "retained_growth_kib": rows[-1]["rss_kib"] - rows[0]["rss_kib"],
            "growth_after_100_kib": rows[-1]["rss_kib"] - samples[0]["rss_kib"] if samples else None})
    if (len(lives) < 2 or not completed <= sum(life["requests"] for life in lives) <= attempted or
            not any(limit <= life["requests"] <= limit + jitter for life in lives)):
        raise ValueError("Bootstrap worker profile did not observe recycling")
    return {"max_requests": limit, "jitter": jitter, "completed_requests": completed, "attempted_requests": attempted,
        "worker_count": 1, "worker_lifetimes": lives, "max_peak_rss_kib": max(life["peak_rss_kib"] for life in lives)}


def stop_group(process):
    """Stop the owned Gunicorn process group, including early-master failures."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def profile_window(*, database, user_id, limit, jitter):
    """Exercise a fixed application window; never starts outside owned Linux CI."""
    if (limit, jitter) not in {(300, 50), (1000, 100)}:
        raise ValueError("Choose a fixed bootstrap profile comparison")
    environment = child_environment(database=database, user_id=user_id)
    with tempfile.TemporaryDirectory(prefix="bootstrap-memory-", dir=environment["RUNNER_TEMP"]) as scratch:
        directory = Path(scratch)
        address, log_path = directory / "web.sock", directory / "worker.log"
        if len(str(address).encode()) >= 104:
            raise ValueError("Bootstrap worker socket boundary failed")
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        command = [sys.executable, "-m", "gunicorn", "--chdir", str(ROOT), "--bind", "unix:" + str(address),
            "--workers", "1", "--worker-class", "sync", "--max-requests", str(limit), "--max-requests-jitter", str(jitter),
            "--timeout", "20", "--graceful-timeout", "3", "--config", str(ROOT / "scripts/gunicorn.conf.py"),
            "--worker-tmp-dir", str(directory), "tests.fixtures.gunicorn_bootstrap_memory_app:application"]
        completed = attempted = 0
        started = time.monotonic()
        with os.fdopen(descriptor, "w") as output:
            process = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = started + 600
            try:
                while completed < REQUESTS and attempted < REQUESTS + 100 and time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ValueError("Bootstrap worker stopped before acceptance")
                    attempted += 1
                    client = http.client.HTTPConnection("fixture", timeout=10)
                    try:
                        client.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        client.sock.settimeout(10)
                        client.sock.connect(str(address))
                        client.request("GET", "/fixture", headers={"X-Fixture-Canary": CANARY})
                        response = client.getresponse()
                        raw = response.read(1025)
                        if response.status != 200 or len(raw) > 1024:
                            raise ValueError("Bootstrap worker response failed")
                        row = json.loads(raw)
                        if (not isinstance(row, dict) or set(row) != {"runs", "bytes"} or type(row["runs"]) is not int or row["runs"] != 24 or
                                type(row["bytes"]) is not int or not 0 < row["bytes"] < 300000):
                            raise ValueError("Bootstrap worker compact response failed")
                        completed += 1
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.1)
                    finally:
                        client.close()
                if completed != REQUESTS:
                    raise ValueError("Bootstrap worker exceeded its fixed request/time budget")
            finally:
                stop_group(process)
        with log_path.open("rb") as handle:
            log = handle.read(MAX_LOG_BYTES + 1)
        if len(log) > MAX_LOG_BYTES:
            raise ValueError("Bootstrap worker log exceeded its fixed budget")
        result = summarize(log.decode(), completed=completed, attempted=attempted, limit=limit, jitter=jitter)
        return {**result, "elapsed_ms": round((time.monotonic() - started) * 1000)}
