"""Базовый класс моделей и общие примитивы схемы."""

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Единая схема имён ограничений: без неё Alembic генерирует безымянные
# constraint'ы, которые потом невозможно удалить в миграции down().
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Базовый декларативный класс."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {  # noqa: RUF012
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
    }


def pg_enum(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """Нативный enum PostgreSQL, хранящий строковые значения (не имена членов)."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=True,
        create_constraint=False,
        values_callable=lambda e: [member.value for member in e],
    )


class StrEnumType(sa.types.TypeDecorator[Any]):
    """varchar, который на чтение возвращает член перечисления, а не строку.

    Без этого декоратора колонка, объявленная как Mapped[SomeStrEnum], молча
    отдавала бы str: сравнение вида `task.origin is TaskOrigin.REGULAR` всегда
    было бы ложным, а обращение к `.value` падало бы AttributeError. Аннотация
    типа обязана соответствовать тому, что реально приходит из БД.
    """

    impl = sa.String
    cache_ok = True

    def __init__(self, enum_cls: type[StrEnum], length: int = 32) -> None:
        self.enum_cls = enum_cls
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: object) -> str | None:
        if value is None:
            return None
        return self.enum_cls(value).value

    def process_result_value(self, value: Any, dialect: object) -> StrEnum | None:
        if value is None:
            return None
        return self.enum_cls(value)


def str_enum_column(
    enum_cls: type[StrEnum],
    constraint_name: str,
    length: int = 32,
) -> tuple[StrEnumType, sa.CheckConstraint]:
    """Колонка varchar + CHECK для перечислений, которые будут пополняться.

    Возвращает тип и готовое ограничение — расширить список значений дешевле,
    чем делать ALTER TYPE для нативного enum'а. В DDL это обычный VARCHAR(n),
    поэтому миграция от типа не зависит.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_cls)
    return (
        StrEnumType(enum_cls, length),
        sa.CheckConstraint(f"{constraint_name} IN ({values})", name=f"{constraint_name}_valid"),
    )


def utcnow_server_default() -> sa.TextClause:
    """Серверный дефолт для timestamptz: время фиксирует БД, а не воркер."""
    return sa.text("now()")


class TimestampMixin:
    """created_at/updated_at, проставляемые сервером БД."""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=utcnow_server_default(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=utcnow_server_default(),
        onupdate=sa.func.now(),
        nullable=False,
    )
