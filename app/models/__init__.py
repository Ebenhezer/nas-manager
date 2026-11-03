"""ORM models. Import every model here so Alembic autogenerate sees it."""

from app.models.api_token import ApiToken
from app.models.app_setting import AppSetting
from app.models.associations import group_roles, user_groups, user_roles
from app.models.audit_event import AuditEvent
from app.models.file_change import FileChange
from app.models.group import Group
from app.models.media import (
    MediaAlbum,
    MediaAlbumItem,
    MediaChange,
    MediaItem,
    MediaLibrary,
    MediaTranscode,
    media_library_groups,
)
from app.models.monitoring import (
    Alert,
    AlertChannel,
    AlertNotification,
    AlertRule,
    MetricSample,
    MonitorCheck,
)
from app.models.public_upload import PublicUploadFile, PublicUploadQuota
from app.models.remote import (
    RemoteConnection,
    SyncConflict,
    SyncJob,
    SyncJobEntry,
    SyncRun,
)
from app.models.role import Role, RolePermission
from app.models.samba_share import SambaShare
from app.models.session import AuthSession
from app.models.upload_session import UploadSession
from app.models.user import User

__all__ = [
    "Alert",
    "AlertChannel",
    "AlertNotification",
    "AlertRule",
    "ApiToken",
    "AppSetting",
    "AuditEvent",
    "AuthSession",
    "FileChange",
    "Group",
    "MediaAlbum",
    "MediaAlbumItem",
    "MediaChange",
    "MediaItem",
    "MediaLibrary",
    "MediaTranscode",
    "MetricSample",
    "MonitorCheck",
    "PublicUploadFile",
    "PublicUploadQuota",
    "RemoteConnection",
    "Role",
    "RolePermission",
    "SambaShare",
    "SyncConflict",
    "SyncJob",
    "SyncJobEntry",
    "SyncRun",
    "UploadSession",
    "User",
    "group_roles",
    "media_library_groups",
    "user_groups",
    "user_roles",
]
