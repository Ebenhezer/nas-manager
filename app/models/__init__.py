"""ORM models. Import every model here so Alembic autogenerate sees it."""

from app.models.app_setting import AppSetting
from app.models.associations import group_roles, user_groups, user_roles
from app.models.audit_event import AuditEvent
from app.models.group import Group
from app.models.role import Role, RolePermission
from app.models.samba_share import SambaShare
from app.models.session import AuthSession
from app.models.user import User

__all__ = [
    "AppSetting",
    "AuditEvent",
    "AuthSession",
    "Group",
    "Role",
    "RolePermission",
    "SambaShare",
    "User",
    "group_roles",
    "user_groups",
    "user_roles",
]
