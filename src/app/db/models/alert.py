"""Уведомления сотрудникам, их реакции и эскалации (ТЗ §11)."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, str_enum_column
from app.db.models.employee import Employee
from app.db.models.monitoring import SlotEvent
from app.enums import EscalationReason, EscalationStatus, ReactionKind

_reaction_type, _reaction_check = str_enum_column(ReactionKind, "kind", length=24)
_reason_type, _reason_check = str_enum_column(EscalationReason, "reason", length=16)
_esc_status_type, _esc_status_check = str_enum_column(EscalationStatus, "status", length=16)


class Alert(Base):
    """Уведомление о событии, ожидающее реакции сотрудника.

    Отделено от SlotEvent намеренно: событие — факт об окружающем мире,
    уведомление — работа, которую кто-то должен сделать. Эскалации и
    измерение времени реакции относятся ко второму.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        sa.ForeignKey("slot_events.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # Кому адресовано изначально. NULL — получателей не нашлось вовсе,
    # это отдельная аварийная ситуация, а не нормальный ход.
    assignee_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    # Момент фактической отправки — от него, а не от создания, отсчитываются
    # пороги эскалации: если сообщение ушло с задержкой, штрафовать сотрудника
    # за эту задержку нельзя.
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Первая реакция любого рода — «время реакции» из ТЗ §11 и §19.
    first_reaction_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Отдельно фиксируется бронирование: ТЗ §11 требует время от уведомления
    # до бронирования, а не только до первого нажатия.
    booked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    # Сколько раз уведомление передавалось другому сотруднику.
    handover_count: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0, server_default=sa.text("0")
    )

    event: Mapped[SlotEvent] = relationship()
    assignee: Mapped[Employee | None] = relationship()
    reactions: Mapped[list["AlertReaction"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )
    escalations: Mapped[list["AlertEscalation"]] = relationship(
        back_populates="alert", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.CheckConstraint("handover_count >= 0", name="handover_count_non_negative"),
        # Ведущий индекс свипа эскалаций: только незакрытые уведомления.
        sa.Index(
            "ix_alerts_open_sent_at",
            "sent_at",
            postgresql_where=sa.text("closed_at IS NULL"),
        ),
        sa.Index("ix_alerts_assignee_created", "assignee_id", sa.desc("created_at")),
    )

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def __repr__(self) -> str:
        return f"<Alert {self.id} event={self.event_id}>"


class AlertReaction(Base):
    """Нажатие кнопки сотрудником (ТЗ §11).

    Хранится каждое нажатие, а не только последнее: «Принял» в 18:14
    и «Слот уже исчез» в 18:19 — это две разные записи, и обе нужны для
    отчёта о том, сколько уведомлений привели к бронированию.
    """

    __tablename__ = "alert_reactions"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )
    employee_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[ReactionKind] = mapped_column(_reaction_type, nullable=False)
    comment: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    reacted_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Материализуется при записи: аналитика за произвольный период не должна
    # пересчитывать это по каждой строке.
    seconds_from_alert: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    alert: Mapped[Alert] = relationship(back_populates="reactions")
    employee: Mapped[Employee | None] = relationship()

    __table_args__ = (
        _reaction_check,
        # Один сотрудник не может дважды поставить одну и ту же реакцию:
        # это и есть защита от двойного нажатия на уровне БД.
        sa.UniqueConstraint(
            "alert_id", "employee_id", "kind", name="uq_alert_reactions_once"
        ),
        sa.CheckConstraint(
            "seconds_from_alert IS NULL OR seconds_from_alert >= 0",
            name="seconds_from_alert_non_negative",
        ),
        sa.Index("ix_alert_reactions_employee_reacted", "employee_id", "reacted_at"),
        sa.Index("ix_alert_reactions_kind_reacted", "kind", "reacted_at"),
    )

    def __repr__(self) -> str:
        return f"<AlertReaction {self.id} {self.kind}>"


class AlertEscalation(Base):
    """Факт срабатывания уровня эскалации по уведомлению.

    UNIQUE (alert_id, level) — защита от дублей: свип может запуститься
    повторно после рестарта, но вторая вставка того же уровня будет отвергнута.
    Статус SUPPRESSED означает, что уровень был просрочен, но не отправлен,
    потому что одновременно оказался просрочен более высокий.
    """

    __tablename__ = "alert_escalations"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        sa.ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[int] = mapped_column(sa.SmallInteger, nullable=False)
    reason: Mapped[EscalationReason] = mapped_column(_reason_type, nullable=False)
    status: Mapped[EscalationStatus] = mapped_column(
        _esc_status_type,
        nullable=False,
        default=EscalationStatus.SENT,
        server_default=EscalationStatus.SENT.value,
    )
    triggered_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    recipients: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    alert: Mapped[Alert] = relationship(back_populates="escalations")

    __table_args__ = (
        _reason_check,
        _esc_status_check,
        sa.UniqueConstraint("alert_id", "level", name="uq_alert_escalations_level"),
        sa.CheckConstraint("level BETWEEN 0 AND 9", name="escalation_level_range"),
    )

    def __repr__(self) -> str:
        return f"<AlertEscalation alert={self.alert_id} level={self.level}>"
