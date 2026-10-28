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
- **Samba.** The app manages shares in its database and renders them into `smb.conf`. The web process runs unprivileged and **cannot write `/etc/samba/smb.conf`** by itself. Deploy the Phase 5 helper (`nas-manager-helper.service`, below) and set `NAS_HELPER_ENABLED=true`: **Apply** then writes the validated config through the helper (re-validating with `testparm -s`, backing up, atomic write) and reloads `smbd`, and the Samba page shows `reload_required: false`.
  - For a dev/test setup without the helper, point `NAS_SAMBA_CONFIG_PATH` at a writable file and `NAS_SAMBA_BACKUP_DIR` at a writable backup directory (default `<DATA_DIR>/samba-backups`); Apply then writes the config and reports `reload_required: true`. Samba must be installed on the host for validation (`testparm`; `apt install samba`).
- **Operators and Files/Samba.** By default only admin (or custom roles with `files:*`/`samba:*`) can open the Files and Samba pages. To let operators *view* them read-only, enable the **Settings → "Allow operators to view the Files and Samba pages"** toggle (stored per-installation; writes are never granted by it).
- **Public upload folder (Phase 4).** The drop folder lives at `NAS_PUBLIC_UPLOAD_DIR` (default `<DATA_DIR>/uploads`, auto-created and owned by the service user; chown it there if you set a custom path). `NAS_PUBLIC_UPLOAD_MAX_FILE_BYTES` caps each file (default 1 GiB) and `NAS_PUBLIC_UPLOAD_DEFAULT_QUOTA_BYTES` sets the per-uploader quota (default 1 GiB). Public uploads are on by default; the **Settings → "Accept public uploads"** toggle and `NAS_PUBLIC_UPLOAD_ENABLED` both disable them (403). Administer quotas and cleanup on the **Uploads** page (needs `uploads:read`/`uploads:write`; the built-in operator role gets read-only). If the folder is exposed publicly, terminate TLS in front and consider rate-limiting `/public/upload` at your reverse proxy.

### Disks, storage and RAID (Phase 5, privileged helper)

The web process stays unprivileged. A second root service performs discovery and the privileged actions:

```bash
# 1. Install the helper's requirements (same venv as the app).
sudo apt install smartmontools mdadm mount util-linux

# 2. Helper unit (runs `python -m app.helper` as root)
sudo cp deploy/systemd/nas-manager-helper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nas-manager-helper

# 3. Enable the web process to use it and authorise the service account.
#    In /etc/nas-manager/nas-manager.env (web + helper share the file):
#      NAS_HELPER_ENABLED=true
#      NAS_HELPER_SOCKET=/run/nas-manager/helper.sock
#      NAS_HELPER_ALLOWED_UIDS=<numeric uid of the nas-manager user>
#      NAS_HELPER_SOCKET_GROUP=nas-manager
sudo systemctl restart nas-manager nas-manager-helper
journalctl -u nas-manager-helper -n 50 --no-pager
```

Then open **Disks** in the UI: physical disks, mounted volumes and RAID arrays render from the helper; the badge in the header shows `helper online`. Mount/unmount/scrub flows always show a **preview first** (the exact action, warnings and data-loss notes) and require an explicit confirmation — nothing is ever mounted, unmounted or scrubbed automatically.

- The helper binds `/run/nas-manager/helper.sock` (`0660 root:<group>`) and accepts connections only from root or the UIDs in `NAS_HELPER_ALLOWED_UIDS` (the service user's UID; `SO_PEERCRED`-enforced) — no password/secret is shared over the wire.
- Discovery (`lsblk`, `findmnt`, `/proc/mdstat`, `smartctl`) and actions (`mount`, `umount`, md `check` scrub, `smbcontrol all reload-config`) run in the helper with a fixed allowlist; the web process sends only validated parameter objects, never command lines.
- With `NAS_HELPER_ENABLED=true`, **Samba Apply** now also writes `/etc/samba/smb.conf` (via the helper) and reloads `smbd`; the Samba page shows the helper state and `reload_required: false`.
- **Test on a scratch disk only** (USB stick or a spare drive): format a test filesystem (`sudo mkfs.ext4 /dev/sdX`), then use the Disks page to mount it under `/mnt` and unmount it again. Never point the page at a disk holding data.

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
