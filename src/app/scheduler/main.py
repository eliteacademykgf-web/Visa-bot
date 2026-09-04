"""Точка входа процесса планировщика."""

import asyncio
import contextlib
import signal

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import configure_logging, get_logger
from app.scheduler import jobs
from app.scheduler.recovery import run_startup_recovery
from app.services.notifications import Notifier
from app.services.webhook_service import HttpxWebhookSender

log = get_logger(__name__)


def build_scheduler(notifier: Notifier) -> AsyncIOScheduler:
    """Собрать планировщик с джобами в памяти процесса.

    Джобов четыре, все — периодические опросы. coalesce=True и
    max_instances=1 нужны, чтобы после паузы процесса не запустилось
    несколько отложившихся проходов одного свипа подряд.

    Джобстор namеренно в памяти, а не в PostgreSQL. Персистентный джобстор
    APScheduler сериализует джобы через pickle, а в args лежат живые объекты
    (Notifier с HTTP-сессией бота, отправитель вебхуков) — SSLContext внутри
    них не пиклится, и планировщик падал прямо на scheduler.start().
    Хранить эти джобы и незачем: они статичны и переобъявляются при каждом
    запуске, от параллельных процессов защищают advisory-локи PostgreSQL
    (app.scheduler.jobs), а пропущенное за простой догоняет первый же проход
    опроса (app.scheduler.recovery).
    """
    config = get_settings()
    scheduler = AsyncIOScheduler(
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
        timezone="UTC",
    )
    tick = config.scheduler_tick_seconds

    scheduler.add_job(
        jobs.run_due_checks,
        "interval",
        seconds=tick,
        args=[notifier],
        id="run_due_checks",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs.dispatch_new_events,
        "interval",
        seconds=tick,
        args=[notifier],
        id="dispatch_new_events",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs.run_escalations,
        "interval",
        seconds=tick,
        args=[notifier],
        id="run_escalations",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs.deliver_webhooks,
        "interval",
        seconds=tick,
        args=[HttpxWebhookSender(config.webhook_timeout_seconds), notifier],
        id="deliver_webhooks",
        replace_existing=True,
    )
    return scheduler


async def run(notifier: Notifier | None = None) -> None:
    """Запустить планировщик и работать до сигнала остановки."""
    configure_logging()

    if notifier is None:
        from app.bot.main import build_bot
        from app.bot.notifier import TelegramNotifier

        notifier = TelegramNotifier(build_bot())

    scheduler = build_scheduler(notifier)
    await run_startup_recovery(notifier)
    scheduler.start()
    log.info("scheduler.started", tick_seconds=get_settings().scheduler_tick_seconds)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("scheduler.stopping")
        scheduler.shutdown(wait=False)
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(run())
