"""Доставка вебхуков: ретраи, подпись, окончательная неудача.

Сеть в тестах не участвует: транспорт подменяется, ответы задаются сценарием.
"""

import json
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import NotificationLog, WebhookDelivery
from app.domain.timeutils import UTC
from app.enums import EmployeeRole, NotificationKind, WebhookStatus
from app.services.settings_service import RuntimeSettings
from app.services.webhook_service import (
    MAX_BACKOFF_SECONDS,
    HttpResponse,
    WebhookTransportError,
    backoff_delay,
    build_headers,
    deliver_one,
    sign_payload,
    sweep_webhooks,
)
from tests.conftest import RecordingNotifier, make_employee

NOW = datetime(2026, 7, 29, 5, 0, tzinfo=UTC)
PAYLOAD = {"event": "slot_found", "city": "almaty", "found_date": "2026-09-15", "task_id": 1}


class ScriptedSender:
    """Транспорт, отвечающий по заранее заданному сценарию."""

    def __init__(self, *responses: HttpResponse | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object], dict[str, str]]] = []

    async def post(
        self, url: str, payload: dict[str, object], headers: dict[str, str]
    ) -> HttpResponse:
        self.calls.append((url, payload, headers))
        item = self._responses.pop(0) if self._responses else HttpResponse(200)
        if isinstance(item, Exception):
            raise item
        return item


async def make_delivery(
    session: AsyncSession,
    *,
    attempts: int = 0,
    next_retry_at: datetime | None = NOW,
    status: WebhookStatus = WebhookStatus.PENDING,
) -> WebhookDelivery:
    delivery = WebhookDelivery(
        event_id=None,
        event="slot_found",
        url="https://crm.example/hook",
        payload=PAYLOAD,
        attempts=attempts,
        status=status,
        next_retry_at=next_retry_at,
    )
    session.add(delivery)
    await session.flush()
    return delivery


class TestBackoff:
    def test_delay_doubles(self) -> None:
        assert backoff_delay(1, 15) == timedelta(seconds=30)
        assert backoff_delay(2, 15) == timedelta(seconds=60)
        assert backoff_delay(3, 15) == timedelta(seconds=120)

    def test_delay_is_capped(self) -> None:
        """Без потолка шестая попытка ушла бы за горизонт смены."""
        assert backoff_delay(20, 15) == timedelta(seconds=MAX_BACKOFF_SECONDS)


class TestSignature:
    def test_signature_is_stable(self) -> None:
        first = sign_payload({"b": 2, "a": 1}, "secret")
        second = sign_payload({"a": 1, "b": 2}, "secret")
        # Порядок ключей не должен менять подпись.
        assert first == second

    def test_signature_depends_on_secret(self) -> None:
        assert sign_payload(PAYLOAD, "one") != sign_payload(PAYLOAD, "two")

    def test_signature_matches_serialized_body(self) -> None:
        """Принимающая сторона считает подпись по телу — форматы должны совпасть."""
        import hashlib
        import hmac

        body = json.dumps(PAYLOAD, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected = hmac.new(b"secret", body.encode(), hashlib.sha256).hexdigest()
        assert sign_payload(PAYLOAD, "secret") == expected

    async def test_headers_carry_delivery_id_and_attempt(
        self, session: AsyncSession
    ) -> None:
        delivery = await make_delivery(session, attempts=2)
        headers = build_headers(delivery, "secret")

        # Постоянный между попытками идентификатор — по нему CRM отличит
        # повтор от новой находки.
        assert headers["X-Delivery-Id"] == str(delivery.id)
        assert headers["X-Attempt"] == "3"
        assert "X-Signature" in headers

    async def test_no_signature_without_secret(self, session: AsyncSession) -> None:
        delivery = await make_delivery(session)
        assert "X-Signature" not in build_headers(delivery, "")


class TestDelivery:
    async def test_success_closes_the_delivery(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        delivery = await make_delivery(session)
        sender = ScriptedSender(HttpResponse(200, "ok"))

        status = await deliver_one(
            session, sender, RecordingNotifier(), delivery, settings, NOW
        )

        assert status is WebhookStatus.SUCCEEDED
        assert delivery.status is WebhookStatus.SUCCEEDED
        assert delivery.attempts == 1
        assert delivery.next_retry_at is None
        assert delivery.completed_at == NOW

    async def test_server_error_schedules_a_retry(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        delivery = await make_delivery(session)
        sender = ScriptedSender(HttpResponse(503, "unavailable"))

        status = await deliver_one(
            session, sender, RecordingNotifier(), delivery, settings, NOW
        )

        assert status is WebhookStatus.PENDING
        assert delivery.attempts == 1
        assert delivery.next_retry_at == NOW + timedelta(seconds=30)
        assert delivery.last_status_code == 503

    async def test_network_error_schedules_a_retry(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        delivery = await make_delivery(session)
        sender = ScriptedSender(WebhookTransportError("connection reset"))

        status = await deliver_one(
            session, sender, RecordingNotifier(), delivery, settings, NOW
        )

        assert status is WebhookStatus.PENDING
        assert delivery.last_status_code is None
        assert "connection reset" in (delivery.last_error or "")

    async def test_bad_request_fails_immediately(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        """400 означает, что тело не примут никогда — шесть попыток лишь
        отложат разбор проблемы на час."""
        await make_employee(session, "Админ", 999, EmployeeRole.ADMIN)
        delivery = await make_delivery(session)
        notifier = RecordingNotifier()
        sender = ScriptedSender(HttpResponse(400, "malformed payload"))

        status = await deliver_one(session, sender, notifier, delivery, settings, NOW)

        assert status is WebhookStatus.FAILED
        assert delivery.attempts == 1
        assert delivery.next_retry_at is None
        assert 999 in notifier.chats()

    async def test_429_is_retried(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        delivery = await make_delivery(session)
        sender = ScriptedSender(HttpResponse(429, "slow down"))

        status = await deliver_one(
            session, sender, RecordingNotifier(), delivery, settings, NOW
        )
        assert status is WebhookStatus.PENDING


class TestExhaustion:
    async def test_last_attempt_fails_and_notifies_admins(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        await make_employee(session, "Админ", 999, EmployeeRole.ADMIN)
        await make_employee(session, "Дежурный", 100)  # не должен получить письмо
        delivery = await make_delivery(session, attempts=settings.webhook_max_attempts - 1)
        notifier = RecordingNotifier()
        sender = ScriptedSender(HttpResponse(500, "boom"))

        status = await deliver_one(session, sender, notifier, delivery, settings, NOW)

        assert status is WebhookStatus.FAILED
        assert delivery.attempts == settings.webhook_max_attempts
        assert delivery.completed_at == NOW
        assert notifier.chats() == [999]

        logged = (
            await session.execute(
                sa.select(NotificationLog).where(
                    NotificationLog.kind == NotificationKind.WEBHOOK_FAILED
                )
            )
        ).scalar_one()
        assert "не доставлен" in logged.text

    async def test_full_retry_sequence(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        """Шесть неудач подряд: пять ретраев, затем окончательный отказ."""
        await make_employee(session, "Админ", 999, EmployeeRole.ADMIN)
        delivery = await make_delivery(session)
        notifier = RecordingNotifier()
        sender = ScriptedSender(*[HttpResponse(500, "boom")] * 6)

        moment = NOW
        delays: list[float] = []
        for _ in range(settings.webhook_max_attempts):
            await deliver_one(session, sender, notifier, delivery, settings, moment)
            if delivery.next_retry_at is not None:
                delays.append((delivery.next_retry_at - moment).total_seconds())
                moment = delivery.next_retry_at

        assert delays == [30, 60, 120, 240, 480]
        assert delivery.status is WebhookStatus.FAILED
        assert delivery.attempts == 6
        assert len(sender.calls) == 6
        # Админ получает ровно одно сообщение — в конце, а не на каждой попытке.
        assert notifier.chats() == [999]

    async def test_recovery_mid_sequence(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        """CRM поднялась со второй попытки — доставка закрывается успехом."""
        delivery = await make_delivery(session)
        notifier = RecordingNotifier()
        sender = ScriptedSender(HttpResponse(502), HttpResponse(200))

        await deliver_one(session, sender, notifier, delivery, settings, NOW)
        assert delivery.status is WebhookStatus.PENDING

        await deliver_one(session, sender, notifier, delivery, settings, NOW)
        assert delivery.status is WebhookStatus.SUCCEEDED
        assert delivery.attempts == 2
        assert notifier.sent == []


class TestSweep:
    async def test_only_due_deliveries_are_sent(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        due = await make_delivery(session, next_retry_at=NOW - timedelta(seconds=1))
        later = await make_delivery(session, next_retry_at=NOW + timedelta(minutes=5))
        done = await make_delivery(session, status=WebhookStatus.SUCCEEDED, next_retry_at=None)
        sender = ScriptedSender(HttpResponse(200), HttpResponse(200))

        count = await sweep_webhooks(session, sender, RecordingNotifier(), settings, NOW)

        assert count == 1
        assert due.status is WebhookStatus.SUCCEEDED
        assert later.status is WebhookStatus.PENDING
        assert done.status is WebhookStatus.SUCCEEDED
        assert len(sender.calls) == 1

    async def test_sweep_survives_a_restart(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        """Очередь живёт в строках, поэтому рестарт процесса её не теряет."""
        delivery = await make_delivery(session)
        await sweep_webhooks(
            session, ScriptedSender(HttpResponse(500)), RecordingNotifier(), settings, NOW
        )
        assert delivery.status is WebhookStatus.PENDING
        retry_at = delivery.next_retry_at
        assert retry_at is not None

        # «Новый процесс» — свежий свип, никакого состояния в памяти.
        await sweep_webhooks(
            session, ScriptedSender(HttpResponse(200)), RecordingNotifier(), settings, retry_at
        )
        assert delivery.status is WebhookStatus.SUCCEEDED

    async def test_payload_is_sent_unchanged(
        self, session: AsyncSession, settings: RuntimeSettings
    ) -> None:
        await make_delivery(session)
        sender = ScriptedSender(HttpResponse(200))

        await sweep_webhooks(session, sender, RecordingNotifier(), settings, NOW)

        url, payload, headers = sender.calls[0]
        assert url == "https://crm.example/hook"
        assert payload == PAYLOAD
        assert headers["Content-Type"] == "application/json"
