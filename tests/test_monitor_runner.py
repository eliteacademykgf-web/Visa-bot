"""Оркестрация проверки на живой БД: журнал, состояние, события.

Проверяется сквозной цикл ТЗ §5: сходили -> записали -> сравнили ->
при изменении создали событие. Браузер подменяется готовым наблюдением,
сеть не участвует.
"""

from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SlotCheck, SlotEvent, SlotState
from app.enums import AccountStatus, CheckStatus, ObservedStatus, SlotEventType
from app.monitor.browser import BrowserResult, CheckArtifacts
from app.monitor.runner import apply_result as _apply_result
from app.services.slot_diff import Observation
from tests.conftest import make_account, make_category, make_city, make_target, utc

NOW = utc(2026, 7, 28, 7, 0)  # 12:00 в Алматы — день
JULY_31 = date(2026, 7, 31)
JULY_29 = date(2026, 7, 29)


async def apply_result(session, target, account, result, *, started_at, **kwargs):
    """apply_result с детерминированным окончанием проверки.

    Без этого finished_at берётся из utcnow(), тогда как started_at прибит
    к фиксированному NOW. Разрыв между ними растёт с каждым днём после
    написания теста: сначала искажает длительность, а через месяц переполняет
    int32 в duration_ms и роняет INSERT. Тестам нужна пара, не зависящая от
    сегодняшней даты; кому важен конкретный финиш — передаёт его сам.
    """
    kwargs.setdefault("finished_at", started_at + timedelta(seconds=12))
    return await _apply_result(session, target, account, result, started_at=started_at, **kwargs)


def browser_result(
    status: ObservedStatus,
    *,
    nearest: date | None = None,
    dates: tuple[date, ...] = (),
    screenshot: str | None = None,
    error: str | None = None,
    session_state: dict[str, object] | None = None,
) -> BrowserResult:
    return BrowserResult(
        observation=Observation(
            status=status,
            nearest_date=nearest,
            available_dates=dates,
            slots_count=len(dates) or None,
            error_text=error,
        ),
        artifacts=CheckArtifacts(screenshot_path=screenshot),
        session_state=session_state,
        logged_in=True,
        reused_session=True,
    )


async def setup_target(session: AsyncSession):
    city = await make_city(session)
    category = await make_category(session)
    target = await make_target(session, city, category)
    account = await make_account(session)
    return target, account


class TestRecording:
    async def test_check_is_always_recorded(self, session: AsyncSession) -> None:
        """ТЗ §6: проверка пишется в журнал и при успехе, и при ошибке."""
        target, account = await setup_target(session)

        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SYSTEM_ERROR, error="timeout"),
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=12),
        )

        check = (await session.execute(sa.select(SlotCheck))).scalar_one()
        assert check.status is CheckStatus.SYSTEM_ERROR
        assert check.error_text == "timeout"
        assert check.duration_ms == 12000

    async def test_slots_are_recorded_with_dates(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)

        await apply_result(
            session,
            target,
            account,
            browser_result(
                ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,),
                screenshot="/artifacts/almaty.png",
            ),
            started_at=NOW,
        )

        check = (await session.execute(sa.select(SlotCheck))).scalar_one()
        assert check.status is CheckStatus.SLOT_AVAILABLE
        assert check.nearest_date == JULY_31
        assert check.available_dates == [JULY_31.isoformat()]
        # ТЗ §16: скриншот обязателен при обнаружении слота.
        assert check.screenshot_path == "/artifacts/almaty.png"


class TestStateAndEvents:
    async def test_first_find_creates_event(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)

        report = await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)),
            started_at=NOW,
        )

        assert [e.event_type for e in report.events] == [SlotEventType.APPEARED]
        state = await session.get(SlotState, target.id)
        assert state is not None
        assert state.nearest_date == JULY_31
        assert state.last_slot_found_at is not None

    async def test_unchanged_picture_creates_no_event(self, session: AsyncSession) -> None:
        """Главное свойство: молчание, когда ничего не изменилось."""
        target, account = await setup_target(session)
        result = browser_result(
            ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)
        )

        await apply_result(session, target, account, result, started_at=NOW)
        second = await apply_result(
            session, target, account, result, started_at=NOW + timedelta(minutes=10)
        )

        assert second.events == ()
        events = (await session.execute(sa.select(SlotEvent))).scalars().all()
        assert len(events) == 1  # только первая находка

    async def test_earlier_date_creates_change_event(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)
        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)),
            started_at=NOW,
        )

        report = await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SLOTS_PRESENT, nearest=JULY_29, dates=(JULY_29,)),
            started_at=NOW + timedelta(minutes=10),
        )

        assert report.check.status is CheckStatus.SLOT_CHANGED
        event = report.events[0]
        assert event.event_type is SlotEventType.DATE_CHANGED
        assert event.previous_date == JULY_31
        assert event.new_date == JULY_29

    async def test_disappearance_is_detected(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)
        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)),
            started_at=NOW,
        )

        report = await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.NO_SLOTS),
            started_at=NOW + timedelta(minutes=10),
        )

        assert report.check.status is CheckStatus.SLOT_DISAPPEARED
        assert report.events[0].event_type is SlotEventType.DISAPPEARED

    async def test_error_does_not_wipe_known_picture(self, session: AsyncSession) -> None:
        """Ошибка означает «мы не знаем», а не «слотов нет».

        Затирание картины нулями выдало бы ложное «слот исчез» на следующей
        успешной проверке — и сотрудник поверил бы, что упустил слот.
        """
        target, account = await setup_target(session)
        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)),
            started_at=NOW,
        )

        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SYSTEM_ERROR, error="timeout"),
            started_at=NOW + timedelta(minutes=10),
        )

        state = await session.get(SlotState, target.id)
        assert state is not None
        assert state.status is CheckStatus.SYSTEM_ERROR
        # Картина сохранена.
        assert state.nearest_date == JULY_31
        assert state.consecutive_errors == 1

    async def test_since_moves_only_on_change(self, session: AsyncSession) -> None:
        """`since` — основа отчёта «сколько слот остаётся доступным» (ТЗ §19)."""
        target, account = await setup_target(session)
        result = browser_result(
            ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)
        )

        await apply_result(session, target, account, result, started_at=NOW)
        state = await session.get(SlotState, target.id)
        assert state is not None
        first_since = state.since

        await apply_result(
            session, target, account, result, started_at=NOW + timedelta(minutes=20)
        )
        await session.refresh(state)
        assert state.since == first_since


class TestScheduling:
    async def test_next_check_is_planned(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)

        report = await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.NO_SLOTS),
            started_at=NOW,
            finished_at=NOW,
        )

        # Базовый интервал 10 минут плюс джиттер 30–90 секунд.
        assert NOW + timedelta(minutes=10, seconds=30) <= report.next_check_at
        assert report.next_check_at <= NOW + timedelta(minutes=10, seconds=90)

    async def test_find_switches_to_boost(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)

        report = await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.SLOTS_PRESENT, nearest=JULY_31, dates=(JULY_31,)),
            started_at=NOW,
            finished_at=NOW,
        )

        # После находки — пятиминутный интервал: слоты выкладывают пачками.
        assert report.next_check_at <= NOW + timedelta(minutes=5, seconds=90)

    async def test_block_pauses_for_long(self, session: AsyncSession) -> None:
        """ТЗ §20: при блокировке останавливаемся надолго, не меняя IP."""
        target, account = await setup_target(session)

        report = await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.ACCESS_BLOCKED),
            started_at=NOW,
            finished_at=NOW,
        )

        assert report.next_check_at >= NOW + timedelta(minutes=30)


class TestAccountState:
    async def test_captcha_stops_the_account(self, session: AsyncSession) -> None:
        """CAPTCHA не обходим — учётная запись ждёт администратора."""
        target, account = await setup_target(session)

        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.CAPTCHA_REQUIRED),
            started_at=NOW,
        )

        await session.refresh(account)
        assert account.status is AccountStatus.CAPTCHA_REQUIRED
        assert account.is_available is False

    async def test_auth_required_marks_account(self, session: AsyncSession) -> None:
        target, account = await setup_target(session)

        await apply_result(
            session,
            target,
            account,
            browser_result(ObservedStatus.AUTH_REQUIRED),
            started_at=NOW,
        )

        await session.refresh(account)
        assert account.status is AccountStatus.AUTH_REQUIRED

    async def test_success_clears_errors_and_saves_session(
        self, session: AsyncSession
    ) -> None:
        target, account = await setup_target(session)
        account.consecutive_errors = 2
        await session.flush()

        await apply_result(
            session,
            target,
            account,
            browser_result(
                ObservedStatus.NO_SLOTS, session_state={"cookies": []}
            ),
            started_at=NOW,
        )

        await session.refresh(account)
        assert account.status is AccountStatus.OK
        assert account.consecutive_errors == 0
        # Сессия сохранена: частые входы — главный признак, по которому
        # VFS блокирует учётные записи.
        assert account.session_state == {"cookies": []}
