"""Свипы планировщика.

Все свипы — опросы состояния, а не отложенные задания. После рестарта
процесса отложенные джобы либо теряются, либо выстреливают пачкой,
а опрос просто видит текущее состояние строк. Восстановление после
простоя не требует отдельного кода.
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, selectinload

from app.config import get_settings
from app.db.models import Alert, MonitorTarget, SlotEvent, SlotState, VfsAccount
from app.db.session import session_scope
from app.domain.timeutils import utcnow
from app.enums import AccountStatus, CheckTrigger
from app.logging import get_logger
from app.monitor.schedule import is_due
from app.services import alert_service
from app.services.notifications import Notifier
from app.services.settings_service import load_settings
from app.services.webhook_service import WebhookSender, sweep_webhooks

log = get_logger(__name__)


async def try_advisory_lock(session: AsyncSession, key: int) -> bool:
    """Взять транзакционный advisory-лок PostgreSQL.

    Лок держится до конца транзакции и снимается сам — в том числе если
    воркер умер. ТЗ §8 требует запрета параллельных проверок одной учётной
    записью; это он и есть.
    """
    acquired = (
        await session.execute(sa.select(sa.func.pg_try_advisory_xact_lock(key)))
    ).scalar_one()
    return bool(acquired)


async def due_targets(session: AsyncSession, now: datetime) -> list[MonitorTarget]:
    """Цели, которым пора пройти проверку.

    Порядок — по приоритету города (ТЗ §3): при нехватке времени первым
    проверяется то, что важнее.
    """
    # Город и категория читаются ниже в цикле. Без явной загрузки SQLAlchemy
    # пошла бы за ними ленивым запросом, а в асинхронной сессии это не просто
    # лишний round-trip, а MissingGreenlet — свип падает целиком.
    rows = (
        await session.execute(
            sa.select(MonitorTarget, SlotState)
            .outerjoin(SlotState, SlotState.target_id == MonitorTarget.id)
            .join(MonitorTarget.city)
            .options(contains_eager(MonitorTarget.city), selectinload(MonitorTarget.category))
            .where(MonitorTarget.is_active.is_(True))
            .order_by(sa.text("cities.priority"), MonitorTarget.id)
        )
    ).all()

    due: list[MonitorTarget] = []
    for target, state in rows:
        if not target.city.is_active or not target.category.is_active:
            continue
        if is_due(
            now=now,
            last_check_at=state.last_check_at if state else None,
            next_planned_at=state.next_check_at if state else None,
        ):
            due.append(target)
    return due


async def available_account(session: AsyncSession, now: datetime) -> VfsAccount | None:
    """Учётная запись, пригодная для проверки прямо сейчас.

    Заблокированные и ожидающие ручного вмешательства пропускаются:
    долбиться в сайт заблокированным аккаунтом — верный способ превратить
    временное ограничение в постоянное.
    """
    return (
        await session.execute(
            sa.select(VfsAccount)
            .where(
                VfsAccount.is_active.is_(True),
                VfsAccount.status == AccountStatus.OK,
                sa.or_(VfsAccount.paused_until.is_(None), VfsAccount.paused_until <= now),
            )
            .order_by(VfsAccount.last_success_at.asc().nulls_first())
            .limit(1)
        )
    ).scalars().first()


async def run_due_checks(notifier: Notifier, now: datetime | None = None) -> int:
    """Выполнить проверки, у которых наступил срок.

    Сами обращения к сайту выполняет app.monitor.worker: этот свип только
    отбирает цели. Разделение нужно, чтобы планировщик не держал транзакцию
    БД открытой на всё время работы браузера.
    """
    moment = now or utcnow()
    config = get_settings()
    if not config.monitoring_enabled:
        return 0

    async with session_scope() as session:
        if not await try_advisory_lock(session, config.lock_key_monitor):
            return 0
        targets = await due_targets(session, moment)
        if targets:
            log.info("sweep.targets_due", count=len(targets))
        return len(targets)


async def dispatch_new_events(notifier: Notifier, now: datetime | None = None) -> int:
    """Разослать события, по которым ещё не уходило уведомление.

    Отправка отделена от проверки намеренно: недоступность Telegram не должна
    откатывать транзакцию с уже выполненной проверкой и потерять факт находки.
    """
    moment = now or utcnow()
    config = get_settings()
    async with session_scope() as session:
        if not await try_advisory_lock(session, config.lock_key_alerts):
            return 0

        settings = await load_settings(session)
        # События без уведомления и без отметки о рассылке.
        pending = (
            await session.execute(
                sa.select(SlotEvent)
                .outerjoin(Alert, Alert.event_id == SlotEvent.id)
                .where(Alert.id.is_(None))
                .order_by(SlotEvent.created_at)
                .limit(50)
            )
        ).scalars().all()

        sent = 0
        for event in pending:
            await alert_service.dispatch_event(session, notifier, event, settings, now=moment)
            sent += 1
        if sent:
            log.info("sweep.events_dispatched", count=sent)
        return sent


async def run_escalations(notifier: Notifier, now: datetime | None = None) -> int:
    """Выдать просроченные эскалации по открытым уведомлениям (ТЗ §11)."""
    config = get_settings()
    async with session_scope() as session:
        if not await try_advisory_lock(session, config.lock_key_escalation):
            return 0
        settings = await load_settings(session)
        applied = await alert_service.sweep_escalations(session, notifier, settings, now)
        if applied:
            log.info("sweep.escalations", count=applied)
        return applied


async def deliver_webhooks(
    sender: WebhookSender,
    notifier: Notifier,
    now: datetime | None = None,
) -> int:
    """Отправить доставки в CRM, у которых наступил срок повтора."""
    config = get_settings()
    async with session_scope() as session:
        if not await try_advisory_lock(session, config.lock_key_webhook):
            return 0
        settings = await load_settings(session)
        sent = await sweep_webhooks(session, sender, notifier, settings, now)
        if sent:
            log.info("sweep.webhooks", count=sent)
        return sent


__all__ = [
    "CheckTrigger",
    "available_account",
    "deliver_webhooks",
    "dispatch_new_events",
    "due_targets",
    "run_due_checks",
    "run_escalations",
    "try_advisory_lock",
]
