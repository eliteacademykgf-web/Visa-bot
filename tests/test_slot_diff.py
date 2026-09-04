"""Сравнение проверки с предыдущим состоянием (ТЗ §10) и тестовые сценарии §27.

Здесь проверяется главное свойство системы: она молчит, когда ничего
не изменилось, и говорит, когда изменилось. Ошибка в любую сторону
одинаково плоха — поток сообщений сотрудник перестанет читать, а тишина
означает пропущенный слот.
"""

from datetime import date, datetime, timedelta

from app.domain.timeutils import UTC
from app.enums import CheckStatus, ObservedStatus, SlotEventType
from app.services.slot_diff import Observation, PreviousState, compare

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
JULY_31 = date(2026, 7, 31)
JULY_29 = date(2026, 7, 29)
AUGUST_5 = date(2026, 8, 5)


def slots(nearest: date, *rest: date, times: tuple[str, ...] = (), count: int | None = None):
    dates = (nearest, *rest)
    return Observation(
        status=ObservedStatus.SLOTS_PRESENT,
        nearest_date=nearest,
        available_dates=dates,
        available_times=times,
        slots_count=count if count is not None else len(dates),
    )


def no_slots():
    return Observation(status=ObservedStatus.NO_SLOTS)


def state_with(nearest: date, *rest: date, notified: datetime | None = None, **kwargs):
    dates = (nearest, *rest)
    return PreviousState(
        status=CheckStatus.SLOT_AVAILABLE,
        nearest_date=nearest,
        available_dates=dates,
        slots_count=kwargs.get("count", len(dates)),
        available_times=kwargs.get("times", ()),
        last_notified_at=notified,
    )


class TestSpecScenarios:
    """Сценарии из таблицы ТЗ §27 — дословно."""

    def test_1_no_slots_no_notification(self) -> None:
        """№1. Слотов нет -> NO_SLOTS, уведомление не отправляется."""
        result = compare(no_slots(), None, now=NOW)
        assert result.status is CheckStatus.NO_SLOTS
        assert result.should_notify is False

    def test_2_slot_appeared_notifies(self) -> None:
        """№2. Появилась дата -> SLOT_AVAILABLE, отправляется уведомление."""
        previous = PreviousState(status=CheckStatus.NO_SLOTS)
        result = compare(slots(JULY_31), previous, now=NOW)
        assert result.status is CheckStatus.SLOT_AVAILABLE
        assert result.events == (SlotEventType.APPEARED,)
        assert result.new_date == JULY_31
        assert result.is_urgent is True

    def test_3_same_date_again_is_silent(self) -> None:
        """№3. Та же дата повторно -> повторное уведомление НЕ отправляется."""
        previous = state_with(JULY_31, notified=NOW - timedelta(minutes=5))
        result = compare(slots(JULY_31), previous, now=NOW, repeat_notice_minutes=30)
        assert result.status is CheckStatus.SLOT_AVAILABLE
        assert result.should_notify is False

    def test_4_earlier_date_is_urgent(self) -> None:
        """№4. Появилась более ранняя дата -> SLOT_CHANGED, срочное уведомление."""
        previous = state_with(JULY_31, notified=NOW)
        result = compare(slots(JULY_29), previous, now=NOW)
        assert result.status is CheckStatus.SLOT_CHANGED
        assert SlotEventType.DATE_CHANGED in result.events
        assert result.previous_date == JULY_31
        assert result.new_date == JULY_29
        assert result.details["earlier"] is True
        assert result.is_urgent is True

    def test_5_date_disappeared(self) -> None:
        """№5. Дата исчезла -> SLOT_DISAPPEARED."""
        previous = state_with(JULY_31, notified=NOW)
        result = compare(no_slots(), previous, now=NOW)
        assert result.status is CheckStatus.SLOT_DISAPPEARED
        assert result.events == (SlotEventType.DISAPPEARED,)
        assert result.previous_date == JULY_31

    def test_6_session_expired(self) -> None:
        """№6. Сессия истекла -> AUTH_REQUIRED с уведомлением."""
        observation = Observation(status=ObservedStatus.AUTH_REQUIRED)
        result = compare(observation, None, now=NOW)
        assert result.status is CheckStatus.AUTH_REQUIRED
        # Не ждём трёх ошибок: сессия сама не восстановится.
        assert result.should_notify is True

    def test_7_captcha_stops_and_notifies(self) -> None:
        """№7. Появилась CAPTCHA -> остановка сценария и уведомление."""
        observation = Observation(status=ObservedStatus.CAPTCHA_REQUIRED)
        result = compare(observation, None, now=NOW)
        assert result.status is CheckStatus.CAPTCHA_REQUIRED
        assert result.status.stops_monitoring is True
        assert result.should_notify is True

    def test_8_interface_changed(self) -> None:
        """№8. Изменился интерфейс -> SITE_CHANGED."""
        observation = Observation(
            status=ObservedStatus.SITE_CHANGED, error_text="не найден календарь"
        )
        result = compare(observation, None, now=NOW)
        assert result.status is CheckStatus.SITE_CHANGED


class TestNoiseSuppression:
    """Защита от повторяющихся уведомлений (ТЗ §10)."""

    def test_silence_while_nothing_changes(self) -> None:
        """Слот висит час — сотрудника не дёргают на каждой проверке."""
        previous = state_with(JULY_31, notified=NOW)
        sent = 0
        moment = NOW
        # Проверка каждые 10 минут в течение часа.
        for _ in range(6):
            moment += timedelta(minutes=10)
            result = compare(
                slots(JULY_31), previous, now=moment, repeat_notice_minutes=30
            )
            if result.should_notify:
                sent += 1
                previous = state_with(JULY_31, notified=moment)
        # Ровно два напоминания за час при интервале 30 минут, а не шесть.
        assert sent == 2

    def test_repeat_after_interval(self) -> None:
        """ТЗ §10: сотрудник мог не увидеть первое сообщение."""
        previous = state_with(JULY_31, notified=NOW - timedelta(minutes=31))
        result = compare(slots(JULY_31), previous, now=NOW, repeat_notice_minutes=30)
        assert result.events == (SlotEventType.STILL_AVAILABLE,)
        # Это напоминание, а не находка: срочности нет.
        assert result.is_urgent is False

    def test_repeat_disabled_by_zero(self) -> None:
        previous = state_with(JULY_31, notified=NOW - timedelta(days=1))
        result = compare(slots(JULY_31), previous, now=NOW, repeat_notice_minutes=0)
        assert result.should_notify is False

    def test_never_notified_repeats_immediately(self) -> None:
        """Состояние есть, уведомления не было — значит его надо отправить."""
        previous = state_with(JULY_31, notified=None)
        result = compare(slots(JULY_31), previous, now=NOW)
        assert result.events == (SlotEventType.STILL_AVAILABLE,)


class TestChangeDetection:
    def test_new_dates_in_list(self) -> None:
        previous = state_with(JULY_31, notified=NOW)
        result = compare(slots(JULY_31, AUGUST_5), previous, now=NOW)
        assert result.status is CheckStatus.SLOT_CHANGED
        assert SlotEventType.NEW_DATES in result.events
        assert result.details["new_dates"] == [AUGUST_5.isoformat()]

    def test_new_times(self) -> None:
        previous = PreviousState(
            status=CheckStatus.SLOT_AVAILABLE,
            nearest_date=JULY_31,
            available_dates=(JULY_31,),
            available_times=("09:00",),
            slots_count=1,
            last_notified_at=NOW,
        )
        observation = slots(JULY_31, times=("09:00", "14:30"))
        result = compare(observation, previous, now=NOW)
        assert SlotEventType.NEW_TIMES in result.events
        assert result.details["new_times"] == ["14:30"]

    def test_count_increase(self) -> None:
        previous = state_with(JULY_31, count=1, notified=NOW)
        observation = slots(JULY_31, count=4)
        result = compare(observation, previous, now=NOW)
        assert SlotEventType.COUNT_INCREASED in result.events
        assert result.details["count"] == {"was": 1, "now": 4}

    def test_count_decrease_is_not_an_event(self) -> None:
        """Уменьшение количества — не новость: слоты разбирают постоянно."""
        previous = state_with(JULY_31, count=5, notified=NOW)
        result = compare(slots(JULY_31, count=2), previous, now=NOW)
        assert result.should_notify is False

    def test_later_date_still_notifies(self) -> None:
        """Дата сдвинулась дальше — это тоже изменение, но не срочное."""
        previous = state_with(JULY_29, notified=NOW)
        result = compare(slots(JULY_31), previous, now=NOW)
        assert result.status is CheckStatus.SLOT_CHANGED
        assert result.details["earlier"] is False

    def test_date_change_is_the_primary_event(self) -> None:
        """Текст уведомления выбирается по главному событию."""
        previous = state_with(JULY_31, notified=NOW)
        result = compare(slots(JULY_29, AUGUST_5), previous, now=NOW)
        assert result.primary_event is SlotEventType.DATE_CHANGED


class TestErrors:
    def test_single_network_error_is_quiet(self) -> None:
        """Одна сетевая ошибка не повод будить администратора (ТЗ §20)."""
        observation = Observation(status=ObservedStatus.SYSTEM_ERROR, error_text="timeout")
        result = compare(observation, None, now=NOW, error_notice_after=3)
        assert result.status is CheckStatus.SYSTEM_ERROR
        assert result.should_notify is False
        assert result.details["consecutive_errors"] == 1

    def test_third_error_notifies(self) -> None:
        previous = PreviousState(status=CheckStatus.SYSTEM_ERROR, consecutive_errors=2)
        observation = Observation(status=ObservedStatus.SYSTEM_ERROR, error_text="timeout")
        result = compare(observation, previous, now=NOW, error_notice_after=3)
        assert result.events == (SlotEventType.ERROR,)
        assert result.details["consecutive_errors"] == 3

    def test_blocked_notifies_immediately(self) -> None:
        """Блокировка не рассасывается сама — молчать про неё нельзя."""
        observation = Observation(status=ObservedStatus.ACCESS_BLOCKED)
        result = compare(observation, None, now=NOW, error_notice_after=3)
        assert result.should_notify is True

    def test_recovery_after_error(self) -> None:
        """ТЗ §10: «восстановилась работа после ошибки» — тоже событие."""
        previous = PreviousState(status=CheckStatus.SYSTEM_ERROR, consecutive_errors=4)
        result = compare(no_slots(), previous, now=NOW)
        assert result.status is CheckStatus.NO_SLOTS
        assert result.events == (SlotEventType.RECOVERED,)

    def test_slot_after_error_is_appearance_not_recovery(self) -> None:
        """Слот важнее факта восстановления: сообщаем о находке."""
        previous = PreviousState(status=CheckStatus.AUTH_REQUIRED, consecutive_errors=1)
        result = compare(slots(JULY_31), previous, now=NOW)
        assert result.events == (SlotEventType.APPEARED,)

    def test_error_does_not_look_like_disappearance(self) -> None:
        """Ошибка не должна выдавать ложное «слот исчез»."""
        previous = state_with(JULY_31, notified=NOW)
        observation = Observation(status=ObservedStatus.SYSTEM_ERROR, error_text="timeout")
        result = compare(observation, previous, now=NOW)
        assert result.status is CheckStatus.SYSTEM_ERROR
        assert SlotEventType.DISAPPEARED not in result.events


class TestFirstEverCheck:
    def test_first_check_without_slots_is_silent(self) -> None:
        assert compare(no_slots(), None, now=NOW).should_notify is False

    def test_first_check_with_slots_notifies(self) -> None:
        result = compare(slots(JULY_31), None, now=NOW)
        assert result.events == (SlotEventType.APPEARED,)
