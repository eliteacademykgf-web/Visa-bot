"""Таймзоны: локальное время города против таймзоны хоста.

============================================================================
ЭТОТ ТЕСТ НЕ УПРОЩАТЬ.
============================================================================

Все пять городов сейчас в UTC+5, поэтому ошибка в таймзонах МЕЖДУ городами
не проявится никогда. Проявится она только относительно машины, с которой
ведётся разработка: Бишкек — UTC+6. Разница ровно в час, и она выглядит
абсолютно правдоподобно: расписание просто начинает работать на час раньше
или позже, никто не замечает месяцами, а по отчёту «находки по времени
суток» принимаются решения о перестройке графика дежурств.

Отсюда декоратор с принудительной подменой TZ на Asia/Bishkek. Он выглядит
избыточным ровно до того дня, когда перестанет им быть. Если этот тест
начнёт мешать в CI — чинить надо код, а не тест.
============================================================================
"""

import os
import time as time_module
from datetime import date, datetime, time, timedelta

import pytest

from app.domain.timeutils import (
    UTC,
    NaiveDatetimeError,
    city_date,
    city_time,
    city_weekday,
    combine_city,
    ensure_aware,
    format_for_city,
    is_within_work_hours,
    next_work_window_start,
    to_city,
)

ALMATY = "Asia/Almaty"  # UTC+5
BISHKEK = "Asia/Bishkek"  # UTC+6 — таймзона машины разработчика


@pytest.fixture(autouse=True)
def _host_tz_is_bishkek():
    """Прогонять весь модуль так, будто хост стоит в Бишкеке.

    Именно эта подмена ловит утечку локальной таймзоны в расчёты.
    """
    previous = os.environ.get("TZ")
    os.environ["TZ"] = BISHKEK
    time_module.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time_module.tzset()


WORK = {"work_start": time(9, 0), "work_end": time(18, 0), "work_days": [1, 2, 3, 4, 5]}


class TestHostTimezoneDoesNotLeak:
    def test_hour_difference_between_host_and_city(self) -> None:
        # 04:30 UTC: 09:30 в Алматы, 10:30 в Бишкеке.
        moment = datetime(2026, 7, 29, 4, 30, tzinfo=UTC)
        assert city_time(moment, ALMATY) == time(9, 30)
        assert city_time(moment, BISHKEK) == time(10, 30)

    def test_work_window_uses_city_not_host(self) -> None:
        # 03:30 UTC = 08:30 в Алматы (ДО начала дня)
        #            = 09:30 в Бишкеке (внутри окна).
        # Если бы использовалась таймзона хоста, проверка началась бы
        # на час раньше и дежурный получал бы задачи до начала смены.
        moment = datetime(2026, 7, 29, 3, 30, tzinfo=UTC)
        assert is_within_work_hours(moment, ALMATY, **WORK) is False
        assert is_within_work_hours(moment, BISHKEK, **WORK) is True

    def test_end_of_day_boundary(self) -> None:
        # 12:30 UTC = 17:30 в Алматы (внутри) = 18:30 в Бишкеке (после).
        moment = datetime(2026, 7, 29, 12, 30, tzinfo=UTC)
        assert is_within_work_hours(moment, ALMATY, **WORK) is True
        assert is_within_work_hours(moment, BISHKEK, **WORK) is False

    def test_weekday_can_differ_across_midnight(self) -> None:
        # 18:30 UTC в воскресенье = 23:30 вс в Алматы, 00:30 пн в Бишкеке.
        moment = datetime(2026, 8, 2, 18, 30, tzinfo=UTC)
        assert city_weekday(moment, ALMATY) == 7
        assert city_weekday(moment, BISHKEK) == 1
        # А значит и рабочий день определяется по-разному.
        assert is_within_work_hours(moment, ALMATY, **WORK) is False

    def test_local_date_differs_across_midnight(self) -> None:
        """Дата важна для отпусков: они заданы датами, а не моментами."""
        moment = datetime(2026, 7, 29, 18, 30, tzinfo=UTC)
        assert city_date(moment, ALMATY) == date(2026, 7, 29)
        assert city_date(moment, BISHKEK) == date(2026, 7, 30)


class TestConversions:
    def test_combine_city_returns_utc(self) -> None:
        moment = combine_city(date(2026, 7, 29), time(9, 0), ALMATY)
        assert moment == datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
        assert moment.utcoffset() == timedelta(0)

    def test_combine_city_differs_by_zone(self) -> None:
        almaty = combine_city(date(2026, 7, 29), time(9, 0), ALMATY)
        bishkek = combine_city(date(2026, 7, 29), time(9, 0), BISHKEK)
        assert almaty - bishkek == timedelta(hours=1)

    def test_round_trip(self) -> None:
        moment = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
        local = to_city(moment, ALMATY)
        assert combine_city(local.date(), local.time(), ALMATY) == moment

    def test_display_is_in_city_time(self) -> None:
        moment = datetime(2026, 7, 29, 4, 30, tzinfo=UTC)
        assert format_for_city(moment, ALMATY, "%H:%M") == "09:30"
        assert format_for_city(moment, BISHKEK, "%H:%M") == "10:30"


class TestNextWorkWindow:
    def test_night_rolls_to_next_morning(self) -> None:
        # 20:00 UTC среды = 01:00 четверга в Алматы.
        moment = datetime(2026, 7, 29, 20, 0, tzinfo=UTC)
        expected = datetime(2026, 7, 30, 4, 0, tzinfo=UTC)  # 09:00 в Алматы
        assert next_work_window_start(moment, ALMATY, time(9, 0), [1, 2, 3, 4, 5]) == expected

    def test_friday_night_rolls_over_the_weekend(self) -> None:
        moment = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)  # пятница
        result = next_work_window_start(moment, ALMATY, time(9, 0), [1, 2, 3, 4, 5])
        assert result == datetime(2026, 8, 3, 4, 0, tzinfo=UTC)  # понедельник
        assert city_weekday(result, ALMATY) == 1

    def test_empty_work_days_returns_none(self) -> None:
        moment = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
        assert next_work_window_start(moment, ALMATY, time(9, 0), []) is None


class TestNaiveRejected:
    def test_naive_datetime_raises(self) -> None:
        """Наивное значение должно падать, а не толковаться по хосту."""
        with pytest.raises(NaiveDatetimeError):
            ensure_aware(datetime(2026, 7, 29, 9, 0))

    def test_conversion_of_naive_raises(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            to_city(datetime(2026, 7, 29, 9, 0), ALMATY)

    def test_work_hours_of_naive_raises(self) -> None:
        with pytest.raises(NaiveDatetimeError):
            is_within_work_hours(datetime(2026, 7, 29, 9, 0), ALMATY, **WORK)
