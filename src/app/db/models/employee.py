"""Сотрудники.

Графика дежурств в ТЗ нет: получатели уведомлений определяются ролью
(см. app.services.recipients), а не недельным расписанием.
"""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pg_enum
from app.enums import EmployeeRole


class Employee(Base, TimestampMixin):
    """Сотрудник.

    Доступ в панель — необязательные login/password_hash на той же строке:
    один человек = одна запись, без теневой учётки администратора.
    """

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(sa.BigInteger, unique=True, nullable=True)
    full_name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role: Mapped[EmployeeRole] = mapped_column(
        pg_enum(EmployeeRole, "employee_role"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, server_default=sa.true())

    login: Mapped[str | None] = mapped_column(sa.String(64), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)

    __table_args__ = (
        sa.CheckConstraint(
            "(login IS NULL) = (password_hash IS NULL)",
            name="login_password_together",
        ),
        sa.Index("ix_employees_role_active", "role", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<Employee {self.id} {self.full_name}>"
