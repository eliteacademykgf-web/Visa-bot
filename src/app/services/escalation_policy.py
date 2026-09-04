"""Политика эскалаций — чистая функция принятия решения.

Вынесена отдельно от работы с БД и Telegram намеренно: это единственное место,
где решается, кого и когда дёргать, и оно должно проверяться тестом без
поднятой инфраструктуры.

Два правила, которые важнее самих порогов:

1. **Пол после возврата задачи.** Порог считается от scheduled_at (важно,
   сколько прошло с момента, когда проверка стала нужна, а не сколько человек
   держал задачу), но эскалация не срабатывает раньше, чем через
   reopen_floor_minutes после reopened_at. Иначе задача, возвращённая после
   уже пройденного порога, эскалируется в ту же секунду, и у сотрудника нет
   ни одной реальной минуты на реакцию.

2. **Схлопывание уровней.** Если к моменту оценки просрочено несколько порогов
   сразу, отправляется только самый высокий. Пройденные уровни записываются
   подавленными. Это касается не только возврата задачи: простой планировщика
   на сорок минут иначе выдал бы по каждой открытой задаче L1, L2 и L3 подряд
   за одну минуту и завалил бы групповой чат пачкой «пропущено» по всем городам.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.timeutils import ensure_aware
from app.enums import EscalationReason


@dataclass(frozen=True, slots=True)
class EscalationDecision:
    """Что делать с задачей на текущем проходе свипа."""

    level: int
    """Уровень, который надо отправить, — самый высокий из просроченных."""

    suppressed_levels: tuple[int, ...]
    """Пройденные уровни, которые отправлять не надо, но надо зафиксировать."""

    reason: EscalationReason

    @property
    def all_levels(self) -> tuple[int, ...]:
        """Все уровни, по которым нужно записать строки."""
        return (*self.suppressed_levels, self.level)


def decide_escalation(
    *,
    scheduled_at: datetime,
    now: datetime,
    thresholds: dict[int, int],
    already_recorded: set[int],
    reopened_at: datetime | None = None,
    reopen_floor_minutes: int = 3,
    reason: EscalationReason = EscalationReason.TIMEOUT,
) -> EscalationDecision | None:
    """Решить, какой уровень эскалации выдать по задаче.

    Вызывающая сторона обязана предварительно убедиться, что задача открыта:
    любой ответ сотрудника переводит её из pending, и до этой функции она
    просто не доходит.

    Возвращает None, если ни один порог не просрочен, все просроченные уже
    зафиксированы, или ещё действует пол после возврата.
    """
    ensure_aware(scheduled_at)
    ensure_aware(now)

    if reopened_at is not None:
        floor = ensure_aware(reopened_at) + timedelta(minutes=reopen_floor_minutes)
        if now < floor:
            return None

    overdue = sorted(
        level
        for level, minutes in thresholds.items()
        if level not in already_recorded and now >= scheduled_at + timedelta(minutes=minutes)
    )
    if not overdue:
        return None

    return EscalationDecision(
        level=overdue[-1],
        suppressed_levels=tuple(overdue[:-1]),
        reason=reason,
    )
