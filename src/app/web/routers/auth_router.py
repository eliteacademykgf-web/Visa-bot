"""Вход и выход из панели."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.db.models import Employee
from app.logging import get_logger
from app.services.audit_service import record
from app.web.auth import dump_session, new_csrf_token, verify_password
from app.web.deps import DbSession, redirect
from app.web.templating import render

router = APIRouter()
log = get_logger(__name__)


@router.get("/login")
async def login_form(request: Request) -> Response:
    """Форма входа."""
    return render(request, "login.html", {"csrf_token": "", "error": None})


@router.post("/login")
async def login(
    request: Request,
    session: DbSession,
    login: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """Проверить логин и пароль, выдать куку сессии.

    Сообщение об ошибке одно на оба случая — так по нему нельзя перебрать
    существующие логины.
    """
    employee = (
        await session.execute(sa.select(Employee).where(Employee.login == login.strip()))
    ).scalar_one_or_none()

    ok = (
        employee is not None
        and employee.is_active
        and verify_password(password, employee.password_hash or "")
    )
    if not ok or employee is None:
        log.warning("panel.login_failed", login=login[:64])
        return render(
            request,
            "login.html",
            {"csrf_token": "", "error": "Неверный логин или пароль."},
        )

    settings = get_settings()
    csrf_token = new_csrf_token()
    response: RedirectResponse = redirect("/")
    response.set_cookie(
        settings.web_session_cookie,
        dump_session(employee.id, csrf_token),
        max_age=settings.web_session_ttl_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.app_env == "production",
    )
    await record(session, employee, "login", "employee", employee.id)
    log.info("panel.login", employee_id=employee.id)
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Выйти: кука удаляется, серверного состояния нет."""
    response: RedirectResponse = redirect("/login")
    response.delete_cookie(get_settings().web_session_cookie)
    return response
