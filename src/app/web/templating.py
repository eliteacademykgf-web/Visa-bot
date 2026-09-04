"""Настройка Jinja2 и общие фильтры шаблонов."""

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from app.domain.timeutils import format_for_city
from app.enums import CheckStatus, ReactionKind

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Подписи девяти статусов ТЗ §7 — используются и в панели, и в экспорте.
STATUS_LABELS = {
    CheckStatus.NO_SLOTS: "слотов нет",
    CheckStatus.SLOT_AVAILABLE: "слот доступен",
    CheckStatus.SLOT_CHANGED: "дата изменилась",
    CheckStatus.SLOT_DISAPPEARED: "слот исчез",
    CheckStatus.AUTH_REQUIRED: "нужна авторизация",
    CheckStatus.CAPTCHA_REQUIRED: "запрошена CAPTCHA",
    CheckStatus.ACCESS_BLOCKED: "доступ ограничен",
    CheckStatus.SITE_CHANGED: "изменился сайт",
    CheckStatus.SYSTEM_ERROR: "ошибка мониторинга",
}

REACTION_LABELS = {
    ReactionKind.ACCEPTED: "принял",
    ReactionKind.CHECKING: "проверяет",
    ReactionKind.BOOKED: "забронирован",
    ReactionKind.GONE: "слот исчез",
    ReactionKind.FALSE_POSITIVE: "ложное срабатывание",
    ReactionKind.HANDOVER: "передано другому",
}


def in_city(moment: datetime | None, timezone: str, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Показать момент в таймзоне города — единственный допустимый способ."""
    if moment is None:
        return "—"
    return format_for_city(moment, timezone, fmt)


def duration(seconds: int | None) -> str:
    """Человеко-читаемая длительность."""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds} с"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин {rest:02d} с"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes:02d} мин"


def build_templates() -> Jinja2Templates:
    """Собрать окружение шаблонов."""
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["in_city"] = in_city
    templates.env.filters["duration"] = duration
    templates.env.globals["status_label"] = lambda s: STATUS_LABELS.get(s, str(s))
    templates.env.globals["reaction_label"] = lambda r: REACTION_LABELS.get(r, "—")
    templates.env.globals["is_error_status"] = lambda s: bool(getattr(s, "is_error", False))
    return templates


templates = build_templates()


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> Response:
    """Отрисовать шаблон, подмешав общий контекст.

    status_code передаётся явно: страница ошибки, отданная с кодом 200,
    сообщает любому клиенту, что запрещённое действие удалось.
    """
    context.setdefault("csrf_token", getattr(request.state, "csrf_token", ""))
    context.setdefault("current_path", request.url.path)
    return templates.TemplateResponse(request, name, context, status_code=status_code)
