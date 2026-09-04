"""Уведомления, реакции сотрудников и эскалации (ТЗ §9, §11).

Разделение обязанностей:

* `slot_diff` решает, что изменилось;
* `runner` записывает проверку и создаёт события;
* этот модуль превращает событие в работу для человека и следит,
  чтобы работа не потерялась.

Отправка отделена от записи специально: падение Telegram не должно
откатывать транзакцию с уже выполненной проверкой.
"""

from dataclasses import dataclass
from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Alert,
    AlertEscalation,
    AlertReaction,
    Category,
    City,
    Employee,
    MonitorTarget,
    SlotEvent,
)
from app.domain.timeutils import format_for_city, utcnow
from app.enums import (
    ESCALATION_LEVEL_BACKUP,
    ESCALATION_LEVEL_REMINDER,
    ESCALATION_LEVEL_SUPERVISOR,
    CheckStatus,
    EscalationReason,
    EscalationStatus,
    NotificationKind,
    ReactionKind,
    SlotEventType,
)
from app.logging import get_logger
from app.services import recipients
from app.services.escalation_policy import decide_escalation
from app.services.notifications import Notifier, send_and_log
from app.services.settings_service import RuntimeSettings

log = get_logger(__name__)

# События, которые не требуют реакции человека: их достаточно показать.
_INFORMATIONAL = frozenset(
    {SlotEventType.DISAPPEARED, SlotEventType.ERROR, SlotEventType.RECOVERED}
)

_KIND_BY_EVENT = {
    SlotEventType.APPEARED: NotificationKind.SLOT_ALERT,
    SlotEventType.DATE_CHANGED: NotificationKind.SLOT_CHANGED,
    SlotEventType.NEW_DATES: NotificationKind.SLOT_CHANGED,
    SlotEventType.NEW_TIMES: NotificationKind.SLOT_CHANGED,
    SlotEventType.COUNT_INCREASED: NotificationKind.SLOT_CHANGED,
    SlotEventType.STILL_AVAILABLE: NotificationKind.REMINDER,
    SlotEventType.DISAPPEARED: NotificationKind.SLOT_DISAPPEARED,
    SlotEventType.ERROR: NotificationKind.MONITOR_ERROR,
    SlotEventType.RECOVERED: NotificationKind.MONITOR_RECOVERED,
}

_ESCALATION_KIND = {
    ESCALATION_LEVEL_REMINDER: NotificationKind.ESCALATION_L1,
    ESCALATION_LEVEL_SUPERVISOR: NotificationKind.ESCALATION_L2,
    ESCALATION_LEVEL_BACKUP: NotificationKind.ESCALATION_L3,
}


@dataclass(frozen=True, slots=True)
class ReactionResult:
    """Итог обработки нажатия кнопки."""

    accepted: bool
    message: str
    alert: Alert | None = None


def _human_date(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def _render_error(event: SlotEvent, city: City, when: str) -> str:
    """Текст ошибки мониторинга.

    Разные причины требуют от администратора разных действий, а раньше все
    они сводились к «требуется действие администратора» — сообщению, из
    которого нельзя понять ни что случилось, ни что делать. Хуже того,
    истёкшая сессия и заблокированный доступ лечатся противоположным:
    в первом случае надо войти, во втором — не трогать сайт, пока
    ограничение не снимется само.
    """
    status = str(event.details.get("status") or "")
    label = str(event.details.get("account_label") or "monitor-1")
    detail = event.details.get("error") or event.details.get("site_message") or ""

    if status == CheckStatus.AUTH_REQUIRED.value:
        return (
            "🔑 Сессия VFS истекла\n\n"
            f"Визовый центр: {city.name}\n"
            f"Время: {when}\n\n"
            "Проверки по этой учётной записи остановлены. Нужно один раз войти "
            "вручную — пароль и проверку «я не робот» проходит человек, "
            "автоматически это не делается.\n\n"
            f"python -m app.cli capture-session --label {label}\n\n"
            "После входа мониторинг продолжится сам."
        )

    if status == CheckStatus.CAPTCHA_REQUIRED.value:
        return (
            "🧩 Сайт запросил проверку «я не робот»\n\n"
            f"Визовый центр: {city.name}\n"
            f"Время: {when}\n\n"
            "Проверки остановлены. Пройдите проверку вручную — обходить её "
            "система не будет, именно за это блокируют учётные записи.\n\n"
            f"python -m app.cli capture-session --label {label}"
        )

    if status == CheckStatus.ACCESS_BLOCKED.value:
        return (
            "⛔️ VFS ограничил доступ\n\n"
            f"Визовый центр: {city.name}\n"
            f"Время: {when}\n"
            f"{detail}\n\n"
            "Проверки приостановлены и возобновятся сами. Заходить на сайт "
            "этой учётной записью сейчас не нужно: повторные обращения "
            "превращают временное ограничение в постоянное."
        )

    return (
        "🛑 Ошибка мониторинга VFS Global\n\n"
        f"Визовый центр: {city.name}\n"
        f"Ошибок подряд: {event.details.get('consecutive_errors', '—')}\n"
        f"{detail}\n"
        "Требуется действие администратора."
    )


def render_alert(
    event: SlotEvent, target: MonitorTarget, city: City, category: Category
) -> str:
    """Текст уведомления по образцу ТЗ §9."""
    when = format_for_city(event.created_at or utcnow(), city.timezone, "%d.%m.%Y, %H:%M")

    if event.event_type is SlotEventType.DATE_CHANGED:
        return (
            f"🔁 Дата записи изменилась\n\n"
            f"Визовый центр: {city.name}\n"
            f"Предыдущая дата: {_human_date(event.previous_date)}\n"
            f"Новая дата: {_human_date(event.new_date)}\n"
            f"Время изменения: {when}"
        )

    if event.event_type is SlotEventType.DISAPPEARED:
        return (
            f"⚪️ Слот больше не отображается\n\n"
            f"Визовый центр: {city.name}\n"
            f"Была дата: {_human_date(event.previous_date)}\n"
            f"Время: {when}"
        )

    if event.event_type is SlotEventType.ERROR:
        return _render_error(event, city, when)

    if event.event_type is SlotEventType.RECOVERED:
        return f"✅ Мониторинг восстановлен\n\nВизовый центр: {city.name}\nВремя: {when}"

    header = (
        "🔔 Напоминание: слот всё ещё доступен"
        if event.event_type is SlotEventType.STILL_AVAILABLE
        else "🟢 Найден слот на визу Италии"
    )
    return (
        f"{header}\n\n"
        f"Визовый центр: {city.name}\n"
        f"Категория: {category.name}\n"
        f"Подкатегория: {category.subcategory_name}\n"
        f"Количество заявителей: {target.applicants}\n"
        f"Ближайшая дата: {_human_date(event.new_date)}\n"
        f"Время обнаружения: {when}\n\n"
        f"Необходимо войти в VFS Global и проверить возможность бронирования."
    )


async def create_alert(
    session: AsyncSession,
    event: SlotEvent,
    *,
    now: datetime | None = None,
) -> Alert | None:
    """Создать уведомление по событию.

    Информационные события (слот исчез, ошибка, восстановление) уведомление
    не создают: реагировать на них кнопками не нужно, они просто рассылаются.
    Уникальность event_id гарантирует, что повторный проход свипа не создаст
    второе уведомление по тому же событию.
    """
    if event.event_type in _INFORMATIONAL:
        return None

    assignee = await recipients.first_available(session)
    alert = Alert(
        event_id=event.id,
        assignee_id=assignee.id if assignee else None,
        created_at=now or utcnow(),
    )
    session.add(alert)
    await session.flush()
    log.info(
        "alert.created",
        alert_id=alert.id,
        event_id=event.id,
        event_type=event.event_type.value,
        assignee_id=alert.assignee_id,
    )
    return alert


async def dispatch_event(
    session: AsyncSession,
    notifier: Notifier,
    event: SlotEvent,
    settings: RuntimeSettings,
    *,
    now: datetime | None = None,
) -> Alert | None:
    """Разослать событие: адресату, в групповой чат, при ошибке — админам."""
    moment = now or utcnow()
    target = await session.get(MonitorTarget, event.target_id)
    if target is None:
        return None
    city = await session.get(City, target.city_id)
    category = await session.get(Category, target.category_id)
    if city is None or category is None:
        return None

    text = render_alert(event, target, city, category)
    kind = _KIND_BY_EVENT.get(event.event_type, NotificationKind.SYSTEM)
    alert = await create_alert(session, event, now=moment)

    # Ошибки мониторинга — адресно администраторам, а не всем подряд:
    # визовому специалисту нечего делать с сообщением про CAPTCHA.
    if event.event_type in (SlotEventType.ERROR, SlotEventType.RECOVERED):
        for admin in await recipients.admins(session):
            await send_and_log(
                session,
                notifier,
                chat_id=admin.telegram_id or 0,
                kind=kind,
                text=text,
                employee_id=admin.id,
            )
        return None

    if alert is not None and alert.assignee_id is not None:
        assignee = await session.get(Employee, alert.assignee_id)
        if assignee is not None and assignee.telegram_id:
            result = await send_and_log(
                session,
                notifier,
                chat_id=assignee.telegram_id,
                kind=kind,
                text=text,
                employee_id=assignee.id,
                alert_id=alert.id,
                with_task_buttons=alert.id,
            )
            if result.delivered:
                alert.sent_at = moment
            else:
                # Недоставленное уведомление эскалируется сразу: ждать двух
                # минут, зная, что сообщение не дошло, бессмысленно.
                await _escalate_now(
                    session, notifier, alert, text, EscalationReason.UNDELIVERED, moment
                )
    elif alert is not None:
        # Получателей нет вовсе — это авария, а не рядовая ситуация.
        await _escalate_now(
            session, notifier, alert, text, EscalationReason.NO_RECIPIENT, moment
        )

    # Групповой чат получает копию находки (ТЗ §9).
    if settings.group_chat_id is not None:
        await send_and_log(
            session,
            notifier,
            chat_id=settings.group_chat_id,
            kind=kind,
            text=text,
            alert_id=alert.id if alert else None,
        )

    await session.flush()
    return alert


async def record_reaction(
    session: AsyncSession,
    alert_id: int,
    employee: Employee,
    kind: ReactionKind,
    *,
    comment: str | None = None,
    now: datetime | None = None,
) -> ReactionResult:
    """Записать реакцию сотрудника (ТЗ §11).

    Повторное нажатие той же кнопки тем же человеком отвергается уникальным
    индексом; здесь оно распознаётся заранее и получает понятный ответ,
    а не молчание.
    """
    moment = now or utcnow()
    alert = (
        await session.execute(
            sa.select(Alert).where(Alert.id == alert_id).with_for_update()
        )
    ).scalar_one_or_none()
    if alert is None:
        return ReactionResult(False, "Уведомление не найдено.")

    if alert.closed_at is not None:
        return ReactionResult(
            False, f"Уведомление №{alert.id} уже закрыто.", alert=alert
        )

    duplicate = (
        await session.execute(
            sa.select(sa.literal(1))
            .select_from(AlertReaction)
            .where(
                AlertReaction.alert_id == alert_id,
                AlertReaction.employee_id == employee.id,
                AlertReaction.kind == kind,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        return ReactionResult(False, "Эта реакция уже записана.", alert=alert)

    anchor = alert.sent_at or alert.created_at
    session.add(
        AlertReaction(
            alert_id=alert.id,
            employee_id=employee.id,
            kind=kind,
            comment=comment,
            reacted_at=moment,
            seconds_from_alert=max(0, int((moment - anchor).total_seconds())),
        )
    )

    if alert.first_reaction_at is None:
        alert.first_reaction_at = moment

    if kind is ReactionKind.BOOKED:
        # ТЗ §11 требует отдельно время от уведомления до бронирования.
        alert.booked_at = moment
    if kind is ReactionKind.HANDOVER:
        alert.handover_count += 1
        successor = await recipients.first_available(
            session, exclude=frozenset({employee.id})
        )
        alert.assignee_id = successor.id if successor else None
        # Пауза перед эскалацией отсчитывается от передачи: у нового
        # адресата должно быть время среагировать.
        alert.sent_at = alert.sent_at or moment
    if kind.is_terminal:
        alert.closed_at = moment

    await session.flush()
    log.info(
        "alert.reaction",
        alert_id=alert.id,
        employee_id=employee.id,
        kind=kind.value,
        seconds=(moment - anchor).total_seconds(),
        closed=alert.closed_at is not None,
    )
    return ReactionResult(True, _reaction_message(kind, alert), alert=alert)


def _reaction_message(kind: ReactionKind, alert: Alert) -> str:
    return {
        ReactionKind.ACCEPTED: "Принято. Зафиксировано время реакции.",
        ReactionKind.CHECKING: "Отмечено: проверяешь.",
        ReactionKind.BOOKED: "Отлично, бронирование зафиксировано.",
        ReactionKind.GONE: "Записано: слот уже исчез.",
        ReactionKind.FALSE_POSITIVE: "Записано как ложное срабатывание.",
        ReactionKind.HANDOVER: "Уведомление передано другому сотруднику.",
    }[kind]


async def recorded_levels(session: AsyncSession, alert_id: int) -> set[int]:
    """Уровни, уже зафиксированные по уведомлению."""
    rows = await session.execute(
        sa.select(AlertEscalation.level).where(AlertEscalation.alert_id == alert_id)
    )
    return set(rows.scalars().all())


async def _record_level(
    session: AsyncSession,
    alert_id: int,
    level: int,
    reason: EscalationReason,
    status: EscalationStatus,
    *,
    people: list[Employee] | None = None,
    note: str | None = None,
) -> None:
    session.add(
        AlertEscalation(
            alert_id=alert_id,
            level=level,
            reason=reason,
            status=status,
            recipients=[{"employee_id": person.id} for person in (people or [])],
            note=note,
        )
    )
    await session.flush()


async def _escalate_now(
    session: AsyncSession,
    notifier: Notifier,
    alert: Alert,
    text: str,
    reason: EscalationReason,
    moment: datetime,
) -> None:
    """Немедленная эскалация руководителю, минуя пороги."""
    if ESCALATION_LEVEL_SUPERVISOR in await recorded_levels(session, alert.id):
        return
    people = await recipients.supervisors(session)
    for person in people:
        await send_and_log(
            session,
            notifier,
            chat_id=person.telegram_id or 0,
            kind=NotificationKind.ESCALATION_L2,
            text=f"⚠️ Уведомление не доставлено адресату.\n\n{text}",
            employee_id=person.id,
            alert_id=alert.id,
            with_task_buttons=alert.id,
        )
    await _record_level(
        session,
        alert.id,
        ESCALATION_LEVEL_SUPERVISOR,
        reason,
        EscalationStatus.SENT,
        people=people,
        note="эскалация по факту недоставки, без ожидания порога",
    )


async def sweep_escalations(
    session: AsyncSession,
    notifier: Notifier,
    settings: RuntimeSettings,
    now: datetime | None = None,
) -> int:
    """Выдать просроченные эскалации по открытым уведомлениям (ТЗ §11).

    Фильтр по closed_at и есть механизм остановки: терминальная реакция
    закрывает уведомление, и оно перестаёт попадать в выборку. Отменять
    отложенные джобы не нужно — их нет.
    """
    moment = now or utcnow()
    alerts = (
        await session.execute(
            sa.select(Alert)
            .where(Alert.closed_at.is_(None), Alert.sent_at.is_not(None))
            .order_by(Alert.sent_at)
        )
    ).scalars().all()

    applied = 0
    for alert in alerts:
        # Любая реакция, кроме передачи, останавливает эскалацию: человек
        # уведомление увидел и взял в работу.
        if alert.first_reaction_at is not None and alert.handover_count == 0:
            continue

        decision = decide_escalation(
            scheduled_at=alert.sent_at or alert.created_at,
            now=moment,
            thresholds=settings.escalation_thresholds,
            already_recorded=await recorded_levels(session, alert.id),
            reopened_at=alert.sent_at if alert.handover_count else None,
            reopen_floor_minutes=1,
        )
        if decision is None:
            continue

        await _apply_escalation(session, notifier, alert, decision, moment)
        applied += 1
    return applied


async def _apply_escalation(
    session: AsyncSession,
    notifier: Notifier,
    alert: Alert,
    decision: object,
    moment: datetime,
) -> None:
    """Отправить решённый уровень и зафиксировать подавленные."""
    level = decision.level  # type: ignore[attr-defined]
    suppressed = decision.suppressed_levels  # type: ignore[attr-defined]
    reason = decision.reason  # type: ignore[attr-defined]

    # Подавленные пишутся ДО отправки: падение между записью и отправкой
    # не должно привести к их повторной выдаче.
    for skipped in suppressed:
        await _record_level(
            session,
            alert.id,
            skipped,
            reason,
            EscalationStatus.SUPPRESSED,
            note="подавлен: одновременно просрочен более высокий уровень",
        )

    event = await session.get(SlotEvent, alert.event_id)
    target = await session.get(MonitorTarget, event.target_id) if event else None
    city = await session.get(City, target.city_id) if target else None
    where = city.name if city else "—"
    sent_text = (
        format_for_city(alert.sent_at or alert.created_at, city.timezone) if city else "—"
    )
    summary = f"Уведомление №{alert.id} · {where}\nОтправлено: {sent_text}"

    people: list[Employee | None] = []
    if level == ESCALATION_LEVEL_REMINDER:
        people = (
            [await session.get(Employee, alert.assignee_id)] if alert.assignee_id else []
        )
    elif level == ESCALATION_LEVEL_SUPERVISOR:
        people = list(await recipients.supervisors(session))
    else:
        # Третий уровень — резервный сотрудник: любой доступный, кроме того,
        # кто уже молчит.
        exclude = frozenset({alert.assignee_id}) if alert.assignee_id else frozenset()
        backup = await recipients.first_available(session, exclude=exclude)
        people = [backup] if backup else list(await recipients.supervisors(session))

    sent_to = [person for person in people if person is not None and person.telegram_id]
    for person in sent_to:
        await send_and_log(
            session,
            notifier,
            chat_id=person.telegram_id or 0,
            kind=_ESCALATION_KIND[level],
            text=f"⏰ Нет реакции на уведомление.\n\n{summary}",
            employee_id=person.id,
            alert_id=alert.id,
            with_task_buttons=alert.id,
        )

    await _record_level(
        session, alert.id, level, reason, EscalationStatus.SENT, people=sent_to
    )
    log.info("alert.escalated", alert_id=alert.id, level=level, recipients=len(sent_to))
