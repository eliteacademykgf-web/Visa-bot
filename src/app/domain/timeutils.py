"""Работа со временем и таймзонами.

Единственное место в системе, где происходит преобразование между UTC и
локальным временем города. Правила:

* в БД всё в UTC (timestamptz), в коде — только aware datetime;
* time-поля (рабочие часы, интервалы дежурств) — локальное время города;
* локальное время получается ТОЛЬКО через City.timezone, никогда через
  таймзону хоста: разработка ведётся из Бишкека (UTC+6), города — UTC+5,
  и захардкоженный сдвиг дал бы правдоподобное, но неверное расписание.

Наивный datetime не принимается ни одной функцией: молчаливая интерпретация
такого значения как локального времени хоста — источник ошибки ровно в час,
которую невозможно заметить глазом.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = [
    "UTC",
    "NaiveDatetimeError",
    "city_date",
    "city_now",
    "city_time",
    "city_tz",
    "city_weekday",
    "combine_city",
    "ensure_aware",
    "format_for_city",
    "is_within_work_hours",
    "next_work_window_start",
    "parse_user_date",
    "to_city",
    "to_utc",
    "utcnow",
]


class NaiveDatetimeError(ValueError):
    """Передан datetime без таймзоны."""


def utcnow() -> datetime:
    """Текущий момент в UTC (aware)."""
    return datetime.now(tz=UTC)


def ensure_aware(value: datetime) -> datetime:
    """Проверить, что datetime aware. Наивное значение — ошибка, не догадка."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise NaiveDatetimeError(f"naive datetime is not allowed: {value!r}")
    return value


def city_tz(tz_name: str) -> ZoneInfo:
    """Таймзона города по имени IANA."""
    return ZoneInfo(tz_name)


def to_city(moment: datetime, tz_name: str) -> datetime:
    """UTC -> локальное время города."""
    return ensure_aware(moment).astimezone(city_tz(tz_name))


def to_utc(moment: datetime) -> datetime:
    """Любой aware datetime -> UTC."""
    return ensure_aware(moment).astimezone(UTC)


def city_now(tz_name: str) -> datetime:
    """Текущий момент в локальном времени города."""
    return utcnow().astimezone(city_tz(tz_name))


def city_date(moment: datetime, tz_name: str) -> date:
    """Локальная дата города для момента времени.

    Нужна при проверке отсутствий: отпуск задан датами, и «сегодня» для
    сотрудника определяется городом дежурства, а не сервером.
    """
    return to_city(moment, tz_name).date()


def city_weekday(moment: datetime, tz_name: str) -> int:
    """День недели по ISO (1 = понедельник ... 7 = воскресенье) в городе."""
    return to_city(moment, tz_name).isoweekday()


def city_time(moment: datetime, tz_name: str) -> time:
    """Локальное время суток в городе."""
    return to_city(moment, tz_name).timetz().replace(tzinfo=None)


def combine_city(day: date, moment_time: time, tz_name: str) -> datetime:
    """Локальные дата и время города -> момент в UTC.

    В Казахстане перевода часов нет, но функция обязана оставаться корректной
    при смене правил зоны: fold=0 разрешает неоднозначный час в пользу первого
    вхождения, что делает результат детерминированным.
    """
    local = datetime.combine(day, moment_time, tzinfo=city_tz(tz_name)).replace(fold=0)
    return local.astimezone(UTC)


def is_within_work_hours(
    moment: datetime,
    tz_name: str,
    work_start: time,
    work_end: time,
    work_days: list[int],
) -> bool:
    """Попадает ли момент в рабочее окно города.

    Окно не переходит через полночь — это гарантирует CHECK-ограничение
    work_start < work_end на таблице городов.
    """
    local = to_city(moment, tz_name)
    if local.isoweekday() not in work_days:
        return False
    local_time = local.time()
    return work_start <= local_time < work_end


def next_work_window_start(
    moment: datetime,
    tz_name: str,
    work_start: time,
    work_days: list[int],
    horizon_days: int = 14,
) -> datetime | None:
    """Начало ближайшего рабочего окна не раньше moment (в UTC).

    Используется, чтобы не создавать задачу «в никуда» на ночь и корректно
    возобновлять мониторинг утром. None — если в горизонте нет рабочих дней
    (например, у города пустой work_days).
    """
    ensure_aware(moment)
    if not work_days:
        return None
    local = to_city(moment, tz_name)
    for offset in range(horizon_days + 1):
        day = (local + timedelta(days=offset)).date()
        candidate_local = datetime.combine(day, work_start, tzinfo=city_tz(tz_name))
        if candidate_local.isoweekday() not in work_days:
            continue
        candidate = candidate_local.astimezone(UTC)
        if candidate >= moment:
            return candidate
    return None


def format_for_city(moment: datetime, tz_name: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Отображение времени пользователю — всегда в таймзоне города."""
    return to_city(moment, tz_name).strftime(fmt)


def parse_user_date(raw: str, today: date) -> date:
    """Разобрать дату, введённую сотрудником: ДД.ММ.ГГГГ или ДД.ММ.

    Для короткой формы год выбирается так, чтобы дата не оказалась в прошлом:
    «05.01», введённое в декабре, — это следующий год, и это подавляющее
    большинство реальных случаев (слоты выкладывают вперёд).
    """
    text = raw.strip().replace("/", ".").replace("-", ".").replace(" ", ".")
    parts = [part for part in text.split(".") if part]

    if not all(part.isdigit() for part in parts):
        raise ValueError(f"unrecognized date format: {raw!r}")

    if len(parts) == 3:
        day, month = int(parts[0]), int(parts[1])
        year = int(parts[2])
        if year < 100:
            year += 2000
        return date(year, month, day)

    if len(parts) == 2:
        day, month = int(parts[0]), int(parts[1])
        candidate = date(today.year, month, day)
        if candidate < today:
            candidate = date(today.year + 1, month, day)
        return candidate

    raise ValueError(f"unrecognized date format: {raw!r}")
