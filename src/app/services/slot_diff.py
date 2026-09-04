"""Сравнение результата проверки с предыдущим состоянием (ТЗ §5, §10).

Это ядро всей системы. От него зависит, получит ли сотрудник осмысленное
сообщение или поток «слотов нет» каждые пять минут, после которого он
перестанет читать уведомления вовсе — и пропустит настоящий слот.

Функция чистая: на вход — наблюдение парсера и предыдущее состояние,
на выход — итоговый статус и список событий. Ни БД, ни Telegram, ни времени
из системных часов. Так правила, по которым дёргают живых людей, можно
проверить тестом за миллисекунды.

Два разных вопроса, которые важно не смешивать:

1. **Что изменилось** — SLOT_AVAILABLE, SLOT_CHANGED, SLOT_DISAPPEARED.
   Определяется сравнением с прошлой картиной.
2. **Надо ли уведомлять** — да при любом изменении, а при неизменной картине
   только если истёк интервал повторного напоминания. ТЗ §10 требует
   повторять, потому что слот может оставаться доступным, а сотрудник —
   не увидеть первое сообщение.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app.domain.timeutils import ensure_aware
from app.enums import CheckStatus, ObservedStatus, SlotEventType


@dataclass(frozen=True, slots=True)
class Observation:
    """Что парсер увидел на странице за одну проверку."""

    status: ObservedStatus
    nearest_date: date | None = None
    available_dates: tuple[date, ...] = ()
    available_times: tuple[str, ...] = ()
    slots_count: int | None = None
    site_message: str | None = None
    error_text: str | None = None

    @property
    def has_slots(self) -> bool:
        return self.status is ObservedStatus.SLOTS_PRESENT

    @property
    def is_error(self) -> bool:
        return self.status not in (ObservedStatus.NO_SLOTS, ObservedStatus.SLOTS_PRESENT)


@dataclass(frozen=True, slots=True)
class PreviousState:
    """Предыдущая известная картина по цели.

    None вместо объекта означает, что цель проверяется впервые.
    """

    status: CheckStatus
    nearest_date: date | None = None
    available_dates: tuple[date, ...] = ()
    available_times: tuple[str, ...] = ()
    slots_count: int | None = None
    last_notified_at: datetime | None = None
    consecutive_errors: int = 0

    @property
    def had_slots(self) -> bool:
        return self.status.is_slot_present


@dataclass(frozen=True, slots=True)
class DiffResult:
    """Итог сравнения: какой статус записать и что произошло."""

    status: CheckStatus
    events: tuple[SlotEventType, ...] = ()
    previous_date: date | None = None
    new_date: date | None = None
    details: dict[str, object] = field(default_factory=dict)

    @property
    def should_notify(self) -> bool:
        """Есть ли повод отправить сообщение."""
        return bool(self.events)

    @property
    def is_urgent(self) -> bool:
        """Слот появился или стал ближе — это срочно (ТЗ §9)."""
        return any(event.is_urgent for event in self.events)

    @property
    def primary_event(self) -> SlotEventType | None:
        """Главное событие — по нему выбирается текст уведомления."""
        return self.events[0] if self.events else None


_OBSERVED_TO_STATUS = {
    ObservedStatus.AUTH_REQUIRED: CheckStatus.AUTH_REQUIRED,
    ObservedStatus.CAPTCHA_REQUIRED: CheckStatus.CAPTCHA_REQUIRED,
    ObservedStatus.ACCESS_BLOCKED: CheckStatus.ACCESS_BLOCKED,
    ObservedStatus.SITE_CHANGED: CheckStatus.SITE_CHANGED,
    ObservedStatus.SYSTEM_ERROR: CheckStatus.SYSTEM_ERROR,
}


def compare(
    observation: Observation,
    previous: PreviousState | None,
    *,
    now: datetime,
    repeat_notice_minutes: int = 30,
    error_notice_after: int = 3,
) -> DiffResult:
    """Сравнить наблюдение с предыдущим состоянием.

    repeat_notice_minutes — через сколько напомнить о том же слоте (ТЗ §10).
    error_notice_after — после скольких ошибок подряд уведомлять админа: одна
    сетевая ошибка не повод будить человека, три подряд — уже повод (ТЗ §20).
    """
    ensure_aware(now)

    if observation.is_error:
        return _compare_error(observation, previous, error_notice_after)

    if observation.has_slots:
        return _compare_with_slots(observation, previous, now, repeat_notice_minutes)

    return _compare_without_slots(previous)


def _compare_error(
    observation: Observation,
    previous: PreviousState | None,
    error_notice_after: int,
) -> DiffResult:
    """Технический сбой.

    Уведомление уходит не с первой ошибки: сеть моргает, и будить админа
    каждый раз — верный способ научить его игнорировать эти сообщения.
    Исключение — блокировка и CAPTCHA: они не рассасываются сами, и чем
    дольше их не видят, тем дольше мониторинг стоит.
    """
    status = _OBSERVED_TO_STATUS[observation.status]
    errors = (previous.consecutive_errors if previous else 0) + 1

    notify = status.stops_monitoring or errors >= error_notice_after
    events = (SlotEventType.ERROR,) if notify else ()

    return DiffResult(
        status=status,
        events=events,
        details={
            "consecutive_errors": errors,
            "error": observation.error_text,
            "site_message": observation.site_message,
            # Статус нужен уведомлению: «сессия истекла» и «моргнула сеть»
            # требуют от администратора разных действий, и текст, одинаковый
            # для обоих случаев, не говорит ему ничего полезного.
            "status": status.value,
        },
    )


def _compare_without_slots(previous: PreviousState | None) -> DiffResult:
    """Слотов нет.

    Событие возникает только если раньше слот был: исчезновение — новость.
    Отсутствие слотов при отсутствии слотов новостью не является, и именно
    это молчание отличает систему от бота, пишущего в чат каждые пять минут.
    """
    if previous is not None and previous.had_slots:
        return DiffResult(
            status=CheckStatus.SLOT_DISAPPEARED,
            events=(SlotEventType.DISAPPEARED,),
            previous_date=previous.nearest_date,
        )

    events: tuple[SlotEventType, ...] = ()
    # Мониторинг восстановился после ошибки — об этом стоит сказать, иначе
    # админ не узнает, что можно перестать чинить.
    if previous is not None and previous.status.is_error:
        events = (SlotEventType.RECOVERED,)

    return DiffResult(status=CheckStatus.NO_SLOTS, events=events)


def _compare_with_slots(
    observation: Observation,
    previous: PreviousState | None,
    now: datetime,
    repeat_notice_minutes: int,
) -> DiffResult:
    """Слот виден. Разбираемся, что именно изменилось."""
    nearest = observation.nearest_date

    # Первая проверка по цели или слот появился после отсутствия/ошибки.
    if previous is None or not previous.had_slots:
        return DiffResult(
            status=CheckStatus.SLOT_AVAILABLE,
            events=(SlotEventType.APPEARED,),
            new_date=nearest,
            details={"dates": [d.isoformat() for d in observation.available_dates]},
        )

    events: list[SlotEventType] = []
    details: dict[str, object] = {}

    # Ближайшая дата изменилась — главное событие, ставим его первым.
    if nearest != previous.nearest_date:
        events.append(SlotEventType.DATE_CHANGED)
        details["earlier"] = bool(
            nearest and previous.nearest_date and nearest < previous.nearest_date
        )

    new_dates = tuple(d for d in observation.available_dates if d not in previous.available_dates)
    if new_dates:
        events.append(SlotEventType.NEW_DATES)
        details["new_dates"] = [d.isoformat() for d in new_dates]

    new_times = tuple(t for t in observation.available_times if t not in previous.available_times)
    if new_times:
        events.append(SlotEventType.NEW_TIMES)
        details["new_times"] = list(new_times)

    if (
        observation.slots_count is not None
        and previous.slots_count is not None
        and observation.slots_count > previous.slots_count
    ):
        events.append(SlotEventType.COUNT_INCREASED)
        details["count"] = {"was": previous.slots_count, "now": observation.slots_count}

    if events:
        return DiffResult(
            status=CheckStatus.SLOT_CHANGED,
            events=tuple(events),
            previous_date=previous.nearest_date,
            new_date=nearest,
            details=details,
        )

    # Картина не изменилась. Напоминаем, только если истёк интервал: слот
    # может висеть часами, а сотрудник — не увидеть первое сообщение.
    if _repeat_is_due(previous.last_notified_at, now, repeat_notice_minutes):
        return DiffResult(
            status=CheckStatus.SLOT_AVAILABLE,
            events=(SlotEventType.STILL_AVAILABLE,),
            new_date=nearest,
            details={"repeat_after_minutes": repeat_notice_minutes},
        )

    return DiffResult(status=CheckStatus.SLOT_AVAILABLE, new_date=nearest)


def _repeat_is_due(
    last_notified_at: datetime | None, now: datetime, repeat_notice_minutes: int
) -> bool:
    """Пора ли повторить напоминание о неизменившемся слоте."""
    if repeat_notice_minutes <= 0:
        return False
    if last_notified_at is None:
        return True
    return now - ensure_aware(last_notified_at) >= timedelta(minutes=repeat_notice_minutes)
