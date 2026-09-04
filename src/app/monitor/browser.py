"""Браузерный сценарий обхода VFS Global (ТЗ §5, §16, §17).

Что этот модуль делает и чего не делает:

* работает через настоящий браузер (Playwright), потому что сайт не отдаёт
  страницы обычному HTTP-клиенту — это не обход защиты, а единственный
  способ вообще открыть страницу;
* останавливается на шаге, где видна ближайшая дата, и НЕ идёт дальше:
  ТЗ §14 запрещает вводить паспортные данные ради мониторинга;
* при CAPTCHA останавливается и сообщает администратору — автоматическое
  прохождение запрещено ТЗ §1 и §20;
* при блокировке останавливается и увеличивает паузу, НЕ меняя IP —
  автоматическая смена IP для обхода ограничений запрещена ТЗ §20;
* никогда не бронирует — ТЗ §1.
"""

import asyncio
import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from app.domain.timeutils import utcnow
from app.enums import ObservedStatus
from app.logging import get_logger
from app.monitor import json_dates, page_reader
from app.monitor.selectors import SelectorConfig
from app.services.slot_diff import Observation

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import BrowserContext, Page, Response

log = get_logger(__name__)

# Параметры браузерного контекста, общие для ручного захвата сессии и для
# проверок. Cloudflare привязывает выданный clearance к отпечатку браузера:
# если войти с одной локалью и часовым поясом, а ходить с другими, сессия
# аннулируется на первом же запросе и мониторинг встаёт. Набор один на оба
# сценария и меняться должен только целиком.
CONTEXT_OPTIONS: dict[str, Any] = {
    "locale": "ru-RU",
    "timezone_id": "Asia/Almaty",
    "viewport": {"width": 1440, "height": 900},
}


def profile_dir(root: Path, label: str) -> Path:
    """Каталог профиля браузера для учётной записи.

    Метка приходит из панели и попадает в путь, поэтому всё, кроме букв,
    цифр и трёх безопасных знаков, заменяется подчёркиванием.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label).strip("._") or "default"
    path = root / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """Что искать: город, категория, подкатегория, число заявителей."""

    centre: str
    category: str
    subcategory: str
    applicants: int = 1


@dataclass(slots=True)
class CheckArtifacts:
    """Скриншот и HTML, сохранённые при проверке (ТЗ §6, §16)."""

    screenshot_path: str | None = None
    html_path: str | None = None


# Имена стратегий — попадают в журнал и в отчёт, чтобы всегда было видно,
# каким путём получен результат.
STRATEGY_DIRECT = "direct"
STRATEGY_WIZARD = "wizard"
STRATEGY_NONE = "none"


@dataclass(slots=True)
class BrowserResult:
    """Результат одного прохода сценария."""

    observation: Observation
    artifacts: CheckArtifacts
    session_state: dict[str, Any] | None = None
    reused_session: bool = False
    logged_in: bool = False
    strategy: str = STRATEGY_NONE
    # Адрес запроса, которым пришли даты. Найден по ходу длинного пути и
    # позволяет следующей проверке пойти коротким.
    discovered_dates_url: str = ""


class ArtifactStore:
    """Сохранение скриншотов и HTML на диск.

    В БД лежит только путь: страница VFS весит сотни килобайт, и складывать
    её в строку таблицы, которая хранится 12 месяцев (ТЗ §18), — верный
    способ получить неподъёмную базу к концу первого года.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _dir_for(self, moment: datetime) -> Path:
        target = self._root / moment.strftime("%Y-%m-%d")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def path_for(self, moment: datetime, city_code: str, suffix: str) -> Path:
        stamp = moment.strftime("%H%M%S")
        return self._dir_for(moment) / f"{city_code}-{stamp}.{suffix}"


async def run_check(
    context: "BrowserContext",
    config: SelectorConfig,
    spec: TargetSpec,
    *,
    store: ArtifactStore | None = None,
    city_code: str = "unknown",
    capture_always: bool = False,
    dates_endpoint: str = "",
) -> BrowserResult:
    """Пройти сценарий и вернуть наблюдение.

    Путей два, и они пробуются по порядку. Если известен прямой запрос дат
    (`dates_endpoint`), сперва идёт он: одно обращение внутри уже открытой
    сессии вместо шести загрузок страниц. Не получилось — управление
    переходит длинному пути по экранам мастера, и он же заново находит
    адрес запроса.

    Исключения наружу не пробрасываются: любая неожиданность превращается
    в SYSTEM_ERROR со скриншотом. Упавший парсер не должен останавливать
    мониторинг остальных городов.
    """
    artifacts = CheckArtifacts()
    if not config.is_configured:
        # Честный отказ вместо имитации работы: незаполненный конфиг легко
        # принять за «слотов нет» и месяцами не замечать, что мониторинга нет.
        missing = ", ".join(config.missing_fields())
        log.error("monitor.not_configured", missing=missing)
        return BrowserResult(
            observation=Observation(
                status=ObservedStatus.SITE_CHANGED,
                error_text=f"не заполнена конфигурация селекторов: {missing}",
            ),
            artifacts=artifacts,
        )

    page = await context.new_page()
    try:
        if dates_endpoint:
            direct = await _try_direct(page, config, dates_endpoint, city_code)
            if direct is not None:
                return direct
            log.info("monitor.direct_failed_falling_back", city=city_code)

        return await _walk(page, context, config, spec, store, city_code, capture_always)
    except Exception as exc:
        log.exception("monitor.unexpected_error", city=city_code)
        artifacts = await _capture(page, store, city_code, always=True)
        return BrowserResult(
            observation=Observation(
                status=ObservedStatus.SYSTEM_ERROR,
                error_text=f"{type(exc).__name__}: {exc}"[:500],
            ),
            artifacts=artifacts,
        )
    finally:
        await page.close()


async def _try_direct(
    page: "Page",
    config: SelectorConfig,
    endpoint: str,
    city_code: str,
) -> BrowserResult | None:
    """Короткий путь: спросить даты одним запросом.

    Возвращает None, если путь не сработал, — тогда вызывающий код идёт
    длинным. None здесь означает именно «не удалось спросить», а не
    «слотов нет»: пустой ответ живого эндпоинта — законный результат.

    Запрос выполняется внутри страницы, а не внешним HTTP-клиентом, и это
    принципиально: только так к нему приложатся куки сессии и заголовки
    настоящего браузера. Снаружи тот же адрес вернул бы 403.
    """
    # Открываем страницу приложения: fetch должен уйти с её origin, иначе
    # это межсайтовый запрос со всеми вытекающими.
    if _is_login_url(page.url, config):
        return None
    if not page.url.startswith(config.base_url):
        await page.goto(config.booking_url, wait_until="domcontentloaded")
        if _is_login_url(page.url, config):
            return None

    try:
        raw = await page.evaluate(
            """async (url) => {
                const response = await fetch(url, {
                    credentials: 'include',
                    headers: {'Accept': 'application/json'},
                });
                return {status: response.status, body: (await response.text()).slice(0, 500000)};
            }""",
            endpoint,
        )
    except Exception as exc:
        log.info("monitor.direct_error", city=city_code, error=str(exc)[:200])
        return None

    status = int(raw.get("status", 0))
    body = str(raw.get("body", ""))

    if status in (401, 403):
        # Сессия или доступ — это диагноз, а не повод молча идти дальше.
        signal = page_reader.detect_status(body, config)
        observed = (
            signal.status
            if signal is not None and signal.status is not ObservedStatus.NO_SLOTS
            else ObservedStatus.AUTH_REQUIRED
        )
        return BrowserResult(
            observation=Observation(status=observed, error_text=f"HTTP {status} на запросе дат"),
            artifacts=CheckArtifacts(),
            reused_session=True,
            logged_in=True,
            strategy=STRATEGY_DIRECT,
        )

    if status != 200:
        log.info("monitor.direct_http", city=city_code, status=status)
        return None

    parsed = json_dates.extract_from_text(body, config.date_format)
    if not parsed.found and not _looks_like_json(body):
        # Пришёл не JSON — скорее всего страница проверки. Пусть длинный
        # путь посмотрит на неё как следует и поставит верный диагноз.
        return None

    observation = (
        Observation(
            status=ObservedStatus.SLOTS_PRESENT,
            nearest_date=page_reader.nearest_of(list(parsed.dates)),
            available_dates=parsed.dates,
            available_times=parsed.times,
            slots_count=len(parsed.dates),
        )
        if parsed.found
        else Observation(status=ObservedStatus.NO_SLOTS)
    )

    log.info(
        "monitor.direct_ok",
        city=city_code,
        dates=len(parsed.dates),
    )
    return BrowserResult(
        observation=observation,
        artifacts=CheckArtifacts(),
        reused_session=True,
        logged_in=True,
        strategy=STRATEGY_DIRECT,
    )


def _looks_like_json(body: str) -> bool:
    """Похоже ли тело на JSON — чтобы отличить пустой ответ от заглушки."""
    head = body.lstrip()[:1]
    return head in ("{", "[")


async def _walk(
    page: "Page",
    context: "BrowserContext",
    config: SelectorConfig,
    spec: TargetSpec,
    store: ArtifactStore | None,
    city_code: str,
    capture_always: bool,
) -> BrowserResult:
    """Основной проход по шагам сценария (ТЗ §17)."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    if not await _session_alive(page, config):
        artifacts = await _capture(page, store, city_code, always=True)
        return BrowserResult(
            observation=Observation(
                status=ObservedStatus.AUTH_REQUIRED,
                error_text="сессия недействительна, нужен ручной вход: capture-session",
            ),
            artifacts=artifacts,
            strategy=STRATEGY_WIZARD,
        )

    # Пока идём по экранам, слушаем сеть: запрос, в ответе которого окажутся
    # даты, и есть тот, которым следующая проверка обойдётся вместо всего
    # этого пути. Слушатель ничего не ждёт и на скорость не влияет.
    found_url: list[str] = []
    watcher = _watch_for_dates(page, config, found_url)
    page.on("response", watcher)

    try:
        result = await _walk_steps(
            page, context, config, spec, store, city_code, capture_always, PlaywrightTimeout
        )
    finally:
        page.remove_listener("response", watcher)

    if found_url:
        result.discovered_dates_url = found_url[0]
    return result


def _watch_for_dates(
    page: "Page", config: SelectorConfig, found: list[str]
) -> Callable[["Response"], None]:
    """Слушатель, запоминающий адрес ответа, в котором пришли даты."""
    pending: set[asyncio.Task[None]] = set()

    async def inspect(response: "Response") -> None:
        try:
            body = await response.text()
        except Exception:
            return
        if not _looks_like_json(body):
            return
        if json_dates.extract_from_text(body, config.date_format).found and not found:
            found.append(response.url)
            log.info("monitor.dates_endpoint_found", url=response.url)

    def handler(response: "Response") -> None:
        if found or response.request.resource_type not in {"xhr", "fetch"}:
            return
        task = asyncio.create_task(inspect(response))
        pending.add(task)
        task.add_done_callback(pending.discard)

    return handler


async def _body_text(page: "Page", timeout_ms: int = 5000) -> str:
    """Текст страницы, дождавшись, что он вообще появился.

    innerText зависит от раскладки: сразу после загрузки документа он часто
    пуст, хотя разметка уже на месте. Читать его без ожидания — значит
    случайно получать «пустую страницу» вместо настоящего результата.
    """
    with contextlib.suppress(Exception):
        await page.wait_for_function(
            "() => (document.body && document.body.innerText || '').trim().length > 0",
            timeout=timeout_ms,
        )
    try:
        return await page.inner_text("body")
    except Exception:
        return ""


async def _walk_steps(
    page: "Page",
    context: "BrowserContext",
    config: SelectorConfig,
    spec: TargetSpec,
    store: ArtifactStore | None,
    city_code: str,
    capture_always: bool,
    PlaywrightTimeout: type[BaseException],
) -> BrowserResult:
    """Пройти экраны мастера и прочитать календарь."""
    await page.goto(config.booking_url, wait_until="domcontentloaded")

    # Ранняя проверка признаков: блокировку и CAPTCHA надо распознать до того,
    # как сценарий начнёт кликать по несуществующим элементам и превратит
    # понятную причину в невнятный таймаут.
    #
    # Пустой текст здесь диагнозом не считается. Сразу после domcontentloaded
    # страница может быть ещё не размечена, и innerText вернёт пустоту — на
    # этом месте это означает «рано смотреть», а не «сайт изменился».
    early_text = await _body_text(page, timeout_ms=2000)
    early = page_reader.detect_status(early_text, config) if early_text.strip() else None
    if early is not None and early.status is not ObservedStatus.NO_SLOTS:
        artifacts = await _capture(page, store, city_code, always=True)
        return BrowserResult(
            observation=Observation(status=early.status, site_message=early.message),
            artifacts=artifacts,
            reused_session=True,
            logged_in=True,
            strategy=STRATEGY_WIZARD,
        )

    for step, value in (
        (config.start_booking, None),
        (config.select_centre, spec.centre),
        (config.select_category, spec.category),
        (config.select_subcategory, spec.subcategory),
    ):
        if not step.is_configured:
            continue
        try:
            await _interact(page, step, value)
        except PlaywrightTimeout:
            # Ожидаемого элемента нет — это изменение интерфейса, а не «нет
            # слотов». Сохраняем HTML и скриншот, чтобы было что чинить.
            artifacts = await _capture(page, store, city_code, always=True)
            log.warning(
                "monitor.site_changed",
                city=city_code,
                selector=step.wait_for or step.action,
            )
            return BrowserResult(
                observation=Observation(
                    status=ObservedStatus.SITE_CHANGED,
                    error_text=f"не найден элемент: {step.wait_for or step.action}",
                ),
                artifacts=artifacts,
                reused_session=True,
                logged_in=True,
                strategy=STRATEGY_WIZARD,
            )

    if config.applicants_input.is_configured:
        await page.fill(config.applicants_input.action, str(spec.applicants))

    observation = await _read_calendar(page, config)
    # Скриншот обязателен при находке и при ошибке (ТЗ §16).
    need_capture = capture_always or observation.has_slots or observation.is_error
    artifacts = await _capture(page, store, city_code, always=need_capture)

    return BrowserResult(
        observation=observation,
        artifacts=artifacts,
        session_state=dict(await context.storage_state()),
        reused_session=True,
        logged_in=True,
        strategy=STRATEGY_WIZARD,
    )


async def _session_alive(page: "Page", config: SelectorConfig) -> bool:
    """Жива ли сохранённая сессия.

    Автоматического входа здесь нет намеренно. Форма входа VFS закрыта
    Cloudflare Turnstile: кнопка отправки остаётся disabled, пока нет токена,
    получить который без человека нельзя, а обходить запрещено (ТЗ §1, §20).
    Попытка «всё же войти» не откроет кабинет, зато даст ровно тот признак,
    по которому VFS блокирует учётные записи, — серию неудачных входов
    подряд. Поэтому мёртвая сессия честно останавливает проверки и зовёт
    администратора пройти `capture-session` один раз руками.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    await page.goto(config.booking_url, wait_until="domcontentloaded")

    # Редирект на форму входа — самый надёжный признак: он не зависит от
    # селектора личного кабинета, который может быть ещё не заполнен.
    if _is_login_url(page.url, config):
        log.info("monitor.session_expired", url=page.url)
        return False

    if not config.logged_in_marker.is_configured:
        return True

    try:
        await page.wait_for_selector(
            config.logged_in_marker.wait_for,
            timeout=config.logged_in_marker.timeout_ms,
        )
        return True
    except PlaywrightTimeout:
        log.info("monitor.session_marker_missing", url=page.url)
        return False


def _is_login_url(url: str, config: SelectorConfig) -> bool:
    """Оказались ли мы на странице входа."""
    if config.login_url and url.split("?")[0].rstrip("/") == config.login_url.rstrip("/"):
        return True
    return "/login" in url.lower()


async def _interact(page: "Page", step: Any, value: str | None) -> None:
    """Выполнить шаг: дождаться элемента и выбрать значение или кликнуть."""
    if step.wait_for:
        await page.wait_for_selector(step.wait_for, timeout=step.timeout_ms)
    if not step.action:
        return

    if value is None:
        await page.click(step.action)
        return

    # Выпадающие списки VFS — обычные select либо кастомные компоненты.
    # Сначала пробуем как select, иначе кликаем по опции с нужным текстом:
    # ТЗ §16 запрещает полагаться только на текст, поэтому текст — запасной путь.
    try:
        await page.select_option(step.action, label=value, timeout=3000)
    except Exception:
        await page.click(step.action)
        await page.click(f"text={value}")


async def _read_calendar(page: "Page", config: SelectorConfig) -> Observation:
    """Прочитать доступные даты и время."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    try:
        await page.wait_for_selector(
            config.calendar_container.wait_for,
            timeout=config.calendar_container.timeout_ms,
        )
    except PlaywrightTimeout:
        body = await _body_text(page)
        signal = page_reader.detect_status(body, config)
        if signal is not None:
            return Observation(status=signal.status, site_message=signal.message)
        return Observation(
            status=ObservedStatus.SITE_CHANGED,
            error_text=f"не найден календарь: {config.calendar_container.wait_for}",
        )

    # Календарь найден, значит страница загрузилась. Отсутствие текста здесь
    # уже не признак поломки: ячейки дня вполне могут быть без подписи, а
    # дата — лежать в атрибуте. Приговор «пустая страница» на этом месте
    # отменил бы успешно найденный календарь, поэтому текст читается только
    # ради признаков CAPTCHA и блокировки, и пустота диагнозом не считается.
    body = await _body_text(page, timeout_ms=1500)
    signal = page_reader.detect_status(body, config) if body.strip() else None
    if signal is not None and signal.status is not ObservedStatus.NO_SLOTS:
        return Observation(status=signal.status, site_message=signal.message)

    raw_dates: list[str] = []
    if config.available_day.is_configured:
        raw_dates = await page.eval_on_selector_all(
            config.available_day.action or config.available_day.wait_for,
            "els => els.map(e => e.getAttribute('data-date') || e.textContent.trim())",
        )

    raw_times: list[str] = []
    if config.available_time.is_configured:
        raw_times = await page.eval_on_selector_all(
            config.available_time.action or config.available_time.wait_for,
            "els => els.map(e => e.textContent.trim())",
        )

    dates = page_reader.parse_dates(raw_dates, config.date_format)
    times = page_reader.normalise_times(raw_times)

    if not dates:
        # Календарь есть, дат нет — это именно «слотов нет», а не сбой.
        return Observation(
            status=ObservedStatus.NO_SLOTS,
            site_message=signal.message if signal else None,
        )

    return Observation(
        status=ObservedStatus.SLOTS_PRESENT,
        nearest_date=page_reader.nearest_of(dates),
        available_dates=tuple(dates),
        available_times=tuple(times),
        slots_count=len(dates),
        site_message=signal.message if signal else None,
    )


async def _capture(
    page: "Page",
    store: ArtifactStore | None,
    city_code: str,
    *,
    always: bool,
) -> CheckArtifacts:
    """Сохранить скриншот и HTML страницы."""
    if store is None or not always:
        return CheckArtifacts()

    moment = utcnow()
    artifacts = CheckArtifacts()
    try:
        shot = store.path_for(moment, city_code, "png")
        await page.screenshot(path=str(shot), full_page=True)
        artifacts.screenshot_path = str(shot)

        html = store.path_for(moment, city_code, "html")
        html.write_text(await page.content(), encoding="utf-8")
        artifacts.html_path = str(html)
    except Exception as exc:
        log.warning("monitor.capture_failed", error=str(exc))
    return artifacts


async def open_context(
    playwright: Any,
    *,
    user_data_dir: Path,
    storage_state: dict[str, Any] | None = None,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> "BrowserContext":
    """Открыть браузер на постоянном профиле учётной записи.

    Профиль на диске, а не свежий контекст с подставленными куками, — потому
    что Cloudflare выдаёт clearance не «аккаунту», а конкретному браузеру.
    Постоянный каталог сохраняет и куки, и localStorage, и сам профиль, и
    разовый ручной вход живёт настолько долго, насколько позволяет VFS.

    Параметры контекста берутся из CONTEXT_OPTIONS и обязаны совпадать с теми,
    с которыми сессию захватывали: подмен отпечатка здесь нет и не должно
    быть, но и расхождений быть не должно — они аннулируют clearance.
    """
    context = await playwright.chromium.launch_persistent_context(
        str(user_data_dir),
        headless=headless,
        **CONTEXT_OPTIONS,
    )
    context.set_default_timeout(timeout_ms)

    # Разовый перенос сессии, захваченной до перехода на профили (или на
    # другой машине). Профиль со своими куками важнее: перезаписывать его
    # снимком из БД нельзя, иначе свежий clearance затрётся старым.
    if storage_state and not await context.cookies():
        with contextlib.suppress(Exception):
            await context.add_cookies(storage_state.get("cookies", []))

    return cast("BrowserContext", context)


async def close_context(context: "BrowserContext") -> None:
    """Закрыть контекст и браузер."""
    browser = context.browser
    await context.close()
    if browser is not None:
        await browser.close()
    await asyncio.sleep(0)
