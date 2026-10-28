# NAS Manager

Web application for managing an Ubuntu NAS: Samba shares, users and permissions, disks and RAID, file management, synchronization, media, monitoring, backups and auditing. Built with FastAPI, SQLAlchemy, Alembic and Bootstrap 5.

**Status: Phases 1–5 complete (foundation, auth/RBAC, file manager + Samba, public uploads + quotas, disks/storage/RAID via the privileged helper).** See `docs/IMPLEMENTATION_PLAN.md`.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
.venv/bin/alembic upgrade head
.venv/bin/python -m app.cli bootstrap-admin   # first-time administrator; prompts for a password. This is meant to create the first administrator account and prompt for a password.
.venv/bin/python -m app        # http://127.0.0.1:8080
.venv/bin/python -m pytest
```

Documentation: `docs/ARCHITECTURE.md`, `docs/INSTALLATION.md`, `docs/SECURITY.md`, `docs/BACKUP_AND_RESTORE.md`, `docs/API.md`, `docs/IMPLEMENTATION_PLAN.md`.
