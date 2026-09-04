"""Служебные таблицы: журнал уведомлений, outbox вебхуков, настройки, аудит."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, pg_enum, str_enum_column
from app.db.models.employee import Employee
from app.enums import NotificationKind, SettingValueType, WebhookStatus

_kind_type, _kind_check = str_enum_column(NotificationKind, "kind", length=48)
_value_type, _value_type_check = str_enum_column(SettingValueType, "value_type", length=16)


class NotificationLog(Base):
    """Все исходящие сообщения бота.

    is_delivered=false — не только запись для отчёта: недоставленное задание
    (дежурный не нажимал /start или заблокировал бота) эскалируется немедленно,
    ждать первого порога в этом случае бессмысленно.
    """

    __tablename__ = "notification_log"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True
    )
    employee_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    chat_id: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    kind: Mapped[NotificationKind] = mapped_column(_kind_type, nullable=False)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(sa.BigInteger, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    is_delivered: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        _kind_check,
        sa.Index("ix_notification_log_alert_id_kind", "alert_id", "kind"),
        sa.Index("ix_notification_log_sent_at", "sent_at"),
        sa.Index(
            "ix_notification_log_failed_sent_at",
            "sent_at",
            postgresql_where=sa.text("is_delivered = false"),
        ),
    )


class WebhookDelivery(Base):
    """Outbox доставки в CRM.

    Ретраи живут в строках, а не в отложенных джобах: после рестарта процесса
    очередь восстанавливается сама, достаточно свипа по (status, next_retry_at).
    """

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    event_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("slot_events.id", ondelete="SET NULL"), nullable=True
    )
    event: Mapped[str] = mapped_column(
        sa.String(32), nullable=False, server_default="slot_found"
    )
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("0")
    )
    status: Mapped[WebhookStatus] = mapped_column(
        pg_enum(WebhookStatus, "webhook_status"),
        nullable=False,
        server_default=WebhookStatus.PENDING.value,
    )
    last_status_code: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        sa.Index(
            "ix_webhook_deliveries_due",
            "next_retry_at",
            postgresql_where=sa.text("status = 'pending'"),
        ),
        sa.Index("ix_webhook_deliveries_event_id", "event_id"),
    )


class Setting(Base):
    """Настройка, редактируемая из панели.

    Типизированное key-value: добавление порога не требует миграции.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    value_type: Mapped[SettingValueType] = mapped_column(_value_type, nullable=False)
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("now()"),
        onupdate=sa.func.now(),
        nullable=False,
    )
    updated_by_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    updated_by: Mapped[Employee | None] = relationship()

    __table_args__ = (_value_type_check,)


class AuditLog(Base):
    """Журнал действий в панели.

    Append-only: в интерфейсе нет маршрутов изменения и удаления, а на уровне БД
    UPDATE/DELETE отвергает триггер (см. миграцию 0001).
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    actor_employee_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_audit_log_entity_entity_id", "entity", "entity_id"),
        sa.Index("ix_audit_log_created_at", "created_at"),
    )
