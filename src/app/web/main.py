"""Точка входа админ-панели."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.config import get_settings
from app.db.session import dispose_engine
from app.logging import configure_logging, get_logger
from app.services.notifications import Notifier, NullNotifier
from app.web.deps import NotAuthenticatedError
from app.web.routers import (
    auth_router,
    cities_router,
    dashboard_router,
    employees_router,
    settings_router,
)
from app.web.templating import render

log = get_logger(__name__)


def build_notifier() -> Notifier:
    """Транспорт сообщений для панели.

    Без токена бота панель поднимается с заглушкой: локальная разработка
    и прогон миграций не должны требовать настоящего Telegram.
    """
    settings = get_settings()
    if not settings.bot_token.get_secret_value():
        log.warning("web.notifier_disabled", reason="BOT_TOKEN is empty")
        return NullNotifier()

    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties

    from app.bot.notifier import TelegramNotifier

    return TelegramNotifier(
        Bot(
            token=settings.bot_token.get_secret_value(),
            default=DefaultBotProperties(parse_mode=None),
        )
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Ресурсы приложения."""
    configure_logging()
    app.state.notifier = build_notifier()
    log.info("web.started")
    yield
    await dispose_engine()
    log.info("web.stopped")


def create_app() -> FastAPI:
    """Собрать приложение панели."""
    app = FastAPI(
        title="Дежурства по визовым слотам",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(NotAuthenticatedError)
    async def _to_login(request: Request, exc: NotAuthenticatedError) -> Response:
        """Неаутентифицированного пользователя ведём на форму входа."""
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        """Страница ошибки отдаётся со своим кодом, а не с 200.

        Иначе отказ по правам или по CSRF выглядит для клиента успехом.
        """
        return render(
            request,
            "error.html",
            {"code": exc.status_code, "message": exc.detail},
            status_code=exc.status_code,
        )

    app.include_router(auth_router.router)
    app.include_router(dashboard_router.router)
    app.include_router(cities_router.router)
    app.include_router(employees_router.router)
    app.include_router(settings_router.router)

    from app.web.routers import journal_router

    app.include_router(journal_router.router)
    return app


app = create_app()
