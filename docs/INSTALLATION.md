# Installation

Target: Ubuntu 24.04 LTS (Python 3.12+). Development was verified on Python 3.13.16.

## Development

```bash
sudo apt install ffmpeg         # optional: video metadata, posters and transcoding (Phase 9)
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

### Remote and cloud sync (Phase 7, sync worker)

Sync jobs run in a separate worker process under the same unprivileged account. Without it, runs stay `queued` and the Remote sync page shows "Worker offline".

```bash
sudo cp deploy/systemd/nas-manager-worker.service /etc/systemd/system/
# Managed file roots outside /var/lib/nas-manager must also be writable by the
# worker: add them with `sudo systemctl edit nas-manager-worker` →
#   [Service]
#   ReadWritePaths=/srv/nas
sudo systemctl daemon-reload
sudo systemctl enable --now nas-manager-worker
journalctl -u nas-manager-worker -n 50 --no-pager
```

- The worker refuses to start until the schema is current (the web unit migrates in `ExecStartPre`, and the worker unit is ordered after it). It logs to `worker.log` in the log directory (`NAS_LOG_FILE_NAME`), separately from the web process.
- Remote credentials are encrypted with the key file `/var/lib/nas-manager/secrets/credentials.key`, created on first use (0600). **Back it up with the database** (see `BACKUP_AND_RESTORE.md`).
- **SFTP:** use **Fetch host key** in the connection form and compare the fingerprint with the server (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server) before trusting it.
- **Google Drive:** in Google Cloud Console create an OAuth client of type *Web application* with the redirect URI `https://<your NAS URL>/api/v1/remotes/oauth/callback` (enable the Google Drive API for the project). Set `NAS_PUBLIC_BASE_URL=https://<your NAS URL>` in `/etc/nas-manager/nas-manager.env`, restart `nas-manager`, create a Google Drive connection with the client id and secret, then click **Connect** and grant access. The scope is full Drive access (`https://www.googleapis.com/auth/drive`) so existing folders can be synced. The callback identifies you by your session cookie, which the browser only sends on Google's redirect with `NAS_SESSION_COOKIE_SAMESITE=lax` (the default); with `strict` the callback answers 401.
- Create jobs disabled, run **Preview** (a dry run that lists every planned action), and only then **Run now** or enable a schedule.

### Photo gallery and video (Phases 8–9)

The gallery needs no extra service: the worker above scans libraries, renders thumbnails and runs transcodes. Pillow is a pinned Python dependency (installed with `requirements.txt`); ffmpeg is a system package:

```bash
sudo apt install ffmpeg     # ffprobe + ffmpeg; without them videos are indexed and downloadable only
ffmpeg -version | head -1   # the gallery's admin status shows the same line
```

- **Where media lives.** Personal libraries: `NAS_MEDIA_DIR/<username>` (default `/var/lib/nas-manager/media`). Thumbnails, posters and transcodes: `NAS_MEDIA_CACHE_DIR` (default `/var/lib/nas-manager/media-cache`, disposable). If you put either elsewhere (e.g. a large data disk), add the path to `ReadWritePaths=` in **both** units (`systemctl edit nas-manager` and `systemctl edit nas-manager-worker`): the web process writes uploads and on-demand thumbnails, the worker writes scans, thumbnails and transcodes.
- **Shared libraries** are folders inside a configured file root (`NAS_FILE_ROOTS`). Create them in the Gallery's *Libraries* card (administrators), pick the groups that may browse them, and let the worker index them (`NAS_MEDIA_SCAN_INTERVAL_MINUTES`, default hourly; **Rescan** forces it). Files copied in over Samba appear after the next scan.
- **Transcoding** is CPU-heavy. The worker unit runs at `Nice=10` and one transcode at a time; tune `NAS_MEDIA_TRANSCODE_PRESET`, `_CRF`, `_MAX_HEIGHT`, `_THREADS` and cap the cache with `NAS_MEDIA_TRANSCODE_CACHE_MAX_BYTES`. Set `NAS_MEDIA_TRANSCODE_ENABLED=false` to serve only browser-compatible videos.
- **Phones.** A backup app authenticates with a personal API token scoped `media:read` + `media:write`, asks `POST /api/v1/media/sync/lookup` which photos are already on the NAS, and uploads the rest (`POST /api/v1/media/upload` or resumable uploads with `target: "media"`); see `API.md`.

### Monitoring and alerting (Phase 10)

The worker does all monitoring: a host sample every `NAS_MONITOR_SAMPLE_SECONDS` (default 60 s, kept `NAS_MONITOR_HISTORY_DAYS`), service and RAID checks every `NAS_MONITOR_CHECK_SECONDS`, and SMART every `NAS_MONITOR_SMART_INTERVAL_MINUTES` (only with the helper enabled). Without the worker, the Monitoring page still shows live values, processes and RAID, but there is no history, no service/SMART state and no alerting. The Alerts page and the dashboard then show a "worker not running" banner.

- **Services.** `NAS_MONITOR_SERVICES` lists the units to watch (default: `smbd`, `nmbd` and the three NAS Manager units). A unit that is not installed shows as `unknown` (not installed) and never alerts. The worker reads unit state with `systemctl show` over D-Bus as the unprivileged service account; the shipped unit allows this (`AF_UNIX`, `/run` readable). Check that it works on your host:

  ```bash
  sudo -u nas-manager systemctl show -p ActiveState smbd.service
  ```

- **Rules.** Ten built-in rules are created by the migration:
  - disk usage above 80 % (warning) and above 90 % (critical) for 5 minutes
  - memory above 90 % for 10 minutes
  - swap above 50 % for 15 minutes
  - load per core above 2 for 15 minutes
  - RAID degraded, RAID member failed, SMART failed (critical, immediate)
  - service down for 2 minutes
  - helper unavailable for 10 minutes

  Tune or disable them on the Alerts page, and add your own rules, for example one limited to a single disk path.
- **Email.** On the Alerts page, add an *Email (SMTP)* channel with your provider's server, port 587 (STARTTLS) or 465 (TLS), the account and password, a sender and the recipients. Then click **Test**. Plain SMTP requires ticking *Allow unencrypted*.
- **Webhook.** A *Webhook* channel POSTs JSON (format in `API.md`) to an HTTPS URL. This works with ntfy, Gotify, Home Assistant, Slack/Teams-compatible relays and so on. For an `Authorization: Bearer …` header, put the whole value in *Authorization header*. A *Signing secret* adds `X-NAS-Signature: sha256=<hex HMAC of the body>`.
- Failed deliveries are retried with backoff (`NAS_ALERT_NOTIFY_MAX_ATTEMPTS`, default 5). After the last attempt the failure is written to the audit log (`alert.notification_failed`) and the worker log.

Verify:

```bash
systemctl status nas-manager
journalctl -u nas-manager -n 50 --no-pager
curl -s http://127.0.0.1:8080/api/v1/health
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/login   # 200
```

### Troubleshooting

The unit applies strict sandboxing (`SystemCallFilter`, `MemoryDenyWriteExecute`, `ProtectSystem=strict`) that was validated with `systemd-analyze` but **not run under a live systemd**. ffmpeg/ffprobe under these sandboxes (web and worker units) is likewise unverified on a real host: if probing or transcoding fails only under systemd, check `journalctl` for `SIGSYS`/`mprotect` errors and the media status card's last transcode error. If the service exits with `SIGSYS`, an `mprotect`/`mmap` error or a "read-only file system" error in `journalctl -u nas-manager`, relax the corresponding directive with a drop-in (`systemctl edit nas-manager`) and report which one was needed.

## Upgrading

1. Back up `/var/lib/nas-manager` (see `BACKUP_AND_RESTORE.md`).
2. Replace application files, reinstall `requirements.txt`.
3. `sudo systemctl restart nas-manager` (migrations apply automatically), then `sudo systemctl restart nas-manager-worker` if the sync worker is installed.
4. After upgrading to Phase 9 with ffmpeg installed, videos indexed earlier are probed on their library's next scan (or press **Rescan**).

## Dependency pinning

`requirements.txt` is the full pinned runtime set, generated in a clean virtualenv with `pip freeze`. Regenerate it the same way after changing a dependency. `requirements-dev.txt` adds test and lint tooling. Bootstrap 5.3.8 is vendored in `app/web/static/vendor/bootstrap/` (license included).
