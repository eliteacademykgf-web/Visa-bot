"""Частота обращений к сайту (ТЗ §8, §20).

Это правила поведения по отношению к чужому сервису. Ошибка здесь не роняет
систему — она приводит к блокировке учётной записи агентства, и узнать
об этом можно только постфактум. Поэтому всё проверяется тестом.
"""

import random
from datetime import time, timedelta

from app.enums import CheckStatus
from app.monitor.schedule import (
    IntervalPolicy,
    apply_jitter,
    base_interval_minutes,
    is_due,
    is_night,
    next_run,
)
from tests.conftest import utc

ALMATY = "Asia/Almaty"  # UTC+5
BISHKEK = "Asia/Bishkek"  # UTC+6 — таймзона машины разработчика

POLICY = IntervalPolicy(
    base_minutes=10,
    night_minutes=20,
    boost_minutes=5,
    boost_window_minutes=60,
    night_start=time(22, 0),
    night_end=time(7, 0),
)

# Фиксированный генератор: джиттер случаен, но тест обязан быть повторяемым.
def rng() -> random.Random:
    return random.Random(42)


class TestNightWindow:
    def test_midday_is_not_night(self) -> None:
        # 07:00 UTC = 12:00 в Алматы.
        assert is_night(utc(2026, 7, 28, 7, 0), ALMATY, POLICY) is False

    def test_after_midnight_is_night(self) -> None:
        # 19:00 UTC = 00:00 следующего дня в Алматы.
        assert is_night(utc(2026, 7, 28, 19, 0), ALMATY, POLICY) is True

    def test_before_midnight_is_night(self) -> None:
        # 17:30 UTC = 22:30 в Алматы.
        assert is_night(utc(2026, 7, 28, 17, 30), ALMATY, POLICY) is True

    def test_night_uses_city_time_not_host(self) -> None:
        """Час разницы между Алматы и Бишкеком не должен теряться.

        16:30 UTC — это 21:30 в Алматы (ещё день) и 22:30 в Бишкеке (уже ночь).
        Если бы расчёт шёл по таймзоне сервера разработки, ночной режим
        включался бы на час раньше, и заметить это было бы почти невозможно.
        """
        moment = utc(2026, 7, 28, 16, 30)
        assert is_night(moment, ALMATY, POLICY) is False
        assert is_night(moment, BISHKEK, POLICY) is True

    def test_night_interval_is_longer(self) -> None:
        minutes, reason = base_interval_minutes(utc(2026, 7, 28, 19, 0), ALMATY, POLICY)
        assert minutes == 20
        assert reason == "night"


class TestBoost:
    def test_recent_find_shortens_the_interval(self) -> None:
        """Слоты выкладывают пачками — после находки проверяем чаще."""
        now = utc(2026, 7, 28, 7, 0)
        minutes, reason = base_interval_minutes(
            now, ALMATY, POLICY, last_slot_found_at=now - timedelta(minutes=10)
        )
        assert minutes == 5
        assert reason == "boost"

    def test_boost_expires(self) -> None:
        now = utc(2026, 7, 28, 7, 0)
        minutes, reason = base_interval_minutes(
            now, ALMATY, POLICY, last_slot_found_at=now - timedelta(minutes=61)
        )
        assert minutes == 10
        assert reason == "base"

    def test_boost_beats_night(self) -> None:
        """Ночью после находки всё равно проверяем часто."""
        now = utc(2026, 7, 28, 19, 0)
        minutes, reason = base_interval_minutes(
            now, ALMATY, POLICY, last_slot_found_at=now - timedelta(minutes=5)
        )
        assert reason == "boost"
        assert minutes == 5


class TestJitter:
    def test_jitter_is_within_spec_range(self) -> None:
        """ТЗ §8: случайное отклонение от 30 до 90 секунд."""
        generator = random.Random(1)
        for _ in range(200):
            delta = apply_jitter(10, POLICY, generator)
            extra = delta - timedelta(minutes=10)
            assert timedelta(seconds=30) <= extra <= timedelta(seconds=90)

    def test_jitter_never_shortens_the_interval(self) -> None:
        """Отрицательный джиттер тайком повышал бы нагрузку на чужой сайт."""
        generator = random.Random(7)
        for _ in range(200):
            assert apply_jitter(10, POLICY, generator) >= timedelta(minutes=10)

    def test_jitter_actually_varies(self) -> None:
        """Ровная сетка обращений — сама по себе признак автоматики."""
        generator = random.Random(3)
        values = {apply_jitter(10, POLICY, generator) for _ in range(50)}
        assert len(values) > 5


class TestPauses:
    def test_block_stops_for_a_long_time(self) -> None:
        """ТЗ §20: при блокировке остановиться, НЕ меняя IP."""
        now = utc(2026, 7, 28, 7, 0)
        plan = next_run(
            now=now,
            timezone=ALMATY,
            policy=POLICY,
            last_status=CheckStatus.ACCESS_BLOCKED,
            rng=rng(),
        )
        assert plan.reason == "blocked"
        assert plan.at == now + timedelta(minutes=POLICY.block_pause_minutes)

    def test_captcha_also_stops(self) -> None:
        """CAPTCHA не обходим — ждём администратора."""
        plan = next_run(
            now=utc(2026, 7, 28, 7, 0),
            timezone=ALMATY,
            policy=POLICY,
            last_status=CheckStatus.CAPTCHA_REQUIRED,
            rng=rng(),
        )
        assert plan.reason == "blocked"

    def test_auth_required_stops(self) -> None:
        plan = next_run(
            now=utc(2026, 7, 28, 7, 0),
            timezone=ALMATY,
            policy=POLICY,
            last_status=CheckStatus.AUTH_REQUIRED,
            rng=rng(),
        )
        assert plan.reason == "blocked"

    def test_three_errors_trigger_a_pause(self) -> None:
        """ТЗ §8: после 3 ошибок — пауза 15 минут."""
        now = utc(2026, 7, 28, 7, 0)
        plan = next_run(
            now=now,
            timezone=ALMATY,
            policy=POLICY,
            last_status=CheckStatus.SYSTEM_ERROR,
            consecutive_errors=3,
            rng=rng(),
        )
        assert plan.reason == "error_pause"
        assert plan.at == now + timedelta(minutes=15)

    def test_two_errors_do_not_pause(self) -> None:
        plan = next_run(
            now=utc(2026, 7, 28, 7, 0),
            timezone=ALMATY,
            policy=POLICY,
            last_status=CheckStatus.SYSTEM_ERROR,
            consecutive_errors=2,
            rng=rng(),
        )
        assert plan.reason == "base"

    def test_block_beats_boost(self) -> None:
        """После блокировки не возвращаемся к пятиминутному интервалу."""
        now = utc(2026, 7, 28, 7, 0)
        plan = next_run(
            now=now,
            timezone=ALMATY,
            policy=POLICY,
            last_status=CheckStatus.ACCESS_BLOCKED,
            last_slot_found_at=now - timedelta(minutes=1),
            rng=rng(),
        )
        assert plan.reason == "blocked"


class TestDue:
    def test_never_checked_runs_immediately(self) -> None:
        assert is_due(now=utc(2026, 7, 28, 7, 0), last_check_at=None, next_planned_at=None) is True

    def test_waits_for_planned_time(self) -> None:
        now = utc(2026, 7, 28, 7, 0)
        assert (
            is_due(
                now=now,
                last_check_at=now - timedelta(minutes=5),
                next_planned_at=now + timedelta(minutes=5),
            )
            is False
        )

    def test_runs_when_planned_time_passed(self) -> None:
        now = utc(2026, 7, 28, 7, 0)
        assert (
            is_due(
                now=now,
                last_check_at=now - timedelta(minutes=15),
                next_planned_at=now - timedelta(seconds=1),
            )
            is True
        )
