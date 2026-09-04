"""Запись сценария записи на приём для снятия селекторов (ТЗ §29).

Зачем отдельная команда. Селекторы шагов от «Записаться на приём» до
календаря нельзя ни угадать, ни получить без входа в аккаунт. Разметка VFS
собрана Angular Material: имена классов сгенерированы и осмысленного вида
не имеют, а часть элементов появляется только после предыдущего шага.

Просить человека самому писать CSS-селекторы — плохая идея: ошибку он
заметит через сутки молчания мониторинга. Поэтому команда не спрашивает
селекторы, а записывает сырьё: HTML каждого шага, скриншот и все сетевые
запросы с их ответами. По записи селекторы выводятся уже спокойно, глазами,
без гонки с живой страницей.

Отдельная ценность — сетевой журнал. Если даты приходят одним JSON-ответом,
парсеру незачем кликать по календарю: он прочитает ответ, что и надёжнее,
и мягче по нагрузке на сайт (ТЗ §8).

Команда ничего не отправляет и ничего не заполняет — она только смотрит.
Вход выполняется заранее командой capture-session.
"""

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config import get_settings
from app.domain.timeutils import utcnow
from app.logging import get_logger
from app.monitor.browser import CONTEXT_OPTIONS, profile_dir
from app.monitor.selectors import load_selectors

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Response

log = get_logger(__name__)

# Ответ с датами узнаётся по самим датам, а не по адресу: адрес у VFS
# меняется между странами и версиями, а формат дат — нет.
DATE_HINT = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}[-/.]\d{2}[-/.]\d{4}")

# Сколько байт ответа сохранять. Календарные ответы небольшие, а вот бандлы
# приложения весят мегабайты и в записи бесполезны.
MAX_BODY_CHARS = 200_000

# Как часто смотреть, не сменилось ли состояние страницы. Две секунды —
# заметно быстрее, чем человек проходит шаг, и заметно реже, чем стоило бы
# дёргать живую страницу.
POLL_SECONDS = 2.0

# Предохранитель: сценарий записи — это 5–7 экранов. Если снимков набежало
# в разы больше, значит отпечаток ловит анимацию, а не шаги, и запись пора
# остановить, пока она не забила диск.
MAX_STEPS = 40


async def capture(label: str) -> None:
    """Открыть браузер и записать шаги, которые отметит человек."""
    settings = get_settings()
    config = load_selectors()

    profile = profile_dir(Path(settings.profiles_dir), label)
    out_dir = (
        Path(settings.artifacts_dir)
        / "selector-capture"
        / utcnow().strftime("%Y%m%d-%H%M%S")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    start_url = config.booking_url or config.login_url or config.base_url

    _print_intro(out_dir)
    await _run(start_url, profile, out_dir)


def _print_intro(out_dir: Path) -> None:
    print()
    print("Открываю браузер на сохранённой сессии.")
    print("Пройдите сценарий записи: «Записаться на приём» → визовый центр →")
    print("категория → подкатегория → экран с датами.")
    print()
    print("В терминал возвращаться не нужно: страница сохраняется сама, как")
    print("только меняется. Закончив — просто закройте окно браузера.")
    print()
    print("Паспортные данные не вводите: до них сценарий доходить не должен.")
    print(f"Запись будет здесь: {out_dir}")
    print()


async def _run(start_url: str, profile: Path, out_dir: Path) -> None:
    from playwright.async_api import async_playwright

    network: list[dict[str, Any]] = []
    pending: set[asyncio.Task[None]] = set()

    async with async_playwright() as playwright:
        # headless=False обязательно: сценарий проходит человек.
        context = await playwright.chromium.launch_persistent_context(
            str(profile), headless=False, **CONTEXT_OPTIONS
        )
        page = context.pages[0] if context.pages else await context.new_page()

        context.on("response", lambda response: _remember(response, network, pending))

        try:
            await page.goto(start_url, wait_until="domcontentloaded")
            if "login" in page.url.lower():
                print("!! Сессия недействительна. Сначала: capture-session --label ...")
                return

            await _watch(page, out_dir)
        finally:
            # Дочитать тела ответов, которые ещё в полёте, — иначе запись
            # потеряет ровно последний, самый интересный запрос календаря.
            if pending:
                await asyncio.wait(pending, timeout=10)
            _write_network(network, out_dir)
            await context.close()

    print()
    print(f"Готово. Запись: {out_dir}")
    print("Передайте этот каталог тому, кто заполняет config/vfs_selectors.json.")


async def _watch(page: Any, out_dir: Path) -> None:
    """Снимать страницу самой, пока человек ходит по сайту.

    Раньше здесь ждали Enter в терминале. Это плохо работало на практике:
    человек и так занят незнакомым сценарием в браузере, а переключение
    туда-обратно на каждом шаге — лишний повод сбиться и потерять именно
    тот экран, ради которого всё затевалось.

    Шаги ловятся опросом, а не событием навигации: VFS — Angular-приложение,
    адрес при переходе между шагами не меняется, и framenavigated не сработал
    бы ни разу. Признак нового состояния — связка адреса, числа элементов и
    объёма текста; длина текста огрубляется, иначе каждый тик анимации
    выглядел бы новым шагом.
    """
    seen: set[str] = set()
    step = 0

    while not page.is_closed() and step < MAX_STEPS:
        await asyncio.sleep(POLL_SECONDS)
        try:
            signature = await _signature(page)
        except Exception:
            # Страница в момент перехода — не состояние, а его отсутствие.
            continue

        if signature in seen:
            continue
        seen.add(signature)

        step += 1
        try:
            await _save_step(page, out_dir, step)
        except Exception as exc:
            print(f"  не удалось сохранить шаг: {type(exc).__name__}: {exc}")

    if step >= MAX_STEPS:
        print(f"  достигнут предел в {MAX_STEPS} снимков, запись остановлена")


async def _signature(page: Any) -> str:
    """Отпечаток текущего состояния страницы."""
    return str(
        await page.evaluate(
            "() => [location.href,"
            " document.querySelectorAll('*').length,"
            " Math.round((document.body ? document.body.innerText.length : 0) / 100)"
            "].join('|')"
        )
    )


async def _save_step(page: Any, out_dir: Path, step: int) -> None:
    """Сохранить HTML, скриншот и адрес одного шага."""
    name = f"step-{step:02d}"
    (out_dir / f"{name}.html").write_text(await page.content(), encoding="utf-8")
    await page.screenshot(path=str(out_dir / f"{name}.png"), full_page=True)
    (out_dir / f"{name}.url.txt").write_text(page.url, encoding="utf-8")
    print(f"  шаг {step} сохранён: {page.url}")


def _remember(
    response: "Response",
    network: list[dict[str, Any]],
    pending: set["asyncio.Task[None]"],
) -> None:
    """Запомнить ответ, если он похож на данные, а не на статику.

    Тело читается асинхронно и отдельной задачей: обработчик события не
    должен ничего ждать, иначе он затормозит саму страницу. Ссылка на задачу
    хранится до её завершения — иначе сборщик мусора вправе убить её на
    середине, и ответ молча пропадёт из записи.
    """
    if response.request.resource_type not in {"xhr", "fetch"}:
        return
    task = asyncio.create_task(_read_body(response, network))
    pending.add(task)
    task.add_done_callback(pending.discard)


async def _read_body(response: "Response", network: list[dict[str, Any]]) -> None:
    entry: dict[str, Any] = {
        "url": response.url,
        "method": response.request.method,
        "status": response.status,
        "content_type": response.headers.get("content-type", ""),
    }
    try:
        body = await response.text()
    except Exception as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        network.append(entry)
        return

    entry["length"] = len(body)
    # Главный признак: есть ли в ответе даты. Именно он отличает запрос
    # календаря от полусотни служебных запросов приложения.
    entry["looks_like_dates"] = bool(DATE_HINT.search(body))
    entry["body"] = body[:MAX_BODY_CHARS]
    network.append(entry)


def _write_network(network: list[dict[str, Any]], out_dir: Path) -> None:
    """Сохранить сетевой журнал, выделив кандидатов на календарь."""
    path = out_dir / "network.json"
    path.write_text(
        json.dumps(network, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    candidates = sorted({entry["url"] for entry in network if entry.get("looks_like_dates")})
    (out_dir / "candidates.txt").write_text("\n".join(candidates), encoding="utf-8")

    print()
    print(f"Сетевых запросов записано: {len(network)}")
    if candidates:
        print("Ответы, в которых встречаются даты, — вероятный источник календаря:")
        for url in candidates[:10]:
            print(f"  {url}")
    else:
        print("Ответов с датами не нашлось — календарь читается из разметки.")
