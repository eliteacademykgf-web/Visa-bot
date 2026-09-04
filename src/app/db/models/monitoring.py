"""Проверки, текущее состояние слотов и события изменений."""

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, pg_enum, str_enum_column
from app.db.models.account import VfsAccount
from app.db.models.reference import MonitorTarget
from app.enums import CheckStatus, CheckTrigger, SlotEventType

_trigger_type, _trigger_check = str_enum_column(CheckTrigger, "trigger", length=16)
_event_type, _event_check = str_enum_column(SlotEventType, "event_type", length=24)


class SlotCheck(Base):
    """Одна выполненная проверка (ТЗ §6).

    Пишется всегда — и при успехе, и при ошибке. Журнал проверок хранится
    не менее 12 месяцев (ТЗ §18), поэтому таблица рассчитана на рост:
    тяжёлые артефакты (скриншот, HTML) лежат на диске, в строке только путь.
    """

    __tablename__ = "slot_checks"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitor_targets.id", ondelete="RESTRICT"), nullable=False
    )
    account_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("vfs_accounts.id", ondelete="SET NULL"), nullable=True
    )

    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    trigger: Mapped[CheckTrigger] = mapped_column(
        _trigger_type, nullable=False, server_default=CheckTrigger.SCHEDULE.value
    )

    status: Mapped[CheckStatus] = mapped_column(
        pg_enum(CheckStatus, "check_status"), nullable=False
    )
    nearest_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    # Полный список доступных дат, если сайт его отдаёт.
    available_dates: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    available_times: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    slots_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    # Текст, показанный сайтом, и технический ответ — оба нужны при разборе
    # изменений интерфейса: по ним видно, что именно изменилось.
    site_message: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    http_status: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    error_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    screenshot_path: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    html_path: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)

    target: Mapped[MonitorTarget] = relationship()
    account: Mapped[VfsAccount | None] = relationship()

    __table_args__ = (
        _trigger_check,
        sa.CheckConstraint("duration_ms IS NULL OR duration_ms >= 0", name="duration_non_negative"),
        # Ведущий индекс журнала и поиска последней проверки по цели.
        sa.Index("ix_slot_checks_target_started", "target_id", sa.desc("started_at")),
        sa.Index("ix_slot_checks_status_started", "status", sa.desc("started_at")),
        sa.Index("ix_slot_checks_started_at", sa.desc("started_at")),
    )

    def __repr__(self) -> str:
        return f"<SlotCheck {self.id} {self.status}>"


class SlotState(Base):
    """Текущее известное состояние слотов по цели.

    Одна строка на цель. Это «предыдущий результат», с которым сравнивается
    каждая новая проверка (ТЗ §5, §10). Хранится отдельно, а не выводится
    запросом по журналу: сравнение выполняется на каждой проверке, и оно
    не должно зависеть от объёма истории.
    """

    __tablename__ = "slot_states"

    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitor_targets.id", ondelete="CASCADE"), primary_key=True
    )

    status: Mapped[CheckStatus] = mapped_column(
        pg_enum(CheckStatus, "check_status"), nullable=False
    )
    nearest_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    available_dates: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    available_times: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    slots_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    # Когда текущая картина установилась — по нему считается, сколько слот
    # остаётся доступным (ТЗ §19).
    since: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_check_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # Плановое время следующей проверки. Хранится, а не вычисляется на лету:
    # в него уже заложен случайный джиттер, и после рестарта процесса он
    # не должен пересчитываться заново — иначе отклонение перестаёт быть
    # отклонением и сетка обращений снова становится идеально ровной.
    next_check_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Когда в последний раз по этой цели был виден слот — основа режима boost.
    last_slot_found_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Момент последнего ОТПРАВЛЕННОГО уведомления: от него отсчитывается
    # интервал повторного напоминания о том же слоте.
    last_notified_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # default нужен рядом с server_default: последний срабатывает только
    # в SQL, а код увеличивает счётчик до записи в БД.
    consecutive_errors: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0, server_default=sa.text("0")
    )
    last_check_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("slot_checks.id", ondelete="SET NULL"), nullable=True
    )

    target: Mapped[MonitorTarget] = relationship()

    __table_args__ = (
        sa.CheckConstraint("consecutive_errors >= 0", name="state_errors_non_negative"),
        # Ведущий индекс свипа проверок: кому пора.
        sa.Index("ix_slot_states_next_check_at", "next_check_at"),
    )

    def __repr__(self) -> str:
        return f"<SlotState target={self.target_id} {self.status}>"


class SlotEvent(Base):
    """Изменение картины слотов, требующее уведомления (ТЗ §10).

    Уведомления порождают события, а не проверки: проверка, не изменившая
    картину, события не создаёт. Именно это отделяет систему от бота,
    который пишет в чат каждые пять минут «слотов нет».
    """

    __tablename__ = "slot_events"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    target_id: Mapped[int] = mapped_column(
        sa.ForeignKey("monitor_targets.id", ondelete="CASCADE"), nullable=False
    )
    check_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("slot_checks.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[SlotEventType] = mapped_column(_event_type, nullable=False)

    previous_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    new_date: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )

    target: Mapped[MonitorTarget] = relationship()
    check: Mapped[SlotCheck | None] = relationship()

    __table_args__ = (
        _event_check,
        sa.Index("ix_slot_events_target_created", "target_id", sa.desc("created_at")),
        sa.Index("ix_slot_events_type_created", "event_type", sa.desc("created_at")),
    )

    def __repr__(self) -> str:
        return f"<SlotEvent {self.id} {self.event_type}>"
