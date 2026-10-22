# Installation

Target: Ubuntu 24.04 LTS (Python 3.12+). Development was verified on Python 3.13.16.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env            # edit as needed; NAS_ENVIRONMENT=development is convenient
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli bootstrap-admin     # create the first administrator (prompts for the password)
.venv/bin/python -m app         # http://127.0.0.1:8080
```

Checks:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
```

## Production (systemd)

Authentication is available since Phase 2; the default `NAS_HOST=127.0.0.1` still fits an SSH-tunnel workflow: `ssh -L 8080:127.0.0.1:8080 nas-host`. To expose the service, put a TLS-terminating reverse proxy in front and set `NAS_SESSION_COOKIE_SECURE=true` (the deploy template enables it).

```bash
# 1. Service account (no login shell, no home directory)
sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin nas-manager

# 2. Application files
sudo apt install python3-venv
sudo mkdir -p /opt/nas-manager
sudo cp -r app migrations deploy docs alembic.ini requirements.txt /opt/nas-manager/
sudo python3 -m venv /opt/nas-manager/.venv
sudo /opt/nas-manager/.venv/bin/pip install -r /opt/nas-manager/requirements.txt

# 3. Configuration (root-owned, readable by the service group)
sudo install -d -m 0750 -o root -g nas-manager /etc/nas-manager
sudo install -m 0640 -o root -g nas-manager deploy/nas-manager.env.example /etc/nas-manager/nas-manager.env
#    Review /etc/nas-manager/nas-manager.env; it ships NAS_SESSION_COOKIE_SECURE=true.

# 4. Unit
sudo cp deploy/systemd/nas-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nas-manager

# 5. First administrator (migrations run in ExecStartPre, then bootstrap)
#    The password is read interactively (or from stdin with --password-stdin);
#    it is never taken from argv, an environment variable or a log file.
sudo -u nas-manager /opt/nas-manager/.venv/bin/python -m app.cli bootstrap-admin --username admin
```

The unit sets `NAS_DATA_DIR=/var/lib/nas-manager` and `NAS_LOG_DIR=/var/log/nas-manager`; systemd creates both with the service user as owner. Migrations run in `ExecStartPre`.

Use `deploy/nas-manager.env.example` (not the development `.env.example`) for the service: systemd lets `EnvironmentFile=` override `Environment=`, so a `NAS_DATA_DIR` in the env file would override the unit's value.

### File manager and Samba (Phase 3)

- **File manager roots.** Set `NAS_FILE_ROOTS` to a JSON list of absolute directories you want to manage, e.g. `NAS_FILE_ROOTS='["/mnt/storage"]'`. When unset the file manager uses `<DATA_DIR>/files`. The web process must be able to read/write those directories (chown them to `nas-manager` or the group it belongs to). `NAS_MAX_UPLOAD_BYTES` caps each upload (default 2 GiB).
- **Samba.** The app manages shares in its database and renders them into `smb.conf`. The web process runs unprivileged and **cannot write `/etc/samba/smb.conf`** in the default setup, so `POST /samba/apply` will report a `configuration_error` until a privileged path exists:
  - Phase 5 introduces an allowlisted helper that will write the config and reload `smbd`. Until then, an operator can apply the rendered config manually (copy it out of the app's status/`apply` flow, or run the service with a writable `NAS_SAMBA_CONFIG_PATH` in a development setup) and run `sudo smbcontrol all reload-config`.
  - For a dev/test setup that writes directly, point `NAS_SAMBA_CONFIG_PATH` at a writable file and `NAS_SAMBA_BACKUP_DIR` at a writable backup directory (default `<DATA_DIR>/samba-backups`).
  - Samba must be installed on the host for validation (`testparm`). `apt install samba` provides it.
- **Operators and Files/Samba.** By default only admin (or custom roles with `files:*`/`samba:*`) can open the Files and Samba pages. To let operators *view* them read-only, enable the **Settings → "Allow operators to view the Files and Samba pages"** toggle (stored per-installation; writes are never granted by it).

Verify:

```bash
systemctl status nas-manager
journalctl -u nas-manager -n 50 --no-pager
curl -s http://127.0.0.1:8080/api/v1/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login   # 200
```

### Troubleshooting

The unit applies strict sandboxing (`SystemCallFilter`, `MemoryDenyWriteExecute`, `ProtectSystem=strict`) that was validated with `systemd-analyze` but **not run under a live systemd**. If the service exits with `SIGSYS`, an `mprotect`/`mmap` error or a "read-only file system" error in `journalctl -u nas-manager`, relax the corresponding directive with a drop-in (`systemctl edit nas-manager`) and report which one was needed.

## Upgrading

1. Back up `/var/lib/nas-manager` (see `BACKUP_AND_RESTORE.md`).
2. Replace application files, reinstall `requirements.txt`.
3. `sudo systemctl restart nas-manager` (migrations apply automatically).

## Dependency pinning

`requirements.txt` is the full pinned runtime set, generated in a clean virtualenv with `pip freeze`. Regenerate it the same way after changing a dependency. `requirements-dev.txt` adds test and lint tooling. Bootstrap 5.3.8 is vendored in `app/web/static/vendor/bootstrap/` (license included).
