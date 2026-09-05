"""Монитор слотов VFS через переиспользование живой сессии Chrome (CDP).

Как это работает:
  * человек один раз входит в VFS в обычном Chrome, запущенном с портом
    отладки (Turnstile проходит человек — ботом это невозможно);
  * монитор подключается к тому же Chrome и раз в несколько минут спрашивает
    доступность прямо внутри вкладки (токен сессии НЕ покидает браузер);
  * появилась дата — шлёт уведомление в Telegram;
  * лимит частоты (409) — тихо ждёт; вылет на логин — просит войти заново.

Ничего не бронирует, не вводит паролей, не обходит капчу — только читает.

Настройки — в файле monitor_config.json рядом. Код править не нужно.
Запуск: 2-start-monitor.bat (или python cdp_monitor.py). Остановка — Ctrl+C.
"""

import asyncio
import json
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("monitor_config.json")

# JS выполняется в контексте страницы VFS. Токен читается из sessionStorage
# здесь же и наружу не выходит — в Python возвращается только результат.
CHECK_JS = r"""
async (t) => {
  const ss = window.sessionStorage;
  const jwt = ss.getItem('JWT');
  const cs  = ss.getItem('csk_str');
  const email = ss.getItem('logged_email') || '';
  if (/\/login/i.test(location.href)) return {on_login: true};
  const headers = {
    'Content-Type': 'application/json;charset=UTF-8',
    'Accept': 'application/json, text/plain, */*',
  };
  if (jwt) headers['authorize'] = jwt;
  if (cs)  headers['clientsource'] = cs;
  const body = JSON.stringify({
    countryCode: 'kaz', missionCode: 'ita', vacCode: t.vacCode,
    visaCategoryCode: t.visaCategoryCode, roleName: 'Individual',
    loginUser: email, payCode: ''
  });
  // keep-alive: держим приложение "активным", чтобы оно продлевало сессию.
  try { localStorage.setItem('ng2Idle.main.expiry', String(Date.now() + 30*60*1000)); } catch(e){}
  for (const ev of ['mousemove','keydown','scroll','mousedown','touchstart'])
    document.dispatchEvent(new Event(ev, {bubbles:true}));
  window.dispatchEvent(new Event('mousemove'));
  let r;
  try {
    r = await fetch('https://lift-api.vfsglobal.com/appointment/CheckIsSlotAvailable',
      {method:'POST', credentials:'include', headers, body});
  } catch (e) { return {error: String(e)}; }
  const text = await r.text();
  let data = null; try { data = JSON.parse(text); } catch (e) {}
  return {status: r.status, data, raw: text.slice(0,300), on_login: false};
}
"""


def _bot_token_from_env() -> str:
    for d in (Path(__file__).parent, Path(__file__).parent.parent):
        env = d / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("BOT_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        raise SystemExit(
            f"Нет файла настроек {CONFIG_PATH.name}. "
            f"Скопируй monitor_config.example.json в monitor_config.json и заполни."
        )
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    token = cfg.get("bot_token")
    if not token or "ВСТАВЬ" in str(token):
        # Запасной путь для тестов из корня: взять BOT_TOKEN из .env рядом.
        token = _bot_token_from_env()
        cfg["bot_token"] = token
    if not cfg.get("bot_token"):
        raise SystemExit("Не заполнен bot_token (в monitor_config.json или BOT_TOKEN в .env).")
    if not cfg.get("chat_id"):
        raise SystemExit("В monitor_config.json не заполнен chat_id.")
    if not cfg.get("targets"):
        raise SystemExit("В monitor_config.json нет ни одной цели (targets).")
    return cfg


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')}  {msg}", flush=True)


def notify(token: str, chat_id: int, text: str) -> None:
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20) as r:
            r.read()
        log("уведомление отправлено")
    except Exception as exc:  # noqa: BLE001
        log(f"НЕ удалось отправить в Telegram: {exc}")


def slots_from(data: dict) -> tuple[bool, str]:
    if not isinstance(data, dict):
        return False, ""
    earliest = data.get("earliestDate")
    lists = data.get("earliestSlotLists") or []
    if earliest:
        return True, str(earliest)
    if lists:
        m = re.search(r"\d{2}[-/.]\d{2}[-/.]\d{4}|\d{4}-\d{2}-\d{2}", json.dumps(lists))
        return True, (m.group(0) if m else "см. сайт")
    return False, ""


async def find_vfs_page(context):
    for p in context.pages:
        if "vfsglobal.com" in p.url:
            return p
    return None


async def main() -> None:
    from playwright.async_api import async_playwright

    cfg = load_config()
    token = cfg["bot_token"]
    chat_id = int(cfg["chat_id"])
    targets = cfg["targets"]
    cdp_url = cfg.get("cdp_url", "http://localhost:9222")
    dashboard = cfg.get("dashboard_url", "https://visa.vfsglobal.com/kaz/ru/ita/dashboard")
    login = cfg.get("login_url", "https://visa.vfsglobal.com/kaz/ru/ita/login")
    imin = int(cfg.get("interval_min_seconds", 180))
    imax = int(cfg.get("interval_max_seconds", 300))
    reload_every = int(cfg.get("reload_seconds", 600))

    last_seen: dict[str, str] = {}
    need_login = False
    warned_error = 0

    log(f"Монитор запущен. Целей: {len(targets)}. Подключаюсь к Chrome ({cdp_url})...")
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:  # noqa: BLE001
            log(f"Не подключился к Chrome: {exc}")
            notify(token, chat_id, "⚠️ Монитор VFS не смог подключиться к Chrome. Запущен ли браузер ярлыком «1-start-chrome»?")
            return
        if not browser.contexts:
            log("Нет контекстов в Chrome — открой в нём кабинет VFS.")
            return
        context = browser.contexts[0]

        notify(token, chat_id, "✅ Монитор VFS запущен, слежу за слотами.")
        last_reload = time.monotonic()

        while True:
            page = await find_vfs_page(context)
            if page is None:
                log("Вкладка vfsglobal.com не найдена — жду.")
                if not need_login:
                    notify(token, chat_id, "⚠️ Открой кабинет VFS в браузере — вкладка пропала.")
                    need_login = True
                await asyncio.sleep(45)
                continue

            # Частое "касание" сайта: переход на дашборд заставляет приложение
            # переобновить сессию/токен, пока текущий ещё жив. Интервал держим
            # заметно ниже времени жизни токена (~30 мин).
            if time.monotonic() - last_reload > reload_every:
                try:
                    await page.goto(dashboard, wait_until="domcontentloaded")
                    await asyncio.sleep(5)
                    log("keep-alive: обновил дашборд")
                except Exception as exc:  # noqa: BLE001
                    log(f"keep-alive goto не удался: {exc}")
                last_reload = time.monotonic()

            login_problem = False
            for t in targets:
                try:
                    res = await page.evaluate(CHECK_JS, t)
                except Exception as exc:  # noqa: BLE001
                    log(f"[{t['name']}] сбой evaluate: {exc}")
                    continue

                status = res.get("status")
                if res.get("on_login") or status in (401, 403):
                    login_problem = True
                    log(f"[{t['name']}] сессия недействительна (on_login={res.get('on_login')}, status={status}).")
                    break

                if res.get("error"):
                    warned_error += 1
                    log(f"[{t['name']}] ошибка запроса: {res.get('error')}")
                    if warned_error == 3:
                        notify(token, chat_id, "⚠️ Монитор VFS: несколько ошибок запроса подряд. Проверь браузер.")
                    continue
                warned_error = 0

                if need_login:
                    need_login = False
                    notify(token, chat_id, "✅ Сессия VFS снова активна, слежу за слотами дальше.")

                if status == 409:
                    log(f"[{t['name']}] 409 Repeated Delay — слишком часто, жду.")
                    continue
                if status != 200:
                    log(f"[{t['name']}] неожиданный статус {status}: {res.get('raw')}")
                    continue

                has, date = slots_from(res.get("data") or {})
                prev = last_seen.get(t["name"], "")
                if has:
                    log(f"[{t['name']}] СЛОТЫ ЕСТЬ, ближайшая дата: {date}")
                    if date != prev:
                        notify(
                            token, chat_id,
                            f"🎉 VFS · {t['name']}\nПоявились слоты! Ближайшая дата: {date}\n"
                            f"Успей записаться: {dashboard}",
                        )
                        last_seen[t["name"]] = date
                else:
                    if prev:
                        notify(token, chat_id, f"ℹ️ VFS · {t['name']}: слоты снова закончились.")
                    log(f"[{t['name']}] слотов нет.")
                    last_seen[t["name"]] = ""

                await asyncio.sleep(3)

            if login_problem:
                if not need_login:
                    need_login = True
                    # Готовим предсказуемое состояние: окно Chrome вперёд и
                    # открытая страница входа (браузер обычно подставляет логин
                    # и пароль сам). Дальше нужен вход.
                    try:
                        await page.bring_to_front()
                        await page.goto(login, wait_until="domcontentloaded")
                    except Exception as exc:  # noqa: BLE001
                        log(f"открыть страницу входа не удалось: {exc}")
                    notify(
                        token, chat_id,
                        "🔑 Сессия VFS истекла. Открыл страницу входа — нажми «Войти», "
                        "и я сам продолжу.",
                    )
                await asyncio.sleep(45)
                continue

            pause = random.randint(imin, imax)
            log(f"Следующая проверка через {pause} с.")
            await asyncio.sleep(pause)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
