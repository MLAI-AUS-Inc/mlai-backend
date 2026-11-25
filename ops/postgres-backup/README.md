# Backend PostgreSQL recovery

This is a logical recovery point for the production `medhack` database and its roles. The daily timer runs at 03:35 Australia/Melbourne with up to ten minutes of jitter. It keeps 14 days with successful snapshots. Changes after the latest dump require a newer backup or separately configured WAL archiving.

Each snapshot contains a PostgreSQL custom-format database archive, role definitions without passwords, and an aggregate manifest. The archive and manifest share an exported read-only snapshot. The manifest records the source PostgreSQL image identity, extension versions, public table/index/sequence totals, migration inventory and Roo purchase count. The backup job uses a private staging directory on the database host. It validates the dump before uploading and removes temporary files. Interrupted uploads and failed dumps do not advance retention. Restic encrypts the repository. Store its recovery password and a retrieval credential independently of the source host. Preserve application credentials in their existing secret store for re-establishing role passwords during disaster recovery.

## Install

Use a **dedicated private off-host repository**. Scope the storage credential to that repository. Run as root on the production database host with Docker, Python 3 and restic 0.16.4 or later. The service expects the existing production database container and local socket authentication.

1. Install `backup.py` as mode 0755 at `/usr/local/lib/mlai-postgres-backup/backup.py`. Install both unit files at `/etc/systemd/system/`.
2. Create root-owned directory `/etc/mlai-postgres-backup` with mode 0700 and files `restic-password` and `repository.env` with mode 0600. Use systemd-compatible shell assignments in the environment file. Set `RESTIC_REPOSITORY` to the dedicated repository. For an S3-compatible repository, also supply its scoped `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and `AWS_DEFAULT_REGION`. Keep secret values out of shell history, source and logs.
3. In a private root shell, run the following to load the configuration. For a new dedicated repository, use `restic init` once. For an existing repository, use `restic snapshots` and verify its identity before installation.

   ```sh
   set -a
   . /etc/mlai-postgres-backup/repository.env
   set +a
   export RESTIC_PASSWORD_FILE=/etc/mlai-postgres-backup/restic-password
   ```

4. Run `systemctl daemon-reload`, then `systemctl start mlai-postgres-backup.service`. Read the full uploaded snapshot ID from `journalctl -u mlai-postgres-backup.service`. Complete the restore rehearsal below before enabling the schedule with `systemctl enable --now mlai-postgres-backup.timer`.
5. Check `systemctl list-timers mlai-postgres-backup.timer`, the last service result and the timestamp of the newest matching snapshot. A missed backup leaves the previous snapshots accessible.

## Restore rehearsal

Run the following in a root shell with the same repository environment and recovery password. The command downloads the actual off-host snapshot and uses a disposable database container with no network, no host ports, one CPU, 1 GiB memory and a fresh anonymous Docker volume. It does not connect to the production database. Allow room on Docker's filesystem for a full restored database, indexes and temporary WAL. On another host, pull the digest recorded in the manifest for the same platform and set `MLAI_RESTORE_IMAGE` to that reference. Its local image ID must equal the manifest image ID.

```sh
/usr/local/lib/mlai-postgres-backup/backup.py restore FULL_SNAPSHOT_ID
```

A successful run prints the snapshot ID and matching aggregate evidence. Restore errors stop the rehearsal and the disposable container and volume are removed. Store the result and date with the backup receipt. If an operator or the host is killed abruptly, inspect containers with the `mlai_restore_` name prefix and remove only the rehearsal container and its anonymous volume. Application promotion, authentication reset and traffic cutover require a separate recovery operation.

To stop future backups, run `systemctl disable --now mlai-postgres-backup.timer`. Retain the repository and its recovery credentials.
