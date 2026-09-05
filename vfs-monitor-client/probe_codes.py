"""Снять коды центра/категории заказчика для monitor_config.json.

Подключается к уже открытому Chrome (порт 9222) и слушает запросы к
lift-api. Ты (или заказчик) вручную проходишь: «Записаться на приём» →
свой центр → своя категория → подкатегория. Как только приложение
дёрнет CheckIsSlotAvailable, скрипт печатает vacCode и visaCategoryCode —
их и вписать в targets.

Секреты (токен, cookie) в консоль не выводятся — только коды из тела.

Запуск: probe.bat  (или python probe_codes.py). Остановка — Ctrl+C.
"""

import asyncio
import json

CDP_URL = "http://localhost:9222"
WATCH_SECONDS = 300


async def main() -> None:
    from playwright.async_api import async_playwright

    found = []

    async def on_response(response) -> None:
        if "CheckIsSlotAvailable" not in response.url:
            return
        pd = response.request.post_data
        try:
            body = json.loads(pd) if pd else {}
        except Exception:
            body = {}
        vac = body.get("vacCode")
        cat = body.get("visaCategoryCode")
        if vac and cat:
            print(f"\n>>> НАЙДЕНО:  vacCode = {vac}   visaCategoryCode = {cat}")
            print(">>> Впиши в monitor_config.json target:")
            print(f'    {{"name": "Замени на понятное имя", "vacCode": "{vac}", "visaCategoryCode": "{cat}"}}\n')
            found.append((vac, cat))

    def handler(response) -> None:
        if response.request.resource_type in {"xhr", "fetch"}:
            asyncio.create_task(on_response(response))

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(CDP_URL)
        except Exception as exc:  # noqa: BLE001
            print(f"Не подключился к Chrome ({CDP_URL}): {exc}")
            print("Сначала запусти 1-start-chrome.bat и войди в кабинет.")
            return
        if not browser.contexts:
            print("Нет контекстов — открой кабинет VFS в этом Chrome.")
            return
        context = browser.contexts[0]
        context.on("response", handler)

        print("Подключился. В этом же Chrome пройди:")
        print("«Записаться на приём» → свой центр → категория → подкатегория.")
        print(f"Жду {WATCH_SECONDS // 60} мин. Как увидишь строку 'НАЙДЕНО' — можно Ctrl+C.\n")
        try:
            await asyncio.sleep(WATCH_SECONDS)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass

    if not found:
        print("\nНичего не поймал. Проверь, что дошёл до выбора подкатегории.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
