"""Профиль браузера и распознавание страницы входа.

Сам браузер здесь не запускается: проверяется то, от чего зависит выживание
сессии между ручным входом и проверками.
"""

from pathlib import Path

from app.monitor.browser import CONTEXT_OPTIONS, _is_login_url, profile_dir
from app.monitor.selectors import SelectorConfig


def config(login_url: str = "https://visa.vfsglobal.com/kaz/ru/ita/login") -> SelectorConfig:
    return SelectorConfig(login_url=login_url)


class TestProfileDir:
    def test_label_becomes_directory(self, tmp_path: Path) -> None:
        assert profile_dir(tmp_path, "monitor-1") == tmp_path / "monitor-1"

    def test_directory_is_created(self, tmp_path: Path) -> None:
        assert profile_dir(tmp_path, "monitor-1").is_dir()

    def test_accounts_do_not_share_a_profile(self, tmp_path: Path) -> None:
        """Общий профиль означал бы общие куки и вход одного под другим."""
        assert profile_dir(tmp_path, "monitor-1") != profile_dir(tmp_path, "monitor-2")

    def test_separators_in_label_cannot_escape_the_root(self, tmp_path: Path) -> None:
        """Метка приходит из панели, а значит в пути ей доверять нельзя."""
        path = profile_dir(tmp_path, "../../etc/passwd")
        assert path.parent == tmp_path
        assert tmp_path in path.resolve().parents

    def test_label_without_usable_characters_still_yields_a_path(self, tmp_path: Path) -> None:
        assert profile_dir(tmp_path, "///").parent == tmp_path


class TestLoginDetection:
    def test_exact_login_url(self) -> None:
        assert _is_login_url("https://visa.vfsglobal.com/kaz/ru/ita/login", config())

    def test_login_url_with_query(self) -> None:
        """VFS добавляет к редиректу параметры возврата."""
        url = "https://visa.vfsglobal.com/kaz/ru/ita/login?returnUrl=%2Fdashboard"
        assert _is_login_url(url, config())

    def test_login_url_in_another_locale(self) -> None:
        """Локаль может отличаться от прописанной в конфиге."""
        assert _is_login_url("https://visa.vfsglobal.com/kaz/en/ita/login", config())

    def test_dashboard_is_not_login(self) -> None:
        assert not _is_login_url("https://visa.vfsglobal.com/kaz/ru/ita/dashboard", config())

    def test_detection_works_without_configured_login_url(self) -> None:
        assert _is_login_url("https://visa.vfsglobal.com/kaz/ru/ita/login", config(""))


class TestContextOptions:
    def test_options_pin_the_fingerprint(self) -> None:
        """Захват сессии и проверки обязаны идти с одним отпечатком.

        Расхождение локали, часового пояса или размера окна аннулирует
        выданный Cloudflare clearance, и мониторинг встаёт на первом запросе.
        """
        assert set(CONTEXT_OPTIONS) == {"locale", "timezone_id", "viewport"}
