"""Аналитика мониторинга (ТЗ §12, §19).

Правило по времени: группировки по времени суток и дням недели считаются
в ЛОКАЛЬНОМ времени города через AT TIME ZONE. Весь смысл отчёта — увидеть
реальные окна выкладки слотов и подстроить под них частоту проверок.
В UTC он бесполезен, а ошибка ровно в час выглядит правдоподобно и потому
не будет замечена.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Alert,
    AlertReaction,
    City,
    MonitorTarget,
    SlotCheck,
    SlotEvent,
    SlotState,
    WebhookDelivery,
)
from app.domain.timeutils import utcnow
from app.enums import CheckStatus, ReactionKind, SlotEventType, WebhookStatus

# Локальное время города — основа всех группировок по времени суток.
LOCAL_STARTED = sa.func.timezone(City.timezone, SlotCheck.started_at)
LOCAL_EVENT = sa.func.timezone(City.timezone, SlotEvent.created_at)


@dataclass(frozen=True, slots=True)
class DashboardStats:
    """Показатели главного экрана (ТЗ §12)."""

    checks_24h: int
    errors_24h: int
    finds_7d: int
    open_alerts: int
    targets_active: int
    accounts_blocked: int
    failed_webhooks: int
    avg_reaction_seconds: int | None
    median_reaction_seconds: int | None
    booked_7d: int
    gone_7d: int
    false_positive_7d: int


@dataclass(frozen=True, slots=True)
class TargetRow:
    """Строка таблицы мониторинга (ТЗ §12)."""

    city: City
    target: MonitorTarget
    state: SlotState | None
    errors_24h: int


async def dashboard_stats(session: AsyncSession) -> DashboardStats:
    """Собрать показатели дашборда."""
    now = utcnow()
    day_ago = now - timedelta(hours=24)
    week_ago = now - timedelta(days=7)

    async def count(query: sa.Select[Any]) -> int:
        return int((await session.execute(query)).scalar_one())

    checks_24h = await count(
        sa.select(sa.func.count()).select_from(SlotCheck).where(SlotCheck.started_at >= day_ago)
    )
    errors_24h = await count(
        sa.select(sa.func.count())
        .select_from(SlotCheck)
        .where(
            SlotCheck.started_at >= day_ago,
            SlotCheck.status.in_(
                [
                    CheckStatus.AUTH_REQUIRED,
                    CheckStatus.CAPTCHA_REQUIRED,
                    CheckStatus.ACCESS_BLOCKED,
                    CheckStatus.SITE_CHANGED,
                    CheckStatus.SYSTEM_ERROR,
                ]
            ),
        )
    )
    finds_7d = await count(
        sa.select(sa.func.count())
        .select_from(SlotEvent)
        .where(
            SlotEvent.created_at >= week_ago,
            SlotEvent.event_type.in_([SlotEventType.APPEARED, SlotEventType.DATE_CHANGED]),
        )
    )
    open_alerts = await count(
        sa.select(sa.func.count()).select_from(Alert).where(Alert.closed_at.is_(None))
    )
    targets_active = await count(
        sa.select(sa.func.count()).select_from(MonitorTarget).where(MonitorTarget.is_active.is_(True))
    )
    failed_webhooks = await count(
        sa.select(sa.func.count())
        .select_from(WebhookDelivery)
        .where(WebhookDelivery.status == WebhookStatus.FAILED)
    )

    from app.db.models import VfsAccount
    from app.enums import AccountStatus

    accounts_blocked = await count(
        sa.select(sa.func.count())
        .select_from(VfsAccount)
        .where(VfsAccount.status != AccountStatus.OK, VfsAccount.is_active.is_(True))
    )

    reaction = (
        await session.execute(
            sa.select(
                sa.func.avg(AlertReaction.seconds_from_alert),
                sa.func.percentile_cont(0.5).within_group(AlertReaction.seconds_from_alert),
            ).where(AlertReaction.reacted_at >= week_ago)
        )
    ).one()

    async def reactions_of(kind: ReactionKind) -> int:
        return await count(
            sa.select(sa.func.count())
            .select_from(AlertReaction)
            .where(AlertReaction.kind == kind, AlertReaction.reacted_at >= week_ago)
        )

    return DashboardStats(
        checks_24h=checks_24h,
        errors_24h=errors_24h,
        finds_7d=finds_7d,
        open_alerts=open_alerts,
        targets_active=targets_active,
        accounts_blocked=accounts_blocked,
        failed_webhooks=failed_webhooks,
        avg_reaction_seconds=int(reaction[0]) if reaction[0] is not None else None,
        median_reaction_seconds=int(reaction[1]) if reaction[1] is not None else None,
        booked_7d=await reactions_of(ReactionKind.BOOKED),
        gone_7d=await reactions_of(ReactionKind.GONE),
        false_positive_7d=await reactions_of(ReactionKind.FALSE_POSITIVE),
    )


async def monitoring_table(session: AsyncSession) -> list[TargetRow]:
    """Таблица мониторинга по образцу ТЗ §12."""
    day_ago = utcnow() - timedelta(hours=24)
    error_rows = (
        (
            await session.execute(
                sa.select(SlotCheck.target_id, sa.func.count())
                .where(
                    SlotCheck.started_at >= day_ago,
                    SlotCheck.status.in_(
                        [
                            CheckStatus.AUTH_REQUIRED,
                            CheckStatus.CAPTCHA_REQUIRED,
                            CheckStatus.ACCESS_BLOCKED,
                            CheckStatus.SITE_CHANGED,
                            CheckStatus.SYSTEM_ERROR,
                        ]
                    ),
                )
                .group_by(SlotCheck.target_id)
            )
        ).all()
    )
    error_counts: dict[int, int] = {int(tid): int(count) for tid, count in error_rows}

    rows = (
        await session.execute(
            sa.select(City, MonitorTarget, SlotState)
            .join(MonitorTarget, MonitorTarget.city_id == City.id)
            .outerjoin(SlotState, SlotState.target_id == MonitorTarget.id)
            .order_by(City.priority, City.name)
        )
    ).all()

    return [
        TargetRow(city=city, target=target, state=state, errors_24h=error_counts.get(target.id, 0))
        for city, target, state in rows
    ]


async def finds_by_hour(session: AsyncSession, days: int = 30) -> list[tuple[int, int]]:
    """Распределение находок по часам суток В МЕСТНОМ ВРЕМЕНИ (ТЗ §19).

    Ради этого отчёта и нужна вся история: он показывает реальные окна
    выкладки слотов, по которым потом настраивается частота проверок.
    """
    since = utcnow() - timedelta(days=days)
    rows = (
        await session.execute(
            sa.select(
                sa.cast(sa.func.extract("hour", LOCAL_EVENT), sa.Integer).label("hour"),
                sa.func.count(),
            )
            .select_from(SlotEvent)
            .join(MonitorTarget, MonitorTarget.id == SlotEvent.target_id)
            .join(City, City.id == MonitorTarget.city_id)
            .where(
                SlotEvent.created_at >= since,
                SlotEvent.event_type.in_(
                    [SlotEventType.APPEARED, SlotEventType.DATE_CHANGED]
                ),
            )
            .group_by("hour")
            .order_by("hour")
        )
    ).all()
    return [(int(hour), int(count)) for hour, count in rows]


async def finds_by_weekday(session: AsyncSession, days: int = 90) -> list[tuple[int, int]]:
    """Распределение находок по дням недели в местном времени (ТЗ §19).

    ISO-нумерация: 1 — понедельник, 7 — воскресенье.
    """
    since = utcnow() - timedelta(days=days)
    rows = (
        await session.execute(
            sa.select(
                sa.cast(sa.func.extract("isodow", LOCAL_EVENT), sa.Integer).label("dow"),
                sa.func.count(),
            )
            .select_from(SlotEvent)
            .join(MonitorTarget, MonitorTarget.id == SlotEvent.target_id)
            .join(City, City.id == MonitorTarget.city_id)
            .where(
                SlotEvent.created_at >= since,
                SlotEvent.event_type.in_(
                    [SlotEventType.APPEARED, SlotEventType.DATE_CHANGED]
                ),
            )
            .group_by("dow")
            .order_by("dow")
        )
    ).all()
    return [(int(dow), int(count)) for dow, count in rows]


def journal_query(
    *,
    city_id: int | None = None,
    status: CheckStatus | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    only_errors: bool = False,
) -> sa.Select[Any]:
    """Запрос журнала проверок с фильтрами (ТЗ §12).

    Вынесен отдельно, чтобы страница и экспорт использовали ровно одну
    выборку: расхождение между тем, что видно на экране, и тем, что
    выгружается в файл, — источник долгих споров.
    """
    query = (
        sa.select(SlotCheck, City, MonitorTarget)
        .join(MonitorTarget, MonitorTarget.id == SlotCheck.target_id)
        .join(City, City.id == MonitorTarget.city_id)
        .order_by(SlotCheck.started_at.desc())
    )
    if city_id is not None:
        query = query.where(MonitorTarget.city_id == city_id)
    if status is not None:
        query = query.where(SlotCheck.status == status)
    if only_errors:
        query = query.where(
            SlotCheck.status.in_(
                [
                    CheckStatus.AUTH_REQUIRED,
                    CheckStatus.CAPTCHA_REQUIRED,
                    CheckStatus.ACCESS_BLOCKED,
                    CheckStatus.SITE_CHANGED,
                    CheckStatus.SYSTEM_ERROR,
                ]
            )
        )
    if date_from is not None:
        query = query.where(SlotCheck.started_at >= date_from)
    if date_to is not None:
        query = query.where(SlotCheck.started_at < date_to)
    return query
