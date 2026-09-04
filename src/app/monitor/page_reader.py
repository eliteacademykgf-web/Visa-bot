"""Распознавание состояния страницы и разбор дат.

Вынесено из браузерного модуля намеренно: это чистые функции над текстом
и списками строк, и они проверяются тестами на сохранённых HTML-фикстурах,
без запуска Playwright. Именно здесь живёт логика «что означает то, что мы
увидели», и она обязана быть проверяемой.
"""

from dataclasses import dataclass
from datetime import date, datetime

from app.enums import ObservedStatus
from app.logging import get_logger
from app.monitor.selectors import SelectorConfig

log = get_logger(__name__)

# Форматы дат, которые встречаются на страницах VFS и в их API. Порядок важен:
# 31-07-2026 и 2026-07-31 различимы, а 07-31-2026 — нет, поэтому американский
# формат сознательно не поддерживается: молча перепутанные день и месяц хуже
# отказа разобрать дату.
DATE_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%d %B %Y", "%d %b %Y")


@dataclass(frozen=True, slots=True)
class PageSignal:
    """Итог распознавания состояния страницы."""

    status: ObservedStatus
    message: str | None = None


def detect_status(page_text: str, config: SelectorConfig) -> PageSignal | None:
    """Определить состояние по тексту страницы.

    None означает «явных признаков нет» — тогда решение принимается по тому,
    удалось ли найти календарь. Пустой текст страницы это не «слотов нет»,
    а признак того, что страница не загрузилась: возвращается SITE_CHANGED.
    """
    if not page_text.strip():
        return PageSignal(ObservedStatus.SITE_CHANGED, "пустая страница")

    matched = config.signals.match(page_text)
    if matched is None:
        return None

    mapping = {
        "blocked": ObservedStatus.ACCESS_BLOCKED,
        "captcha": ObservedStatus.CAPTCHA_REQUIRED,
        "auth_required": ObservedStatus.AUTH_REQUIRED,
        "no_slots": ObservedStatus.NO_SLOTS,
    }
    return PageSignal(mapping[matched], _extract_message(page_text, matched))


def _extract_message(page_text: str, matched: str) -> str:
    """Короткий фрагмент текста вокруг найденного признака — для журнала."""
    for line in page_text.splitlines():
        if line.strip():
            lowered = line.lower()
            if any(
                needle in lowered
                for needle in ("no appointment", "записей", "expired", "denied", "captcha")
            ):
                return line.strip()[:500]
    return matched


def parse_date(raw: str, preferred_format: str | None = None) -> date | None:
    """Разобрать дату из текста страницы.

    Возвращает None вместо исключения: неразобранная дата не должна ронять
    проверку целиком — она превращается в SITE_CHANGED уровнем выше, и это
    честнее, чем упасть или подставить сегодняшнее число.
    """
    text = raw.strip()
    if not text:
        return None

    formats = (preferred_format, *DATE_FORMATS) if preferred_format else DATE_FORMATS
    for fmt in formats:
        if not fmt:
            continue
        try:
            # Разбирается календарная дата, не момент времени: таймзона тут
            # не участвует и добавлять её нечего.
            return datetime.strptime(text, fmt).date()  # noqa: DTZ007
        except ValueError:
            continue
    return None


def parse_dates(values: list[str], preferred_format: str | None = None) -> list[date]:
    """Разобрать список дат, отбросив нераспознанные."""
    parsed = []
    for value in values:
        day = parse_date(value, preferred_format)
        if day is None:
            log.warning("page_reader.unparsed_date", value=value[:64])
            continue
        parsed.append(day)
    return sorted(set(parsed))


def nearest_of(dates: list[date]) -> date | None:
    """Ближайшая доступная дата."""
    return min(dates) if dates else None


def normalise_times(values: list[str]) -> list[str]:
    """Привести список времён к стабильному виду для сравнения.

    Сравнение состояний идёт по этим строкам, поэтому важно, чтобы «09:00»
    и « 09:00 » не считались разными значениями и не порождали ложное
    событие «появилось новое время».
    """
    seen = {value.strip() for value in values if value.strip()}
    return sorted(seen)
