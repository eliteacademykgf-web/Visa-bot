"""Текст уведомления об ошибке мониторинга.

Проверяется главное свойство этих сообщений: администратор должен понять
из них, что делать, не заглядывая в документацию. Разные причины требуют
разных действий, и одинаковый текст на все случаи здесь — дефект.
"""

from app.db.models import SlotEvent
from app.enums import CheckStatus, SlotEventType
from app.services.alert_service import _render_error


class FakeCity:
    name = "Алматы"
    timezone = "Asia/Almaty"


def error_event(status: CheckStatus, **details: object) -> SlotEvent:
    return SlotEvent(
        event_type=SlotEventType.ERROR,
        details={"status": status.value, "consecutive_errors": 1, **details},
    )


def render(status: CheckStatus, **details: object) -> str:
    return _render_error(error_event(status, **details), FakeCity(), "24.08.2026, 12:00")


class TestAuthRequired:
    def test_says_session_expired(self) -> None:
        assert "истекла" in render(CheckStatus.AUTH_REQUIRED)

    def test_gives_the_exact_command(self) -> None:
        text = render(CheckStatus.AUTH_REQUIRED, account_label="monitor-2")
        assert "capture-session --label monitor-2" in text

    def test_command_falls_back_when_label_is_missing(self) -> None:
        """Событие могло быть создано без учётной записи — команда всё равно нужна."""
        assert "capture-session --label monitor-1" in render(CheckStatus.AUTH_REQUIRED)

    def test_states_that_checks_have_stopped(self) -> None:
        assert "остановлены" in render(CheckStatus.AUTH_REQUIRED)


class TestCaptchaRequired:
    def test_asks_a_human_to_pass_it(self) -> None:
        text = render(CheckStatus.CAPTCHA_REQUIRED)
        assert "вручную" in text
        assert "capture-session" in text

    def test_says_the_system_will_not_bypass_it(self) -> None:
        """Обещание обхода в тексте создало бы ложные ожидания."""
        assert "обходить" in render(CheckStatus.CAPTCHA_REQUIRED)


class TestAccessBlocked:
    def test_tells_admin_not_to_touch_the_site(self) -> None:
        """Здесь совет «войдите» навредил бы: он продлевает блокировку."""
        text = render(CheckStatus.ACCESS_BLOCKED, error="too many requests")
        assert "не нужно" in text
        assert "capture-session" not in text

    def test_says_it_resumes_on_its_own(self) -> None:
        assert "сами" in render(CheckStatus.ACCESS_BLOCKED)


class TestOtherErrors:
    def test_generic_text_for_a_network_glitch(self) -> None:
        text = render(CheckStatus.SYSTEM_ERROR, error="timeout")
        assert "Ошибка мониторинга" in text
        assert "timeout" in text

    def test_generic_text_without_a_status(self) -> None:
        """Старые события в журнале статуса не содержат — падать нельзя."""
        event = SlotEvent(event_type=SlotEventType.ERROR, details={"consecutive_errors": 3})
        text = _render_error(event, FakeCity(), "24.08.2026, 12:00")
        assert "Ошибка мониторинга" in text
