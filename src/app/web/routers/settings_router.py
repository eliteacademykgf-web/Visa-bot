"""Настройки и аудит-лог."""

from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Form, Request, Response

from app.db.models import AuditLog, Employee, Setting
from app.domain.timeutils import utcnow
from app.enums import SettingValueType
from app.services.audit_service import record
from app.services.settings_service import DEFAULTS, load_settings
from app.services.webhook_service import HttpxWebhookSender, WebhookTransportError, sign_payload
from app.web.deps import AdminUser, CsrfProtected, CurrentUser, DbSession, redirect
from app.web.templating import render

router = APIRouter(prefix="/settings")


@router.get("")
async def settings_page(request: Request, session: DbSession, user: CurrentUser) -> Response:
    """Редактируемые настройки и последние записи аудита."""
    rows = (
        await session.execute(sa.select(Setting).order_by(Setting.key))
    ).scalars().all()
    audit = (
        await session.execute(
            sa.select(AuditLog, Employee)
            .outerjoin(Employee, Employee.id == AuditLog.actor_employee_id)
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    ).all()

    return render(
        request,
        "settings.html",
        {
            "user": user,
            "settings": rows,
            "audit": audit,
            "defaults": DEFAULTS,
            "test_result": request.query_params.get("test"),
        },
    )


@router.post("/save")
async def save_settings(
    request: Request,
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
) -> Response:
    """Сохранить значения настроек.

    Значение приводится к типу, объявленному в value_type: строка «десять»
    в поле порога эскалации сломала бы планировщик молча.
    """
    form = await request.form()
    rows = (await session.execute(sa.select(Setting))).scalars().all()
    changed: dict[str, object] = {}

    for setting in rows:
        raw = form.get(f"setting__{setting.key}")
        if raw is None:
            continue
        text = str(raw).strip()
        try:
            value = _coerce(text, setting.value_type)
        except ValueError:
            return redirect(f"/settings?error=bad_value&key={setting.key}")
        if value != setting.value:
            changed[setting.key] = value
            setting.value = value
            setting.updated_by_id = user.id
            setting.updated_at = utcnow()

    await session.flush()
    if changed:
        await record(session, user, "settings.update", "settings", None, changed)
    return redirect("/settings?saved=1")


def _coerce(text: str, value_type: SettingValueType) -> object:
    """Привести введённую строку к объявленному типу настройки."""
    if text == "" or text.lower() == "null":
        return None
    if value_type is SettingValueType.INT:
        return int(text)
    if value_type is SettingValueType.FLOAT:
        return float(text)
    if value_type is SettingValueType.BOOL:
        return text.lower() in ("1", "true", "да", "on")
    if value_type is SettingValueType.JSON:
        import json

        return json.loads(text)
    return text


@router.post("/test-webhook")
async def test_webhook(
    session: DbSession,
    user: AdminUser,
    _csrf: CsrfProtected,
) -> Response:
    """Отправить пробный вебхук в CRM.

    Тело помечено test=true, чтобы принимающая сторона не приняла проверку
    связи за настоящую находку.
    """
    settings = await load_settings(session)
    if not settings.crm_webhook_url:
        return redirect("/settings?test=no_url")

    payload = {
        "event": "test",
        "test": True,
        "sent_at": utcnow().isoformat(),
        "sent_by": user.full_name,
    }
    headers = {"Content-Type": "application/json", "X-Event": "test"}
    if settings.crm_webhook_secret:
        headers["X-Signature"] = sign_payload(payload, settings.crm_webhook_secret)

    sender = HttpxWebhookSender()
    try:
        response = await sender.post(settings.crm_webhook_url, payload, headers)
    except (WebhookTransportError, OSError) as exc:
        await record(session, user, "settings.test_webhook", "settings", None, {"error": str(exc)})
        return redirect("/settings?test=error")

    await record(
        session,
        user,
        "settings.test_webhook",
        "settings",
        None,
        {"status_code": response.status_code},
    )
    return redirect(f"/settings?test={response.status_code}")


@router.post("/password")
async def change_password(
    session: DbSession,
    user: CurrentUser,
    _csrf: CsrfProtected,
    current_password: Annotated[str, Form()],
    new_password: Annotated[str, Form()],
) -> Response:
    """Смена собственного пароля."""
    from app.web.auth import hash_password, verify_password

    if not verify_password(current_password, user.password_hash or ""):
        return redirect("/settings?error=wrong_password")
    if len(new_password) < 10:
        return redirect("/settings?error=weak_password")

    user.password_hash = hash_password(new_password)
    await session.flush()
    await record(session, user, "employee.password_change", "employee", user.id)
    return redirect("/settings?saved=1")
