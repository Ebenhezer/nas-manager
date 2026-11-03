"""ORM models. Import every model here so Alembic autogenerate sees it."""

from app.models.api_token import ApiToken
from app.models.app_setting import AppSetting
from app.models.associations import group_roles, user_groups, user_roles
from app.models.audit_event import AuditEvent
from app.models.file_change import FileChange
from app.models.group import Group
from app.models.public_upload import PublicUploadFile, PublicUploadQuota
from app.models.role import Role, RolePermission
from app.models.samba_share import SambaShare
from app.models.session import AuthSession
from app.models.upload_session import UploadSession
from app.models.user import User

__all__ = [
    "ApiToken",
    "AppSetting",
    "AuditEvent",
    "AuthSession",
    "FileChange",
    "Group",
    "PublicUploadFile",
    "PublicUploadQuota",
    "Role",
    "RolePermission",
    "SambaShare",
    "UploadSession",
    "User",
    "group_roles",
    "user_groups",
    "user_roles",
]
