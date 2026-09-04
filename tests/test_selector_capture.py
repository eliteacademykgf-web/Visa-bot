"""Разбор записи сценария: что считается ответом с датами.

Браузер здесь не участвует. Проверяется отбор, ради которого команда и
существует: среди десятков служебных запросов приложения найти тот, в
котором приходит календарь.
"""

import json
from pathlib import Path
from typing import Any

from app.monitor.selector_capture import DATE_HINT, _write_network


def entry(url: str, body: str) -> dict[str, Any]:
    return {"url": url, "body": body, "looks_like_dates": bool(DATE_HINT.search(body))}


class TestDateHint:
    def test_iso_date(self) -> None:
        assert DATE_HINT.search('{"dates":["2026-09-14"]}')

    def test_dotted_date(self) -> None:
        assert DATE_HINT.search("Ближайшая дата: 14.09.2026")

    def test_slashed_date(self) -> None:
        assert DATE_HINT.search('{"first":"14/09/2026"}')

    def test_dashed_day_first(self) -> None:
        """Формат из конфига по умолчанию — %d-%m-%Y."""
        assert DATE_HINT.search('{"first":"14-09-2026"}')

    def test_settings_response_is_not_a_date(self) -> None:
        assert not DATE_HINT.search('{"theme":"orange","locale":"ru"}')

    def test_version_number_is_not_a_date(self) -> None:
        """Версии сборки встречаются в каждом втором ответе."""
        assert not DATE_HINT.search('{"version":"8.0.29"}')


class TestWriteNetwork:
    def test_only_date_bearing_urls_become_candidates(self, tmp_path: Path) -> None:
        _write_network(
            [
                entry("https://vfs/api/settings", '{"theme":"orange"}'),
                entry("https://vfs/api/dates", '{"dates":["2026-09-14"]}'),
            ],
            tmp_path,
        )
        assert (tmp_path / "candidates.txt").read_text().strip() == "https://vfs/api/dates"

    def test_repeated_url_is_listed_once(self, tmp_path: Path) -> None:
        """Календарь опрашивается на каждом шаге — список должен остаться коротким."""
        same = entry("https://vfs/api/dates", '{"dates":["2026-09-14"]}')
        _write_network([same, same, same], tmp_path)
        assert (tmp_path / "candidates.txt").read_text().splitlines() == [
            "https://vfs/api/dates"
        ]

    def test_full_log_is_kept_even_without_candidates(self, tmp_path: Path) -> None:
        """Полный журнал нужен и тогда, когда дат не нашлось: по нему ищут причину."""
        _write_network([entry("https://vfs/api/settings", "{}")], tmp_path)
        assert json.loads((tmp_path / "network.json").read_text())
        assert (tmp_path / "candidates.txt").read_text() == ""

    def test_failed_response_without_body_does_not_break_the_report(
        self, tmp_path: Path
    ) -> None:
        """У отброшенного ответа тела нет — запись всё равно должна сохраниться."""
        _write_network([{"url": "https://vfs/api/dates", "error": "aborted"}], tmp_path)
        assert (tmp_path / "candidates.txt").read_text() == ""
