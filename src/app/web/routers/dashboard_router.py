"""Главный экран и ручной запуск проверки (ТЗ §12)."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Form, Request, Response
from sqlalchemy.orm import selectinload

from app.db.models import Alert, City, MonitorTarget, SlotEvent, VfsAccount
from app.domain.timeutils import utcnow
from app.services.analytics_service import dashboard_stats, monitoring_table
from app.services.audit_service import record
from app.web.deps import CsrfProtected, CurrentUser, DbSession, redirect
from app.web.templating import render

router = APIRouter()


@router.get("/")
async def dashboard(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Статус системы, таблица мониторинга и открытые уведомления."""
    stats = await dashboard_stats(session)
    table = await monitoring_table(session)

    accounts = (
        await session.execute(sa.select(VfsAccount).order_by(VfsAccount.label))
    ).scalars().all()

    open_alerts = (
        await session.execute(
            sa.select(Alert, City, SlotEvent)
            .join(SlotEvent, SlotEvent.id == Alert.event_id)
            .join(MonitorTarget, MonitorTarget.id == SlotEvent.target_id)
            .join(City, City.id == MonitorTarget.city_id)
            .where(Alert.closed_at.is_(None))
            .order_by(Alert.created_at)
        )
    ).all()

    # Ближайшая запланированная проверка — ТЗ §12 требует показывать
    # «дата следующей проверки» на главном экране.
    next_check = min(
        (row.state.next_check_at for row in table if row.state and row.state.next_check_at),
        default=None,
    )

    return render(
        request,
        "dashboard.html",
        {
            "user": user,
            "stats": stats,
            "table": table,
            "accounts": accounts,
            "open_alerts": open_alerts,
            "next_check": next_check,
            "now": utcnow(),
        },
    )


@router.post("/checks/run")
async def run_check_now(
    session: DbSession,
    user: CurrentUser,
    _csrf: CsrfProtected,
    target_id: Annotated[int, Form()],
) -> Response:
    """Запустить проверку вне расписания (ТЗ §12).

    Кнопка не ходит на сайт сама: она сбрасывает плановое время, и ближайший
    проход воркера возьмёт цель первой. Так ручной запуск подчиняется тем же
    ограничениям частоты, что и обычный, — иначе нажатием кнопки можно было бы
    обойти собственную защиту от блокировки.
    """
    # Город нужен для журнала аудита ниже. Связь грузится сразу: ленивая
    # подгрузка в асинхронной сессии падает с MissingGreenlet.
    target = await session.get(
        MonitorTarget, target_id, options=[selectinload(MonitorTarget.city)]
    )
    if target is None:
        return redirect("/?error=target_not_found")

    from app.db.models import SlotState

    state = await session.get(SlotState, target_id)
    if state is not None:
        state.next_check_at = utcnow()
        await session.flush()

    await record(
        session, user, "check.run_manual", "monitor_target", target_id, {"city": target.city.code}
    )
    return redirect(f"/?queued={target_id}")
