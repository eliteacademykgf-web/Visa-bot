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
import sys
import time
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path(__file__).with_name("monitor_config.json")

# Через сколько секунд непрерывной потери сессии слать тревогу: значит
# авто-вход не справился и нужен человек. Обычное истечение восстанавливается
# за секунды — о нём не пишем вовсе.
STUCK_ALERT_AFTER = 900

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


def check_git_update() -> bool:
    """`git pull --ff-only` в папке проекта. True — если подтянулись изменения
    (нужен перезапуск). Всё тихо и защищённо: не git-клон / нет git / конфликт —
    просто возвращаем False и продолжаем работать на текущем коде."""
    repo = Path(__file__).resolve().parent
    if not (repo / ".git").exists() and not (repo.parent / ".git").exists():
        return False  # папку просто скопировали, не клонировали — обновлять нечем
    try:
        r = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(repo), capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        log(f"автообновление: git недоступен ({exc})")
        return False
    out = (r.stdout + "\n" + r.stderr).strip()
    if r.returncode != 0:
        log(f"автообновление: git pull не удался: {out[:200]}")
        return False
    if not out or "up to date" in out.lower() or "актуальн" in out.lower():
        return False
    log(f"автообновление: подтянул изменения, перезапускаюсь.\n{out[:300]}")
    return True


# Код выхода, по которому 2-start-monitor.bat перезапускает монитор с новым кодом.
UPDATE_EXIT_CODE = 42


def restart_self() -> None:
    """Выйти с особым кодом — обёртка (2-start-monitor.bat) перезапустит
    монитор уже с обновлённым кодом. На Windows надёжнее, чем подмена процесса."""
    log("перезапуск для применения обновления...")
    sys.exit(UPDATE_EXIT_CODE)


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


# Готовность формы входа: кнопка «Войти» существует и НЕ заблокирована. VFS
# держит её disabled, пока не заполнены поля и не пройден Turnstile, поэтому
# активная кнопка — надёжный признак «можно входить» (лучше слепого таймера).
READY_JS = r"""
() => {
  const onLogin = /\/login/i.test(location.href);
  const btns = Array.from(document.querySelectorAll('button'));
  const ready = btns.some(b => /войти|login|sign in/i.test(b.textContent || '') && !b.disabled);
  return { on_login: onLogin, ready };
}
"""


async def login_ready(page) -> bool:
    """Готова ли форма входа к нажатию (кнопка активна)."""
    try:
        r = await page.evaluate(READY_JS)
        return bool(r.get("ready"))
    except Exception:
        return False


async def prepare_login_page(page, login_url: str) -> None:
    """Привести окно в предсказуемое состояние: развернуть, вывести вперёд,
    открыть форму входа. Работает и когда окно свёрнуто (через CDP), и не
    зависит от координат экрана."""
    try:
        cdp = await page.context.new_cdp_session(page)
        info = await cdp.send("Browser.getWindowForTarget")
        wid = info.get("windowId")
        if wid is not None:
            # Развернуть, если свёрнуто (windowState=minimized -> normal).
            await cdp.send(
                "Browser.setWindowBounds",
                {"windowId": wid, "bounds": {"windowState": "normal"}},
            )
    except Exception as exc:  # noqa: BLE001
        log(f"развернуть окно не удалось: {exc}")
    try:
        await page.bring_to_front()
        await page.goto(login_url, wait_until="domcontentloaded")
    except Exception as exc:  # noqa: BLE001
        log(f"открыть страницу входа не удалось: {exc}")


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
    auto_update_hours = float(cfg.get("auto_update_hours", 0))  # 0 = не обновляться

    last_seen: dict[str, str] = {}
    need_login = False
    warned_error = 0
    relogin_started = False  # была ли попытка входа в текущем эпизоде
    login_since = 0.0        # когда начался текущий эпизод потери сессии
    login_alerted = False    # слали ли уже тревогу «не могу восстановить»

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
        last_update = time.monotonic()

        while True:
            # Самообновление: раз в auto_update_hours подтянуть код из git и,
            # если что-то поменялось, перезапуститься (Chrome и сессия живут
            # отдельно, переподключимся). Всё защищённо — при любой заминке
            # просто продолжаем на текущем коде.
            if auto_update_hours > 0 and time.monotonic() - last_update > auto_update_hours * 3600:
                last_update = time.monotonic()
                if check_git_update():
                    restart_self()

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
                    relogin_started = False  # эпизод закрыт: вход состоялся
                    # «Восстановлено» пишем ТОЛЬКО если до этого слали тревогу,
                    # иначе штатное авто-восстановление проходит молча.
                    if login_alerted:
                        notify(token, chat_id, "✅ Сессия VFS восстановлена, слежу дальше.")
                    login_alerted = False

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
                        log(f"[{t['name']}] слоты закончились.")
                    else:
                        log(f"[{t['name']}] слотов нет.")
                    last_seen[t["name"]] = ""

                await asyncio.sleep(3)

            if login_problem:
                now_mono = time.monotonic()

                if not need_login:
                    # Новый эпизод потери сессии: тихо готовим окно и форму,
                    # авто-вход попробует сам. Пользователю НЕ пишем — обычное
                    # истечение восстанавливается за секунды.
                    need_login = True
                    relogin_started = False
                    login_since = now_mono
                    login_alerted = False

                    await prepare_login_page(page, login)

                # Проверяем, появилась ли активная кнопка «Войти».
                ready = await login_ready(page)

                # Если форма полностью готова — один раз запускаем AHK.
                # После запуска relogin_started больше НЕ сбрасываем
                # до восстановления сессии.
                if ready and not relogin_started:
                    relogin_started = True

                    log("форма входа готова (login_ready) — запускаю relogin.ahk")
                    AUTOHOTKEY_EXE = str(Path(__file__).with_name("ahk") / "AutoHotkey.exe")
                    RELOGIN_AHK = str(Path(__file__).with_name("relogin.ahk"))
                    try:
                        subprocess.Popen(
                            [
                                AUTOHOTKEY_EXE,
                                RELOGIN_AHK,
                            ],
                            cwd=str(Path(RELOGIN_AHK).parent),
                        )

                        log("relogin.ahk запущен — одна попытка на текущий эпизод")


                    except Exception as exc:

                        log(f"Не удалось запустить relogin.ahk: {exc}")

                # Тревога только если авто-вход НЕ справился долго (нужен
                # человек). Штатное восстановление проходит совсем без сообщений.
                if now_mono - login_since > STUCK_ALERT_AFTER and not login_alerted:
                    login_alerted = True
                    mins = int((now_mono - login_since) // 60)
                    notify(
                        token,
                        chat_id,
                        f"🔴 Не удаётся восстановить сессию VFS уже {mins} мин. "
                        f"Открой Chrome и нажми «Войти» вручную.",
                    )

                await asyncio.sleep(15)
                continue

            pause = random.randint(imin, imax)
            log(f"Следующая проверка через {pause} с.")
            await asyncio.sleep(pause)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено.")
