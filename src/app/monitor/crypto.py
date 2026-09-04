"""Шифрование учётных данных VFS (ТЗ §13, §21).

Пароль от VFS хранится только в шифрованном виде. Ключ живёт в переменной
окружения и никогда не попадает в репозиторий — ТЗ §21 требует этого явно
(«запрет хранения паролей в исходном коде»).

Расшифровка происходит в момент проверки и только в памяти процесса
мониторинга. Панель пароль не показывает и не отдаёт (ТЗ §13, §22).
"""

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.logging import get_logger

log = get_logger(__name__)


class CredentialsError(RuntimeError):
    """Ключ шифрования не задан или шифротекст повреждён."""


def _fernet() -> Fernet:
    key = get_settings().credentials_key.get_secret_value()
    if not key:
        raise CredentialsError(
            "не задан CREDENTIALS_KEY: без него пароли VFS хранить нельзя"
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialsError(f"некорректный CREDENTIALS_KEY: {exc}") from exc


def generate_key() -> str:
    """Сгенерировать новый ключ (используется management-командой)."""
    return Fernet.generate_key().decode()


def encrypt(secret: str) -> str:
    """Зашифровать пароль для хранения."""
    return _fernet().encrypt(secret.encode()).decode()


def decrypt(token: str) -> str:
    """Расшифровать пароль.

    Смена ключа делает старые шифротексты нечитаемыми — это осознанное
    свойство: пароли придётся ввести заново, что честнее молчаливого отказа
    входа на сайт с непонятной причиной.
    """
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise CredentialsError(
            "не удалось расшифровать пароль: сменился CREDENTIALS_KEY?"
        ) from exc


def mask(value: str) -> str:
    """Замаскировать значение для журнала (ТЗ §21: чистить логи)."""
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"
