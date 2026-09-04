"""Политика эскалаций (ТЗ §11): пороги, схлопывание, пауза после передачи.

Тесты работают с чистой функцией, без БД и Telegram: правила, по которым
дёргают живых людей, должны проверяться быстро и не зависеть ни от чего.

Пороги по ТЗ §11 отсчитываются от отправки уведомления:
    2 минуты  — повторное уведомление сотруднику;
    5 минут   — уведомление руководителю;
    10 минут  — эскалация резервному сотруднику.
"""

from datetime import datetime, timedelta

from app.domain.timeutils import UTC
from app.enums import EscalationReason
from app.services.escalation_policy import decide_escalation

THRESHOLDS = {1: 2, 2: 5, 3: 10}
SENT_AT = datetime(2026, 7, 28, 18, 13, tzinfo=UTC)


def decide(minutes_passed: float, **kwargs: object):
    """Решение через minutes_passed минут после отправки уведомления."""
    return decide_escalation(
        scheduled_at=SENT_AT,
        now=SENT_AT + timedelta(minutes=minutes_passed),
        thresholds=THRESHOLDS,
        already_recorded=kwargs.pop("already_recorded", set()),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


class TestThresholds:
    def test_silent_before_first_threshold(self) -> None:
        """Полторы минуты — сотрудник ещё читает сообщение."""
        assert decide(1.5) is None

    def test_level_1_reminds_the_specialist(self) -> None:
        decision = decide(2)
        assert decision is not None
        assert decision.level == 1
        assert decision.suppressed_levels == ()

    def test_level_2_goes_to_supervisor(self) -> None:
        decision = decide(5, already_recorded={1})
        assert decision is not None
        assert decision.level == 2

    def test_level_3_goes_to_backup(self) -> None:
        decision = decide(10, already_recorded={1, 2})
        assert decision is not None
        assert decision.level == 3

    def test_nothing_when_all_levels_recorded(self) -> None:
        assert decide(60, already_recorded={1, 2, 3}) is None


class TestCollapsing:
    """Схлопывание уровней при одновременной просрочке нескольких порогов.

    Пороги ТЗ короткие — 2, 5 и 10 минут, — поэтому даже недолгий простой
    планировщика просрочивает сразу все три. Без схлопывания руководитель
    и резервный сотрудник получили бы три сообщения об одной проблеме
    за одну минуту.
    """

    def test_outage_yields_single_top_level(self) -> None:
        decision = decide(15)
        assert decision is not None
        # Отправляется только третий уровень...
        assert decision.level == 3
        # ...а первый и второй фиксируются подавленными.
        assert decision.suppressed_levels == (1, 2)
        # Записать нужно все три, иначе уникальный индекс не удержит их
        # от повторного срабатывания на следующем тике.
        assert decision.all_levels == (1, 2, 3)

    def test_partial_outage_collapses_to_level_2(self) -> None:
        decision = decide(6)
        assert decision is not None
        assert decision.level == 2
        assert decision.suppressed_levels == (1,)

    def test_recorded_levels_are_not_suppressed_again(self) -> None:
        decision = decide(15, already_recorded={1, 2})
        assert decision is not None
        assert decision.level == 3
        assert decision.suppressed_levels == ()

    def test_second_sweep_after_outage_is_silent(self) -> None:
        """Повторный проход сразу после восстановления ничего не шлёт."""
        recorded: set[int] = set()
        first = decide(15)
        assert first is not None
        recorded.update(first.all_levels)
        assert decide(16, already_recorded=recorded) is None


class TestQuietPeriodAfterHandover:
    """Пауза после передачи уведомления другому сотруднику (ТЗ §11).

    «Передать другому сотруднику» переназначает уведомление. Пороги
    по-прежнему считаются от исходной отправки — важно, сколько времени
    слот стоит без реакции, а не сколько его держал последний человек, —
    но у нового адресата должна быть хотя бы минута, прежде чем его
    начнут эскалировать за чужое молчание.
    """

    def test_no_escalation_inside_quiet_period(self) -> None:
        handover = SENT_AT + timedelta(minutes=4)
        assert decide(4.2, reopened_at=handover, reopen_floor_minutes=1) is None
        assert decide(4.9, reopened_at=handover, reopen_floor_minutes=1) is None

    def test_escalation_resumes_after_quiet_period(self) -> None:
        handover = SENT_AT + timedelta(minutes=4)
        decision = decide(5.1, reopened_at=handover, reopen_floor_minutes=1)
        assert decision is not None
        assert decision.level == 2

    def test_quiet_period_does_not_reset_the_clock(self) -> None:
        """Якорь остаётся на моменте отправки, а не передачи.

        Через 12 минут после отправки просрочены все пороги: после снятия
        паузы выдаётся сразу третий уровень, а не первый.
        """
        handover = SENT_AT + timedelta(minutes=11)
        decision = decide(12.5, reopened_at=handover, reopen_floor_minutes=1)
        assert decision is not None
        assert decision.level == 3
        assert decision.suppressed_levels == (1, 2)

    def test_no_cascade_after_handover(self) -> None:
        """Передача не должна дать два пинга руководителю подряд."""
        handover = SENT_AT + timedelta(minutes=4)
        recorded: set[int] = set()
        sent: list[int] = []

        # Минута за минутой прогоняем свип, как в реальной работе.
        for tick in range(0, 25):
            minute = tick * 0.5
            decision = decide_escalation(
                scheduled_at=SENT_AT,
                now=SENT_AT + timedelta(minutes=minute),
                thresholds=THRESHOLDS,
                already_recorded=recorded,
                reopened_at=handover,
                reopen_floor_minutes=1,
            )
            if decision is not None:
                sent.append(decision.level)
                recorded.update(decision.all_levels)

        # Каждый уровень ровно один раз и в правильном порядке.
        assert sent == sorted(set(sent))
        assert recorded == {1, 2, 3}


class TestReason:
    def test_reason_is_carried_into_decision(self) -> None:
        decision = decide(2, reason=EscalationReason.HANDOVER)
        assert decision is not None
        assert decision.reason is EscalationReason.HANDOVER
