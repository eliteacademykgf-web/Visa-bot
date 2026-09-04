"""Восстановление состояния при старте планировщика.

Отдельного механизма «догнать пропущенное» не требуется: все свипы —
опросы, и первый же проход после запуска видит истёкшие сроки. Важно
другое — чтобы этот проход не превратился в лавину.

Схлопывание уровней в политике эскалаций гарантирует, что после простоя
любой длины каждое открытое уведомление получит ровно одно сообщение —
максимального просроченного уровня. При порогах ТЗ в 2, 5 и 10 минут
это особенно существенно: даже недолгий простой просрочивает все три.
"""

from app.config import get_settings
from app.domain.timeutils import utcnow
from app.logging import get_logger
from app.scheduler import jobs
from app.services.notifications import Notifier
from app.services.webhook_service import HttpxWebhookSender

log = get_logger(__name__)


async def run_startup_recovery(notifier: Notifier) -> None:
    """Первый проход свипов после запуска процесса.

    Порядок важен: сперва разослать накопившиеся события (иначе эскалации
    не найдут уведомлений), затем эскалации, затем очередь вебхуков.
    """
    started = utcnow()
    log.info("recovery.started", at=started.isoformat())

    dispatched = await jobs.dispatch_new_events(notifier, started)
    escalated = await jobs.run_escalations(notifier, started)
    webhooks = await jobs.deliver_webhooks(
        HttpxWebhookSender(get_settings().webhook_timeout_seconds), notifier, started
    )

    log.info(
        "recovery.finished",
        dispatched=dispatched,
        escalated=escalated,
        webhooks=webhooks,
        duration_seconds=(utcnow() - started).total_seconds(),
    )
