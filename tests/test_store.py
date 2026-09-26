"""Tests for the room registry.

Both backends must behave identically, so the shared behaviour is asserted by
a mixin that each backend test case reuses. Redis is only exercised when a
Redis server is actually reachable (CI and a laptop should not require one).
"""

import asyncio
import os
import unittest

import store
from store import (
    CODE_TTL_SECONDS,
    MAX_ROOMS_PER_CLIENT,
    MemoryRegistry,
    CodeRecord,
    generate_code,
    normalise_code,
)


class RegistryBehaviourMixin:
    """Behaviour every RoomRegistry implementation must satisfy."""

    registry = None

    def setUp(self):
        store._ip_room_creations.clear()

    async def _reserve(self, owner="client-key"):
        return await self.registry.reserve_code(owner)

    def test_generated_codes_use_the_unambiguous_alphabet(self):
        for _ in range(50):
            code = generate_code()
            self.assertEqual(len(code), store.CODE_LENGTH)
            self.assertTrue(set(code) <= set(store.CODE_ALPHABET))
        # 0/O/1/I/L are the classic mis-read pairs
        self.assertFalse(set("01OIL") & set(store.CODE_ALPHABET))

    def test_codes_are_unique(self):
        codes = {generate_code() for _ in range(500)}
        self.assertEqual(len(codes), 500)

    def test_normalise_code_folds_case_and_separators(self):
        self.assertEqual(normalise_code("ab-c 12"), "ABC12")
        self.assertEqual(normalise_code("  ab3x9z  "), "AB3X9Z")

    def test_reserve_then_lookup_roundtrip(self):
        async def scenario():
            code = await self._reserve()
            self.assertIsNotNone(code)
            record = await self.registry.get(code)
            self.assertIsNotNone(record)
            # a user typing it lowercase must still find the room
            self.assertIsNotNone(await self.registry.get(code.lower()))
            return record

        record = asyncio.run(scenario())
        self.assertFalse(record.live)
        # a reserved-but-not-yet-joined code is on a countdown
        self.assertIsNotNone(record.expires_at)

    def test_reserved_code_expires_but_a_live_room_does_not(self):
        async def scenario():
            code = await self._reserve()
            record = await self.registry.get(code)
            self.assertAlmostEqual(record.expires_at, record.created_at + CODE_TTL_SECONDS, delta=1)

            await self.registry.mark_live(code, 2)
            live = await self.registry.get(code)
            self.assertTrue(live.live)
            # once live the TTL no longer applies; the AFK sweep owns it
            self.assertIsNone(live.expires_at)
            return code

        asyncio.run(scenario())

    def test_reap_removes_only_expired_unlive_codes(self):
        async def scenario():
            stale = CodeRecord(code="STALE1", owner_key="h", created_at=0.0)
            live = CodeRecord(code="LIVE22", owner_key="h", created_at=0.0, live=True)
            fresh = CodeRecord(code="FRESH3", owner_key="h", created_at=9_999_999_999.0)
            self.registry._codes[stale.code] = stale
            self.registry._codes[live.code] = live
            self.registry._codes[fresh.code] = fresh
            reaped = await self.registry.reap_expired()
            return reaped, await self.registry.get("FRESH3"), await self.registry.get("LIVE22")

        reaped, fresh, live = asyncio.run(scenario())
        self.assertIn("STALE1", reaped)
        self.assertIsNotNone(fresh, "a fresh code must survive")
        self.assertIsNotNone(live, "a live room must survive the code TTL")

    def test_per_client_creation_is_capped(self):
        async def scenario():
            results = [
                await self.registry.allow_creation("1.2.3.4")
                for _ in range(MAX_ROOMS_PER_CLIENT + 2)
            ]
            other = await self.registry.allow_creation("5.6.7.8")
            return results, other

        results, other = asyncio.run(scenario())
        self.assertEqual(results[:MAX_ROOMS_PER_CLIENT], [True] * MAX_ROOMS_PER_CLIENT)
        self.assertTrue(all(result is False for result in results[MAX_ROOMS_PER_CLIENT:]))
        # the cap is per client, not global
        self.assertTrue(other, "a different client must not be blocked")

    def test_release_frees_the_code(self):
        async def scenario():
            code = await self._reserve()
            await self.registry.release(code)
            return await self.registry.get(code)

        self.assertIsNone(asyncio.run(scenario()))


class MemoryRegistryTests(RegistryBehaviourMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        store._codes.clear()
        self.registry = MemoryRegistry()
        self.registry._codes.clear()


class RedisRegistryTests(RegistryBehaviourMixin, unittest.TestCase):
    """Runs only when REDIS_URL points at a reachable server."""

    @classmethod
    def setUpClass(cls):
        url = os.environ.get("REDIS_URL")
        if not url:
            raise unittest.SkipTest("REDIS_URL not set; skipping Redis backend tests")
        from store import RedisRegistry

        cls.registry = RedisRegistry(url)
        if not asyncio.run(cls.registry.healthy()):
            raise unittest.SkipTest("Redis not reachable")

    def setUp(self):
        super().setUp()
        self.registry = type(self).registry
        asyncio.run(self._flush())

    async def _flush(self):
        for code in await self.registry._scan_codes():
            await self.registry.release(code)


if __name__ == "__main__":
    unittest.main()
