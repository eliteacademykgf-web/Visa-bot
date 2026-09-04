"""Города и цели мониторинга: интервалы, ночное окно, включение (ТЗ §3, §12)."""

from datetime import time
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Form, Request, Response

from app.db.models import Category, City, MonitorTarget
from app.services.audit_service import record
from app.web.deps import AdminUser, CsrfProtected, CurrentUser, DbSession, redirect
from app.web.templating import render

router = APIRouter(prefix="/cities")

# Нижняя граница интервала. Защита не от злого умысла, а от опечатки:
# «1» вместо «10» в поле формы — это 1440 обращений в сутки на город
# и почти гарантированная блокировка учётной записи.
MIN_INTERVAL_MINUTES = 2

KNOWN_TIMEZONES = [
    "Asia/Almaty",
    "Asia/Atyrau",
    "Asia/Aqtau",
    "Asia/Aqtobe",
    "Asia/Oral",
    "Asia/Qostanay",
    "Asia/Bishkek",
]


@router.get("")
async def list_cities(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Города, их интервалы и цели мониторинга."""
    cities = (
        await session.execute(sa.select(City).order_by(City.priority, City.name))
    ).scalars().all()
    categories = (
        await session.execute(sa.select(Category).order_by(Category.subcategory_name))
    ).scalars().all()
    targets = (await session.execute(sa.select(MonitorTarget))).scalars().all()

    by_city: dict[int, list[MonitorTarget]] = {}
    for target in targets:
        by_city.setdefault(target.city_id, []).append(target)

    return render(
        request,
        "cities.html",
        {
            "user": user,
            "cities": cities,
            "categories": {c.id: c for c in categories},
            "all_categories": categories,
            "targets_by_city": by_city,
            "timezones": KNOWN_TIMEZONES,
        },
    )


@router.post("/{city_id}")
async def update_city(
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
    city_id: int,
    name: Annotated[str, Form()],
    timezone: Annotated[str, Form()],
    priority: Annotated[int, Form()],
    check_interval_minutes: Annotated[int, Form()],
    night_interval_minutes: Annotated[int, Form()],
    boost_interval_minutes: Annotated[int, Form()],
    boost_window_minutes: Annotated[int, Form()],
    night_start: Annotated[str, Form()],
    night_end: Annotated[str, Form()],
    is_active: Annotated[bool, Form()] = False,
) -> Response:
    """Сохранить настройки города."""
    city = await session.get(City, city_id)
    if city is None:
        return redirect("/cities?error=not_found")

    if min(check_interval_minutes, night_interval_minutes, boost_interval_minutes) < (
        MIN_INTERVAL_MINUTES
    ):
        return redirect("/cities?error=interval_too_small")

    before = {
        "check_interval_minutes": city.check_interval_minutes,
        "night_interval_minutes": city.night_interval_minutes,
        "is_active": city.is_active,
    }

    city.name = name.strip()
    city.timezone = timezone
    city.priority = priority
    city.check_interval_minutes = check_interval_minutes
    city.night_interval_minutes = night_interval_minutes
    city.boost_interval_minutes = boost_interval_minutes
    city.boost_window_minutes = boost_window_minutes
    city.night_start = time.fromisoformat(night_start)
    city.night_end = time.fromisoformat(night_end)
    city.is_active = is_active
    await session.flush()

    await record(
        session,
        user,
        "city.update",
        "city",
        city.id,
        {
            "before": before,
            "after": {
                "check_interval_minutes": city.check_interval_minutes,
                "night_interval_minutes": city.night_interval_minutes,
                "is_active": city.is_active,
            },
        },
    )
    return redirect("/cities?saved=1")


@router.post("/targets/{target_id}/toggle")
async def toggle_target(
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
    target_id: int,
) -> Response:
    """Включить или выключить цель мониторинга."""
    target = await session.get(MonitorTarget, target_id)
    if target is None:
        return redirect("/cities?error=not_found")

    target.is_active = not target.is_active
    await session.flush()
    await record(
        session,
        user,
        "target.toggle",
        "monitor_target",
        target_id,
        {"is_active": target.is_active},
    )
    return redirect("/cities?saved=1")


@router.post("/targets")
async def create_target(
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
    city_id: Annotated[int, Form()],
    category_id: Annotated[int, Form()],
    applicants: Annotated[int, Form()] = 1,
) -> Response:
    """Добавить цель мониторинга: город + категория + число заявителей.

    ТЗ §3 и §4 требуют добавлять города и категории без изменения кода —
    это она и есть.
    """
    exists = (
        await session.execute(
            sa.select(MonitorTarget.id).where(
                MonitorTarget.city_id == city_id,
                MonitorTarget.category_id == category_id,
                MonitorTarget.applicants == applicants,
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        return redirect("/cities?error=target_exists")

    target = MonitorTarget(
        city_id=city_id, category_id=category_id, applicants=applicants, is_active=True
    )
    session.add(target)
    await session.flush()
    await record(session, user, "target.create", "monitor_target", target.id, {})
    return redirect("/cities?saved=1")
