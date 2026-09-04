"""Журнал проверок с фильтрами и экспортом (ТЗ §12, §18)."""

import csv
import io
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import StreamingResponse

from app.db.models import Category, City
from app.domain.timeutils import UTC, format_for_city
from app.enums import CheckStatus
from app.services.analytics_service import journal_query
from app.web.deps import CurrentUser, DbSession
from app.web.templating import STATUS_LABELS, render

router = APIRouter(prefix="/journal")

PAGE_SIZE = 100


def parse_day(raw: str | None, *, end: bool = False) -> datetime | None:
    """Дата из формы -> момент в UTC.

    Границы берутся по UTC-суткам: фильтр журнала — инструмент поиска,
    а не отчёт, и смещение в час здесь не искажает выводы. Отчёты
    по времени суток считаются иначе, в локальном времени города.
    """
    if not raw:
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    moment = datetime.combine(day, datetime.min.time(), tzinfo=UTC)
    return moment + timedelta(days=1) if end else moment


def _filters(
    city_id: int | None,
    status: str | None,
    date_from: str | None,
    date_to: str | None,
    only_errors: bool,
) -> dict[str, object]:
    return {
        "city_id": city_id,
        "status": CheckStatus(status) if status else None,
        "date_from": parse_day(date_from),
        "date_to": parse_day(date_to, end=True),
        "only_errors": only_errors,
    }


@router.get("")
async def journal(
    request: Request,
    session: DbSession,
    user: CurrentUser,
    city_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_errors: bool = False,
    page: int = Query(1, ge=1),
) -> Response:
    """Таблица проверок."""
    query = journal_query(**_filters(city_id, status, date_from, date_to, only_errors))  # type: ignore[arg-type]

    total = (
        await session.execute(sa.select(sa.func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(query.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))
    ).all()

    cities = (await session.execute(sa.select(City).order_by(City.name))).scalars().all()

    return render(
        request,
        "journal.html",
        {
            "user": user,
            "rows": rows,
            "total": total,
            "page": page,
            "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "cities": cities,
            "statuses": list(CheckStatus),
            "selected": {
                "city_id": city_id,
                "status": status,
                "date_from": date_from or "",
                "date_to": date_to or "",
                "only_errors": only_errors,
            },
            "query_string": request.url.query,
        },
    )


@router.get("/export.csv")
async def export_csv(
    session: DbSession,
    user: CurrentUser,
    city_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    only_errors: bool = False,
) -> StreamingResponse:
    """Выгрузка журнала в CSV (ТЗ §12).

    Использует ровно тот же запрос, что и страница: расхождение между тем,
    что видно на экране, и тем, что попало в файл, — источник долгих споров.
    """
    query = journal_query(**_filters(city_id, status, date_from, date_to, only_errors))  # type: ignore[arg-type]
    rows = (await session.execute(query.limit(50000))).all()

    categories = {
        category.id: category
        for category in (await session.execute(sa.select(Category))).scalars().all()
    }

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        [
            "Время проверки (местное)",
            "Город",
            "Категория",
            "Подкатегория",
            "Заявителей",
            "Статус",
            "Ближайшая дата",
            "Доступных дат",
            "Длительность, мс",
            "Сообщение сайта",
            "Ошибка",
        ]
    )
    for check, city, target in rows:
        category = categories.get(target.category_id)
        writer.writerow(
            [
                format_for_city(check.started_at, city.timezone),
                city.name,
                category.name if category else "",
                category.subcategory_name if category else "",
                target.applicants,
                STATUS_LABELS.get(check.status, check.status.value),
                check.nearest_date.isoformat() if check.nearest_date else "",
                len(check.available_dates or []),
                check.duration_ms or "",
                (check.site_message or "")[:200],
                (check.error_text or "")[:200],
            ]
        )

    # utf-8-sig: иначе Excel показывает кириллицу мусором.
    payload = buffer.getvalue().encode("utf-8-sig")
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="checks-{stamp}.csv"'},
    )
