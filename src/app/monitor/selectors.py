"""Селекторы и текстовые признаки страниц VFS Global.

ТЗ §16 требует устойчивых селекторов и запрещает строить логику на координатах
и тексте кнопок. ТЗ §4 запрещает зашивать категории в код. Поэтому всё, что
описывает конкретную вёрстку, живёт в отдельном JSON-файле и загружается
на старте: интерфейс VFS меняется регулярно (ТЗ §28), и его правка не должна
требовать релиза.

ВАЖНО. Значения по умолчанию ниже — ЗАГОТОВКА, а не рабочая конфигурация.
Их нужно заполнить, один раз пройдя сценарий вручную с открытым DevTools
(протокол — в docs/research-vfs.md). Пока файл не заполнен, парсер честно
возвращает SITE_CHANGED вместо того, чтобы делать вид, что проверил.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging import get_logger

log = get_logger(__name__)

DEFAULT_PATH = Path("config/vfs_selectors.json")


@dataclass(frozen=True, slots=True)
class StepSelector:
    """Один шаг сценария: чего дождаться и с чем взаимодействовать."""

    wait_for: str = ""
    """CSS-селектор элемента, по появлению которого шаг считается загруженным."""

    action: str = ""
    """CSS-селектор элемента, с которым выполняется действие."""

    timeout_ms: int = 15000

    @property
    def is_configured(self) -> bool:
        return bool(self.wait_for or self.action)


@dataclass(frozen=True, slots=True)
class SiteSignals:
    """Текстовые признаки состояний страницы (ТЗ §7).

    Это не «логика на тексте кнопок», а распознавание сообщений сайта —
    ровно то, что ТЗ и требует фиксировать в поле «Текст ответа сайта».
    Списки регистронезависимые и хранятся в конфиге: формулировки VFS
    меняются и различаются между языками интерфейса.
    """

    no_slots: tuple[str, ...] = (
        "no appointment slots are currently available",
        "записей не найдено",
        "no slots available",
    )
    auth_required: tuple[str, ...] = (
        "session has expired",
        "please log in",
        "сессия истекла",
    )
    captcha: tuple[str, ...] = (
        "verify you are human",
        "recaptcha",
        "подтвердите, что вы не робот",
    )
    blocked: tuple[str, ...] = (
        "access denied",
        "too many requests",
        "temporarily blocked",
        "доступ запрещён",
    )

    def match(self, text: str) -> str | None:
        """Определить состояние по тексту страницы.

        Порядок важен: блокировка и CAPTCHA проверяются раньше «слотов нет»,
        потому что страница-заглушка может содержать оба текста, а реагировать
        надо на более серьёзный.
        """
        lowered = text.lower()
        for name, needles in (
            ("blocked", self.blocked),
            ("captcha", self.captcha),
            ("auth_required", self.auth_required),
            ("no_slots", self.no_slots),
        ):
            if any(needle.lower() in lowered for needle in needles):
                return name
        return None


@dataclass(frozen=True, slots=True)
class SelectorConfig:
    """Полная конфигурация сценария обхода сайта."""

    base_url: str = "https://visa.vfsglobal.com"
    login_url: str = ""
    booking_url: str = ""

    login_username: StepSelector = field(default_factory=StepSelector)
    login_password: StepSelector = field(default_factory=StepSelector)
    login_submit: StepSelector = field(default_factory=StepSelector)
    logged_in_marker: StepSelector = field(default_factory=StepSelector)

    start_booking: StepSelector = field(default_factory=StepSelector)
    select_centre: StepSelector = field(default_factory=StepSelector)
    select_category: StepSelector = field(default_factory=StepSelector)
    select_subcategory: StepSelector = field(default_factory=StepSelector)
    applicants_input: StepSelector = field(default_factory=StepSelector)

    calendar_container: StepSelector = field(default_factory=StepSelector)
    available_day: StepSelector = field(default_factory=StepSelector)
    available_time: StepSelector = field(default_factory=StepSelector)
    nearest_date_text: StepSelector = field(default_factory=StepSelector)
    message_container: StepSelector = field(default_factory=StepSelector)

    # Запрос, в ответе которого приходят даты. Если исследование покажет, что
    # календарь наполняется отдельным XHR, читать надо его: это на порядок
    # устойчивее к смене вёрстки, чем разбор DOM.
    dates_api_pattern: str = ""

    date_format: str = "%d-%m-%Y"
    signals: SiteSignals = field(default_factory=SiteSignals)

    @property
    def is_configured(self) -> bool:
        """Заполнена ли конфигурация настолько, чтобы вообще идти на сайт.

        Без этих шагов сценарий не имеет смысла: парсер не должен изображать
        работу, возвращая «слотов нет» просто потому, что не нашёл календарь.
        """
        required = (
            self.login_url,
            self.booking_url,
            self.login_username.action,
            self.login_password.action,
            self.login_submit.action,
            self.calendar_container.wait_for,
        )
        return all(required)

    def missing_fields(self) -> list[str]:
        """Что именно осталось заполнить — для внятного сообщения админу."""
        checks = {
            "login_url": self.login_url,
            "booking_url": self.booking_url,
            "login_username.action": self.login_username.action,
            "login_password.action": self.login_password.action,
            "login_submit.action": self.login_submit.action,
            "calendar_container.wait_for": self.calendar_container.wait_for,
        }
        return [name for name, value in checks.items() if not value]


def _step(raw: dict[str, Any] | None) -> StepSelector:
    if not raw:
        return StepSelector()
    return StepSelector(
        wait_for=raw.get("wait_for", ""),
        action=raw.get("action", ""),
        timeout_ms=int(raw.get("timeout_ms", 15000)),
    )


def load_selectors(path: Path | None = None) -> SelectorConfig:
    """Загрузить конфигурацию селекторов.

    Отсутствие файла — не ошибка запуска: система должна подниматься,
    показывать панель и внятно сообщать, что мониторинг не настроен.
    """
    target = path or DEFAULT_PATH
    if not target.exists():
        log.warning("selectors.not_configured", path=str(target))
        return SelectorConfig()

    raw = json.loads(target.read_text(encoding="utf-8"))
    signals_raw = raw.get("signals", {})
    default_signals = SiteSignals()
    signals = SiteSignals(
        no_slots=tuple(signals_raw.get("no_slots", default_signals.no_slots)),
        auth_required=tuple(signals_raw.get("auth_required", default_signals.auth_required)),
        captcha=tuple(signals_raw.get("captcha", default_signals.captcha)),
        blocked=tuple(signals_raw.get("blocked", default_signals.blocked)),
    )

    config = SelectorConfig(
        base_url=raw.get("base_url", "https://visa.vfsglobal.com"),
        login_url=raw.get("login_url", ""),
        booking_url=raw.get("booking_url", ""),
        login_username=_step(raw.get("login_username")),
        login_password=_step(raw.get("login_password")),
        login_submit=_step(raw.get("login_submit")),
        logged_in_marker=_step(raw.get("logged_in_marker")),
        start_booking=_step(raw.get("start_booking")),
        select_centre=_step(raw.get("select_centre")),
        select_category=_step(raw.get("select_category")),
        select_subcategory=_step(raw.get("select_subcategory")),
        applicants_input=_step(raw.get("applicants_input")),
        calendar_container=_step(raw.get("calendar_container")),
        available_day=_step(raw.get("available_day")),
        available_time=_step(raw.get("available_time")),
        nearest_date_text=_step(raw.get("nearest_date_text")),
        message_container=_step(raw.get("message_container")),
        dates_api_pattern=raw.get("dates_api_pattern", ""),
        date_format=raw.get("date_format", "%d-%m-%Y"),
        signals=signals,
    )
    log.info("selectors.loaded", path=str(target), configured=config.is_configured)
    return config
