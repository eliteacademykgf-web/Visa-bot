"""Конфигурация приложения.

В переменных окружения живёт только инфраструктура и секреты. Все бизнес-пороги
(интервалы эскалаций, ID чата, URL вебхука) редактируются из панели и хранятся
в таблице settings — иначе изменение порога требовало бы передеплоя.
"""

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Настройки, читаемые из окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["local", "staging", "production"] = "local"
    log_level: str = "INFO"
    debug: bool = False

    # --- PostgreSQL ---------------------------------------------------------
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "visa"
    postgres_password: SecretStr = SecretStr("visa")
    postgres_db: str = "visa_duty"

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # --- Telegram -----------------------------------------------------------
    bot_token: SecretStr = SecretStr("")

    # --- Учётные данные VFS (ТЗ §13, §21) -----------------------------------
    # Ключ Fernet для шифрования паролей. В коде и репозитории его быть
    # не должно; сгенерировать — `python -m app.cli generate-key`.
    credentials_key: SecretStr = SecretStr("")

    # --- Мониторинг ---------------------------------------------------------
    selectors_path: str = "config/vfs_selectors.json"
    artifacts_dir: str = "artifacts"
    # Профили браузера по учётным записям. Сессия живёт здесь, а не только
    # в куках: Cloudflare выдаёт clearance конкретному профилю, и постоянный
    # каталог продлевает жизнь разового ручного входа с часов до недель.
    profiles_dir: str = "profiles"
    playwright_headless: bool = True
    playwright_timeout_ms: int = 30000
    # Глобальный выключатель мониторинга (ТЗ §12: «остановить мониторинг»).
    monitoring_enabled: bool = True

    # --- Админ-панель -------------------------------------------------------
    web_secret_key: SecretStr = SecretStr("change-me")
    web_session_cookie: str = "visa_duty_session"
    web_session_ttl_hours: int = 12
    web_host: str = "0.0.0.0"
    web_port: int = 8000

    # --- Планировщик --------------------------------------------------------
    scheduler_tick_seconds: int = 60
    # Ключ advisory-локов PostgreSQL. Разные ключи для разных свипов, чтобы
    # медленный свип не блокировал остальные.
    lock_key_task_creation: int = 815001
    lock_key_escalation: int = 815002
    lock_key_ack_reclaim: int = 815003
    lock_key_webhook: int = 815004
    lock_key_monitor: int = 815005
    lock_key_alerts: int = 815006

    # --- Вебхук CRM ---------------------------------------------------------
    webhook_timeout_seconds: float = 10.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_async(self) -> str:
        """DSN для asyncpg — рабочий путь приложения."""
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url_sync(self) -> str:
        """DSN для psycopg — нужен Alembic и джобстору APScheduler."""
        pwd = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Синглтон настроек."""
    return Settings()
