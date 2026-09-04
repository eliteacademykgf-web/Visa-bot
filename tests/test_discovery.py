"""Память о прямом запросе дат и политика доверия к нему.

Смысл политики — не дать устаревшему адресу тихо превратиться в вечное
«слотов нет». Проверяется именно это: короткий путь работает, но не вечно.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.monitor import discovery
from app.monitor.discovery import (
    EndpointMemory,
    after_api_use,
    after_validation,
    should_use_api,
)

URL = "https://visa.vfsglobal.com/api/dates?centre=almaty"


class TestPolicy:
    def test_unknown_endpoint_forces_the_long_path(self) -> None:
        assert not should_use_api(EndpointMemory())

    def test_fresh_endpoint_is_used(self) -> None:
        assert should_use_api(after_validation(URL))

    def test_endpoint_expires_after_the_limit(self) -> None:
        """Адрес хранит параметры момента находки и со временем врёт."""
        memory = EndpointMemory(url=URL, checks_since_validation=discovery.REVALIDATE_EVERY)
        assert not should_use_api(memory)

    def test_last_allowed_use(self) -> None:
        memory = EndpointMemory(url=URL, checks_since_validation=discovery.REVALIDATE_EVERY - 1)
        assert should_use_api(memory)

    def test_counter_grows_with_each_short_path(self) -> None:
        memory = after_validation(URL)
        for expected in (1, 2, 3):
            memory = after_api_use(memory)
            assert memory.checks_since_validation == expected
            assert memory.url == URL

    def test_validation_resets_the_counter(self) -> None:
        stale = EndpointMemory(url="old", checks_since_validation=99)
        assert not should_use_api(stale)
        assert should_use_api(after_validation(URL))

    def test_trust_runs_out_after_exactly_the_configured_number(self) -> None:
        """Полный цикл: свежий адрес живёт ровно REVALIDATE_EVERY проверок."""
        memory = after_validation(URL)
        used = 0
        while should_use_api(memory):
            memory = after_api_use(memory)
            used += 1
        assert used == discovery.REVALIDATE_EVERY


@pytest.mark.asyncio
class TestStorage:
    async def test_unknown_target_returns_empty_memory(self, session: AsyncSession) -> None:
        assert await discovery.load(session, 1) == EndpointMemory()

    async def test_roundtrip(self, session: AsyncSession) -> None:
        await discovery.save(session, 1, EndpointMemory(url=URL, checks_since_validation=3))
        loaded = await discovery.load(session, 1)
        assert loaded.url == URL
        assert loaded.checks_since_validation == 3

    async def test_save_overwrites(self, session: AsyncSession) -> None:
        await discovery.save(session, 1, after_validation("first"))
        await discovery.save(session, 1, after_validation("second"))
        assert (await discovery.load(session, 1)).url == "second"

    async def test_targets_do_not_share_memory(self, session: AsyncSession) -> None:
        """У каждой цели свой центр и своя категория — адрес общим быть не может."""
        await discovery.save(session, 1, after_validation("almaty"))
        await discovery.save(session, 2, after_validation("astana"))
        assert (await discovery.load(session, 1)).url == "almaty"
        assert (await discovery.load(session, 2)).url == "astana"
