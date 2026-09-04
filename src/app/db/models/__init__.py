"""Модели БД. Импорт всех модулей нужен Alembic для автогенерации."""

from app.db.base import Base
from app.db.models.account import AccountLoginLog, VfsAccount
from app.db.models.alert import Alert, AlertEscalation, AlertReaction
from app.db.models.employee import Employee
from app.db.models.monitoring import SlotCheck, SlotEvent, SlotState
from app.db.models.ops import AuditLog, NotificationLog, Setting, WebhookDelivery
from app.db.models.reference import Category, City, MonitorTarget

__all__ = [
    "AccountLoginLog",
    "Alert",
    "AlertEscalation",
    "AlertReaction",
    "AuditLog",
    "Base",
    "Category",
    "City",
    "Employee",
    "MonitorTarget",
    "NotificationLog",
    "Setting",
    "SlotCheck",
    "SlotEvent",
    "SlotState",
    "VfsAccount",
    "WebhookDelivery",
]
