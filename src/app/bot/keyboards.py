"""Клавиатуры бота (ТЗ §11)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.enums import ReactionKind

CALLBACK_PREFIX = "re"

# Подписи ровно по ТЗ §11 — их видит сотрудник, и менять формулировки
# без согласования не стоит.
LABELS: dict[ReactionKind, str] = {
    ReactionKind.ACCEPTED: "Принял",
    ReactionKind.CHECKING: "Проверяю",
    ReactionKind.BOOKED: "Слот забронирован",
    ReactionKind.GONE: "Слот уже исчез",
    ReactionKind.FALSE_POSITIVE: "Ложное срабатывание",
    ReactionKind.HANDOVER: "Передать другому",
}

# Порядок кнопок повторяет ход работы: сначала подтверждение, потом исход,
# в последнюю очередь — передача.
LAYOUT: tuple[tuple[ReactionKind, ...], ...] = (
    (ReactionKind.ACCEPTED, ReactionKind.CHECKING),
    (ReactionKind.BOOKED, ReactionKind.GONE),
    (ReactionKind.FALSE_POSITIVE, ReactionKind.HANDOVER),
)


def reaction_callback(alert_id: int, kind: ReactionKind) -> str:
    """Данные колбэка: re:<id уведомления>:<реакция>.

    id обязателен: по нему хендлер проверяет, открыто ли уведомление, поэтому
    нажатие на старое сообщение не может закрыть чужое или уже закрытое.
    """
    return f"{CALLBACK_PREFIX}:{alert_id}:{kind.value}"


def parse_reaction_callback(data: str) -> tuple[int, ReactionKind] | None:
    """Разобрать данные колбэка. None — чужой или испорченный колбэк."""
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX:
        return None
    try:
        return int(parts[1]), ReactionKind(parts[2])
    except ValueError:
        return None


def alert_keyboard(alert_id: int) -> InlineKeyboardMarkup:
    """Шесть кнопок реакции на уведомление о слоте."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=LABELS[kind], callback_data=reaction_callback(alert_id, kind)
                )
                for kind in row
            ]
            for row in LAYOUT
        ]
    )


# Обратная совместимость с Notifier: он передаёт id, для которого нужна
# клавиатура, под нейтральным именем.
def task_keyboard(alert_id: int) -> InlineKeyboardMarkup:
    """Псевдоним alert_keyboard для транспортного слоя."""
    return alert_keyboard(alert_id)
