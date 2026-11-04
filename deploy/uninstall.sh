#!/usr/bin/env bash
# Remove NAS Manager from this host (Phase 14). Run as root:
#
#   sudo deploy/uninstall.sh                 # remove units and code, keep data
#   sudo deploy/uninstall.sh --purge --yes   # also delete data, logs, config, user
#
# Without --purge, /var/lib/nas-manager (database, credentials key, uploads,
# media, local backups), /var/log/nas-manager, /etc/nas-manager and the
# nas-manager account are kept, so a later install.sh picks them up again.
# --purge permanently deletes them and needs --yes. Managed file roots, media
# folders and backup roots outside /var/lib/nas-manager, smb.conf and mounted
# disks are never touched.
set -euo pipefail

readonly PREFIX=/opt/nas-manager
readonly DATA_DIR=/var/lib/nas-manager
readonly LOG_DIR=/var/log/nas-manager
readonly ETC_DIR=/etc/nas-manager
readonly UNIT_DIR=/etc/systemd/system
readonly SERVICE_USER=nas-manager
readonly UNITS=(nas-manager-worker.service nas-manager.service nas-manager-helper.service)

PURGE=false
YES=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --purge) PURGE=true ;;
        --yes) YES=true ;;
        -h | --help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

log() { printf '==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
if $PURGE && ! $YES; then
    echo "--purge deletes $DATA_DIR, $LOG_DIR, $ETC_DIR and the $SERVICE_USER account." >&2
    echo "Back up first (see docs/BACKUP_AND_RESTORE.md), then re-run with --purge --yes." >&2
    exit 1
fi

for unit in "${UNITS[@]}"; do
    if [[ -e $UNIT_DIR/$unit ]]; then
        log "stopping and disabling $unit"
        systemctl disable --now --quiet "$unit" || true
        rm -f -- "${UNIT_DIR:?}/$unit"
    fi
done
systemctl daemon-reload
systemctl reset-failed "${UNITS[@]}" 2>/dev/null || true

if [[ -d $PREFIX ]]; then
    log "removing $PREFIX"
    rm -rf -- "${PREFIX:?}"
fi

if $PURGE; then
    for dir in "$DATA_DIR" "$LOG_DIR" "$ETC_DIR"; do
        if [[ -e $dir ]]; then
            log "deleting $dir"
            rm -rf -- "${dir:?}"
        fi
    done
    if getent passwd "$SERVICE_USER" >/dev/null; then
        log "deleting user $SERVICE_USER"
        userdel "$SERVICE_USER"
    fi
    if getent group "$SERVICE_USER" >/dev/null; then
        groupdel "$SERVICE_USER"
    fi
else
    log "kept $DATA_DIR, $LOG_DIR, $ETC_DIR and the $SERVICE_USER account"
fi
log "done"
