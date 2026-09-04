"""Когда запускать следующую проверку (ТЗ §8, §20).

Чистые функции без БД и системных часов: правила частоты обращения к чужому
сайту — ровно то, что должно проверяться тестом, а не выясняться в проде
по факту блокировки учётной записи.

Правила из ТЗ §8, реализованные здесь:

* базовый интервал настраивается на город и на цель;
* ночью интервал длиннее;
* после находки — короче (слоты выкладывают пачками);
* случайное отклонение 30–90 секунд, чтобы обращения не выстраивались
  в идеально ровную сетку — именно она выдаёт автоматику;
* пауза после серии ошибок;
* длинная остановка при признаках блокировки, БЕЗ смены IP.
"""

import random
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.domain.timeutils import city_time, ensure_aware
from app.enums import CheckStatus

# ТЗ §8: «случайное отклонение — от 30 до 90 секунд».
DEFAULT_JITTER_MIN_SECONDS = 30
DEFAULT_JITTER_MAX_SECONDS = 90

# ТЗ §8: «после 3 ошибок — пауза 15 минут».
DEFAULT_ERROR_THRESHOLD = 3
DEFAULT_ERROR_PAUSE_MINUTES = 15

# ТЗ §8: «после признаков блокировки — остановка на 30–60 минут».
DEFAULT_BLOCK_PAUSE_MINUTES = 45


@dataclass(frozen=True, slots=True)
class IntervalPolicy:
    """Настройки частоты проверок для одной цели."""

    base_minutes: int = 10
    night_minutes: int = 20
    boost_minutes: int = 5
    boost_window_minutes: int = 60
    night_start: time = time(22, 0)
    night_end: time = time(7, 0)
    jitter_min_seconds: int = DEFAULT_JITTER_MIN_SECONDS
    jitter_max_seconds: int = DEFAULT_JITTER_MAX_SECONDS
    error_threshold: int = DEFAULT_ERROR_THRESHOLD
    error_pause_minutes: int = DEFAULT_ERROR_PAUSE_MINUTES
    block_pause_minutes: int = DEFAULT_BLOCK_PAUSE_MINUTES


@dataclass(frozen=True, slots=True)
class NextRun:
    """Когда и почему запланирован следующий запуск."""

    at: datetime
    reason: str
    interval_minutes: int


def is_night(moment: datetime, timezone: str, policy: IntervalPolicy) -> bool:
    """Ночное ли сейчас время в городе.

    Окно переходит через полночь (22:00–07:00), поэтому сравнение
    двустороннее. Время берётся в таймзоне города, а не сервера: иначе
    ночной режим включался бы на час раньше или позже и никто бы
    не заметил — ошибка выглядит правдоподобно.
    """
    local = city_time(moment, timezone)
    if policy.night_start <= policy.night_end:
        return policy.night_start <= local < policy.night_end
    return local >= policy.night_start or local < policy.night_end


def base_interval_minutes(
    moment: datetime,
    timezone: str,
    policy: IntervalPolicy,
    *,
    last_slot_found_at: datetime | None = None,
) -> tuple[int, str]:
    """Интервал без джиттера и причина его выбора."""
    if last_slot_found_at is not None:
        ensure_aware(last_slot_found_at)
        window = timedelta(minutes=policy.boost_window_minutes)
        if moment - last_slot_found_at <= window:
            # Слоты выкладывают пачками: после находки проверяем чаще.
            return policy.boost_minutes, "boost"

    if is_night(moment, timezone, policy):
        return policy.night_minutes, "night"

    return policy.base_minutes, "base"


def apply_jitter(
    interval_minutes: int,
    policy: IntervalPolicy,
    rng: random.Random | None = None,
) -> timedelta:
    """Добавить случайное отклонение к интервалу.

    Джиттер только положительный: он отодвигает проверку, а не приближает.
    Отрицательный уменьшал бы фактический интервал ниже настроенного,
    то есть тайком повышал бы нагрузку на чужой сайт.
    """
    generator = rng or random
    low = max(0, policy.jitter_min_seconds)
    high = max(low, policy.jitter_max_seconds)
    return timedelta(minutes=interval_minutes, seconds=generator.randint(low, high))


def next_run(
    *,
    now: datetime,
    timezone: str,
    policy: IntervalPolicy,
    last_status: CheckStatus | None = None,
    consecutive_errors: int = 0,
    last_slot_found_at: datetime | None = None,
    rng: random.Random | None = None,
) -> NextRun:
    """Рассчитать момент следующей проверки.

    Порядок приоритетов: блокировка перевешивает всё, затем серия ошибок,
    затем обычный расчёт интервала. Так проверка после блокировки не уйдёт
    через пять минут просто потому, что до этого была находка.
    """
    ensure_aware(now)

    # Признаки блокировки, CAPTCHA или потери авторизации: долгая пауза,
    # уведомление админу и НИКАКОЙ смены IP (ТЗ §20).
    if last_status is not None and last_status.stops_monitoring:
        return NextRun(
            at=now + timedelta(minutes=policy.block_pause_minutes),
            reason="blocked",
            interval_minutes=policy.block_pause_minutes,
        )

    if consecutive_errors >= policy.error_threshold:
        return NextRun(
            at=now + timedelta(minutes=policy.error_pause_minutes),
            reason="error_pause",
            interval_minutes=policy.error_pause_minutes,
        )

    minutes, reason = base_interval_minutes(
        now, timezone, policy, last_slot_found_at=last_slot_found_at
    )
    return NextRun(
        at=now + apply_jitter(minutes, policy, rng),
        reason=reason,
        interval_minutes=minutes,
    )


def is_due(
    *,
    now: datetime,
    last_check_at: datetime | None,
    next_planned_at: datetime | None,
) -> bool:
    """Пора ли запускать проверку.

    Цель без единой проверки запускается сразу. Плановое время хранится
    в состоянии, поэтому перезапуск процесса ничего не сбивает: свип просто
    видит, что срок наступил.
    """
    ensure_aware(now)
    if last_check_at is None:
        return True
    if next_planned_at is None:
        return True
    return now >= ensure_aware(next_planned_at)
