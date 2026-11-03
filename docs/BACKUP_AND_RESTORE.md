# Backup and Restore

> **Interim, manual procedure.** Automated, versioned, verified backups and tested restore workflows are Phase 11 and do not exist yet.

## What state exists (Phases 1–9)

| Path (systemd deployment) | Contents |
|---------------------------|----------|
| `/var/lib/nas-manager/nas.db` (+ `-wal`, `-shm`) | SQLite database: `app_settings`, users, groups, roles/permissions, sessions (token SHA-256 only), audit events, public-upload records and quotas, API tokens (SHA-256 only), open resumable uploads (`upload_sessions`), the file change journal (`file_changes`), remote connections (credentials encrypted), sync jobs, per-file sync records, run history and conflicts, media libraries and their group grants, the media index (`media_items`, incl. EXIF metadata and SHA-256), albums, the media change journal and transcode records |
| `/var/lib/nas-manager/secrets/credentials.key` | Fernet key that decrypts the stored remote credentials (Phase 7). **Without it every remote credential must be re-entered.** Anyone holding both the key and the database can decrypt the credentials, so keep backups of the key protected (and preferably stored separately from database copies). |
| `/var/lib/nas-manager/media/` (`NAS_MEDIA_DIR`) | **User data**: every user's personal photo/video library (`<username>/…`, Phase 8). Back it up like any other share. |
| `/var/lib/nas-manager/media-cache/` (`NAS_MEDIA_CACHE_DIR`) | Thumbnails, video posters and transcodes (Phases 8–9). **Disposable**: exclude it from backups; it is regenerated on demand (transcodes on the next request). |
| `/var/log/nas-manager/` | Rotated application logs |
| `/etc/nas-manager/nas-manager.env` | Configuration (sensitive once production overrides are in place) |

Sessions are stored in the database, so a restore also restores login state; the password hashes are Argon2id (safe to back up). To invalidate all sessions after a restore run `python -m app.cli bootstrap-admin --username <admin> --reset-password` or delete rows from `auth_sessions`.

API tokens are restored too, including any revoked after the backup was taken. After restoring an older backup, review **API tokens** (`/tokens`) and revoke anything that should no longer work. Open resumable uploads in the backup refer to `.nas-resumable-<id>.part` staging files in the destination folders; if those files were not restored with the data, the uploads fail cleanly and clients start over. Restoring the database also rewinds the sync journal, so a client holding a newer cursor gets `reset: true` and re-lists.

Restoring an older database also rewinds the remote sync records (`sync_job_entries`). Sync never hard-deletes, so the worst outcome is extra work: files that changed since the backup are treated as conflicts or first-sync comparisons and kept in both versions. Stop the worker (`systemctl stop nas-manager-worker`) during a restore and preview each job before re-enabling it. Files archived by sync live in `.nas-sync-archive/` inside the synced directories (locally and on the remotes) and are ordinary user data for backup purposes.

The media index can always be rebuilt from the files (a library rescan), but albums, library grants and the per-file journal live only in the database. Restoring an older database next to newer media files is safe: the next scan indexes files it does not know, drops rows whose files are gone (and their album entries) and re-reads changed files; mobile clients holding a newer media cursor get `reset: true` and re-list. Restoring media files without the database leaves them in place as plain files; the gallery re-indexes personal libraries once their owners exist again, and shared libraries must be re-registered.

## Manual backup

Use SQLite's online backup so the copy is consistent while the service runs. Never copy `nas.db` alone while WAL files exist.

```bash
sudo -u nas-manager sqlite3 /var/lib/nas-manager/nas.db ".backup '/var/lib/nas-manager/nas-backup.db'"
sudo -u nas-manager sqlite3 /var/lib/nas-manager/nas-backup.db "PRAGMA integrity_check;"   # expect: ok
sudo cp -p /etc/nas-manager/nas-manager.env /root/nas-manager.env.bak
sudo cp -p /var/lib/nas-manager/secrets/credentials.key /root/nas-manager-credentials.key.bak   # Phase 7; keep it protected
sudo rsync -a /var/lib/nas-manager/media/ /path/to/backup/media/   # Phase 8 personal libraries (skip media-cache/)
```

Move the backup off the machine.

## Manual restore

```bash
sudo systemctl stop nas-manager-worker nas-manager
sudo mv /var/lib/nas-manager/nas.db /var/lib/nas-manager/nas.db.before-restore   # keep, do not delete
sudo mv -f /var/lib/nas-manager/nas.db-wal /var/lib/nas-manager/nas.db-wal.before-restore 2>/dev/null || true
sudo mv -f /var/lib/nas-manager/nas.db-shm /var/lib/nas-manager/nas.db-shm.before-restore 2>/dev/null || true
sudo install -m 0640 -o nas-manager -g nas-manager /path/to/nas-backup.db /var/lib/nas-manager/nas.db
# Phase 7: restore the credentials key that matches this database (0600, owned by the service user)
sudo install -m 0600 -o nas-manager -g nas-manager /root/nas-manager-credentials.key.bak /var/lib/nas-manager/secrets/credentials.key
sudo systemctl start nas-manager nas-manager-worker
curl -s http://127.0.0.1:8080/api/v1/health      # status should be "ok"
```

If `status` is `degraded`, the backup predates a migration: `sudo -u nas-manager /opt/nas-manager/.venv/bin/alembic upgrade head` (run from `/opt/nas-manager`).

## Not covered yet

Automated copies of user data (file roots, personal media libraries), Samba configuration, shares, remote backup targets, retention, integrity manifests and restore testing arrive with Phases 3, 11 and 12. This procedure has **not** been tested against a real systemd deployment; the SQLite commands are standard but should be rehearsed before relying on them.
