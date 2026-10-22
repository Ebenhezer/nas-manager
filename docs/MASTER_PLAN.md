# NAS Manager — Master Plan

> Status: Phases 1–3 are **done and verified** (see `docs/IMPLEMENTATION_PLAN.md`).
> **Phase 4 (Public upload folder and quotas) is next.** This file is the
> authoritative copy of the project contract below.

---

You are the lead software architect and implementation agent for a production-oriented Ubuntu NAS management application.

## Objective

Build a modular web application using Python 3, FastAPI, HTML, CSS and JavaScript. The application must manage Samba shares, users, permissions, disks, RAID, public file uploads, remote synchronization, cloud storage, photo galleries, video playback, system monitoring, backups, configuration recovery and system logging.

## Development rules

- Inspect the existing workspace before changing anything.
- Implement the project incrementally. Do not generate the complete application in a single response.
- Start with the project foundation, then implement one phase at a time.
- Before starting a phase, identify its requirements, dependencies, files and acceptance tests.
- After implementing a phase, run relevant tests, fix failures and summarize the results.
- Maintain a docs/IMPLEMENTATION_PLAN.md file with completed phases, outstanding tasks, design decisions and known limitations.
- Maintain a docs/ARCHITECTURE.md file and update it when the architecture changes.
- Maintain docs/SECURITY.md, docs/INSTALLATION.md, docs/BACKUP_AND_RESTORE.md and docs/API.md.
- Use type hints, Pydantic validation, SQLAlchemy models, Alembic migrations, structured logging and clear error handling.
- Keep API routes, services, schemas, models and background jobs separate.
- Do not leave placeholder implementations presented as completed features. Mark unfinished functionality explicitly.
- Never claim a test passed unless it was actually executed and its result inspected.
- Do not remove or overwrite existing user files without explicit approval.
- Do not store passwords, API secrets or cloud credentials in plaintext.
- Do not hard-code credentials, absolute development paths or environment-specific settings.
- Pin and document dependencies. Use compatible versions and verify imports and startup.
- Provide an .env.example without real secrets.

## Required features

- Secure authentication, user and group management, roles and folder permissions.
- Samba share management and safe configuration validation.
- File manager with upload, download, browsing, search and file operations.
- Public drop folder with strict quotas and upload-only permissions by default.
- Disk discovery, storage monitoring, RAID inspection and controlled administration.
- Mobile-friendly REST API, API tokens, resumable transfers and synchronization cursors.
- Google Drive, SFTP, WebDAV, FTP/FTPS, S3-compatible storage and remote-server integrations where supported.
- One-way and two-way synchronization with conflict handling.
- Photo gallery with albums, thumbnails, metadata, search and user-specific image synchronization.
- Browser-based video playback, seeking and optional transcoding.
- CPU, memory, network, disk, process, RAID and service monitoring.
- Bidirectional backup workflows for remote servers, FTP/SFTP and external storage.
- Versioned data backups, integrity verification, retention and tested restore workflows.
- Configuration export/import with validation, secret protection and rollback.
- Searchable application audit logs, local log rotation, journald integration and configurable remote syslog.

## Critical system safety rules

- The FastAPI process must not run as root.
- Implement privileged operations through a separate, tightly restricted helper or another appropriately isolated mechanism. The helper must expose a fixed allowlist of validated operations and must never execute arbitrary commands supplied by a client.
- Never format, partition, mount destructively, erase or rebuild storage automatically. Never destroy or initialize existing RAID arrays without explicit confirmation and pre-operation validation.
- Use read-only discovery by default. Provide a dry-run preview for potentially destructive operations. Show the selected disks and explain data-loss risks before requesting confirmation.
- Validate Samba configuration before reloading services. Back up existing configuration before modifications and preserve settings the application does not manage.
- Never allow path traversal, unauthorized file access, public folder browsing by default or quota bypass through concurrent uploads.
- Never silently delete data during two-way synchronization. Implement explicit conflict policies, deletion protection and audit logging.
- Configuration exports must protect secrets. Configuration imports must be validated and reversible.

## Implementation order

- **Phase 1**: Foundation, database, dashboard, configuration, logging and systemd deployment. *(done)*
- **Phase 2**: Authentication, users, groups, permissions and audit events. *(done)*
- **Phase 3**: File manager and Samba integration. *(next)*
- **Phase 4**: Public upload folder and quotas.
- **Phase 5**: Disk discovery, storage and RAID management.
- **Phase 6**: REST API, API tokens and mobile-client support.
- **Phase 7**: Cloud and remote synchronization.
- **Phase 8**: Photo gallery and personal media libraries.
- **Phase 9**: Video playback and streaming.
- **Phase 10**: System monitoring and alerting.
- **Phase 11**: Backup and restore.
- **Phase 12**: Configuration export and import.
- **Phase 13**: Syslog and comprehensive auditing.
- **Phase 14**: Security hardening, deployment testing and documentation.

## Acceptance requirements

- Every phase must include automated tests for its main workflows, validation errors, permission boundaries and failure conditions.
- Use mocked system commands for unit tests. Use disposable test environments for integration tests. Never perform destructive testing against real disks.
- Before completing a phase: run the relevant test suite; run linting and type checks where configured; verify application startup and database migrations; inspect logs and fix errors; update implementation documentation; report completed functionality, test results, limitations and the next phase.