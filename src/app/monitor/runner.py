"""Оркестрация одной проверки: сходить на сайт, записать, сравнить, событие.

Порядок операций здесь не случаен и важнее, чем кажется:

1. запись проверки в журнал происходит ВСЕГДА, даже при ошибке — ТЗ §6;
2. сравнение с предыдущим состоянием делается ПОСЛЕ записи, чтобы сбой
   в сравнении не потерял факт самой проверки;
3. состояние обновляется в той же транзакции, что и события, — иначе
   уведомление может уйти по событию, которого «не было» в состоянии,
   и следующая проверка выдаст его повторно.
"""

import random
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccountLoginLog,
    MonitorTarget,
    SlotCheck,
    SlotEvent,
    SlotState,
    VfsAccount,
)
from app.domain.timeutils import utcnow
from app.enums import AccountStatus, CheckStatus, CheckTrigger, ObservedStatus, SlotEventType
from app.logging import get_logger
from app.monitor.browser import BrowserResult, TargetSpec
from app.monitor.schedule import IntervalPolicy, next_run
from app.services.slot_diff import DiffResult, Observation, PreviousState, compare

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CheckReport:
    """Что произошло за одну проверку — для журнала и уведомлений."""

    check: SlotCheck
    diff: DiffResult
    events: tuple[SlotEvent, ...]
    next_check_at: datetime

    @property
    def should_notify(self) -> bool:
        return bool(self.events)


def policy_for(target: MonitorTarget) -> IntervalPolicy:
    """Собрать политику интервалов из настроек города и цели."""
    city = target.city
    return IntervalPolicy(
        base_minutes=target.check_interval_minutes or city.check_interval_minutes,
        night_minutes=city.night_interval_minutes,
        boost_minutes=city.boost_interval_minutes,
        boost_window_minutes=city.boost_window_minutes,
        night_start=city.night_start,
        night_end=city.night_end,
    )


def spec_for(target: MonitorTarget) -> TargetSpec:
    """Что искать на сайте для этой цели."""
    return TargetSpec(
        centre=target.city.name,
        category=target.category.name,
        subcategory=target.category.subcategory_name,
        applicants=target.applicants,
    )


def _to_previous(state: SlotState | None) -> PreviousState | None:
    """Перевести строку состояния в снимок для сравнения."""
    if state is None:
        return None
    return PreviousState(
        status=state.status,
        nearest_date=state.nearest_date,
        available_dates=tuple(_as_dates(state.available_dates)),
        available_times=tuple(state.available_times or []),
        slots_count=state.slots_count,
        last_notified_at=state.last_notified_at,
        consecutive_errors=state.consecutive_errors,
    )


def _as_dates(values: list[str] | None) -> list[date]:
    """Разобрать даты из JSONB, молча пропуская мусор."""
    parsed: list[date] = []
    for value in values or []:
        try:
            parsed.append(date.fromisoformat(str(value)))
        except ValueError:
            continue
    return parsed


async def get_state(session: AsyncSession, target_id: int) -> SlotState | None:
    """Текущее состояние цели с блокировкой строки.

    Блокировка исключает гонку, если два воркера каким-то образом взялись
    за одну цель: сравнение и обновление состояния должны быть атомарны,
    иначе одно и то же событие создастся дважды.
    """
    return (
        await session.execute(
            sa.select(SlotState).where(SlotState.target_id == target_id).with_for_update()
        )
    ).scalar_one_or_none()


# Колонка duration_ms — INTEGER, то есть int32. Проверка длиной в четверть
# месяца невозможна, но перевод часов, спящий ноутбук или зависший воркер
# дают именно такую разницу, и тогда INSERT падает, теряя всю проверку.
# Потерять точность в заведомо аномальном значении дешевле, чем потерять факт.
_DURATION_MAX_MS = 2_147_483_647


def _duration_ms(started_at: datetime, finished_at: datetime) -> int:
    """Длительность проверки в миллисекундах, обрезанная до диапазона int32."""
    raw = int((finished_at - started_at).total_seconds() * 1000)
    return min(max(0, raw), _DURATION_MAX_MS)


async def record_check(
    session: AsyncSession,
    target: MonitorTarget,
    account: VfsAccount | None,
    result: BrowserResult,
    *,
    started_at: datetime,
    finished_at: datetime,
    trigger: CheckTrigger,
    status: CheckStatus,
) -> SlotCheck:
    """Записать проверку в журнал (ТЗ §6)."""
    observation = result.observation
    check = SlotCheck(
        target_id=target.id,
        account_id=account.id if account else None,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=_duration_ms(started_at, finished_at),
        trigger=trigger,
        status=status,
        nearest_date=observation.nearest_date,
        available_dates=[d.isoformat() for d in observation.available_dates],
        available_times=list(observation.available_times),
        slots_count=observation.slots_count,
        site_message=observation.site_message,
        error_text=observation.error_text,
        screenshot_path=result.artifacts.screenshot_path,
        html_path=result.artifacts.html_path,
    )
    session.add(check)
    await session.flush()
    return check


async def apply_result(
    session: AsyncSession,
    target: MonitorTarget,
    account: VfsAccount | None,
    result: BrowserResult,
    *,
    started_at: datetime,
    finished_at: datetime | None = None,
    trigger: CheckTrigger = CheckTrigger.SCHEDULE,
    repeat_notice_minutes: int = 30,
    error_notice_after: int = 3,
    rng: random.Random | None = None,
) -> CheckReport:
    """Полный разбор результата проверки.

    Возвращает отчёт: записанную проверку, итог сравнения, созданные события
    и момент следующего запуска. Уведомления отсюда НЕ отправляются — это
    делает отдельный слой, чтобы падение Telegram не откатывало транзакцию
    с уже выполненной проверкой.
    """
    ended = finished_at or utcnow()
    state = await get_state(session, target.id)
    previous = _to_previous(state)

    diff = compare(
        result.observation,
        previous,
        now=ended,
        repeat_notice_minutes=repeat_notice_minutes,
        error_notice_after=error_notice_after,
    )

    check = await record_check(
        session,
        target,
        account,
        result,
        started_at=started_at,
        finished_at=ended,
        trigger=trigger,
        status=diff.status,
    )

    events = await _create_events(session, target, check, diff, account)
    state = await _update_state(session, target, check, diff, state, ended)

    if account is not None:
        await _update_account(session, account, result, diff, ended)

    planned = next_run(
        now=ended,
        timezone=target.city.timezone,
        policy=policy_for(target),
        last_status=diff.status,
        consecutive_errors=state.consecutive_errors,
        last_slot_found_at=state.last_slot_found_at,
        rng=rng,
    )
    state.next_check_at = planned.at
    await session.flush()

    log.info(
        "monitor.checked",
        target_id=target.id,
        city=target.city.code,
        status=diff.status.value,
        nearest_date=check.nearest_date.isoformat() if check.nearest_date else None,
        events=[event.event_type.value for event in events],
        duration_ms=check.duration_ms,
        next_check_at=planned.at.isoformat(),
        next_reason=planned.reason,
    )
    return CheckReport(check=check, diff=diff, events=tuple(events), next_check_at=planned.at)


async def _create_events(
    session: AsyncSession,
    target: MonitorTarget,
    check: SlotCheck,
    diff: DiffResult,
    account: VfsAccount | None = None,
) -> list[SlotEvent]:
    """Создать строки событий по итогу сравнения."""
    details = dict(diff.details)
    if account is not None and diff.status.stops_monitoring:
        # Метка нужна уведомлению: администратору отдаётся готовая команда,
        # а она зависит от того, какой учётной записи требуется вход.
        details["account_label"] = account.label

    events: list[SlotEvent] = []
    for event_type in diff.events:
        event = SlotEvent(
            target_id=target.id,
            check_id=check.id,
            event_type=event_type,
            previous_date=diff.previous_date,
            new_date=diff.new_date,
            details=dict(details),
        )
        session.add(event)
        events.append(event)
    if events:
        await session.flush()
    return events


async def _update_state(
    session: AsyncSession,
    target: MonitorTarget,
    check: SlotCheck,
    diff: DiffResult,
    state: SlotState | None,
    moment: datetime,
) -> SlotState:
    """Обновить текущее состояние цели.

    `since` двигается только при смене картины: по нему считается, сколько
    слот остаётся доступным (ТЗ §19), и обновлять его на каждой проверке
    означало бы всегда получать «доступен пять минут».
    """
    picture_changed = state is None or state.status != diff.status or (
        state.nearest_date != check.nearest_date
    )

    if state is None:
        # Счётчик и списки задаются явно: server_default срабатывает только
        # при INSERT, а код ниже увеличивает счётчик до записи в БД.
        state = SlotState(
            target_id=target.id,
            status=diff.status,
            since=moment,
            last_check_at=moment,
            consecutive_errors=0,
            available_dates=[],
            available_times=[],
        )
        session.add(state)

    state.status = diff.status
    state.last_check_at = moment
    state.last_check_id = check.id

    if diff.status.is_error:
        # Картина слотов при ошибке не обновляется: мы просто не знаем,
        # что там сейчас. Затирать её нулями означало бы выдать ложное
        # «слот исчез» на следующей успешной проверке.
        state.consecutive_errors += 1
    else:
        state.consecutive_errors = 0
        state.nearest_date = check.nearest_date
        state.available_dates = list(check.available_dates)
        state.available_times = list(check.available_times)
        state.slots_count = check.slots_count
        if diff.status.is_slot_present:
            state.last_slot_found_at = moment

    if picture_changed:
        state.since = moment
    if diff.should_notify:
        state.last_notified_at = moment

    await session.flush()
    return state


async def _update_account(
    session: AsyncSession,
    account: VfsAccount,
    result: BrowserResult,
    diff: DiffResult,
    moment: datetime,
) -> None:
    """Обновить состояние учётной записи и журнал входов (ТЗ §13, §20)."""
    if result.logged_in:
        session.add(
            AccountLoginLog(
                account_id=account.id,
                attempted_at=moment,
                succeeded=True,
                reused_session=result.reused_session,
            )
        )
        if not result.reused_session:
            account.last_login_at = moment
        if result.session_state is not None:
            account.session_state = result.session_state
            account.session_saved_at = moment

    if diff.status is CheckStatus.AUTH_REQUIRED:
        account.status = AccountStatus.AUTH_REQUIRED
        account.status_note = "сессия истекла, требуется повторная авторизация"
        session.add(
            AccountLoginLog(
                account_id=account.id,
                attempted_at=moment,
                succeeded=False,
                error="auth required",
            )
        )
    elif diff.status is CheckStatus.CAPTCHA_REQUIRED:
        # CAPTCHA не обходим — останавливаем проверки и зовём администратора.
        account.status = AccountStatus.CAPTCHA_REQUIRED
        account.status_note = "сайт запросил CAPTCHA, нужно пройти вручную"
    elif diff.status is CheckStatus.ACCESS_BLOCKED:
        account.status = AccountStatus.BLOCKED
        account.status_note = "сайт ограничил доступ"
    elif not diff.status.is_error:
        account.status = AccountStatus.OK
        account.status_note = None
        account.last_success_at = moment
        account.consecutive_errors = 0
        return

    account.consecutive_errors += 1
    await session.flush()


def error_observation(status: ObservedStatus, message: str) -> Observation:
    """Наблюдение-заглушка для случаев, когда до браузера дело не дошло."""
    return Observation(status=status, error_text=message)


def summarise(report: CheckReport) -> dict[str, Any]:
    """Компактная сводка проверки для логов и панели."""
    return {
        "check_id": report.check.id,
        "status": report.check.status.value,
        "nearest_date": (
            report.check.nearest_date.isoformat() if report.check.nearest_date else None
        ),
        "events": [event.event_type.value for event in report.events],
        "next_check_at": report.next_check_at.isoformat(),
    }


__all__ = [
    "CheckReport",
    "SlotEventType",
    "apply_result",
    "error_observation",
    "get_state",
    "policy_for",
    "record_check",
    "spec_for",
    "summarise",
]
