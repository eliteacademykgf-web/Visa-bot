"""Аутентификация в админ-панели.

Сессия — подписанная кука, без серверного хранилища: панелью пользуются
несколько человек, и держать ради этого таблицу сессий незачем. Смена
WEB_SECRET_KEY разлогинивает всех, это осознанное свойство.
"""

import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

# Argon2id напрямую, а не через passlib: passlib импортирует модуль crypt,
# удалённый в Python 3.13, и первый же апгрейд интерпретатора уронил бы вход
# в панель. argon2-cffi — та самая библиотека, которую passlib оборачивает.
_hasher = PasswordHasher()

SESSION_SALT = "visa-duty-session"


@dataclass(frozen=True, slots=True)
class SessionData:
    """Содержимое куки сессии."""

    employee_id: int
    csrf_token: str


def hash_password(password: str) -> str:
    """Хеш пароля для хранения в БД."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Проверить пароль.

    Пустой хеш — сотрудник без доступа в панель, такой пароль не подходит
    никогда. Исключения библиотеки гасятся: наверх должно уходить решение,
    а не тип ошибки, по которому можно различить «нет пользователя»
    и «неверный пароль».
    """
    if not password_hash:
        return False
    try:
        return bool(_hasher.verify(password_hash, password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().web_secret_key.get_secret_value(), salt=SESSION_SALT
    )


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def dump_session(employee_id: int, csrf_token: str) -> str:
    """Упаковать сессию в значение куки."""
    return str(_serializer().dumps({"sub": employee_id, "csrf": csrf_token}))


def load_session(raw: str | None) -> SessionData | None:
    """Разобрать куку. None — подпись не сошлась или срок истёк."""
    if not raw:
        return None
    settings = get_settings()
    try:
        payload = _serializer().loads(raw, max_age=settings.web_session_ttl_hours * 3600)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(payload, dict) or "sub" not in payload:
        return None
    return SessionData(employee_id=int(payload["sub"]), csrf_token=str(payload.get("csrf", "")))


def csrf_is_valid(session: SessionData | None, submitted: str | None) -> bool:
    """Сверить CSRF-токен формы с токеном сессии.

    Сравнение постоянного времени: токен подписан вместе с сессией, но
    утечка через тайминг всё равно ни к чему.
    """
    if session is None or not submitted:
        return False
    return secrets.compare_digest(session.csrf_token, submitted)
