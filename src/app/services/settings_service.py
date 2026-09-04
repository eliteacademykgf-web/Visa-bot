"""Доступ к настройкам, редактируемым из панели."""

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    """Снимок настроек на момент начала свипа.

    Читается один раз за проход: значения не должны меняться в середине
    обработки пачки задач, иначе часть задач получит один порог, а часть — другой.
    """

    escalation_level_1_minutes: int
    escalation_level_2_minutes: int
    escalation_level_3_minutes: int
    repeat_notice_minutes: int
    error_notice_after: int
    group_chat_id: int | None
    crm_webhook_url: str | None
    crm_webhook_secret: str
    webhook_max_attempts: int
    webhook_backoff_base_seconds: int

    @property
    def escalation_thresholds(self) -> dict[int, int]:
        """Уровень -> задержка в минутах от scheduled_at."""
        return {
            1: self.escalation_level_1_minutes,
            2: self.escalation_level_2_minutes,
            3: self.escalation_level_3_minutes,
        }


DEFAULTS: dict[str, Any] = {
    # ТЗ §11: 2 минуты — повтор, 5 — руководителю, 10 — резервному.
    "escalation_level_1_minutes": 2,
    "escalation_level_2_minutes": 5,
    "escalation_level_3_minutes": 10,
    # ТЗ §10: через сколько напомнить о том же слоте.
    "repeat_notice_minutes": 30,
    # ТЗ §20: после скольких ошибок подряд звать администратора.
    "error_notice_after": 3,
    "group_chat_id": None,
    "crm_webhook_url": None,
    "crm_webhook_secret": "",
    "webhook_max_attempts": 6,
    "webhook_backoff_base_seconds": 15,
}


async def load_settings(session: AsyncSession) -> RuntimeSettings:
    """Прочитать все настройки одним запросом.

    Отсутствующий ключ подменяется значением из DEFAULTS: свежая БД или
    удалённая вручную строка не должны ронять планировщик.
    """
    rows = (await session.execute(sa.select(Setting.key, Setting.value))).all()
    raw: dict[str, Any] = dict(DEFAULTS)
    for key, value in rows:
        if key in raw:
            raw[key] = value

    def as_int(key: str) -> int:
        value = raw[key]
        return int(value) if value is not None else int(DEFAULTS[key])

    chat_id = raw["group_chat_id"]
    return RuntimeSettings(
        escalation_level_1_minutes=as_int("escalation_level_1_minutes"),
        escalation_level_2_minutes=as_int("escalation_level_2_minutes"),
        escalation_level_3_minutes=as_int("escalation_level_3_minutes"),
        repeat_notice_minutes=as_int("repeat_notice_minutes"),
        error_notice_after=as_int("error_notice_after"),
        group_chat_id=int(chat_id) if chat_id not in (None, "") else None,
        crm_webhook_url=raw["crm_webhook_url"] or None,
        crm_webhook_secret=raw["crm_webhook_secret"] or "",
        webhook_max_attempts=as_int("webhook_max_attempts"),
        webhook_backoff_base_seconds=as_int("webhook_backoff_base_seconds"),
    )
