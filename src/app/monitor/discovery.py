"""Запомненный эндпоинт календаря и политика доверия к нему.

Идея. Первая проверка идёт длинным путём — по экранам мастера записи — и
попутно замечает, каким запросом пришли даты. Дальше этот запрос вызывается
напрямую: одно обращение вместо шести загрузок страниц.

Почему это не просто оптимизация. Каждая загрузка страницы `schedule` —
повод показать CAPTCHA (она включена там в конфигурации приложения VFS) и
лишний след в глазах антибота. Прямой вызов внутри уже открытой сессии и
дешевле, и незаметнее.

Почему нельзя доверять эндпоинту вечно. В запомненном адресе остаются
параметры того момента — центр, категория, диапазон дат. Через месяц такой
адрес может честно вернуть пустой ответ просто потому, что спрашивает про
прошедший интервал, а система примет это за «слотов нет» и будет молчать,
пока слоты есть. Поэтому доверие ограничено по времени: раз в несколько
проверок система обязана снова пройти длинным путём и обновить адрес.

Пустой ответ при этом остаётся законным результатом «слотов нет» — иначе
длинный путь пришлось бы проходить почти всегда, ведь отсутствие слотов
и есть обычное состояние.
"""

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Setting
from app.enums import SettingValueType
from app.logging import get_logger

log = get_logger(__name__)

# Через сколько проверок на прямом эндпоинте вернуться к длинному пути.
# При интервале в 10 минут двенадцать проверок — это два часа: столько
# максимум может продлиться незамеченным устаревший адрес.
REVALIDATE_EVERY = 12

_KEY_PREFIX = "dates_endpoint:"


@dataclass(frozen=True, slots=True)
class EndpointMemory:
    """Что известно про прямой запрос дат для одной цели."""

    url: str = ""
    checks_since_validation: int = 0

    @property
    def is_known(self) -> bool:
        return bool(self.url)


def should_use_api(memory: EndpointMemory, revalidate_every: int = REVALIDATE_EVERY) -> bool:
    """Можно ли на этой проверке идти коротким путём."""
    if not memory.is_known:
        return False
    return memory.checks_since_validation < revalidate_every


def after_api_use(memory: EndpointMemory) -> EndpointMemory:
    """Учесть ещё одну проверку, сделанную коротким путём."""
    return EndpointMemory(
        url=memory.url,
        checks_since_validation=memory.checks_since_validation + 1,
    )


def after_validation(url: str) -> EndpointMemory:
    """Длинный путь пройден: адрес обновлён, счётчик доверия сброшен."""
    return EndpointMemory(url=url, checks_since_validation=0)


def _key(target_id: int) -> str:
    return f"{_KEY_PREFIX}{target_id}"


async def load(session: AsyncSession, target_id: int) -> EndpointMemory:
    """Прочитать запомненный эндпоинт цели."""
    row = await session.get(Setting, _key(target_id))
    if row is None or not isinstance(row.value, dict):
        return EndpointMemory()
    value: dict[str, Any] = row.value
    return EndpointMemory(
        url=str(value.get("url") or ""),
        checks_since_validation=int(value.get("checks_since_validation") or 0),
    )


async def save(session: AsyncSession, target_id: int, memory: EndpointMemory) -> None:
    """Сохранить состояние. Настройки — типизированный key-value, миграция не нужна."""
    payload = {
        "url": memory.url,
        "checks_since_validation": memory.checks_since_validation,
    }
    statement = (
        pg_insert(Setting)
        .values(
            key=_key(target_id),
            value=payload,
            value_type=SettingValueType.JSON,
            description="Прямой запрос дат календаря, найденный автоматически",
        )
        .on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": payload, "updated_at": sa.func.now()},
        )
    )
    await session.execute(statement)
