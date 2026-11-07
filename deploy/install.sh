#!/usr/bin/env bash
# NAS Manager installer for Ubuntu (Phase 14).
#
# Installs NAS Manager for the first time, or upgrades an existing install.
# Run it as root from a checkout of the source tree:
#
#   sudo deploy/install.sh [--without-helper] [--start] [--python /usr/bin/python3]
#
# Options:
#   --without-helper  do not install the root helper service (needed for
#                   disks, mounts, SMART and Samba); it is installed by
#                   default, and kept on upgrade once installed
#   --start         start every service when done (by default only the
#                   services that were already running are started again)
#   --python PATH   Python 3.12+ interpreter used to build the virtual
#                   environment (default: python3)
#   -h, --help      show this help
#
# What it does, in order. Every step is safe to repeat, so re-running the
# script is how you upgrade:
#   1. creates the nas-manager system user and group (no login shell);
#   2. stops the running NAS Manager services (worker, web, helper);
#   3. copies the application to /opt/nas-manager (owned by root, read-only
#      for the service) and creates or updates its Python virtual
#      environment from the pinned requirements.txt;
#   4. creates /etc/nas-manager and installs nas-manager.env from the
#      template if it does not exist yet, then brings it up to date with
#      deploy/env_setup.py: settings new in this version are appended, the
#      helper is enabled and the service account allowed to call it, and (on a
#      terminal) the host names and HTTPS question are asked while
#      NAS_TRUSTED_HOSTS is still the template placeholder. Values you set are
#      never overwritten. The result is validated before anything starts;
#   5. installs the systemd unit files, reloads systemd and enables them;
#   6. starts the services again: helper, then web (which applies database
#      migrations on start-up), then worker.
#
# It never creates an administrator, formats or mounts disks, or deletes
# data. The next steps are printed at the end.
set -euo pipefail

# --- Settings ----------------------------------------------------------------

readonly PREFIX=/opt/nas-manager                # where the code and .venv live
readonly ETC_DIR=/etc/nas-manager               # configuration directory
readonly ENV_FILE=$ETC_DIR/nas-manager.env      # environment file read by the units
readonly UNIT_DIR=/etc/systemd/system           # where systemd unit files go
readonly SERVICE_USER=nas-manager               # account the web and worker run as
readonly WEB_UNIT=nas-manager.service           # web UI and API
readonly WORKER_UNIT=nas-manager-worker.service # background jobs
readonly HELPER_UNIT=nas-manager-helper.service # optional privileged helper (root)

# The source tree is the parent of the directory this script lives in.
SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

# Defaults; the command-line options below can change these.
PYTHON=python3
WITH_HELPER=true
START=false

# --- Helpers -----------------------------------------------------------------

usage() {
    cat <<'EOF'
Install or upgrade NAS Manager on Ubuntu. Run as root from the source tree:

  sudo deploy/install.sh [--without-helper] [--start] [--python /usr/bin/python3]

  --without-helper  do not install the root helper service
  --start         start every service when done
  --python PATH   Python 3.12+ used for the virtual environment
  -h, --help      show this help
EOF
}

# Print a progress message.
log() { printf '==> %s\n' "$*"; }

# Print an error and stop.
die() { echo "error: $*" >&2; exit 1; }

# --- Command-line options ----------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-helper) WITH_HELPER=true ;; # the default; kept for old scripts
        --without-helper) WITH_HELPER=false ;;
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

# --- Pre-flight checks -------------------------------------------------------
# Check everything up front so the script fails before it changes anything.

# Creating users and writing to /opt, /etc and systemd needs root.
[[ $EUID -eq 0 ]] || die "run as root (sudo)"

# The services are managed by systemd.
command -v systemctl >/dev/null || die "systemd is required"

# An installed helper is always upgraded, so --without-helper cannot leave a
# stale helper unit running old code.
if ! $WITH_HELPER && [[ -e $UNIT_DIR/$HELPER_UNIT ]]; then
    log "the helper is already installed; upgrading it despite --without-helper"
    WITH_HELPER=true
fi

# Make sure we are running from a real NAS Manager source tree.
for path in app migrations alembic.ini requirements.txt deploy/systemd/$WEB_UNIT; do
    [[ -e $SOURCE_DIR/$path ]] || die "not a NAS Manager source tree: $SOURCE_DIR"
done

# The application needs Python 3.12 or newer...
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "$PYTHON must be Python 3.12 or newer"

# ...with the venv module (Ubuntu ships it separately as python3-venv).
"$PYTHON" -c 'import venv, ensurepip' 2>/dev/null \
    || die "$PYTHON lacks venv/ensurepip (apt install python3-venv)"

# 1. Service account ---------------------------------------------------------
# A system account with no login shell and no home directory; the web and
# worker services run as this user.
if ! getent group "$SERVICE_USER" >/dev/null; then
    log "creating group $SERVICE_USER"
    groupadd --system "$SERVICE_USER"
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
    log "creating user $SERVICE_USER"
    useradd --system --gid "$SERVICE_USER" --home-dir /var/lib/nas-manager \
        --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# Numeric user id, used in step 4 to let this account talk to the helper.
SERVICE_UID=$(id -u "$SERVICE_USER")

# 2. Stop running units before replacing the code ----------------------------
# Stop in reverse dependency order: worker, then web, then helper. Remember
# which ones were running so step 6 can bring exactly those back.
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
# the virtual environment (.venv) is kept and only updated.
rm -rf -- "$PREFIX/app" "$PREFIX/migrations" "$PREFIX/deploy" "$PREFIX/docs"

# Copy the files we need from the source tree, skipping Python caches.
# --no-same-owner makes root the owner instead of whoever owns the checkout.
(
    cd "$SOURCE_DIR"
    tar --exclude='__pycache__' --exclude='*.pyc' -cf - \
        app migrations alembic.ini requirements.txt pyproject.toml README.md deploy docs
) | tar -C "$PREFIX" --no-same-owner -xf -

# Make the code root-owned and read-only for the service account: folders
# 0755, files 0644. Only the deploy scripts are executable (checkouts on some
# filesystems mark everything +x).
CODE=("$PREFIX/app" "$PREFIX/migrations" "$PREFIX/deploy" "$PREFIX/docs"
    "$PREFIX/alembic.ini" "$PREFIX/requirements.txt" "$PREFIX/pyproject.toml" "$PREFIX/README.md")
chown -R root:root "${CODE[@]}"
find "${CODE[@]}" -type d -exec chmod 0755 {} +
find "${CODE[@]}" -type f -exec chmod 0644 {} +
chmod 0755 "$PREFIX/deploy/install.sh" "$PREFIX/deploy/uninstall.sh" \
    "$PREFIX/deploy/nas-manager-cli" "$PREFIX/deploy/smoke_test.py" "$PREFIX/deploy/env_setup.py"

# Create the virtual environment on the first install only.
if [[ ! -x $PREFIX/.venv/bin/python ]]; then
    log "creating the virtual environment"
    "$PYTHON" -m venv "$PREFIX/.venv"
fi

# Install (or upgrade to) the exact dependency versions in requirements.txt.
log "installing pinned dependencies"
"$PREFIX/.venv/bin/pip" install --quiet --disable-pip-version-check --no-cache-dir \
    -r "$PREFIX/requirements.txt"

# Quick sanity check that the installed code imports. Run it from $PREFIX so
# it tests the installed copy, not the source tree in the current directory.
(cd "$PREFIX" && .venv/bin/python -c 'import app.main, app.worker.runner, app.helper.server')

# 4. Configuration -----------------------------------------------------------
# /etc/nas-manager is readable by root and the service group only, because
# the env file can hold secrets.
install -d -o root -g "$SERVICE_USER" -m 0750 "$ETC_DIR"

# USB drives are auto-mounted below this folder by the helper (Phase 16); the
# service group may enter it to read and copy files.
install -d -o root -g "$SERVICE_USER" -m 0750 /media/nas-manager

# Create the env file from the template only on the first install; an
# existing file holds your settings and is never overwritten.
if [[ -e $ENV_FILE ]]; then
    log "keeping the existing $ENV_FILE"
else
    log "installing $ENV_FILE from the template"
    install -o root -g "$SERVICE_USER" -m 0640 \
        "$SOURCE_DIR/deploy/nas-manager.env.example" "$ENV_FILE"
    NEW_ENV=true # remembered for the "next steps" message
fi

# Bring the env file up to date, new or kept: append settings this version
# added, enable (or disable) the helper and allow the service account to call
# it, and, on a terminal, ask for the host names and HTTPS while
# NAS_TRUSTED_HOSTS is still the template placeholder.
log "checking $ENV_FILE"
ENV_ARGS=(--env-file "$ENV_FILE" --template "$SOURCE_DIR/deploy/nas-manager.env.example"
    --service-uid "$SERVICE_UID")
if $WITH_HELPER; then ENV_ARGS+=(--helper); else ENV_ARGS+=(--no-helper); fi
[[ -t 0 ]] && ENV_ARGS+=(--interactive)
"$PYTHON" "$SOURCE_DIR/deploy/env_setup.py" "${ENV_ARGS[@]}"

# Refuse to start services on a configuration the application rejects (they
# would only crash-loop): load it exactly as the installed code will.
(cd "$PREFIX" && .venv/bin/python -c '
import sys
from pydantic import ValidationError
from app.core.config import Settings
try:
    Settings(_env_file=sys.argv[1])
except ValidationError as exc:
    sys.exit(f"invalid configuration in {sys.argv[1]}:\n{exc}")
' "$ENV_FILE") || die "fix $ENV_FILE and run the installer again"

# 5. systemd units -----------------------------------------------------------
# Web and worker are always installed; the helper only with --with-helper
# (put first in the list, since the web unit starts after it so the
# helper's socket exists).
UNITS=($WEB_UNIT $WORKER_UNIT)
$WITH_HELPER && UNITS=($HELPER_UNIT "${UNITS[@]}")
for unit in "${UNITS[@]}"; do
    install -o root -g root -m 0644 "$SOURCE_DIR/deploy/systemd/$unit" "$UNIT_DIR/$unit"
done
# Make systemd pick up the new unit files and start them at boot.
systemctl daemon-reload
systemctl enable --quiet "${UNITS[@]}"
log "installed units: ${UNITS[*]}"

# 6. Start -------------------------------------------------------------------
# --start starts every installed unit; otherwise only what was running
# before the upgrade comes back.
#
# TO_START is a space-separated list with a space at each end, so the check
# below can match a whole unit name with *" name "*. The "-" in
# ${WAS_RUNNING[*]-} keeps "set -u" happy when nothing was running.
if $START; then
    TO_START=" ${UNITS[*]} "
else
    TO_START=" ${WAS_RUNNING[*]-} "
fi
# Start in dependency order: helper, then web, then worker.
for unit in $HELPER_UNIT $WEB_UNIT $WORKER_UNIT; do
    if [[ $TO_START == *" $unit "* ]]; then
        log "starting $unit"
        systemctl start "$unit"
    fi
done

# --- Next steps --------------------------------------------------------------

# Without the helper the smoke test must skip its helper checks.
SMOKE_ARGS=""
$WITH_HELPER || SMOKE_ARGS=" --no-helper"

cat <<EOF

NAS Manager is installed in $PREFIX.
Next steps:
EOF
if grep -q '^NAS_TRUSTED_HOSTS=.*nas\.example\.org' "$ENV_FILE"; then
    echo "  - Set NAS_TRUSTED_HOSTS in $ENV_FILE to the names this NAS is reached by"
    echo "    (it still holds the placeholder nas.example.org), then restart the services."
elif [[ ${NEW_ENV:-false} == true ]]; then
    echo "  - Review $ENV_FILE (reverse proxy, file roots)."
fi
cat <<EOF
  - Create the first administrator (prompts for the password):
      sudo $PREFIX/deploy/nas-manager-cli bootstrap-admin
  - Put the TLS reverse proxy in front (deploy/nginx/nas-manager.conf).
  - Verify: sudo python3 $PREFIX/deploy/smoke_test.py$SMOKE_ARGS
EOF
