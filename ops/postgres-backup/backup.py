#!/usr/bin/env python3
"""Back up the MLAI Backend database and rehearse recovery into an isolated database."""
import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HOST = "mlai-backend"
TAG = "backend-postgresql"
DATABASE = "medhack"
DB_CONTAINER = "mlai-backend-db-1"
FILES = ("manifest.json", "database.dump", "roles.sql")
PROOF = """
SELECT jsonb_build_object(
    'database', current_database(),
    'server_major', current_setting('server_version_num')::integer / 10000,
    'extensions', (SELECT jsonb_object_agg(extname, extversion) FROM pg_extension),
    'db_owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()),
    'ledger_owner', (SELECT pg_get_userbyid(relowner) FROM pg_class
                     WHERE oid='public.roo_pointspurchase'::regclass),
    'readonly_ledger_select', has_table_privilege('readonly','public.roo_pointspurchase','SELECT'),
    'readonly_ledger_insert', has_table_privilege('readonly','public.roo_pointspurchase','INSERT'),
    'public_tables', (SELECT count(*) FROM pg_class
                      WHERE relnamespace='public'::regnamespace AND relkind IN ('r','p')),
    'public_indexes', (SELECT count(*) FROM pg_class
                       WHERE relnamespace='public'::regnamespace AND relkind IN ('i','I')),
    'public_sequences', (SELECT count(*) FROM pg_class
                         WHERE relnamespace='public'::regnamespace AND relkind='S'),
    'django_migrations', (SELECT count(*) FROM public.django_migrations),
    'migration_names_md5', (SELECT md5(string_agg(app || '/' || name, ',' ORDER BY app COLLATE "C", name COLLATE "C"))
                            FROM public.django_migrations),
    'django_content_types', (SELECT count(*) FROM public.django_content_type),
    'roo_purchases', (SELECT count(*) FROM public.roo_pointspurchase)
)
"""


def output(*args):
    return subprocess.check_output(args, text=True).strip()


def psql(container, role, db):
    return ["docker", "exec", "-i", container, "psql", "-XqAtw", "-v", "ON_ERROR_STOP=1",
            "--username", role, "--dbname", db]


def validate_source_layout():
    sql = ("SELECT (SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global'))"
           " + (SELECT count(*) FROM pg_class WHERE relpersistence='u' AND relkind IN ('r','m'));")
    if output(*psql(DB_CONTAINER, DATABASE, DATABASE), "-c", sql) != "0":
        raise ValueError("Custom tablespaces or unlogged tables require a revised recovery plan")


def require_space(directory, required):
    if shutil.disk_usage(directory).free < required + 2 * 1024**3:
        raise ValueError("Insufficient free space for a backup or restore with a 2 GiB reserve")


def create_files(directory):
    validate_source_layout()
    source_bytes = int(output(*psql(DB_CONTAINER, DATABASE, DATABASE),
                              "-c", "SELECT pg_database_size(current_database())"))
    require_space(directory, source_bytes)
    source_image = output("docker", "inspect", "-f", "{{.Image}}", DB_CONTAINER)
    image = json.loads(output("docker", "image", "inspect", source_image))[0]
    db_exec = ["docker", "exec", DB_CONTAINER]
    with (directory / "roles.sql").open("wb") as roles:
        subprocess.run([*db_exec, "pg_dumpall", "-w", "--roles-only", "--no-role-passwords",
                        "--username", DATABASE, "--database", DATABASE], check=True, stdout=roles)
    # Keep the exporter alive until pg_dump has finished using the same MVCC snapshot.
    with subprocess.Popen(psql(DB_CONTAINER, DATABASE, DATABASE), stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as session:
        try:
            session.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
                                "SET LOCAL max_parallel_workers_per_gather=0;\n"
                                "SELECT pg_export_snapshot();\n" + PROOF + ";\n")
            session.stdin.flush()
            snapshot = session.stdout.readline().strip()
            if not re.fullmatch(r"[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+", snapshot):
                raise ValueError("Could not export a PostgreSQL snapshot")
            proof = json.loads(session.stdout.readline())
            if proof["django_migrations"] < 1 or "vector" not in proof["extensions"]:
                raise ValueError("Source does not match the Backend database")
            with (directory / "database.dump").open("wb") as dump:
                subprocess.run([*db_exec, "nice", "-n", "15", "pg_dump", "-w", "-Fc", "-Z1",
                                "--lock-wait-timeout=30s", "--snapshot", snapshot,
                                "--username", DATABASE, "--dbname", DATABASE], check=True, stdout=dump)
        finally:
            session.stdin.close()
        if session.wait() != 0:
            raise RuntimeError("Snapshot exporter failed")
    with (directory / "database.dump").open("rb") as dump:
        subprocess.run(["docker", "exec", "-i", DB_CONTAINER, "pg_restore", "--list"],
                       stdin=dump, stdout=subprocess.DEVNULL, check=True)
    (directory / "manifest.json").write_text(json.dumps({
        "format": 1, "source_image": source_image, "image_digests": image.get("RepoDigests") or [],
        "database_bytes": source_bytes, "proof": proof,
    }, sort_keys=True) + "\n")


def backup():
    state = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/mlai-postgres-backup"))
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    if state.is_symlink() or state.stat().st_uid != os.geteuid() or state.stat().st_mode & 0o077:
        raise ValueError("Backup scratch directory must be owned by the operator and private")
    with (state / "backup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        pending = state / "pending"
        # Remove only this job's known artifacts if a power failure interrupted the last dump.
        if pending.exists():
            if pending.is_symlink() or not pending.is_dir() or set(p.name for p in pending.iterdir()) - set(FILES):
                raise ValueError("Unexpected backup scratch contents")
            for name in FILES:
                (pending / name).unlink(missing_ok=True)
            pending.rmdir()
        pending.mkdir(mode=0o700)
        try:
            create_files(pending)
            if any((pending / n).stat().st_size == 0 for n in FILES):
                raise ValueError("Refusing to upload an empty backup file")
            proc = subprocess.run(["restic", "backup", "--host", HOST, "--tag", TAG,
                                   "--json", "--", *FILES], cwd=pending, stdout=subprocess.PIPE,
                                  text=True, check=True)
            snapshot = next(json.loads(l)["snapshot_id"] for l in proc.stdout.splitlines()
                            if json.loads(l).get("message_type") == "summary")
            options = ["--host", HOST, "--tag", TAG]
            candidates = json.loads(output("restic", "snapshots", "--json", *options))
            matches = [row["id"] for row in candidates if row["id"].startswith(snapshot)]
            if len(matches) != 1:
                raise ValueError("Could not identify the uploaded snapshot")
            print("Uploaded snapshot", matches[0], flush=True)
        finally:
            shutil.rmtree(pending)
        subprocess.run(["restic", "forget", *options, "--group-by", "host,tags",
                        "--keep-daily", "14", "--keep-last", "1", "--prune"], check=True)


def restore_member(snapshot, member, command):
    # A restore error may include COPY row contents. Keep client stderr out of shared logs.
    with subprocess.Popen(["restic", "dump", snapshot, "/" + member], stdout=subprocess.PIPE) as src:
        try:
            destination = subprocess.run(command, stdin=src.stdout, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        finally:
            src.stdout.close()
        if src.wait() != 0 or destination.returncode != 0:
            raise RuntimeError("Failed to download or restore " + member)


def restore(snapshot):
    if not re.fullmatch(r"[a-f0-9]{64}", snapshot):
        raise ValueError("Use the full snapshot ID from the backup receipt")
    candidates = json.loads(output("restic", "snapshots", "--json", "--host", HOST, "--tag", TAG))
    if snapshot not in {r["id"] for r in candidates}:
        raise ValueError("The requested snapshot is outside this deployment's backups")
    manifest = json.loads(output("restic", "dump", snapshot, "/manifest.json"))
    if manifest["format"] != 1 or manifest["proof"]["database"] != DATABASE:
        raise ValueError("Unexpected backup manifest")
    # The image must already be available locally. Use a verified digest even on another host.
    image = os.environ.get("MLAI_RESTORE_IMAGE") or manifest["source_image"]
    resolved = json.loads(output("docker", "image", "inspect", image))[0]
    if resolved["Id"] != manifest["source_image"]:
        raise ValueError("Restore image differs from the source image ID; retrieve the recorded digest")
    require_space(output("docker", "info", "--format", "{{.DockerRootDir}}"),
                  2 * manifest["database_bytes"])
    role = "mlai_restore_" + uuid.uuid4().hex[:16]
    container = output("docker", "run", "--detach", "--rm", "--network", "none", "--cpus", "1",
                       "--memory", "1024m", "--memory-swap", "1024m", "--pids-limit", "128",
                       "--name", role, "--env", "POSTGRES_USER=" + role, "--env", "POSTGRES_DB=postgres",
                       "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--volume", "/var/lib/postgresql/data",
                       resolved["Id"], "postgres", "-c", "listen_addresses=",
                       "-c", "max_parallel_maintenance_workers=0", "-c", "max_parallel_workers_per_gather=0")
    try:
        # The entrypoint's temporary initialization server also accepts pg_isready. Wait for final PID 1.
        for _ in range(120):
            ready = subprocess.run(["docker", "exec", container, "sh", "-ec",
                                    'test "$(cat /proc/1/comm)" = postgres && pg_isready -q -U "$POSTGRES_USER" -d postgres'],
                                   stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("Isolated PostgreSQL failed to start")
        restore_member(snapshot, "roles.sql", psql(container, role, "postgres"))
        restore_member(snapshot, "database.dump", ["docker", "exec", "-i", container, "pg_restore", "-w",
                       "--exit-on-error", "--create", "--username", role, "--dbname", "postgres"])
        restored = json.loads(output(*psql(container, role, DATABASE), "-c", PROOF))
        if restored != manifest["proof"]:
            raise ValueError("Restored database differs from the snapshot proof")
        print("Restore verified", snapshot, json.dumps(restored, sort_keys=True), flush=True)
    finally:
        subprocess.run(["docker", "rm", "--force", "--volumes", container], check=True,
                       stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    commands.add_parser("restore").add_argument("snapshot_id")
    args = parser.parse_args()
    os.umask(0o077)
    # The systemd service terminates the whole control group and allows finally blocks to clean up.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))
    if args.command == "backup":
        backup()
    else:
        restore(args.snapshot_id)


if __name__ == "__main__":
    main()
