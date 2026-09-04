"""Процесс мониторинга: берёт цели, ходит на сайт, записывает результат.

Отдельный процесс, а не джоба планировщика: браузер живёт секунды и держит
память, и мешать его с лёгкими свипами нельзя. Планировщик отвечает
за «когда», воркер — за «как».

Порядок работы одного цикла:

1. взять цели, которым наступил срок (короткая транзакция);
2. взять учётную запись, пригодную к использованию;
3. сходить на сайт браузером (БЕЗ открытой транзакции — обращение к чужому
   сервису не должно держать соединение с БД);
4. записать результат, сравнить, создать события (снова короткая транзакция).
"""

import asyncio
import contextlib
import random
import signal
from datetime import datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.models import MonitorTarget, SlotState, VfsAccount
from app.db.session import dispose_engine, session_scope
from app.domain.timeutils import utcnow
from app.enums import CheckTrigger, ObservedStatus
from app.logging import configure_logging, get_logger
from app.monitor import discovery
from app.monitor.browser import (
    STRATEGY_DIRECT,
    ArtifactStore,
    BrowserResult,
    CheckArtifacts,
    profile_dir,
    run_check,
)
from app.monitor.runner import apply_result, spec_for
from app.monitor.schedule import is_due
from app.monitor.selectors import SelectorConfig, load_selectors
from app.scheduler.jobs import available_account, try_advisory_lock
from app.services.slot_diff import Observation

log = get_logger(__name__)


async def claim_due_targets(session: AsyncSession, now: datetime) -> list[int]:
    """Идентификаторы целей, которым пора пройти проверку.

    Возвращаются именно id, а не объекты: между отбором и проверкой проходят
    секунды работы браузера, и держать всё это время открытую сессию БД
    незачем.
    """
    # Город и категория читаются ниже в цикле; в асинхронной сессии ленивая
    # подгрузка связи падает с MissingGreenlet, поэтому грузим их сразу.
    rows = (
        await session.execute(
            sa.select(MonitorTarget, SlotState)
            .outerjoin(SlotState, SlotState.target_id == MonitorTarget.id)
            .options(selectinload(MonitorTarget.city), selectinload(MonitorTarget.category))
            .where(MonitorTarget.is_active.is_(True))
            .order_by(MonitorTarget.id)
        )
    ).all()

    due: list[int] = []
    for target, state in rows:
        if not target.city.is_active or not target.category.is_active:
            continue
        if is_due(
            now=now,
            last_check_at=state.last_check_at if state else None,
            next_planned_at=state.next_check_at if state else None,
        ):
            due.append(target.id)
    return due


async def load_target(session: AsyncSession, target_id: int) -> MonitorTarget | None:
    """Цель вместе с городом и категорией.

    Обычный session.get() связи не подтягивает, а и spec_for, и policy_for
    читают target.city и target.category. В асинхронной сессии такое чтение
    не сходит за ленивую подгрузку — оно падает с MissingGreenlet.
    """
    return await session.get(
        MonitorTarget,
        target_id,
        options=[selectinload(MonitorTarget.city), selectinload(MonitorTarget.category)],
    )


async def check_target(
    target_id: int,
    config: SelectorConfig,
    store: ArtifactStore,
    *,
    trigger: CheckTrigger = CheckTrigger.SCHEDULE,
    rng: random.Random | None = None,
) -> None:
    """Выполнить одну проверку по цели и записать результат."""
    settings = get_settings()

    async with session_scope() as session:
        target = await load_target(session, target_id)
        if target is None or not target.is_active:
            return
        account = await available_account(session, utcnow())
        if account is None:
            log.warning("monitor.no_account", target_id=target_id)
            await _record_blocked(session, target, trigger)
            return

        spec = spec_for(target)
        city_code = target.city.code
        account_id = account.id
        session_state = account.session_state
        profile = profile_dir(Path(settings.profiles_dir), account.label)
        memory = await discovery.load(session, target_id)

    # Короткий путь берётся только пока ему можно доверять: запомненный адрес
    # содержит параметры момента находки и со временем протухает (подробнее —
    # в app.monitor.discovery). Конфиг может задать адрес и вручную.
    endpoint = ""
    if discovery.should_use_api(memory):
        endpoint = memory.url
    elif config.dates_api_pattern and not memory.is_known:
        endpoint = config.dates_api_pattern

    # Пароль сюда не читается и в браузер не передаётся: автоматического входа
    # больше нет (Turnstile его всё равно не пропустит, а серия неудачных
    # попыток блокирует учётную запись). Вход — разовый, руками, через
    # `python -m app.cli capture-session`.
    started = utcnow()
    result = await _run_browser(
        config, spec, store, city_code, profile, session_state, settings, endpoint
    )

    async with session_scope() as session:
        target = await load_target(session, target_id)
        account = await session.get(VfsAccount, account_id)
        if target is None:
            return

        await _remember_endpoint(session, target_id, memory, result)
        report = await apply_result(
            session,
            target,
            account,
            result,
            started_at=started,
            trigger=trigger,
            repeat_notice_minutes=await _repeat_minutes(session),
            error_notice_after=await _error_threshold(session),
            rng=rng,
        )
        log.info(
            "monitor.cycle_done",
            target_id=target_id,
            status=report.check.status.value,
            strategy=result.strategy,
        )


async def _remember_endpoint(
    session: AsyncSession,
    target_id: int,
    memory: discovery.EndpointMemory,
    result: BrowserResult,
) -> None:
    """Обновить память о прямом запросе дат по итогу проверки."""
    if result.discovered_dates_url:
        # Длинный путь прошёл целиком и заново увидел живой адрес — отсчёт
        # доверия к короткому пути начинается заново.
        fresh = discovery.after_validation(result.discovered_dates_url)
        await discovery.save(session, target_id, fresh)
        return

    if result.strategy == STRATEGY_DIRECT and memory.is_known:
        await discovery.save(session, target_id, discovery.after_api_use(memory))


async def _run_browser(
    config: SelectorConfig,
    spec: object,
    store: ArtifactStore,
    city_code: str,
    user_data_dir: Path,
    session_state: dict[str, object] | None,
    settings: object,
    dates_endpoint: str = "",
) -> BrowserResult:
    """Запустить браузер и выполнить сценарий."""
    from playwright.async_api import async_playwright

    from app.monitor.browser import close_context, open_context

    async with async_playwright() as playwright:
        context = await open_context(
            playwright,
            user_data_dir=user_data_dir,
            storage_state=session_state,
            headless=settings.playwright_headless,  # type: ignore[attr-defined]
            timeout_ms=settings.playwright_timeout_ms,  # type: ignore[attr-defined]
        )
        try:
            return await run_check(
                context,
                config,
                spec,  # type: ignore[arg-type]
                store=store,
                city_code=city_code,
                dates_endpoint=dates_endpoint,
            )
        finally:
            await close_context(context)


async def _repeat_minutes(session: AsyncSession) -> int:
    from app.services.settings_service import load_settings

    return (await load_settings(session)).repeat_notice_minutes


async def _error_threshold(session: AsyncSession) -> int:
    from app.services.settings_service import load_settings

    return (await load_settings(session)).error_notice_after


async def _record_blocked(
    session: AsyncSession, target: MonitorTarget, trigger: CheckTrigger
) -> None:
    """Записать проверку, не состоявшуюся из-за отсутствия учётной записи."""
    moment = utcnow()
    await apply_result(
        session,
        target,
        None,
        BrowserResult(
            observation=Observation(
                status=ObservedStatus.SYSTEM_ERROR,
                error_text="нет доступной учётной записи VFS",
            ),
            artifacts=CheckArtifacts(),
        ),
        started_at=moment,
        finished_at=moment,
        trigger=trigger,
    )


async def _record_error(
    session: AsyncSession,
    target: MonitorTarget,
    account: VfsAccount,
    trigger: CheckTrigger,
    message: str,
) -> None:
    moment = utcnow()
    await apply_result(
        session,
        target,
        account,
        BrowserResult(
            observation=Observation(
                status=ObservedStatus.SYSTEM_ERROR, error_text=message[:500]
            ),
            artifacts=CheckArtifacts(),
        ),
        started_at=moment,
        finished_at=moment,
        trigger=trigger,
    )


async def run_once(config: SelectorConfig, store: ArtifactStore) -> int:
    """Один проход: проверить все цели, которым наступил срок."""
    settings = get_settings()
    if not settings.monitoring_enabled:
        return 0

    now = utcnow()
    async with session_scope() as session:
        # Лок на весь проход: ТЗ §8 и тестовый сценарий §27 №11 требуют,
        # чтобы второй процесс не начинал проверку параллельно.
        if not await try_advisory_lock(session, settings.lock_key_monitor):
            log.info("monitor.another_worker_active")
            return 0
        target_ids = await claim_due_targets(session, now)

    for target_id in target_ids:
        await check_target(target_id, config, store)
    return len(target_ids)


async def run() -> None:
    """Цикл воркера мониторинга."""
    configure_logging()
    settings = get_settings()
    config = load_selectors(Path(settings.selectors_path))
    store = ArtifactStore(Path(settings.artifacts_dir))

    if not config.is_configured:
        log.error(
            "monitor.selectors_missing",
            path=settings.selectors_path,
            missing=config.missing_fields(),
            hint="заполните конфиг по протоколу из docs/research-vfs.md",
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    log.info("monitor.started", tick_seconds=settings.scheduler_tick_seconds)
    try:
        while not stop.is_set():
            try:
                await run_once(config, store)
            except Exception:
                log.exception("monitor.cycle_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.scheduler_tick_seconds)
    finally:
        log.info("monitor.stopping")
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
