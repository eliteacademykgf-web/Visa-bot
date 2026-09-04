"""Граница отправки сообщений.

Планировщик не знает про aiogram: он работает через протокол Notifier.
Это разделение нужно не ради красоты, а ради тестов — цепочку эскалаций
надо проверять без Telegram, и подменяемая реализация здесь единственная
внешняя зависимость.
"""

from dataclasses import dataclass
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationLog
from app.enums import NotificationKind
from app.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Итог отправки одного сообщения."""

    delivered: bool
    message_id: int | None = None
    error: str | None = None


class Notifier(Protocol):
    """Что вызывающему коду нужно от транспорта сообщений."""

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        with_task_buttons: int | None = None,
        photo_file_id: str | None = None,
    ) -> DeliveryResult:
        """Отправить сообщение.

        with_task_buttons — id задачи для клавиатуры ответа;
        photo_file_id — file_id скриншота, тогда текст уходит подписью к фото.
        """
        ...


class NullNotifier:
    """Заглушка: ничего не отправляет, всё считает доставленным."""

    async def send_text(
        self,
        chat_id: int,
        text: str,
        *,
        with_task_buttons: int | None = None,
        photo_file_id: str | None = None,
    ) -> DeliveryResult:
        return DeliveryResult(delivered=True)


async def send_and_log(
    session: AsyncSession,
    notifier: Notifier,
    *,
    chat_id: int,
    kind: NotificationKind,
    text: str,
    employee_id: int | None = None,
    alert_id: int | None = None,
    with_task_buttons: int | None = None,
    photo_file_id: str | None = None,
) -> DeliveryResult:
    """Отправить сообщение и записать факт в журнал.

    Журнал пишется и при неудаче: недоставленное задание — не строка для
    отчёта, а повод эскалировать немедленно. Если дежурный не нажимал /start
    или заблокировал бота, ждать первого порога бессмысленно.
    """
    result = await notifier.send_text(
        chat_id, text, with_task_buttons=with_task_buttons, photo_file_id=photo_file_id
    )
    session.add(
        NotificationLog(
            alert_id=alert_id,
            employee_id=employee_id,
            chat_id=chat_id,
            kind=kind,
            text=text,
            telegram_message_id=result.message_id,
            is_delivered=result.delivered,
            error=result.error,
        )
    )
    await session.flush()
    if not result.delivered:
        log.warning(
            "notification.undelivered",
            chat_id=chat_id,
            kind=kind.value,
            alert_id=alert_id,
            error=result.error,
        )
    return result


async def already_notified(
    session: AsyncSession, alert_id: int, kind: NotificationKind
) -> bool:
    """Отправлялось ли по уведомлению сообщение такого типа (успешно)."""
    found = (
        await session.execute(
            sa.select(sa.literal(1))
            .select_from(NotificationLog)
            .where(
                NotificationLog.alert_id == alert_id,
                NotificationLog.kind == kind,
                NotificationLog.is_delivered.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None
