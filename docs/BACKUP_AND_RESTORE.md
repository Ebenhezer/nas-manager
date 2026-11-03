# Backup and Restore

Phase 11 adds automated, encrypted, versioned backups with integrity verification, retention and tested restore workflows. Backups are made by the worker (`python -m app.worker`) and managed on the **Backups** page or through the API (`docs/API.md`). Configuration export and import (Phase 12) is described [at the end](#configuration-export-and-import-phase-12).

## Concepts

| Term | Meaning |
|------|---------|
| Repository | An encrypted, deduplicated store on a **target**: a folder under one of `NAS_BACKUP_LOCAL_ROOTS` (a USB disk, a second disk or an NFS mount; *external storage*) or a path on a remote connection (SFTP, FTP/FTPS, WebDAV, S3-compatible, Google Drive). |
| Job | What to back up into which repository, on which schedule, kept for how long. |
| Snapshot | One completed backup of one job: a complete, independently restorable version of its source. |
| Run | One execution of a backup, verify, prune or restore, shown under *Recent runs*. |

### Sources and directions

| Source | Direction | What it contains |
|--------|-----------|------------------|
| `files` | push (NAS → target) | A folder inside a managed file root (`NAS_FILE_ROOTS`). |
| `media` | push | The personal photo and video libraries (`NAS_MEDIA_DIR`), or one user's folder. The thumbnail/transcode cache is never included. |
| `system` | push | A consistent online copy of the SQLite database (SQLite backup API, then `PRAGMA integrity_check`), the credentials key (`credentials.key`) and the Samba configuration (`smb.conf`, if readable). |
| `remote` | **pull** (remote server → NAS) | A folder on a remote connection, downloaded and stored in a *local* repository. Use it to keep versioned copies of another server's data on the NAS. |

Restores go in the opposite direction: into a file root, the media libraries, a remote connection, or a staging area for system restores.

## Repository format

```
<repository>/
  repo.json            format, version, scrypt parameters and a key-check value (no secrets)
  snapshots/<id>.snap  encrypted, compressed manifest: every file's path, size, mtime, mode, SHA-256
  blobs/<aa>/<name>    encrypted file content, stored once however many snapshots use it
```

- **Encryption.** The passphrase is stretched with scrypt (`NAS_BACKUP_SCRYPT_N`, r=8, p=1; parameters stored in `repo.json`). HKDF-SHA256 derives a content key and a naming key. Content and manifests are encrypted with AES-256-GCM in 4 MiB segments, each with a random nonce and its index and "last segment" flag authenticated, so corruption, truncation and reordering are detected. Blob names are an HMAC of the content hash, so the storage provider learns neither file names nor content hashes. File sizes and the number of files are visible to the provider as blob sizes.
- **Deduplication.** Identical content is stored once per repository, across snapshots and jobs. Unchanged files (same size and modification time as in the job's previous snapshot) are not even re-read.
- **Atomicity.** A snapshot is written after all of its blobs. An interrupted or cancelled backup leaves no snapshot; its already-uploaded blobs are reused by the next run or removed by garbage collection.

> **Keep the passphrase.** It is stored (encrypted with the credentials key) so scheduled backups can run, but if the NAS is lost, the passphrase is the only way to read the backups. Write it down or keep it in a password manager, separately from the backups.

## Setting up

1. **Choose a target.**
   - *External storage:* mount the disk (Disks page, or your own `fstab` entry), then add the mountpoint to `NAS_BACKUP_LOCAL_ROOTS` (a JSON list, e.g. `["/mnt/backup-usb"]`) and to the worker unit's `ReadWritePaths=` (see `INSTALLATION.md`). Without configuration the root is `<data_dir>/backups`, which is on the same disk as the data: it protects against mistakes and ransomware-style overwrites through the app, **not** against disk failure.
   - *Remote:* create a remote connection on the Remote sync page first (use a dedicated account restricted to one folder or bucket).
2. **Add a repository** (Backups → Add repository): name, target, a folder, and a passphrase of at least 12 characters. Saving initializes it:
   - an empty or new folder becomes a new repository;
   - a folder that already holds a repository is **adopted** when the passphrase unlocks it, and its snapshots appear in the list (this is how you reconnect after a reinstall);
   - a non-empty folder that is not a repository is refused, so backups never mix with other data.
3. **Add jobs.** Pick the repository, the source, an optional schedule (minutes) and retention. Enabling a scheduled job or a retention policy asks for confirmation, because it then runs and deletes old snapshots without further prompts.

A sensible starting point for one NAS:

| Job | Source | Schedule | Retention |
|-----|--------|----------|-----------|
| system | system state | every 1440 min | 7 last, 7 daily, 4 weekly, 6 monthly |
| shares | the main file root | every 1440 min | 7 / 7 / 4 / 6 |
| photos | media | every 1440 min | 7 / 7 / 4 / 12 |

Keep at least one repository **off the machine** (remote, or a disk that is unplugged between backups).

## Retention and pruning

Each job keeps the newest `keep_last` snapshots plus the newest snapshot of each of the last `keep_daily` days, `keep_weekly` ISO weeks and `keep_monthly` months. The newest snapshot is always kept. With every value at 0 the job keeps everything.

- Retention runs automatically after each successful backup of the job, and on demand with **Prune…** (a dry-run preview first, then a confirmed run).
- After snapshots are removed, garbage collection deletes content no remaining snapshot references. It **refuses to delete anything** if any snapshot in the repository cannot be read (an unreadable snapshot might still need that content).
- Snapshots of deleted jobs are never pruned automatically.
- Deleting a repository or job in the app never touches the data on the target.

## Verification

**Verify** checks every snapshot in a repository:

- *full* (the default in the UI) downloads and decrypts every referenced blob and compares its SHA-256 with the manifest;
- *quick* (`"full": false` over the API) only checks that every blob exists.

Each snapshot gets a status (`ok` or `failed` with the reason), the repository records the last result, and a failure is audited (`backup.verify_failed`). Verification also rebuilds the local catalogue and blob cache from the target, so it repairs the app's view after a database restore.

Schedule a full verify at least monthly for each repository; for large remote repositories a quick verify weekly is a cheaper alternative. An alert rule for verify failures is not built in yet; watch the audit log or the Backups page.

## Restoring

### Files, media and pulled data

On the Backups page, pick a snapshot, **Browse** it if needed, then **Restore…**:

- **Into a new folder (default).** Files go to `restored/<snapshot>` inside the chosen file root (or any folder you type). Nothing existing is touched.
- **Selected paths only.** List files or folders, one per line.
- **In place.** Choose the original folder and tick *Replace existing files*. You are asked to confirm, and every file that is replaced is first moved to `.nas-backup-replaced/<timestamp>/` inside the target folder: **nothing is deleted**. Without the tick, existing files are skipped.
- **Preview.** *Dry run* reports what would be written, skipped and replaced.
- **To a remote server.** Choose *A remote connection*; replaced files are moved to `.nas-backup-replaced/` on the remote.

Every restored file is written to a temporary name, checked against its SHA-256 and only then renamed into place, with its permission bits and modification time restored. Files that fail the check are reported and not written.

### System state (database, credentials key)

The running application cannot replace its own database, so a system restore is two steps:

1. **Stage it** (Backups page: restore a *system state* snapshot to *Staging*). The worker decrypts and verifies the files into `<staging dir>/restore/<run id>/` and writes `restore.json` with their checksums.
2. **Install it** from the command line with the services stopped:

   ```bash
   sudo systemctl stop nas-manager-worker nas-manager
   sudo -u nas-manager /opt/nas-manager/.venv/bin/python -m app.cli restore-system --run <run id> --yes
   sudo systemctl start nas-manager nas-manager-worker
   ```

   The command re-checks every staged file's checksum, renames the current `nas.db` (and `-wal`/`-shm`) and, if different, `credentials.key` to `*.before-restore-<timestamp>` (never deleted), installs the staged copies (database `0640`, key `0600`), applies migrations (an older backup is upgraded) and records `backup.system_restored` in the restored audit log. Without `--yes` it only explains what it would do.

3. **Samba.** `smb.conf` is staged next to the database but not written to `/etc`. The restored database contains the share definitions: open **Samba** and apply the configuration to regenerate the managed section. Compare with the staged `smb.conf` if you had unmanaged settings.

What a database restore rewinds (all as before Phase 11): sessions and API tokens return to their state at backup time (review **API tokens** and revoke anything that should not work); sync journals and media change journals rewind, so mobile clients get `reset: true` and re-list; remote sync records rewind, which makes later changes look like first-sync comparisons (sync never deletes, so the cost is extra work); open alerts are re-evaluated. Backup history rewinds too: snapshots made after the system backup are missing from the catalogue until you run **Verify** on each repository, which re-adds them.

### Disaster recovery (the NAS or its database is gone)

1. Install NAS Manager (`INSTALLATION.md`) and bootstrap an administrator.
2. Make the repository readable locally: plug in or mount the backup disk, or copy a remote repository to a local folder (e.g. with `rsync`, `rclone` or the provider's tools; the copy contains only encrypted data).
3. List the snapshots and restore the system state into an empty folder:

   ```bash
   sudo -u nas-manager /opt/nas-manager/.venv/bin/python -m app.cli backup-recover --repo-dir /mnt/backup-usb/nas
   sudo -u nas-manager /opt/nas-manager/.venv/bin/python -m app.cli backup-recover --repo-dir /mnt/backup-usb/nas \
       --snapshot <system snapshot id> --target /var/lib/nas-manager/recovered
   ```

   The passphrase is prompted for (or read from stdin with `--passphrase-stdin`), never taken from argv. `--path` restricts the restore to some files; the target must be empty or new. Integrity is checked per file, and the command exits non-zero if anything failed.
4. With the services stopped, install the recovered `nas.db` and `credentials.key` as in the manual procedure below, start the services, then **Initialize** each repository on the Backups page (it is adopted, and its snapshots reappear) and restore the data folders through the UI.

## Manual procedure (no backup job configured)

Still valid, for example before an upgrade:

```bash
sudo -u nas-manager sqlite3 /var/lib/nas-manager/nas.db ".backup '/var/lib/nas-manager/nas-backup.db'"
sudo -u nas-manager sqlite3 /var/lib/nas-manager/nas-backup.db "PRAGMA integrity_check;"   # expect: ok
sudo cp -p /etc/nas-manager/nas-manager.env /root/nas-manager.env.bak
sudo cp -p /var/lib/nas-manager/secrets/credentials.key /root/nas-manager-credentials.key.bak   # keep it protected
```

Manual restore:

```bash
sudo systemctl stop nas-manager-worker nas-manager
sudo mv /var/lib/nas-manager/nas.db /var/lib/nas-manager/nas.db.before-restore   # keep, do not delete
sudo mv -f /var/lib/nas-manager/nas.db-wal /var/lib/nas-manager/nas.db-wal.before-restore 2>/dev/null || true
sudo mv -f /var/lib/nas-manager/nas.db-shm /var/lib/nas-manager/nas.db-shm.before-restore 2>/dev/null || true
sudo install -m 0640 -o nas-manager -g nas-manager /path/to/nas-backup.db /var/lib/nas-manager/nas.db
sudo install -m 0600 -o nas-manager -g nas-manager /path/to/credentials.key /var/lib/nas-manager/secrets/credentials.key
sudo systemctl start nas-manager nas-manager-worker
curl -s http://127.0.0.1:8080/api/v1/health      # "ok"; "degraded" means: alembic upgrade head
```

## Configuration export and import (Phase 12)

An export is a JSON file with the configuration (users, groups and roles, Samba shares, remote connections, sync jobs, shared media libraries, alert rules and channels, upload quotas, backup repositories and jobs, and runtime settings), not the data. Use it to move a configuration to a new installation, keep a readable record of settings, or undo a bad change. It complements *system* backups, which contain everything (and need the services stopped to restore).

- **Export** (Configuration page, or `POST /api/v1/config/export`). Without a passphrase, the file contains **no secrets**: no password hashes, credentials, channel secrets or backup passphrases; imported users then stay inactive until their passwords are reset, and credentials must be re-entered. With a passphrase (12+ characters), those secrets are encrypted in the file (scrypt + AES-256-GCM) and the file is protected against modification. Treat even a passphrase-less export as sensitive: it lists users, e-mail addresses and server names.
- **Import.** Choose the file, give its passphrase, pick *Merge* (create and update; never delete) or *Replace* (also delete shares, groups, jobs, alert rules and channels and quotas that are not in the file), and **Preview**. The preview runs the real import and rolls it back, so it shows exactly what would change, every problem and every warning. **Apply** is all-or-nothing: if any object is invalid, nothing changes. Warnings (jobs that will start running, deletions, users without passwords, missing credentials) need confirmation.
- **Accounts in the file win.** A user in the file updates the user with the same name here, including the importing administrator: with a sealed export, `admin`'s password becomes the one from the source installation. Remove users from the file (or export without a passphrase) if that is not wanted.
- **Safety.** Paths are re-validated on this host (missing folders are an error for jobs and libraries; Samba shares with a missing path are imported disabled). The import can never leave the system without an administrator or remove your own admin rights. Imported backup repositories must be *initialized* on the Backups page (the existing repository is adopted). `smb.conf` is not rewritten automatically: open **Samba** and apply.
- **Rollback.** Every applied import stores an encrypted snapshot of the configuration before it (with secrets, encrypted with the credentials key) and, for SQLite, a copy of the database file in `<data_dir>/config-snapshots/before-import-<id>.db` (the last 10 are kept). **Roll back** on the most recent import deletes what it created and restores everything it changed or deleted. Changes made after the import to the same objects are overwritten. Roll back newer imports first. The database copy is for manual recovery only (install it like a manual restore, with the services stopped).

## What to back up

| Path (systemd deployment) | Back up? |
|---------------------------|----------|
| `/var/lib/nas-manager/nas.db` | Yes: a *system* job (it copies it consistently while the service runs). |
| `/var/lib/nas-manager/secrets/credentials.key` | Yes: included in *system* snapshots. Whoever has both the key and the database can decrypt every stored remote credential, channel secret and repository passphrase, which is why the repository itself is encrypted with a passphrase you keep elsewhere. |
| File roots (`NAS_FILE_ROOTS`) | Yes: *files* jobs. |
| `/var/lib/nas-manager/media/` | Yes: a *media* job. |
| `/var/lib/nas-manager/media-cache/` | No: disposable, regenerated on demand. |
| `/var/lib/nas-manager/backup-staging/` | No: temporary files and staged restores. |
| `/etc/nas-manager/nas-manager.env` | Keep a copy with your installation notes (not covered by jobs; it is outside the data directory). |
| `/var/log/nas-manager/` | Optional. |

## Limitations

- The app's own database must be SQLite for *system* jobs (PostgreSQL deployments need their own dump procedure).
- No bandwidth limit, no partial-file resume (an interrupted large file is re-uploaded), and no compression of file content (manifests are compressed).
- Changing a repository's passphrase in the app does not re-encrypt the repository; it only changes the passphrase the app uses, and initialization then checks that it unlocks the repository. To rotate the key, create a new repository and back up into it.
- Only one run per repository executes at a time, and the worker runs one backup-related run at a time overall.
- Files changing while they are read can be stored in an inconsistent state (no filesystem snapshots); stop writers or back up snapshots made by the filesystem (e.g. ZFS/Btrfs) for strict consistency.
- Restores and disaster recovery were exercised against local and SFTP (loopback stub) repositories and the real SQLite database, not against real cloud providers or after a real hardware loss.
