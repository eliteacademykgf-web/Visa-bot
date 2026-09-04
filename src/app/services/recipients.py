"""Получатели уведомлений.

Заменяет прежний резолвер дежурных по недельному расписанию: в ТЗ графика
дежурств нет. Есть роли (§22) и цепочка эскалации §11 — специалист, затем
руководитель, затем резервный сотрудник.

Список получателей редактируется в панели (ТЗ §12 «добавить или удалить
получателей»), поэтому здесь только выборки по ролям, без расписаний.
"""

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee
from app.enums import EmployeeRole


def _reachable() -> sa.ColumnElement[bool]:
    """Сотрудник активен и до него можно достучаться в Telegram."""
    return sa.and_(Employee.is_active.is_(True), Employee.telegram_id.is_not(None))


async def by_role(session: AsyncSession, *roles: EmployeeRole) -> list[Employee]:
    """Активные сотрудники указанных ролей, в стабильном порядке."""
    result = await session.execute(
        sa.select(Employee)
        .where(_reachable(), Employee.role.in_(roles))
        .order_by(Employee.id)
    )
    return list(result.scalars().all())


async def specialists(session: AsyncSession) -> list[Employee]:
    """Визовые специалисты — первые адресаты уведомления о слоте (ТЗ §22)."""
    return await by_role(session, EmployeeRole.SPECIALIST)


async def supervisors(session: AsyncSession) -> list[Employee]:
    """Руководители и администраторы — второй уровень эскалации."""
    return await by_role(session, EmployeeRole.SUPERVISOR, EmployeeRole.ADMIN)


async def admins(session: AsyncSession) -> list[Employee]:
    """Администраторы — адресаты технических уведомлений."""
    return await by_role(session, EmployeeRole.ADMIN)


async def first_available(
    session: AsyncSession, *, exclude: frozenset[int] = frozenset()
) -> Employee | None:
    """Кому адресовать реакцию, если конкретный сотрудник не назначен.

    Порядок: специалист -> руководитель -> администратор. Уведомление не
    должно остаться без адресата: слот живёт минуты, и некому реагировать —
    это тот же пропуск, что и не отправленное сообщение.
    """
    for role in (EmployeeRole.SPECIALIST, EmployeeRole.SUPERVISOR, EmployeeRole.ADMIN):
        for candidate in await by_role(session, role):
            if candidate.id not in exclude:
                return candidate
    return None
