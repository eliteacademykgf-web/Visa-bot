"""Зависимости FastAPI: сессия БД, текущий пользователь, защита форм."""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Employee
from app.db.session import get_sessionmaker
from app.enums import EmployeeRole
from app.services.notifications import Notifier
from app.web.auth import SessionData, csrf_is_valid, load_session


class NotAuthenticatedError(HTTPException):
    """Нет валидной сессии — пользователя нужно отправить на форму входа."""

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_401_UNAUTHORIZED)


async def get_db() -> AsyncIterator[AsyncSession]:
    """Сессия БД на запрос."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_db)]


def read_session(request: Request) -> SessionData | None:
    """Разобрать куку сессии."""
    return load_session(request.cookies.get(get_settings().web_session_cookie))


async def current_employee(
    request: Request,
    session: DbSession,
) -> Employee:
    """Текущий пользователь панели.

    Отключённый сотрудник теряет доступ немедленно, не дожидаясь истечения
    куки: проверка идёт по БД на каждом запросе.
    """
    data = read_session(request)
    if data is None:
        raise NotAuthenticatedError

    employee = await session.get(Employee, data.employee_id)
    if employee is None or not employee.is_active or not employee.login:
        raise NotAuthenticatedError

    request.state.csrf_token = data.csrf_token
    return employee


CurrentUser = Annotated[Employee, Depends(current_employee)]


async def require_admin(user: CurrentUser) -> Employee:
    """Действия, доступные только администратору."""
    if user.role is not EmployeeRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нужны права админа")
    return user


AdminUser = Annotated[Employee, Depends(require_admin)]


async def verify_csrf(
    request: Request,
    csrf_token: Annotated[str, Form()] = "",
) -> None:
    """Проверить CSRF-токен формы.

    Панель работает на куках, поэтому без токена сторонняя страница могла бы
    отправить форму от имени залогиненного администратора.
    """
    if not csrf_is_valid(read_session(request), csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Неверный CSRF-токен")


CsrfProtected = Annotated[None, Depends(verify_csrf)]


def get_notifier(request: Request) -> Notifier:
    """Транспорт сообщений, созданный при старте приложения.

    Панель тоже шлёт в Telegram: ручная проверка должна уходить дежурному
    сразу, а не ждать следующего прохода планировщика.
    """
    notifier: Notifier = request.app.state.notifier
    return notifier


WebNotifier = Annotated[Notifier, Depends(get_notifier)]


def redirect(url: str) -> RedirectResponse:
    """Редирект после успешного POST (см. PRG)."""
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)
