"""Доставка вебхуков в CRM с ретраями.

Отправка вынесена из момента находки: сотрудник не должен ждать ответа
чужого сервиса, а недоступность CRM не должна мешать записать результат.
Очередь живёт строками в webhook_deliveries, поэтому переживает рестарт —
свипу достаточно выбрать всё, у чего наступил next_retry_at.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Employee, WebhookDelivery
from app.domain.timeutils import utcnow
from app.enums import EmployeeRole, NotificationKind, WebhookStatus
from app.logging import get_logger
from app.services.notifications import Notifier, send_and_log
from app.services.settings_service import RuntimeSettings

log = get_logger(__name__)

# Верхняя граница задержки: без неё шестая попытка ушла бы за горизонт смены.
MAX_BACKOFF_SECONDS = 3600

# Коды, при которых повтор осмыслен. Прочие 4xx означают, что CRM не примет
# это тело никогда — шесть попыток лишь отложат разбор проблемы на час.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429})


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Ответ CRM, каким он важен для решения о ретрае."""

    status_code: int
    body: str = ""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def is_retryable(self) -> bool:
        return self.status_code >= 500 or self.status_code in RETRYABLE_STATUS_CODES


class WebhookSender(Protocol):
    """Транспорт HTTP. Подменяется в тестах — сеть в тестах не участвует."""

    async def post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> HttpResponse: ...


class HttpxWebhookSender:
    """Отправка через httpx."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    async def post(
        self, url: str, payload: dict[str, Any], headers: dict[str, str]
    ) -> HttpResponse:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
            return HttpResponse(status_code=response.status_code, body=response.text[:1000])


class WebhookTransportError(Exception):
    """Сеть не ответила: таймаут, DNS, обрыв соединения."""


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Подпись тела запроса.

    Сериализация с sort_keys и без пробелов: подпись должна совпадать
    с той, что посчитает принимающая сторона по полученному телу.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def build_headers(delivery: WebhookDelivery, secret: str) -> dict[str, str]:
    """Заголовки запроса.

    X-Delivery-Id постоянен между попытками: по нему CRM отличит повтор
    от новой находки, если наш ответ потерялся уже после её обработки.
    """
    headers = {
        "Content-Type": "application/json",
        "X-Delivery-Id": str(delivery.id),
        "X-Event": delivery.event,
        "X-Attempt": str(delivery.attempts + 1),
    }
    if secret:
        headers["X-Signature"] = sign_payload(delivery.payload, secret)
    return headers


def backoff_delay(attempts: int, base_seconds: int) -> timedelta:
    """Экспоненциальная задержка перед следующей попыткой."""
    seconds = min(base_seconds * (2**attempts), MAX_BACKOFF_SECONDS)
    return timedelta(seconds=seconds)


async def due_delivery_ids(session: AsyncSession, now: datetime, limit: int = 50) -> list[int]:
    """Идентификаторы доставок, которым пора уйти."""
    rows = await session.execute(
        sa.select(WebhookDelivery.id)
        .where(
            WebhookDelivery.status == WebhookStatus.PENDING,
            WebhookDelivery.next_retry_at.is_not(None),
            WebhookDelivery.next_retry_at <= now,
        )
        .order_by(WebhookDelivery.next_retry_at)
        .limit(limit)
    )
    return list(rows.scalars().all())


async def notify_admins_of_failure(
    session: AsyncSession,
    notifier: Notifier,
    delivery: WebhookDelivery,
) -> None:
    """Сообщить админам об окончательной неудаче доставки.

    Молчаливо потерянная находка хуже, чем шумное уведомление: слот уже
    нашли, но CRM об этом не узнает, и никто не заметит.
    """
    admins = (
        await session.execute(
            sa.select(Employee).where(
                Employee.role == EmployeeRole.ADMIN,
                Employee.is_active.is_(True),
                Employee.telegram_id.is_not(None),
            )
        )
    ).scalars().all()

    text = (
        f"🛑 Вебхук в CRM не доставлен после {delivery.attempts} попыток.\n\n"
        f"Событие №{delivery.event_id}\n"
        f"URL: {delivery.url}\n"
        f"Последний ответ: {delivery.last_status_code or '—'}\n"
        f"{(delivery.last_error or '')[:200]}"
    )
    for admin in admins:
        await send_and_log(
            session,
            notifier,
            chat_id=admin.telegram_id or 0,
            kind=NotificationKind.WEBHOOK_FAILED,
            text=text,
            employee_id=admin.id,
            alert_id=None,
        )


async def deliver_one(
    session: AsyncSession,
    sender: WebhookSender,
    notifier: Notifier,
    delivery: WebhookDelivery,
    settings: RuntimeSettings,
    now: datetime | None = None,
) -> WebhookStatus:
    """Выполнить одну попытку доставки и обновить строку очереди."""
    moment = now or utcnow()
    delivery.attempts += 1

    try:
        response = await sender.post(
            delivery.url, delivery.payload, build_headers(delivery, settings.crm_webhook_secret)
        )
    except (WebhookTransportError, OSError) as exc:
        # Сеть не ответила — это всегда повод повторить: CRM могла просто
        # перезагружаться.
        delivery.last_error = f"{type(exc).__name__}: {exc}"[:500]
        delivery.last_status_code = None
        return await _schedule_retry_or_fail(
            session, notifier, delivery, settings, moment, retryable=True
        )

    delivery.last_status_code = response.status_code
    delivery.last_error = None if response.is_success else response.body[:500]

    if response.is_success:
        delivery.status = WebhookStatus.SUCCEEDED
        delivery.next_retry_at = None
        delivery.completed_at = moment
        await session.flush()
        log.info(
            "webhook.delivered",
            delivery_id=delivery.id,
            alert_id=None,
            attempts=delivery.attempts,
        )
        return WebhookStatus.SUCCEEDED

    return await _schedule_retry_or_fail(
        session, notifier, delivery, settings, moment, retryable=response.is_retryable
    )


async def _schedule_retry_or_fail(
    session: AsyncSession,
    notifier: Notifier,
    delivery: WebhookDelivery,
    settings: RuntimeSettings,
    moment: datetime,
    *,
    retryable: bool,
) -> WebhookStatus:
    """Назначить следующую попытку либо признать доставку неудавшейся."""
    exhausted = delivery.attempts >= settings.webhook_max_attempts
    if exhausted or not retryable:
        delivery.status = WebhookStatus.FAILED
        delivery.next_retry_at = None
        delivery.completed_at = moment
        await session.flush()
        log.warning(
            "webhook.failed",
            delivery_id=delivery.id,
            alert_id=None,
            attempts=delivery.attempts,
            status_code=delivery.last_status_code,
            reason="attempts_exhausted" if exhausted else "non_retryable",
        )
        await notify_admins_of_failure(session, notifier, delivery)
        return WebhookStatus.FAILED

    delivery.next_retry_at = moment + backoff_delay(
        delivery.attempts, settings.webhook_backoff_base_seconds
    )
    await session.flush()
    log.info(
        "webhook.retry_scheduled",
        delivery_id=delivery.id,
        attempts=delivery.attempts,
        next_retry_at=delivery.next_retry_at.isoformat(),
    )
    return WebhookStatus.PENDING


async def sweep_webhooks(
    session: AsyncSession,
    sender: WebhookSender,
    notifier: Notifier,
    settings: RuntimeSettings,
    now: datetime | None = None,
) -> int:
    """Отправить все доставки, у которых наступил срок."""
    moment = now or utcnow()
    sent = 0
    for delivery_id in await due_delivery_ids(session, moment):
        delivery = (
            await session.execute(
                sa.select(WebhookDelivery)
                .where(WebhookDelivery.id == delivery_id)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        # Строку уже забрал другой воркер — пропускаем без ожидания.
        if delivery is None or delivery.status is not WebhookStatus.PENDING:
            continue
        await deliver_one(session, sender, notifier, delivery, settings, moment)
        sent += 1
    return sent
