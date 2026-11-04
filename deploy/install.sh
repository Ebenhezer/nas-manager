#!/usr/bin/env bash
# Install or upgrade NAS Manager on Ubuntu (Phase 14). Run as root from the
# source tree:
#
#   sudo deploy/install.sh [--with-helper] [--start] [--python /usr/bin/python3]
#
# What it does (idempotent; safe to re-run for an upgrade):
#   1. creates the nas-manager system user and group (no login shell);
#   2. stops running NAS Manager units (worker, web, helper) before an upgrade;
#   3. copies the application to /opt/nas-manager (root-owned, read-only for
#      the service) and builds/updates its virtual environment from the
#      pinned requirements.txt;
#   4. creates /etc/nas-manager and installs nas-manager.env from the
#      template ONLY when it does not exist yet (an existing file is never
#      touched);
#   5. installs the systemd units (the root helper only with --with-helper),
#      reloads systemd and enables them;
#   6. with --start (or when the units were running before) starts helper,
#      web (which applies migrations) and worker, in that order.
#
# It never creates an administrator, formats, mounts or deletes anything.
# Next steps are printed at the end.
set -euo pipefail

readonly PREFIX=/opt/nas-manager
readonly ETC_DIR=/etc/nas-manager
readonly ENV_FILE=$ETC_DIR/nas-manager.env
readonly UNIT_DIR=/etc/systemd/system
readonly SERVICE_USER=nas-manager
readonly WEB_UNIT=nas-manager.service
readonly WORKER_UNIT=nas-manager-worker.service
readonly HELPER_UNIT=nas-manager-helper.service

SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=python3
WITH_HELPER=false
START=false

usage() {
    sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-helper) WITH_HELPER=true ;;
        --start) START=true ;;
        --python)
            [[ $# -ge 2 ]] || { echo "--python needs a path" >&2; exit 2; }
            PYTHON=$2
            shift
            ;;
        -h | --help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

log() { printf '==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "systemd is required" >&2; exit 1; }
for path in app migrations alembic.ini requirements.txt deploy/systemd/$WEB_UNIT; do
    [[ -e $SOURCE_DIR/$path ]] || { echo "not a NAS Manager source tree: $SOURCE_DIR" >&2; exit 1; }
done
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || { echo "$PYTHON must be Python 3.12 or newer" >&2; exit 1; }
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
    || { echo "$PYTHON lacks venv/ensurepip (apt install python3-venv)" >&2; exit 1; }

# 1. Service account ---------------------------------------------------------
if ! getent group "$SERVICE_USER" >/dev/null; then
    log "creating group $SERVICE_USER"
    groupadd --system "$SERVICE_USER"
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
    log "creating user $SERVICE_USER"
    useradd --system --gid "$SERVICE_USER" --home-dir /var/lib/nas-manager \
        --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
SERVICE_UID=$(id -u "$SERVICE_USER")

# 2. Stop running units before replacing the code ----------------------------
WAS_RUNNING=()
for unit in $WORKER_UNIT $WEB_UNIT $HELPER_UNIT; do
    if systemctl is-active --quiet "$unit"; then
        log "stopping $unit"
        systemctl stop "$unit"
        WAS_RUNNING+=("$unit")
    fi
done

# 3. Application code and virtual environment --------------------------------
log "installing the application into $PREFIX"
install -d -o root -g root -m 0755 "$PREFIX"
# Replace the code directories wholesale so removed modules do not linger;
# the virtual environment is kept and updated.
rm -rf -- "$PREFIX/app" "$PREFIX/migrations" "$PREFIX/deploy" "$PREFIX/docs"
(
    cd "$SOURCE_DIR"
    tar --exclude='__pycache__' --exclude='*.pyc' -cf - \
        app migrations alembic.ini requirements.txt pyproject.toml README.md deploy docs
) | tar -C "$PREFIX" --no-same-owner -xf -
# Root-owned and read-only for the service account; only the deploy
# scripts are executable (checkouts on some filesystems mark everything +x).
CODE=("$PREFIX/app" "$PREFIX/migrations" "$PREFIX/deploy" "$PREFIX/docs"
    "$PREFIX/alembic.ini" "$PREFIX/requirements.txt" "$PREFIX/pyproject.toml" "$PREFIX/README.md")
chown -R root:root "${CODE[@]}"
find "${CODE[@]}" -type d -exec chmod 0755 {} +
find "${CODE[@]}" -type f -exec chmod 0644 {} +
chmod 0755 "$PREFIX/deploy/install.sh" "$PREFIX/deploy/uninstall.sh" \
    "$PREFIX/deploy/nas-manager-cli" "$PREFIX/deploy/smoke_test.py"

if [[ ! -x $PREFIX/.venv/bin/python ]]; then
    log "creating the virtual environment"
    "$PYTHON" -m venv "$PREFIX/.venv"
fi
log "installing pinned dependencies"
"$PREFIX/.venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir \
    -r "$PREFIX/requirements.txt"
# Import check from the installed tree (not the source tree in the cwd).
(cd "$PREFIX" && .venv/bin/python -c 'import app.main, app.worker.runner, app.helper.server')

# 4. Configuration -----------------------------------------------------------
install -d -o root -g "$SERVICE_USER" -m 0750 "$ETC_DIR"
if [[ -e $ENV_FILE ]]; then
    log "keeping the existing $ENV_FILE"
else
    log "installing $ENV_FILE from the template"
    install -o root -g "$SERVICE_USER" -m 0640 \
        "$SOURCE_DIR/deploy/nas-manager.env.example" "$ENV_FILE"
    if $WITH_HELPER; then
        printf '\n# Added by install.sh: the service account may call the helper.\nNAS_HELPER_ALLOWED_UIDS=%s\n' \
            "$SERVICE_UID" >>"$ENV_FILE"
    else
        sed -i 's/^NAS_HELPER_ENABLED=true$/NAS_HELPER_ENABLED=false/' "$ENV_FILE"
    fi
    NEW_ENV=true
fi

# 5. systemd units -----------------------------------------------------------
UNITS=($WEB_UNIT $WORKER_UNIT)
$WITH_HELPER && UNITS=($HELPER_UNIT "${UNITS[@]}")
for unit in "${UNITS[@]}"; do
    install -o root -g root -m 0644 "$SOURCE_DIR/deploy/systemd/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
systemctl enable --quiet "${UNITS[@]}"
log "installed units: ${UNITS[*]}"

# 6. Start -------------------------------------------------------------------
# --start starts every installed unit; otherwise only what was running
# before the upgrade comes back.
if $START; then
    TO_START=" ${UNITS[*]} "
else
    TO_START=" ${WAS_RUNNING[*]-} "
fi
for unit in $HELPER_UNIT $WEB_UNIT $WORKER_UNIT; do
    if [[ $TO_START == *" $unit "* ]]; then
        log "starting $unit"
        systemctl start "$unit"
    fi
done

cat <<EOF

NAS Manager is installed in $PREFIX.
Next steps:
EOF
if [[ ${NEW_ENV:-false} == true ]]; then
    echo "  - Review $ENV_FILE (NAS_TRUSTED_HOSTS, reverse proxy, file roots)."
fi
cat <<EOF
  - Create the first administrator (prompts for the password):
      sudo $PREFIX/deploy/nas-manager-cli bootstrap-admin
  - Put the TLS reverse proxy in front (deploy/nginx/nas-manager.conf).
  - Verify: sudo python3 $PREFIX/deploy/smoke_test.py$($WITH_HELPER || echo " --no-helper")
EOF
