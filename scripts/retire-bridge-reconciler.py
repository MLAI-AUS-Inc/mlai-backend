"""Retire only the old reconciler after the current durable worker proves healthy.

Compose retains the retired service definition for this release so the existing
image rollback manifest can restore it if later deployment checks fail.
"""
import json
import subprocess
import time


def command(args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30)
    if result.returncode:
        raise ValueError("retirement command failed")
    return result.stdout


def retire(*, attempts=30):
    started = time.monotonic()
    for attempt in range(attempts):
        if time.monotonic() - started >= 180:
            raise ValueError("durable worker readiness deadline exceeded")
        try:
            result = json.loads(command([
                "docker", "compose", "exec", "-T", "bridge-worker", "python", "manage.py",
                "message_sync_status", "--check", "--local-worker",
            ]))
            if isinstance(result, dict) and result.get("enabled") is True and result.get("stale_lanes") == []:
                break
        except (ValueError, OSError, subprocess.SubprocessError):
            pass
        if attempt + 1 < attempts:
            time.sleep(3)
    else:
        raise ValueError("durable worker health unproven; old reconciler retained")
    command(["docker", "compose", "stop", "--timeout", "30", "bridge-reconciler"])
    command(["docker", "compose", "rm", "--force", "bridge-reconciler"])
    print("Verified durable worker; duplicate reconciler retired")


if __name__ == "__main__":
    try:
        retire()
    except (ValueError, OSError, subprocess.SubprocessError):
        raise SystemExit("Reconciler retirement incomplete; deployment recovery required") from None
