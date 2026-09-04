"""Справочники: города, категории виз и цели мониторинга."""

from datetime import time

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin


class City(Base, TimestampMixin):
    """Визовый центр (ТЗ §3).

    Города добавляются и отключаются из панели, без правки кода. Интервалы
    и приоритет настраиваются на город.
    """

    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    # Имя зоны IANA, например Asia/Almaty. Захардкоженный сдвиг сломал бы
    # ночной интервал и отчёт по времени суток при разработке из другой зоны.
    timezone: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    # Меньше значение — выше приоритет при разборе очереди проверок.
    priority: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("100")
    )

    # --- Интервалы проверки (ТЗ §8) ---------------------------------------
    check_interval_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("10")
    )
    night_interval_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("20")
    )
    # Укороченный интервал в окне после находки: слоты выкладывают пачками.
    boost_interval_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("5")
    )
    boost_window_minutes: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("60")
    )
    # Ночное окно в локальном времени города.
    night_start: Mapped[time] = mapped_column(
        sa.Time, nullable=False, server_default=sa.text("'22:00'")
    )
    night_end: Mapped[time] = mapped_column(
        sa.Time, nullable=False, server_default=sa.text("'07:00'")
    )

    targets: Mapped[list["MonitorTarget"]] = relationship(
        back_populates="city", cascade="all, delete-orphan"
    )

    __table_args__ = (
        sa.CheckConstraint("check_interval_minutes > 0", name="check_interval_positive"),
        sa.CheckConstraint("night_interval_minutes > 0", name="night_interval_positive"),
        sa.CheckConstraint("boost_interval_minutes > 0", name="boost_interval_positive"),
    )

    def __repr__(self) -> str:
        return f"<City {self.code}>"


class Category(Base, TimestampMixin):
    """Категория и подкатегория визы (ТЗ §4).

    Хранится в БД, а не в коде: ТЗ прямо запрещает зашивать категории
    в исходники. «Student - other than pre enrolment» заводится выключенной
    и включается администратором.
    """

    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    subcategory_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    subcategory_name: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    __table_args__ = (
        sa.UniqueConstraint("code", "subcategory_code", name="uq_categories_pair"),
    )

    @property
    def full_name(self) -> str:
        return f"{self.name} / {self.subcategory_name}"

    def __repr__(self) -> str:
        return f"<Category {self.code}/{self.subcategory_code}>"


class MonitorTarget(Base, TimestampMixin):
    """Что именно мониторим: город + категория + количество заявителей.

    Одна цель — одна независимая цепочка проверок и одно состояние слотов.
    Количество заявителей входит в ключ, потому что VFS показывает разные
    даты для разного числа заявителей (ТЗ §6, §12).
    """

    __tablename__ = "monitor_targets"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    city_id: Mapped[int] = mapped_column(
        sa.ForeignKey("cities.id", ondelete="CASCADE"), nullable=False
    )
    category_id: Mapped[int] = mapped_column(
        sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False
    )
    applicants: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, server_default=sa.text("1")
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())
    # Переопределяет интервал города для конкретной цели.
    check_interval_minutes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)

    city: Mapped[City] = relationship(back_populates="targets")
    category: Mapped[Category] = relationship()

    __table_args__ = (
        sa.UniqueConstraint(
            "city_id", "category_id", "applicants", name="uq_monitor_targets_key"
        ),
        sa.CheckConstraint("applicants BETWEEN 1 AND 20", name="applicants_range"),
        sa.CheckConstraint(
            "check_interval_minutes IS NULL OR check_interval_minutes > 0",
            name="override_interval_positive",
        ),
        sa.Index("ix_monitor_targets_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<MonitorTarget {self.id} city={self.city_id} cat={self.category_id}>"
