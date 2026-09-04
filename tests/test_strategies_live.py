"""Обе стратегии на настоящем браузере и макете сайта.

Здесь запускается Playwright против локального макета, повторяющего то,
как устроен портал VFS: страница подтягивает календарь отдельным запросом.
Это единственное место, где проверяется само переключение стратегий, —
всё остальное в браузерном слое покрыть чистыми тестами нельзя.

Проверяется главное свойство цепочки: длинный путь работает сам по себе и
попутно находит адрес запроса, а следующая проверка идёт коротким путём.
"""

import json
import threading
from collections.abc import Iterator
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

from app.enums import ObservedStatus
from app.monitor.browser import (
    CONTEXT_OPTIONS,
    STRATEGY_DIRECT,
    STRATEGY_WIZARD,
    TargetSpec,
    run_check,
)
from app.monitor.selectors import SelectorConfig, StepSelector

DATES_PAYLOAD = {"data": {"availableDates": [{"date": "2026-09-14", "available": True}]}}
EMPTY_PAYLOAD = {"data": {"availableDates": []}}

# Макет повторяет устройство портала VFS: страница отдаётся почти пустой,
# а календарь дорисовывается отдельным запросом. Ячейка дня подписана
# числом, как на настоящем календаре, — пустой контейнер имел бы нулевую
# высоту и не считался бы видимым.
PAGE = """<!doctype html><meta charset="utf-8"><title>booking</title>
<div id="app">загрузка</div>
<script>
fetch('/api/dates').then(r => r.json()).then(d => {
  const days = (d.data.availableDates || [])
    .map(x => `<span class="day" data-date="${x.date}">${x.date.slice(-2)}</span>`)
    .join(' ');
  document.getElementById('app').innerHTML =
    `<div id="calendar">${days || 'записей не найдено'}</div>`;
});
</script>"""


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, payload: dict, **kwargs) -> None:
        self._payload = payload
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        if self.path.startswith("/api/dates"):
            body = json.dumps(self._payload).encode()
            content_type = "application/json"
        else:
            body = PAGE.encode()
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Тише: сервер живёт внутри теста, его лог только мешает."""


def serve(payload: dict) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, payload=payload))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}"


def config_for(base: str) -> SelectorConfig:
    """Конфигурация, заполненная ровно настолько, чтобы парсер считал её рабочей."""
    return SelectorConfig(
        base_url=base,
        login_url=f"{base}/login",
        booking_url=f"{base}/dashboard",
        login_username=StepSelector(action="#email"),
        login_password=StepSelector(action="#password"),
        login_submit=StepSelector(action="button"),
        calendar_container=StepSelector(wait_for="#calendar", timeout_ms=10000),
        available_day=StepSelector(action=".day"),
        date_format="%Y-%m-%d",
    )


@pytest.fixture
def site() -> Iterator[str]:
    server, base = serve(DATES_PAYLOAD)
    yield base
    server.shutdown()


@pytest.fixture
def empty_site() -> Iterator[str]:
    server, base = serve(EMPTY_PAYLOAD)
    yield base
    server.shutdown()


@pytest_asyncio.fixture
async def context(tmp_path):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        ctx = await playwright.chromium.launch_persistent_context(
            str(tmp_path / "profile"), headless=True, **CONTEXT_OPTIONS
        )
        yield ctx
        await ctx.close()


SPEC = TargetSpec(centre="Алматы", category="D Visa Study", subcategory="Enrollment")


@pytest.mark.asyncio
class TestWizardPath:
    async def test_reads_dates_from_markup(self, context, site: str) -> None:
        result = await run_check(context, config_for(site), SPEC, city_code="almaty")
        assert result.observation.status is ObservedStatus.SLOTS_PRESENT
        assert result.observation.nearest_date.isoformat() == "2026-09-14"
        assert result.strategy == STRATEGY_WIZARD

    async def test_discovers_the_dates_endpoint_on_the_way(self, context, site: str) -> None:
        """Ради этого длинный путь и слушает сеть."""
        result = await run_check(context, config_for(site), SPEC, city_code="almaty")
        assert result.discovered_dates_url.endswith("/api/dates")

    async def test_empty_calendar_is_no_slots(self, context, empty_site: str) -> None:
        result = await run_check(context, config_for(empty_site), SPEC, city_code="almaty")
        assert result.observation.status is ObservedStatus.NO_SLOTS

    async def test_nothing_discovered_when_response_has_no_dates(
        self, context, empty_site: str
    ) -> None:
        """Пустой ответ не доказывает, что это тот самый запрос."""
        result = await run_check(context, config_for(empty_site), SPEC, city_code="almaty")
        assert result.discovered_dates_url == ""


@pytest.mark.asyncio
class TestDirectPath:
    async def test_reads_dates_without_walking_the_wizard(self, context, site: str) -> None:
        result = await run_check(
            context, config_for(site), SPEC, city_code="almaty",
            dates_endpoint=f"{site}/api/dates",
        )
        assert result.observation.status is ObservedStatus.SLOTS_PRESENT
        assert result.observation.nearest_date.isoformat() == "2026-09-14"
        assert result.strategy == STRATEGY_DIRECT

    async def test_empty_response_is_a_legitimate_no_slots(
        self, context, empty_site: str
    ) -> None:
        result = await run_check(
            context, config_for(empty_site), SPEC, city_code="almaty",
            dates_endpoint=f"{empty_site}/api/dates",
        )
        assert result.observation.status is ObservedStatus.NO_SLOTS
        assert result.strategy == STRATEGY_DIRECT

    async def test_falls_back_to_the_wizard_when_endpoint_is_dead(
        self, context, site: str
    ) -> None:
        """Смысл цепочки: сломанный короткий путь не должен ронять проверку."""
        result = await run_check(
            context, config_for(site), SPEC, city_code="almaty",
            dates_endpoint="http://127.0.0.1:9/nowhere",
        )
        assert result.observation.status is ObservedStatus.SLOTS_PRESENT
        assert result.strategy == STRATEGY_WIZARD


@pytest.mark.asyncio
class TestUnconfigured:
    async def test_refuses_to_pretend_it_checked(self, context, site: str) -> None:
        """Незаполненный конфиг обязан выглядеть как поломка, а не как «слотов нет»."""
        result = await run_check(context, SelectorConfig(), SPEC, city_code="almaty")
        assert result.observation.status is ObservedStatus.SITE_CHANGED
