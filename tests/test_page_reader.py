"""Распознавание состояния страницы и разбор дат (ТЗ §7, §16).

Тесты работают на текстовых фикстурах, без Playwright. Это осознанно:
логика «что означает увиденное» должна проверяться независимо от того,
работает ли сейчас сайт и заполнены ли селекторы.
"""

from datetime import date

from app.enums import ObservedStatus
from app.monitor.page_reader import (
    detect_status,
    nearest_of,
    normalise_times,
    parse_date,
    parse_dates,
)
from app.monitor.selectors import SelectorConfig

CONFIG = SelectorConfig()


class TestStatusDetection:
    def test_no_slots_english(self) -> None:
        text = "Appointment booking\nNo appointment slots are currently available."
        signal = detect_status(text, CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.NO_SLOTS

    def test_no_slots_russian(self) -> None:
        signal = detect_status("Записей не найдено", CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.NO_SLOTS

    def test_session_expired(self) -> None:
        signal = detect_status("Your session has expired. Please log in.", CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.AUTH_REQUIRED

    def test_captcha(self) -> None:
        signal = detect_status("Please verify you are human", CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.CAPTCHA_REQUIRED

    def test_blocked(self) -> None:
        signal = detect_status("Access Denied. Too many requests.", CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.ACCESS_BLOCKED

    def test_blocked_wins_over_no_slots(self) -> None:
        """Страница-заглушка может содержать оба текста.

        Реагировать надо на более серьёзный: приняв блокировку за «слотов
        нет», система продолжила бы долбиться в сайт и получила бы бан.
        """
        text = "Access denied.\nNo appointment slots are currently available."
        signal = detect_status(text, CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.ACCESS_BLOCKED

    def test_captcha_wins_over_auth(self) -> None:
        text = "Please log in. reCAPTCHA verification required."
        signal = detect_status(text, CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.CAPTCHA_REQUIRED

    def test_empty_page_is_not_no_slots(self) -> None:
        """Пустая страница — это сбой загрузки, а не отсутствие слотов.

        Принять её за «слотов нет» означало бы месяцами показывать
        спокойный дашборд при неработающем мониторинге.
        """
        signal = detect_status("   \n  ", CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.SITE_CHANGED

    def test_calendar_page_has_no_signal(self) -> None:
        """Обычная страница календаря — решение принимается по датам."""
        assert detect_status("Select an available date\n31-07-2026", CONFIG) is None

    def test_detection_is_case_insensitive(self) -> None:
        signal = detect_status("NO APPOINTMENT SLOTS ARE CURRENTLY AVAILABLE", CONFIG)
        assert signal is not None
        assert signal.status is ObservedStatus.NO_SLOTS


class TestDateParsing:
    def test_vfs_default_format(self) -> None:
        assert parse_date("31-07-2026") == date(2026, 7, 31)

    def test_iso_format(self) -> None:
        assert parse_date("2026-07-31") == date(2026, 7, 31)

    def test_slash_format(self) -> None:
        assert parse_date("31/07/2026") == date(2026, 7, 31)

    def test_whitespace_is_trimmed(self) -> None:
        assert parse_date("  31-07-2026  ") == date(2026, 7, 31)

    def test_unparsable_returns_none(self) -> None:
        """Неразобранная дата не должна ронять проверку целиком."""
        assert parse_date("скоро") is None
        assert parse_date("") is None

    def test_ambiguous_us_format_is_rejected(self) -> None:
        """07-31-2026 не поддерживается намеренно.

        Молча перепутанные день и месяц дали бы правдоподобную, но неверную
        дату — и сотрудник поехал бы в визовый центр не в тот день.
        """
        assert parse_date("07-31-2026") is None

    def test_preferred_format_wins(self) -> None:
        # 01-02-2026 в европейском формате — 1 февраля.
        assert parse_date("01-02-2026", "%d-%m-%Y") == date(2026, 2, 1)

    def test_parse_dates_sorts_and_deduplicates(self) -> None:
        result = parse_dates(["05-08-2026", "31-07-2026", "31-07-2026"])
        assert result == [date(2026, 7, 31), date(2026, 8, 5)]

    def test_parse_dates_skips_garbage(self) -> None:
        result = parse_dates(["31-07-2026", "мусор", ""])
        assert result == [date(2026, 7, 31)]

    def test_nearest_of(self) -> None:
        assert nearest_of([date(2026, 8, 5), date(2026, 7, 31)]) == date(2026, 7, 31)
        assert nearest_of([]) is None


class TestTimes:
    def test_normalise_trims_and_deduplicates(self) -> None:
        """Иначе « 09:00 » и «09:00» дали бы ложное «появилось новое время»."""
        assert normalise_times([" 09:00 ", "09:00", "14:30"]) == ["09:00", "14:30"]

    def test_empty_values_dropped(self) -> None:
        assert normalise_times(["", "  ", "10:00"]) == ["10:00"]


class TestSelectorConfig:
    def test_empty_config_is_not_usable(self) -> None:
        """Незаполненный конфиг обязан честно об этом сообщать."""
        assert SelectorConfig().is_configured is False
        assert "login_url" in SelectorConfig().missing_fields()

    def test_missing_fields_lists_what_is_left(self) -> None:
        missing = SelectorConfig(login_url="https://x/login").missing_fields()
        assert "login_url" not in missing
        assert "booking_url" in missing
