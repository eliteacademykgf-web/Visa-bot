"""Учётные записи VFS Global (ТЗ §13)."""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, pg_enum
from app.enums import AccountStatus


class VfsAccount(Base, TimestampMixin):
    """Учётная запись для мониторинга.

    Пароль хранится ТОЛЬКО в зашифрованном виде (Fernet, ключ в окружении)
    и никогда не отдаётся в панель и не пишется в логи — ТЗ §13 и §21
    требуют этого явно, а §22 запрещает визовому специалисту видеть пароль.

    Отдельная учётная запись для мониторинга — не пожелание, а следствие
    исследования: одновременная работа одного аккаунта с разных устройств
    вызывает сбои аутентификации, а автоматическая активность может привести
    к постоянной блокировке. Блокировка рабочего аккаунта остановила бы
    подачу документов, а не только мониторинг.
    """

    __tablename__ = "vfs_accounts"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    label: Mapped[str] = mapped_column(sa.String(64), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    # Шифротекст Fernet. В открытом виде пароль не появляется нигде.
    password_encrypted: Mapped[str] = mapped_column(sa.Text, nullable=False)

    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
    status: Mapped[AccountStatus] = mapped_column(
        pg_enum(AccountStatus, "account_status"),
        nullable=False,
        default=AccountStatus.OK,
        server_default=AccountStatus.OK.value,
    )
    status_note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    # Сохранённое состояние браузера (куки, localStorage) — позволяет не
    # логиниться на каждой проверке. Частые входы и есть основной признак,
    # по которому VFS блокирует учётные записи.
    session_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    session_saved_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    # Подряд идущие ошибки: после порога проверки ставятся на паузу (ТЗ §8).
    consecutive_errors: Mapped[int] = mapped_column(
        sa.SmallInteger, nullable=False, default=0, server_default=sa.text("0")
    )
    # До этого момента учётная запись не используется: пауза после ошибок
    # или остановка после признаков блокировки.
    paused_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        sa.CheckConstraint("consecutive_errors >= 0", name="consecutive_errors_non_negative"),
        sa.Index("ix_vfs_accounts_usable", "is_active", "status"),
    )

    @property
    def is_available(self) -> bool:
        """Готова ли учётная запись к использованию прямо сейчас."""
        return self.is_active and self.status is AccountStatus.OK

    def __repr__(self) -> str:
        # Ни пароля, ни имени пользователя: repr попадает в логи и трейсбеки.
        return f"<VfsAccount {self.id} {self.label} {self.status}>"


class AccountLoginLog(Base):
    """Журнал входов в учётную запись VFS (ТЗ §13 «вести журнал входов»)."""

    __tablename__ = "account_login_log"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        sa.ForeignKey("vfs_accounts.id", ondelete="CASCADE"), nullable=False
    )
    attempted_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    succeeded: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    # Использовалась сохранённая сессия вместо ввода пароля.
    reused_session: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (
        sa.Index(
            "ix_account_login_log_account_attempted", "account_id", "attempted_at"
        ),
    )
