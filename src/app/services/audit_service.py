"""Запись в аудит-лог.

Таблица append-only на уровне БД (триггер в миграции 0001), поэтому здесь
только вставка — функций изменения и удаления не существует намеренно.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Employee
from app.logging import get_logger

log = get_logger(__name__)


async def record(
    session: AsyncSession,
    actor: Employee | None,
    action: str,
    entity: str,
    entity_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Зафиксировать действие в панели."""
    session.add(
        AuditLog(
            actor_employee_id=actor.id if actor else None,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            payload=payload or {},
        )
    )
    await session.flush()
    log.info(
        "audit",
        action=action,
        entity=entity,
        entity_id=entity_id,
        actor_id=actor.id if actor else None,
    )
