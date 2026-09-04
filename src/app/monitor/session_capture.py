"""Разовый ручной вход в VFS и сохранение сессии.

Зачем это нужно. Форма входа VFS защищена Cloudflare Turnstile: кнопка
отправки остаётся disabled, пока в скрытом поле cf-turnstile-response нет
валидного токена. Автоматически такой токен не получить, а обходить защиту
запрещено — и ТЗ §1 и §20, и здравым смыслом: именно за это блокируют
учётные записи навсегда.

Правильное решение прямо описано в ТЗ §20: «предоставить администратору
ссылку или доступ к сессии для ручного прохождения; после прохождения
продолжить мониторинг». Эта команда открывает обычный видимый браузер,
человек входит сам (и сам проходит Turnstile), после чего сессия остаётся
жить в постоянном профиле браузера, а её снимок пишется в БД. Дальше воркер
открывает тот же профиль и не логинится повторно — автоматического входа
в системе нет вовсе: частые входы и есть главный признак, по которому VFS
банит аккаунты.

Пароль здесь НЕ вводится автоматически и не передаётся в браузер: человек
вводит его руками. Так пароль не проходит через код лишний раз.
"""

import asyncio
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from app.config import get_settings
from app.db.models import VfsAccount
from app.db.session import session_scope
from app.domain.timeutils import utcnow
from app.enums import AccountStatus
from app.logging import get_logger
from app.monitor.browser import profile_dir
from app.monitor.selectors import load_selectors

log = get_logger(__name__)

# Сколько ждать, пока человек войдёт. Turnstile, письмо с кодом и просто
# поиск пароля в менеджере занимают время — торопить тут нечего.
LOGIN_TIMEOUT_SECONDS = 600


async def capture(label: str) -> None:
    """Открыть браузер, дождаться ручного входа и сохранить сессию."""
    config = load_selectors()

    login_url = config.login_url or f"{config.base_url}/kaz/ru/ita/login"
    booking_url = config.booking_url or login_url

    async with session_scope() as session:
        account = (
            await session.execute(sa.select(VfsAccount).where(VfsAccount.label == label))
        ).scalar_one_or_none()
        if account is None:
            raise SystemExit(
                f"Учётная запись «{label}» не найдена. "
                f"Сначала: python -m app.cli add-account --label {label} --username ..."
            )
        username = account.username

    print(f"Открываю браузер. Войдите как {username} и пройдите проверку Cloudflare.")
    print("Пароль вводите сами — программа его не подставляет.")
    print(
        "Когда окажетесь в личном кабинете, вернитесь сюда. "
        f"Жду до {LOGIN_TIMEOUT_SECONDS // 60} минут."
    )

    profile = profile_dir(Path(get_settings().profiles_dir), label)
    print(f"Профиль браузера: {profile}")

    state = await _run_browser(login_url, booking_url, profile)
    if state is None:
        raise SystemExit("Вход не подтверждён, сессия не сохранена.")

    async with session_scope() as session:
        account = (
            await session.execute(sa.select(VfsAccount).where(VfsAccount.label == label))
        ).scalar_one()
        account.session_state = state
        account.session_saved_at = utcnow()
        account.last_login_at = utcnow()
        account.last_success_at = utcnow()
        # Ручной вход снимает и блокировку по CAPTCHA, и требование
        # переавторизации: причина, по которой проверки стояли, устранена.
        account.status = AccountStatus.OK
        account.status_note = None
        account.consecutive_errors = 0
        account.paused_until = None
        await session.flush()

    cookies = len(state.get("cookies", []))
    print(f"Сессия сохранена ({cookies} cookie). Мониторинг может продолжаться.")
    log.info("session.captured", label=label, cookies=cookies)


async def _run_browser(
    login_url: str, booking_url: str, user_data_dir: Path
) -> dict[str, Any] | None:
    """Открыть видимый браузер и дождаться, пока человек войдёт.

    Браузер открывается на том же постоянном профиле и с теми же параметрами
    контекста, с которыми потом ходит воркер. Это не деталь: Cloudflare
    выдаёт clearance конкретному браузеру, и вход в одном окружении с
    последующими проверками в другом даёт мёртвую сессию сразу же.

    Признак успеха — уход со страницы входа. Специально не привязываемся
    к конкретному селектору личного кабинета: он ещё не выяснен, а команда
    должна работать уже сейчас.
    """
    from playwright.async_api import async_playwright

    from app.monitor.browser import CONTEXT_OPTIONS

    async with async_playwright() as playwright:
        # headless=False обязательно: человек должен видеть окно и Turnstile.
        context = await playwright.chromium.launch_persistent_context(
            str(user_data_dir), headless=False, **CONTEXT_OPTIONS
        )
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(login_url, wait_until="domcontentloaded")
            deadline = asyncio.get_running_loop().time() + LOGIN_TIMEOUT_SECONDS

            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(2)
                if page.is_closed():
                    print("Окно браузера закрыто до подтверждения входа.")
                    return None
                if "login" not in page.url.lower():
                    # Ушли со страницы входа — считаем, что вход состоялся.
                    await asyncio.sleep(3)  # дать догрузиться и доставить куки
                    state: dict[str, Any] = dict(await context.storage_state())
                    return state

            print("Время ожидания истекло.")
            return None
        finally:
            await context.close()
