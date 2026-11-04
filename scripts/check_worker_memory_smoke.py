"""Remote-only real Gunicorn/procfs acceptance with a private Unix socket."""

import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CANARY = "do-not-log-request-data"
FIELDS = {"schema_version", "phase", "worker_pid", "completed_requests", "baseline_completed_requests",
    "rss_kib", "peak_rss_kib", "rss_delta_kib"}


def require_ci():
    """Admit only the owned Linux CI runner and its existing temporary root."""
    temporary = Path(os.environ.get("RUNNER_TEMP", ""))
    if (sys.platform != "linux" or os.environ.get("GITHUB_ACTIONS") != "true" or
            os.environ.get("GITHUB_REPOSITORY") != "MLAI-AUS-Inc/mlai-backend" or
            not temporary.is_absolute() or not temporary.is_dir() or temporary.is_symlink()):
        raise ValueError("Worker memory smoke requires isolated Linux repository CI")
    return temporary


def validate_samples(log, *, completed, attempted):
    """Validate closed numeric events and real recycled workers without log export."""
    if CANARY in log or len(log) > 2 * 1024 * 1024:
        raise ValueError("Worker memory log boundary failed")
    rows = []
    for line in log.splitlines():
        _, marker, payload = line.partition("worker_memory_profile ")
        if not marker:
            continue
        value = json.loads(payload)
        if (not isinstance(value, dict) or set(value) != FIELDS or value.get("phase") not in {"start", "sample", "exit"} or
                any(type(value[key]) is not int for key in FIELDS - {"phase"}) or value["schema_version"] != 1 or
                value["worker_pid"] <= 0 or value["rss_kib"] <= 0 or value["peak_rss_kib"] <= 0 or
                not 0 <= value["baseline_completed_requests"] <= value["completed_requests"]):
            raise ValueError("Worker memory event shape failed")
        if value["phase"] == "start" and value["completed_requests"] != 0:
            raise ValueError("Worker memory startup count failed")
        if value["phase"] == "sample" and value["completed_requests"] % 2:
            raise ValueError("Worker memory interval failed")
        rows.append(value)
    starts = {row["worker_pid"] for row in rows if row["phase"] == "start"}
    exits = [row for row in rows if row["phase"] == "exit"]
    if (len(starts) < 3 or not exits or not any(row["phase"] == "sample" for row in rows) or
            any(row["worker_pid"] not in starts for row in rows) or
            not any(5 <= row["completed_requests"] <= 8 for row in exits) or
            not completed <= sum(row["completed_requests"] for row in exits) <= attempted):
        raise ValueError("Worker memory recycle acceptance failed")
    return {"worker_starts": len(starts), "worker_exits": len(exits), "samples": len(rows),
        "completed_requests": completed, "max_rss_kib": max(row["rss_kib"] for row in rows),
        "max_peak_rss_kib": max(row["peak_rss_kib"] for row in rows)}


def stop(process):
    """Stop only the process group created for this disposable fixture."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def child_environment():
    """Pass only fixed fixture settings, excluding all account/database secrets."""
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONUNBUFFERED": "1", "GUNICORN_MEMORY_PROFILE_ENABLED": "true", "GUNICORN_MEMORY_PROFILE_INTERVAL": "2"}


def main():
    """Exercise actual hooks/procfs/recycling without HTTP outside this runner."""
    temporary = require_ci()
    with tempfile.TemporaryDirectory(prefix="worker-profile-", dir=temporary) as scratch:
        directory = Path(scratch)
        address = directory / "web.sock"
        if len(str(address).encode()) >= 104:
            raise ValueError("Worker memory socket path exceeds its fixed boundary")
        log_path = directory / "worker.log"
        descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        child_env = child_environment()
        command = [sys.executable, "-m", "gunicorn", "--chdir", str(ROOT), "--bind", "unix:" + str(address),
            "--workers", "2", "--worker-class", "sync", "--max-requests", "5", "--max-requests-jitter", "3",
            "--timeout", "10", "--graceful-timeout", "2", "--config", str(ROOT / "tests/fixtures/gunicorn_memory_config.py"),
            "--worker-tmp-dir", str(directory),
            "tests.fixtures.gunicorn_memory_app:application"]
        completed = attempted = 0
        with os.fdopen(descriptor, "w") as output:
            process = subprocess.Popen(command, cwd=ROOT, env=child_env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 45
            try:
                while completed < 32 and attempted < 96 and time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ValueError("Worker memory fixture stopped before acceptance")
                    attempted += 1
                    client = http.client.HTTPConnection("fixture", timeout=1)
                    try:
                        client.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        client.sock.settimeout(1)
                        client.sock.connect(str(address))
                        client.request("GET", "/" + CANARY, headers={"X-Fixture-Canary": CANARY})
                        response = client.getresponse()
                        if response.status != 200 or response.read(16) != b"ok":
                            raise ValueError("Worker memory fixture response failed")
                        completed += 1
                    except (OSError, http.client.HTTPException):
                        time.sleep(0.1)
                    finally:
                        client.close()
                if completed != 32:
                    raise ValueError("Worker memory fixture exceeded its request/time budget")
            finally:
                stop(process)
        with log_path.open("r", encoding="utf-8") as handle:
            log = handle.read(2 * 1024 * 1024 + 1)
        result = validate_samples(log, completed=completed, attempted=attempted)
        print(json.dumps({"fixture": "disposable Gunicorn lifecycle", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
