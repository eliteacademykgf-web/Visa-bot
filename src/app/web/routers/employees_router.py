"""Сотрудники: роли и доступ в панель."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Form, Request, Response

from app.db.models import Employee
from app.enums import EmployeeRole
from app.services.audit_service import record
from app.web.auth import hash_password
from app.web.deps import AdminUser, CsrfProtected, CurrentUser, DbSession, redirect
from app.web.templating import render

router = APIRouter(prefix="/employees")


@router.get("")
async def list_employees(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Список сотрудников и их отсутствий."""
    employees = (
        await session.execute(
            sa.select(Employee).order_by(Employee.is_active.desc(), Employee.full_name)
        )
    ).scalars().all()
    return render(
        request,
        "employees.html",
        {
            "user": user,
            "employees": employees,
            "roles": list(EmployeeRole),
        },
    )


@router.post("/create")
async def create_employee(
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
    full_name: Annotated[str, Form()],
    role: Annotated[str, Form()],
    telegram_id: Annotated[str, Form()] = "",
    login: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
) -> Response:
    """Добавить сотрудника.

    Логин и пароль задаются вместе или не задаются вовсе — это же условие
    закреплено CHECK-ограничением в БД.
    """
    employee = Employee(
        full_name=full_name.strip(),
        role=EmployeeRole(role),
        telegram_id=int(telegram_id) if telegram_id.strip() else None,
        login=login.strip() or None,
        password_hash=hash_password(password) if login.strip() and password else None,
    )
    if employee.login and not employee.password_hash:
        return redirect("/employees?error=password_required")

    session.add(employee)
    await session.flush()
    await record(
        session,
        user,
        "employee.create",
        "employee",
        employee.id,
        {"full_name": employee.full_name, "role": employee.role},
    )
    return redirect("/employees?saved=1")


@router.post("/{employee_id}")
async def update_employee(
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
    employee_id: int,
    full_name: Annotated[str, Form()],
    role: Annotated[str, Form()],
    telegram_id: Annotated[str, Form()] = "",
    login: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    is_active: Annotated[bool, Form()] = False,
) -> Response:
    """Изменить сотрудника. Пустой пароль означает «оставить прежний»."""
    employee = await session.get(Employee, employee_id)
    if employee is None:
        return redirect("/employees?error=not_found")

    employee.full_name = full_name.strip()
    employee.role = EmployeeRole(role)
    employee.telegram_id = int(telegram_id) if telegram_id.strip() else None
    employee.is_active = is_active

    new_login = login.strip() or None
    if new_login is None:
        employee.login = None
        employee.password_hash = None
    else:
        employee.login = new_login
        if password:
            employee.password_hash = hash_password(password)
        elif employee.password_hash is None:
            return redirect("/employees?error=password_required")

    await session.flush()
    await record(
        session,
        user,
        "employee.update",
        "employee",
        employee.id,
        {"is_active": employee.is_active, "role": employee.role},
    )
    return redirect("/employees?saved=1")
