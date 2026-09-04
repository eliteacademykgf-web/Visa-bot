"""Проверка доставки уведомлений от начала до конца.

Зачем. Мониторинг может месяцами не находить слотов — это его нормальное
состояние. И всё это время неизвестно, дойдёт ли сообщение, когда слот
наконец появится: не протух ли токен, заведены ли получатели, не заблокировал
ли кто-то бота. Узнать об этом в момент находки — слишком поздно.

Команда проходит настоящий путь: берёт живую цель, строит наблюдение
«слот найден», отдаёт его тому же составителю текста, что и настоящее
событие, находит получателей по ролям и отправляет через обычный
Telegram-транспорт.

Чего команда НЕ делает: не трогает состояние мониторинга. Записать «слот
найден» в slot_states значило бы, что следующая настоящая проверка увидит
исчезновение слота и разошлёт ложное «слот пропал». Проверка связи не
должна оставлять следов в данных.
"""

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import City, Employee, MonitorTarget, SlotEvent
from app.db.session import session_scope
from app.domain.timeutils import utcnow
from app.enums import SlotEventType
from app.logging import get_logger
from app.services import recipients
from app.services.alert_service import render_alert

log = get_logger(__name__)


async def run() -> None:
    """Отправить пробное уведомление всем, кто должен его получать."""
    async with session_scope() as session:
        target = await _first_active_target(session)
        if target is None:
            raise SystemExit(
                "Нет активной цели мониторинга. Включите город и категорию в панели."
            )

        people = await _audience(session)
        if not people:
            raise SystemExit(
                "Некому отправлять: нет активных сотрудников с Telegram ID.\n"
                "Заведите администратора: python -m app.cli createadmin ..."
            )

        text = _demo_text(target)

    await _deliver(text, people)


async def _first_active_target(session: AsyncSession) -> MonitorTarget | None:
    """Первая цель, по которой реально идут проверки."""
    rows = (
        await session.execute(
            sa.select(MonitorTarget)
            .options(selectinload(MonitorTarget.city), selectinload(MonitorTarget.category))
            .join(MonitorTarget.city)
            .where(MonitorTarget.is_active.is_(True), City.is_active.is_(True))
            .order_by(MonitorTarget.id)
            .limit(1)
        )
    ).scalars().all()
    return rows[0] if rows else None


async def _audience(session: AsyncSession) -> list[Employee]:
    """Кто получит сообщение: те же роли, что и при настоящей находке."""
    seen: dict[int, Employee] = {}
    for group in (
        await recipients.admins(session),
        await recipients.supervisors(session),
        await recipients.specialists(session),
    ):
        for person in group:
            if person.telegram_id and person.is_active:
                seen[person.telegram_id] = person
    return list(seen.values())


def _demo_text(target: MonitorTarget) -> str:
    """Текст настоящего уведомления о находке, помеченный как проверка.

    Используется тот же render_alert, что и в бою: смысл проверки в том,
    чтобы увидеть ровно то сообщение, которое придёт при реальном слоте,
    а не отдельную заглушку, которая может разойтись с боевой.
    """
    event = SlotEvent(
        event_type=SlotEventType.APPEARED,
        new_date=(utcnow() + timedelta(days=18)).date(),
        created_at=utcnow(),
        details={},
    )
    body = render_alert(event, target, target.city, target.category)
    return (
        "🧪 ПРОВЕРКА СВЯЗИ — слот не найден, это тест доставки.\n"
        "Ниже — то, как выглядит настоящее уведомление.\n"
        f"{'─' * 28}\n\n{body}"
    )


async def _deliver(text: str, people: list[Employee]) -> None:
    """Отправить сообщение и честно отчитаться о каждом получателе."""
    from app.bot.main import build_bot
    from app.bot.notifier import TelegramNotifier

    bot = build_bot()
    notifier = TelegramNotifier(bot)

    delivered = 0
    try:
        for person in people:
            assert person.telegram_id is not None
            result = await notifier.send_text(person.telegram_id, text)
            if result.delivered:
                delivered += 1
                print(f"  ✓ {person.full_name} (telegram_id={person.telegram_id})")
            else:
                print(f"  ✗ {person.full_name} (telegram_id={person.telegram_id}): {result.error}")
    finally:
        await bot.session.close()

    print()
    if delivered:
        print(f"Доставлено: {delivered} из {len(people)}.")
    else:
        print(
            "Не доставлено никому. Обычная причина — получатель не нажал /start у бота: "
            "Telegram запрещает боту писать первым."
        )
    log.info("selftest.finished", delivered=delivered, total=len(people))
