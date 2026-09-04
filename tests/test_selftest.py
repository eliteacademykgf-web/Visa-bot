"""Отбор получателей пробного уведомления.

Смысл проверки связи — узнать заранее, дойдёт ли сообщение. Поэтому список
получателей обязан совпадать с боевым: проверка, которая уходит не тем
людям, создаёт ложную уверенность и хуже отсутствия проверки.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import EmployeeRole
from app.monitor.selftest import _audience, _first_active_target
from tests.conftest import make_category, make_city, make_employee, make_target


@pytest.mark.asyncio
class TestAudience:
    async def test_admin_is_included(self, session: AsyncSession) -> None:
        await make_employee(session, "Админ", 1, EmployeeRole.ADMIN)
        assert [p.telegram_id for p in await _audience(session)] == [1]

    async def test_all_notified_roles_are_included(self, session: AsyncSession) -> None:
        await make_employee(session, "Админ", 1, EmployeeRole.ADMIN)
        await make_employee(session, "Руководитель", 2, EmployeeRole.SUPERVISOR)
        await make_employee(session, "Специалист", 3, EmployeeRole.SPECIALIST)
        assert sorted(p.telegram_id or 0 for p in await _audience(session)) == [1, 2, 3]

    async def test_inactive_employee_is_skipped(self, session: AsyncSession) -> None:
        await make_employee(session, "Уволен", 1, EmployeeRole.ADMIN, is_active=False)
        assert await _audience(session) == []

    async def test_employee_without_telegram_is_skipped(self, session: AsyncSession) -> None:
        """Панельный пользователь без Telegram уведомление получить не может."""
        await make_employee(session, "Только панель", None, EmployeeRole.ADMIN)  # type: ignore[arg-type]
        assert await _audience(session) == []

    async def test_nobody_configured(self, session: AsyncSession) -> None:
        assert await _audience(session) == []


@pytest.mark.asyncio
class TestTargetChoice:
    async def test_picks_an_active_target(self, session: AsyncSession) -> None:
        city = await make_city(session, "almaty")
        category = await make_category(session)
        target = await make_target(session, city, category)
        found = await _first_active_target(session)
        assert found is not None
        assert found.id == target.id

    async def test_disabled_target_is_not_used(self, session: AsyncSession) -> None:
        city = await make_city(session, "almaty")
        category = await make_category(session)
        await make_target(session, city, category, is_active=False)
        assert await _first_active_target(session) is None

    async def test_target_in_a_disabled_city_is_not_used(self, session: AsyncSession) -> None:
        """Город выключен — проверок по нему нет, и проверять связь незачем."""
        city = await make_city(session, "astana", is_active=False)
        category = await make_category(session)
        await make_target(session, city, category)
        assert await _first_active_target(session) is None

    async def test_city_and_category_are_loaded(self, session: AsyncSession) -> None:
        """Текст уведомления читает город и категорию — связи нужны сразу."""
        city = await make_city(session, "almaty")
        category = await make_category(session)
        await make_target(session, city, category)
        found = await _first_active_target(session)
        assert found is not None
        assert found.city.name
        assert found.category.name

    async def test_nothing_configured(self, session: AsyncSession) -> None:
        assert await _first_active_target(session) is None
