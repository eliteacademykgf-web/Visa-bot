"""Извлечение доступных дат из произвольного JSON-ответа.

Зачем это нужно. Календарь VFS наполняется отдельным запросом, и читать
его ответ надёжнее, чем разбирать DOM: вёрстка меняется каждые несколько
месяцев (ТЗ §28), формат данных — почти никогда. Но точную форму ответа
заранее знать нельзя, а ждать, пока её впишут в конфиг, — значит не
работать до тех пор.

Поэтому разбор устроен структурно, а не по фиксированному пути: обходим
дерево и собираем всё, что похоже на дату. Такой разбор переживает и
`{"dates": [...]}`, и `{"data": {"availableDates": [...]}}`, и список
объектов с датой внутри.

Главная осторожность здесь — не принять занятый день за свободный.
Календари часто отдают все дни подряд с флагом доступности, и наивный
сбор всех дат подряд превратил бы «мест нет» в «свободен весь месяц».
Поэтому объект с датой и явно отрицательным признаком доступности
отбрасывается.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.logging import get_logger
from app.monitor.page_reader import parse_date

log = get_logger(__name__)

# Ключи, которыми календари помечают доступность дня. Если такой ключ есть
# и он отрицательный — день занят, каким бы «доступным» ни выглядел объект.
AVAILABILITY_KEYS = (
    "available",
    "isavailable",
    "isopen",
    "isenabled",
    "enabled",
    "hasslots",
    "hasavailability",
    "isbookable",
    "bookable",
)

# Ключи с количеством свободных мест. Ноль — тоже «занято».
COUNT_KEYS = ("slots", "slotcount", "availableslots", "availablecount", "count", "capacity")

# Ключи, под которыми лежит сама дата. Нужны, чтобы отличить дату приёма
# от служебных дат вроде «дата генерации ответа».
DATE_KEYS = (
    "date",
    "dates",
    "appointmentdate",
    "appointmentdates",
    "availabledate",
    "availabledates",
    "availableday",
    "availabledays",
    "slotdate",
    "slotdates",
    "day",
    "days",
    "value",
)

# Служебные даты, которые нельзя принимать за доступный день.
IGNORED_DATE_KEYS = (
    "createdat",
    "updatedat",
    "generatedat",
    "timestamp",
    "expiry",
    "expiresat",
    "dob",
    "dateofbirth",
    "passportexpiry",
    "from",
    "to",
    "mindate",
    "maxdate",
    "startdate",
    "enddate",
)

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
TIME_KEYS = ("time", "slottime", "appointmenttime", "starttime", "timeslot")


@dataclass(frozen=True, slots=True)
class JsonDates:
    """Что удалось вычитать из ответа."""

    dates: tuple[date, ...] = ()
    times: tuple[str, ...] = ()

    @property
    def found(self) -> bool:
        return bool(self.dates)


def _norm(key: str) -> str:
    """Ключи в API приходят в разных стилях — сводим к одному виду."""
    return key.replace("_", "").replace("-", "").lower()


def _is_unavailable(node: dict[str, Any]) -> bool:
    """Помечен ли этот узел как недоступный.

    Проверяется только явное отрицание. Отсутствие признака доступности
    трактуется как «доступен»: ответы, отдающие исключительно свободные дни,
    флага обычно не содержат вовсе, и требовать его значило бы не находить
    ничего.
    """
    for raw_key, value in node.items():
        key = _norm(raw_key)
        if key in AVAILABILITY_KEYS:
            if value is False or value in (0, "0", "false", "False", "N", "n"):
                return True
        elif key in COUNT_KEYS and isinstance(value, int | float) and value <= 0:
            return True
    return False


def _collect_times(node: dict[str, Any], into: list[str]) -> None:
    for raw_key, value in node.items():
        if _norm(raw_key) in TIME_KEYS and isinstance(value, str) and TIME_RE.match(value.strip()):
            into.append(value.strip())


def extract(payload: Any, preferred_format: str | None = None) -> JsonDates:
    """Собрать доступные даты и времена из разобранного JSON."""
    dates: list[date] = []
    times: list[str] = []
    _walk(payload, preferred_format, dates, times, unavailable=False, parent_key="")

    unique_dates = sorted(set(dates))
    unique_times = sorted({value for value in times if value})
    return JsonDates(dates=tuple(unique_dates), times=tuple(unique_times))


def _walk(
    node: Any,
    preferred_format: str | None,
    dates: list[date],
    times: list[str],
    *,
    unavailable: bool,
    parent_key: str,
) -> None:
    """Обойти дерево, собирая даты доступных дней.

    Ключ передаётся вниз по обходу, потому что дата встречается в двух видах:
    значением под своим ключом (`{"date": "..."}`) и элементом списка под
    ключом во множественном числе (`{"dates": ["...", "..."]}`). Без имени
    родителя второй случай неотличим от произвольной строки.
    """
    if isinstance(node, str):
        if unavailable or parent_key in IGNORED_DATE_KEYS or parent_key not in DATE_KEYS:
            return
        parsed = parse_date(node, preferred_format)
        if parsed is not None:
            dates.append(parsed)
        return

    if isinstance(node, list):
        for item in node:
            _walk(
                item,
                preferred_format,
                dates,
                times,
                unavailable=unavailable,
                parent_key=parent_key,
            )
        return

    if not isinstance(node, dict):
        return

    # Недоступность наследуется вниз: если день помечен занятым, вложенные
    # в него времена и вложенные объекты тоже не считаются свободными.
    blocked = unavailable or _is_unavailable(node)

    if not blocked:
        _collect_times(node, times)

    for raw_key, value in node.items():
        _walk(
            value,
            preferred_format,
            dates,
            times,
            unavailable=blocked,
            parent_key=_norm(raw_key),
        )


def extract_from_text(
    body: str, preferred_format: str | None = None
) -> JsonDates:
    """Разобрать тело ответа и вытащить даты.

    Невалидный JSON — не повод падать: ответ мог оказаться HTML-заглушкой
    или страницей проверки, и распознать это должен вызывающий код.
    """
    import json

    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return JsonDates()
    return extract(payload, preferred_format)
