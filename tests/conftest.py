"""Общие фикстуры тестов.

БД берётся из TEST_DATABASE_URL, если она задана, иначе поднимается
контейнер через testcontainers. Первый путь нужен там, где Docker
недоступен (в том числе на машине разработчика с локальным PostgreSQL),
второй — в CI.
"""

import os
from collections.abc import AsyncIterator, Iterator
from datetime import datetime, time

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import Base, Category, City, Employee, MonitorTarget, VfsAccount
from app.enums import EmployeeRole
from app.services.notifications import DeliveryResult
from app.services.settings_service import RuntimeSettings


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL тестовой БД."""
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container.get_connection_url()


@pytest_asyncio.fixture
async def session(database_url: str) -> AsyncIterator[AsyncSession]:
    """Чистая схема на каждый тест: связей между тестами быть не должно."""
    engine = create_async_engine(database_url, poolclass=sa.pool.NullPool)
    async with engine.begin() as conn:
        # CASCADE нужен, потому что схема между запусками может содержать
        # таблицы, которых уже нет в метаданных. asyncpg не принимает две
        # инструкции в одном запросе, поэтому они идут по отдельности.
        await conn.execute(sa.text("DROP SCHEMA public CASCADE"))
        await conn.execute(sa.text("CREATE SCHEMA public"))
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with maker() as db:
        yield db
        await db.rollback()
    await engine.dispose()


class RecordingNotifier:
    """Notifier, который ничего не отправляет, но всё запоминает."""

    def __init__(self, *, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.photos: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        with_task_buttons: int | None = None,
        photo_file_id: str | None = None,
    ) -> DeliveryResult:
        if chat_id in self.fail_for:
            return DeliveryResult(delivered=False, error="bot was blocked by the user")
        self.sent.append((chat_id, text, with_task_buttons))
        if photo_file_id:
            self.photos.append((chat_id, photo_file_id))
        return DeliveryResult(delivered=True, message_id=len(self.sent))

    def chats(self) -> list[int]:
        return [chat_id for chat_id, _, _ in self.sent]

    def texts(self) -> list[str]:
        return [text for _, text, _ in self.sent]


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def settings() -> RuntimeSettings:
    """Пороги по умолчанию, как в сиде миграции (ТЗ §11: 2/5/10 минут)."""
    return RuntimeSettings(
        escalation_level_1_minutes=2,
        escalation_level_2_minutes=5,
        escalation_level_3_minutes=10,
        repeat_notice_minutes=30,
        error_notice_after=3,
        group_chat_id=-100500,
        crm_webhook_url=None,
        crm_webhook_secret="",
        webhook_max_attempts=6,
        webhook_backoff_base_seconds=15,
    )


# --------------------------------------------------------------------------
# Хелперы построения данных
# --------------------------------------------------------------------------


async def make_city(
    session: AsyncSession,
    code: str = "almaty",
    tz: str = "Asia/Almaty",
    *,
    check_interval_minutes: int = 10,
    night_interval_minutes: int = 20,
    boost_interval_minutes: int = 5,
    boost_window_minutes: int = 60,
    night_start: time = time(22, 0),
    night_end: time = time(7, 0),
    is_active: bool = True,
) -> City:
    city = City(
        code=code,
        name=code.title(),
        timezone=tz,
        is_active=is_active,
        check_interval_minutes=check_interval_minutes,
        night_interval_minutes=night_interval_minutes,
        boost_interval_minutes=boost_interval_minutes,
        boost_window_minutes=boost_window_minutes,
        night_start=night_start,
        night_end=night_end,
    )
    session.add(city)
    await session.flush()
    return city


async def make_category(
    session: AsyncSession,
    code: str = "D Visa Study",
    subcategory: str = "Enrollment at Universities",
    *,
    is_active: bool = True,
) -> Category:
    category = Category(
        code=code,
        name=code,
        subcategory_code=subcategory,
        subcategory_name=subcategory,
        is_active=is_active,
    )
    session.add(category)
    await session.flush()
    return category


async def make_target(
    session: AsyncSession,
    city: City,
    category: Category,
    *,
    applicants: int = 1,
    is_active: bool = True,
    check_interval_minutes: int | None = None,
) -> MonitorTarget:
    target = MonitorTarget(
        city_id=city.id,
        category_id=category.id,
        applicants=applicants,
        is_active=is_active,
        check_interval_minutes=check_interval_minutes,
    )
    session.add(target)
    await session.flush()
    await session.refresh(target, ["city", "category"])
    return target


async def make_employee(
    session: AsyncSession,
    name: str,
    telegram_id: int,
    role: EmployeeRole = EmployeeRole.SPECIALIST,
    *,
    is_active: bool = True,
) -> Employee:
    employee = Employee(
        full_name=name, telegram_id=telegram_id, role=role, is_active=is_active
    )
    session.add(employee)
    await session.flush()
    return employee


async def make_account(
    session: AsyncSession,
    label: str = "monitor-1",
    *,
    username: str = "monitor@example.com",
    password_encrypted: str = "encrypted-placeholder",
) -> VfsAccount:
    account = VfsAccount(
        label=label, username=username, password_encrypted=password_encrypted
    )
    session.add(account)
    await session.flush()
    return account


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Момент в UTC — короткая запись для тестов."""
    from app.domain.timeutils import UTC

    return datetime(year, month, day, hour, minute, tzinfo=UTC)
