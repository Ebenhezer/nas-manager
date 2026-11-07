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

Exercised end to end in Phase 14 with systemd 255 in an Ubuntu 24.04 container (see `IMPLEMENTATION_PLAN.md` for what that did not cover). The application listens on `127.0.0.1:8080` behind a TLS-terminating reverse proxy; an SSH tunnel (`ssh -L 8080:127.0.0.1:8080 nas-host`) works too.

```bash
# 1. Prerequisites (Python 3.12+, venv; helper tools; reverse proxy)
sudo apt install python3-venv smartmontools mdadm util-linux nginx

# 2. Install from the source tree (idempotent; also the upgrade command).
#    The root helper for disks, RAID and Samba reloads is installed too
#    (--without-helper skips it). On a terminal it asks for the names the NAS
#    is reached by and whether HTTPS is in front.
sudo deploy/install.sh

# 3. Review the rest of the configuration: NAS_FILE_ROOTS, NAS_MEDIA_DIR, ...
sudoedit /etc/nas-manager/nas-manager.env

# 4. Start helper, web (applies migrations in ExecStartPre) and worker.
sudo systemctl start nas-manager-helper nas-manager nas-manager-worker

# 5. First administrator. The password is read interactively (or from stdin
#    with --password-stdin); never from argv, the environment or a log.
sudo /opt/nas-manager/deploy/nas-manager-cli bootstrap-admin --username admin

# 6. Verify (add --no-helper without the helper; --login reads the password from stdin).
sudo python3 /opt/nas-manager/deploy/smoke_test.py
printf '%s\n' "$ADMIN_PASSWORD" | sudo python3 /opt/nas-manager/deploy/smoke_test.py --login admin
```

What `install.sh` does: creates the `nas-manager` system user (no shell, no home), stops running units, copies the code to `/opt/nas-manager` (root-owned, read-only for the service), builds the virtual environment from the pinned `requirements.txt`, creates `/etc/nas-manager` (`0750 root:nas-manager`) and installs `nas-manager.env` (`0640`) from the template **only when it does not exist**, then brings it up to date with `deploy/env_setup.py` on every run (see below), validates it with the installed code (it stops before starting anything when the application would reject it), installs and enables the units and starts what was running before (`--start` starts everything). It never creates accounts in the application, formats, mounts or deletes data.

The units set `NAS_DATA_DIR=/var/lib/nas-manager` and `NAS_LOG_DIR=/var/log/nas-manager`; systemd creates both with the service user as owner. Use `deploy/nas-manager.env.example` (not the development `.env.example`) for the service: systemd lets `EnvironmentFile=` override `Environment=`, so the per-unit settings (`NAS_DATA_DIR`, `NAS_LOG_DIR`, `NAS_ENVIRONMENT`, `NAS_LOG_FILE_NAME`, `NAS_LOG_TO_FILE`, `NAS_SYSLOG_IDENTIFIER`) must stay out of the env file (a test enforces this).

On every install and upgrade, `deploy/env_setup.py` changes the env file only where the application would otherwise not work, and prints each change; values you set are never overwritten:

- settings active in `deploy/nas-manager.env.example` but missing from your file (new in this version) are appended under an `# Added by install.sh (<date>)` comment;
- with the helper, `NAS_HELPER_ENABLED=true` and the service account's UID is added to `NAS_HELPER_ALLOWED_UIDS`; with `--without-helper` (and no helper installed), `NAS_HELPER_ENABLED=false`;
- while `NAS_TRUSTED_HOSTS` still holds the placeholder `nas.example.org`, an installer run on a terminal asks for the names and addresses the NAS is reached by (default: host name, FQDN, LAN addresses, `localhost`, `127.0.0.1`) and whether clients use HTTPS; answering no sets `NAS_SESSION_COOKIE_SECURE=false` and `NAS_HSTS_MAX_AGE_SECONDS=0`, since secure cookies are never sent over plain HTTP. Without a terminal it only warns.

An already installed helper is always upgraded, even with `--without-helper`. The installer still accepts `--with-helper` (the default) for older scripts.

Always run CLI commands (`bootstrap-admin`, `restore-system`, `backup-recover`) through `deploy/nas-manager-cli`: it runs `python -m app.cli` as the service account with the units' environment through `systemd-run`. A plain `sudo -u nas-manager … python -m app.cli` (the instruction before Phase 14) does not load the env file or the unit's `NAS_DATA_DIR` and acts on a `data/` directory relative to the current directory.

### Reverse proxy and TLS

`deploy/nginx/nas-manager.conf` is a complete nginx site (tested with nginx 1.24 in Phase 14): HTTP → HTTPS redirect, TLS 1.2/1.3, HTTP/2, `X-Forwarded-For $remote_addr` (**overwritten**, never appended, so clients cannot choose their address), `X-Forwarded-Proto`, a 2100 MiB body limit, unbuffered upload and media locations and `limit_req` on the drop endpoint and the login routes.

```bash
sudo install -d -m 0750 /etc/ssl/nas-manager      # fullchain.pem + privkey.pem (e.g. from certbot)
sudo cp /opt/nas-manager/deploy/nginx/nas-manager.conf /etc/nginx/sites-available/
sudo sed -i 's/nas.example.org/nas.your-domain/' /etc/nginx/sites-available/nas-manager.conf
sudo ln -s ../sites-available/nas-manager.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Then, in `/etc/nas-manager/nas-manager.env`:

| Setting | Value | Why |
|---------|-------|-----|
| `NAS_SESSION_COOKIE_SECURE` | `true` (template default) | cookies only over TLS |
| `NAS_TRUSTED_HOSTS` | `nas.your-domain,localhost,127.0.0.1` | refuse other `Host` headers (400); keep the loopback names for the smoke test |
| `NAS_FORWARDED_ALLOW_IPS` | `127.0.0.1` (template default) | only the local proxy may set the client address and scheme |
| `NAS_HSTS_MAX_AGE_SECONDS` | `31536000` (template default) | browsers stay on HTTPS; set `0` if the name is ever served over plain HTTP |

On nginx 1.25.1 or newer, replace `listen 443 ssl http2;` with `listen 443 ssl;` plus `http2 on;`.

### Firewall

Only the proxy should be reachable. With ufw:

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'            # 80 + 443
sudo ufw allow from 192.168.1.0/24 to any app Samba   # LAN only, if Samba is used
sudo ufw enable
```

Port 8080 is bound to loopback and needs no rule. Do not bind `NAS_HOST` to `0.0.0.0` without a firewall rule restricting it.

### Uninstalling

```bash
sudo /opt/nas-manager/deploy/uninstall.sh                 # removes units and /opt/nas-manager, keeps data
sudo /src/nas-manager/deploy/uninstall.sh --purge --yes   # also /var/lib, /var/log, /etc/nas-manager and the user
```

`--purge` without `--yes` refuses. Managed file roots, media folders and backup roots outside `/var/lib/nas-manager`, `smb.conf` and mounted disks are never touched. A later `install.sh` picks up kept data again (verified in Phase 14).

### File manager and Samba (Phase 3)

- **File manager roots.** Set `NAS_FILE_ROOTS` to a JSON list of absolute directories you want to manage, e.g. `NAS_FILE_ROOTS='["/mnt/storage"]'`. When unset the file manager uses `<DATA_DIR>/files`. The web process must be able to read/write those directories (chown them to `nas-manager` or the group it belongs to). `NAS_MAX_UPLOAD_BYTES` caps each upload (default 2 GiB).
- **Samba.** The app manages shares in its database and renders them into `smb.conf`. The web process runs unprivileged and **cannot write `/etc/samba/smb.conf`** by itself. Deploy the Phase 5 helper (`nas-manager-helper.service`, below) and set `NAS_HELPER_ENABLED=true`: **Apply** then writes the validated config through the helper (re-validating with `testparm -s`, backing up, atomic write) and reloads `smbd`, and the Samba page shows `reload_required: false`.
  - For a dev/test setup without the helper, point `NAS_SAMBA_CONFIG_PATH` at a writable file and `NAS_SAMBA_BACKUP_DIR` at a writable backup directory (default `<DATA_DIR>/samba-backups`); Apply then writes the config and reports `reload_required: true`. Samba must be installed on the host for validation (`testparm`; `apt install samba`).
- **Operators and Files/Samba.** By default only admin (or custom roles with `files:*`/`samba:*`) can open the Files and Samba pages. To let operators *view* them read-only, enable the **Settings → "Allow operators to view the Files and Samba pages"** toggle (stored per-installation; writes are never granted by it).
- **Public upload folder (Phase 4).** The drop folder lives at `NAS_PUBLIC_UPLOAD_DIR` (default `<DATA_DIR>/uploads`, auto-created and owned by the service user; chown it there if you set a custom path). `NAS_PUBLIC_UPLOAD_MAX_FILE_BYTES` caps each file (default 1 GiB) and `NAS_PUBLIC_UPLOAD_DEFAULT_QUOTA_BYTES` sets the per-uploader quota (default 1 GiB). Public uploads are on by default; the **Settings → "Accept public uploads"** toggle and `NAS_PUBLIC_UPLOAD_ENABLED` both disable them (403). Administer quotas and cleanup on the **Uploads** page (needs `uploads:read`/`uploads:write`; the built-in operator role gets read-only). Uploads are rate limited per uploader before the body is read (`NAS_PUBLIC_UPLOAD_RATE_MAX_REQUESTS` per `NAS_PUBLIC_UPLOAD_RATE_WINDOW_SECONDS`, default 30 per 5 minutes; 429 with `Retry-After`), and the nginx example adds a per-address `limit_req` in front.

### Disks, storage and RAID (Phase 5, privileged helper)

The web process stays unprivileged. A second root service performs discovery and the privileged actions:

`install.sh` installs and enables `nas-manager-helper.service` (unless `--without-helper`), sets `NAS_HELPER_ENABLED=true` and authorises the service account in `NAS_HELPER_ALLOWED_UIDS`, also in an existing env file. To add the helper to an installation made with `--without-helper`, re-run the installer without that flag and restart:

```bash
sudo deploy/install.sh
# NAS_HELPER_SOCKET=/run/nas-manager/helper.sock   (default)
# NAS_HELPER_SOCKET_GROUP=nas-manager              (default)
sudo systemctl restart nas-manager-helper nas-manager nas-manager-worker
journalctl -u nas-manager-helper -n 50 --no-pager
```

`refusing connection from uid N (not allowlisted)` in that log means `NAS_HELPER_ALLOWED_UIDS` lacks the service account's UID: re-run the installer.

The helper runs in the host's mount namespace on purpose (no `PrivateTmp`/`ProtectHome`/`ProtectSystem`), so a disk it mounts is visible to Samba, the web unit and the shell. Its run directory `/run/nas-manager` is `0711` (traverse only) and the socket `0660 root:nas-manager`.

Then open **Disks** in the UI: physical disks, mounted volumes and RAID arrays render from the helper; the badge in the header shows `helper online`. Mount/unmount/scrub flows always show a **preview first** (the exact action, warnings and data-loss notes) and require an explicit confirmation. Apart from USB drives (see below), nothing is ever mounted, unmounted or scrubbed automatically.

- The helper binds `/run/nas-manager/helper.sock` (`0660 root:<group>`, in a `0711` directory) and accepts connections only from root or the UIDs in `NAS_HELPER_ALLOWED_UIDS` (the service user's UID; `SO_PEERCRED`-enforced) — no password/secret is shared over the wire.
- Discovery (`lsblk`, `findmnt`, `/proc/mdstat`, `smartctl`) and actions (`mount`, `umount`, md `check` scrub, `smbcontrol all reload-config`) run in the helper with a fixed allowlist; the web process sends only validated parameter objects, never command lines.
- With `NAS_HELPER_ENABLED=true`, **Samba Apply** now also writes `/etc/samba/smb.conf` (via the helper) and reloads `smbd`; the Samba page shows the helper state and `reload_required: false`.
- **Test on a scratch disk only** (USB stick, a spare drive or a loop device: `truncate -s 64M /root/scratch.img && mkfs.ext4 /root/scratch.img && sudo losetup -f --show /root/scratch.img`), then use the Disks page to mount it under `/mnt` and unmount it again (`losetup -d /dev/loopN` afterwards). Never point the page at a disk holding data.

### USB drives (Phase 16)

With the helper installed, plugging in a USB stick or disk is enough. Within a few seconds (`NAS_REMOVABLE_POLL_SECONDS`, default 3) the helper mounts it under `/media/nas-manager/<label>`, and it appears under **USB drives** on the Files page and on the Disks page. When it is pulled out the helper unmounts it. Files copied from the Files page are flushed to the stick when the transfer shows "Done", so it can be pulled out right away. A stick pulled out during a copy fails that copy.

- Supported formats and the drivers they need:

  | Format | Typical for | Driver |
  |--------|-------------|--------|
  | FAT32 (`vfat`) | small sticks, cameras | built into the kernel |
  | exFAT | most sticks over 32 GB, SD cards | kernel 5.7+ built in (`exfatprogs` for tools); older kernels: `sudo apt install exfat-fuse` |
  | NTFS | Windows external disks | kernel `ntfs3` (5.15+) or `sudo apt install ntfs-3g` |
  | ext2/3/4, btrfs, xfs | Linux disks | built in |
  | HFS+ | older Mac disks | kernel module `hfsplus` (writes only if not journaled) |

  The Windows/Mac formats are fully writable. A Linux-formatted stick is writable only where its own permissions allow the `nas-manager` user, and is otherwise shown as read-only.
- The file system is read with `lsblk`. When that has no answer (no udev database: containers, WSL, minimal installs, or a stick udev is still probing), the helper reads it straight from the device with `blkid -p`.
- Who may use them: the `removable:read` / `removable:write` permissions (admin and operator by default; add them to other roles on the Roles page).
- `install.sh` creates `/media/nas-manager` (`root:nas-manager 0750`), and both units list it in `ReadWritePaths`. If you change `NAS_REMOVABLE_MOUNT_DIR`, change `ReadWritePaths=-/media/nas-manager` in `nas-manager.service` and `nas-manager-worker.service` to match.
- `NAS_REMOVABLE_AUTOMOUNT=false` (in the env file, then restart `nas-manager-helper`) turns automatic mounting off. Drives can then still be mounted by hand on the Disks page.
- Internal disks are never mounted automatically, and neither are disks listed in `/etc/fstab`.
- **If a stick does not mount**, the Disks page's USB card shows each partition with a state (ready, could not mount, not supported, left alone) and the reason. An admin can press **Try again** after fixing it, without waiting for the one-minute retry. Common reasons and fixes:
  - *"no exFAT driver"* (or NTFS): install the driver from the table above, then press Try again.
  - *"file system not recognised"*: the stick is empty or unformatted, or uses a format not listed above.
  - *"listed in /etc/fstab"*: the system mounts it itself; remove the fstab line to let the NAS manage it.
  - *"helper too old"* badge: after upgrading, re-run `sudo deploy/install.sh` and `sudo systemctl restart nas-manager-helper nas-manager nas-manager-worker`.
- Check it by hand: `lsblk -f /dev/sdX`, `sudo blkid -p /dev/sdX1`, `grep -E 'exfat|ntfs' /proc/filesystems`, `journalctl -u nas-manager-helper | grep -i removable`, and the audit log (`disk.removable_mounted` / `disk.removable_unmounted` / `disk.removable_mount_failed`).

### Remote and cloud sync (Phase 7, sync worker)

Sync jobs run in a separate worker process under the same unprivileged account. Without it, runs stay `queued` and the Remote sync page shows "Worker offline".

```bash
sudo cp deploy/systemd/nas-manager-worker.service /etc/systemd/system/
# Managed file roots and backup roots outside /var/lib/nas-manager must also be
# writable by the worker: add them with `sudo systemctl edit nas-manager-worker` →
#   [Service]
#   ReadWritePaths=/srv/nas /mnt/backup-usb/nas-manager
sudo systemctl daemon-reload
sudo systemctl enable --now nas-manager-worker
journalctl -u nas-manager-worker -n 50 --no-pager
```

- The worker refuses to start until the schema is current (the web unit migrates in `ExecStartPre`, and the worker unit is ordered after it). It logs to `worker.log` in the log directory (`NAS_LOG_FILE_NAME`), separately from the web process.
- Remote credentials are encrypted with the key file `/var/lib/nas-manager/secrets/credentials.key`, created on first use (0600). **Back it up with the database** (see `BACKUP_AND_RESTORE.md`).
- **SFTP:** use **Fetch host key** in the connection form and compare the fingerprint with the server (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the server) before trusting it.
- **Google Drive:** in Google Cloud Console create an OAuth client of type *Web application* with the redirect URI `https://<your NAS URL>/api/v1/remotes/oauth/callback` (enable the Google Drive API for the project). Set `NAS_PUBLIC_BASE_URL=https://<your NAS URL>` in `/etc/nas-manager/nas-manager.env`, restart `nas-manager`, create a Google Drive connection with the client id and secret, then click **Connect** and grant access. The scope is full Drive access (`https://www.googleapis.com/auth/drive`) so existing folders can be synced. The callback identifies you by your session cookie, which the browser only sends on Google's redirect with `NAS_SESSION_COOKIE_SAMESITE=lax` (the default); with `strict` the callback answers 401.
- Create jobs disabled, run **Preview** (a dry run that lists every planned action), and only then **Run now** or enable a schedule.

### Backups (Phase 11)

Backups run in the same worker. Decide where repositories live:

- **External storage:** mount the backup disk (the Disks page, or your own `/etc/fstab` entry with `nofail`), create a folder owned by the service account and allow it:

  ```bash
  sudo install -d -o nas-manager -g nas-manager -m 0750 /mnt/backup-usb/nas-manager
  # /etc/nas-manager/nas-manager.env
  NAS_BACKUP_LOCAL_ROOTS=["/mnt/backup-usb/nas-manager"]
  # the worker must be able to write there:
  sudo systemctl edit nas-manager-worker    # [Service] ReadWritePaths=/mnt/backup-usb/nas-manager
  sudo systemctl restart nas-manager nas-manager-worker
  ```

  Point the root at a folder *on* the disk (not the bare mountpoint) so an unmounted disk shows up as a missing directory and the backup fails instead of writing to the root filesystem.
- **Remote:** create a remote connection first (Remote sync page), with an account limited to the backup folder or bucket. Retention needs delete rights there; Google Drive moves pruned files to the trash.
- Without configuration, repositories can only go to `/var/lib/nas-manager/backups` (same disk as the data).

Then create repositories and jobs on the **Backups** page, store each passphrase safely, and run a first backup and a **Verify**. Staging space (`NAS_BACKUP_STAGING_DIR`, default `/var/lib/nas-manager/backup-staging`) needs room for the largest single file being backed up or restored and for a copy of the database. Restores and disaster recovery: `BACKUP_AND_RESTORE.md`.

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

### Logging, auditing and remote syslog (Phase 13)

- **journald.** Under systemd, `NAS_LOG_JOURNALD=auto` (default) writes native journal entries and stops the duplicate console copy. Query them by identifier and structured field:

  ```bash
  journalctl -t nas-manager -t nas-manager-worker -n 50
  journalctl SYSLOG_IDENTIFIER=nas-manager NAS_AUDIT_EVENT=auth.login.failure -o json-pretty
  journalctl -t nas-manager NAS_AUDIT_OUTCOME=failure --since today
  ```

- **Log files** in `NAS_LOG_DIR` rotate by size (`NAS_LOG_MAX_BYTES`, `NAS_LOG_BACKUP_COUNT`) or daily at midnight UTC (`NAS_LOG_ROTATION=daily`, keeping `NAS_LOG_BACKUP_COUNT` days); `NAS_LOG_COMPRESS=true` gzips rotated files. Do not add a logrotate rule for these files as well.
- **Remote syslog** is configured on the **Logging** page (or `/api/v1/syslog/destinations`). The worker must be running: it seals audit events into the hash chain and forwards them. A minimal rsyslog TLS receiver on the collector:

  ```
  # /etc/rsyslog.d/10-nas-manager.conf on the collector (needs rsyslog-gnutls)
  global(DefaultNetstreamDriver="gtls"
         DefaultNetstreamDriverCAFile="/etc/ssl/nas/ca.pem"
         DefaultNetstreamDriverCertFile="/etc/ssl/nas/collector.pem"
         DefaultNetstreamDriverKeyFile="/etc/ssl/nas/collector.key")
  module(load="imtcp" StreamDriver.Name="gtls" StreamDriver.Mode="1" StreamDriver.AuthMode="anon")
  input(type="imtcp" port="6514")
  if $programname startswith "nas-manager" then /var/log/nas-manager-remote.log
  ```

  Then add a destination with protocol TLS, the collector's host name (it must match its certificate) and, for a private CA, the CA certificate. Use **Test** to check the connection; the backlog column should drop to 0 within one worker tick.
- **Retention.** Audit events are kept `NAS_AUDIT_RETENTION_DAYS` (default **90**; `0` keeps them forever). On upgrading to Phase 13, the worker's first hourly prune removes older events once they are sealed and forwarded to every enabled destination. Export them first (Audit page → Export) if you need them, or set the retention before starting the worker.

### Troubleshooting

- `journalctl -u nas-manager -u nas-manager-worker -u nas-manager-helper -p warning` shows every warning, including the production configuration warnings logged at startup (cookie not Secure, debug on, no trusted hosts, `NAS_FORWARDED_ALLOW_IPS=*`, drop folder without a rate limit).
- `helper probe failed` / Disks page "helper offline": the helper is not running, `NAS_HELPER_ALLOWED_UIDS` lacks the service account's UID, or `/run/nas-manager` is not traversable (it must be `0711`; the helper never re-modes an existing directory).
- `400 Invalid host header`: add the name to `NAS_TRUSTED_HOSTS`.
- The sandbox ran under systemd 255 in Phase 14 (web, worker, helper; a real mount/unmount through the helper). ffmpeg under the worker sandbox was not exercised there. If a unit exits with `SIGSYS`, an `mprotect`/`mmap` error or "read-only file system", relax the corresponding directive with a drop-in (`systemctl edit <unit>`) and report which one was needed. File roots, media folders and backup roots outside `/var/lib/nas-manager` need `ReadWritePaths=` in a drop-in for both the web and the worker unit.

## Upgrading

1. Back up `/var/lib/nas-manager` (see `BACKUP_AND_RESTORE.md`) and keep the previous source tree.
2. From the new source tree: `sudo deploy/install.sh` (add `--without-helper` if you never installed the helper). It stops worker, web and helper, replaces the code, updates the virtual environment, keeps `/etc/nas-manager/nas-manager.env` (appending settings new in this version and fixing the helper settings, see above) and starts the units that were running, helper first; the web unit applies migrations before it starts.
3. Run `deploy/smoke_test.py`. Settings the installer appended are at the end of the env file; commented-out ones (optional) are listed in `deploy/nas-manager.env.example`.
4. **Rollback:** stop the units, restore `/var/lib/nas-manager` from the backup (migrations are not downgraded automatically), run `install.sh` from the previous source tree.
5. After upgrading to Phase 9 with ffmpeg installed, videos indexed earlier are probed on their library's next scan (or press **Rescan**).
6. Phase 13 introduces a 90-day audit retention (see above): set `NAS_AUDIT_RETENTION_DAYS=0` before restarting the worker to keep the full history.
7. Phase 14 adds `NAS_TRUSTED_HOSTS`, `NAS_FORWARDED_ALLOW_IPS`, `NAS_HSTS_MAX_AGE_SECONDS` and the drop-folder rate limit. An existing env file keeps working; set `NAS_TRUSTED_HOSTS` to silence the startup warning.

## Dependency pinning

`requirements.txt` is the full pinned runtime set, generated in a clean virtualenv with `pip freeze`. Regenerate it the same way after changing a dependency, then scan it: `.venv/bin/pip-audit -r requirements.txt` (pinned in `requirements-dev.txt`; needs network access to PyPI/OSV). Phase 14 found no known vulnerabilities in either requirements file. `requirements-dev.txt` adds test and lint tooling. Bootstrap 5.3.8 is vendored in `app/web/static/vendor/bootstrap/` (license included).
