# Backup and Restore

> **Interim, manual procedure.** Automated, versioned, verified backups and tested restore workflows are Phase 11 and do not exist yet.

## What state exists in Phase 2

| Path (systemd deployment) | Contents |
|---------------------------|----------|
| `/var/lib/nas-manager/nas.db` (+ `-wal`, `-shm`) | SQLite database: `app_settings`, users, groups, roles/permissions, sessions (token SHA-256 only), audit events |
| `/var/log/nas-manager/` | Rotated application logs |
| `/etc/nas-manager/nas-manager.env` | Configuration (sensitive once production overrides are in place) |

Sessions are stored in the database, so a restore also restores login state; the password hashes are Argon2id (safe to back up). To invalidate all sessions after a restore run `python -m app.cli bootstrap-admin --username <admin> --reset-password` or delete rows from `auth_sessions`.

## Manual backup

Use SQLite's online backup so the copy is consistent while the service runs. Never copy `nas.db` alone while WAL files exist.

```bash
sudo -u nas-manager sqlite3 /var/lib/nas-manager/nas.db ".backup '/var/lib/nas-manager/nas-backup.db'"
sudo -u nas-manager sqlite3 /var/lib/nas-manager/nas-backup.db "PRAGMA integrity_check;"   # expect: ok
sudo cp -p /etc/nas-manager/nas-manager.env /root/nas-manager.env.bak
```

Move the backup off the machine.

## Manual restore

```bash
sudo systemctl stop nas-manager
sudo mv /var/lib/nas-manager/nas.db /var/lib/nas-manager/nas.db.before-restore   # keep, do not delete
sudo mv -f /var/lib/nas-manager/nas.db-wal /var/lib/nas-manager/nas.db-wal.before-restore 2>/dev/null || true
sudo mv -f /var/lib/nas-manager/nas.db-shm /var/lib/nas-manager/nas.db-shm.before-restore 2>/dev/null || true
sudo install -m 0640 -o nas-manager -g nas-manager /path/to/nas-backup.db /var/lib/nas-manager/nas.db
sudo systemctl start nas-manager
curl -s http://127.0.0.1:8080/api/v1/health      # status should be "ok"
```

If `status` is `degraded`, the backup predates a migration: `sudo -u nas-manager /opt/nas-manager/.venv/bin/alembic upgrade head` (run from `/opt/nas-manager`).

## Not covered yet

User data, Samba configuration, shares, remote backup targets, retention, integrity manifests and restore testing arrive with Phases 3, 11 and 12. This procedure has **not** been tested against a real systemd deployment; the SQLite commands are standard but should be rehearsed before relying on them.
