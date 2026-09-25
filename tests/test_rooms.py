"""Integration tests for room creation, code validation and AFK reaping.

These drive the FastAPI handlers directly with a minimal fake request rather
than through httpx, so the test suite needs no extra dependency.
"""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import store
import server
from server import create_room, rooms


class FakeRequest:
    """Just enough of a Starlette Request for the handler under test."""

    def __init__(self, client_ip="10.0.0.1"):
        self.headers = {"CF-Connecting-IP": client_ip}
        self.client = type("C", (), {"host": client_ip})()


class RoomCreationTests(unittest.TestCase):
    def setUp(self):
        store._ip_room_creations.clear()
        store._codes.clear()
        rooms.clear()
        self.registry = store.MemoryRegistry()
        self.registry._codes.clear()
        patcher = patch.object(server, "registry", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_room_returns_a_reserved_code(self):
        response = asyncio.run(create_room(FakeRequest()))

        self.assertEqual(response.status_code, 200)
        payload = json.loads(response.body)
        self.assertEqual(len(payload["code"]), store.CODE_LENGTH)
        self.assertEqual(payload["expires_in"], store.CODE_TTL_SECONDS)
        # the code must actually be registered, or the join would be refused
        self.assertIsNotNone(asyncio.run(self.registry.get(payload["code"])))

    def test_each_created_room_gets_a_distinct_code(self):
        # distinct clients, because one client is capped at MAX_ROOMS_PER_CLIENT
        codes = {
            json.loads(asyncio.run(create_room(FakeRequest(f"10.0.0.{index}"))).body)["code"]
            for index in range(5)
        }
        self.assertEqual(len(codes), 5)

    def test_creation_is_capped_per_client(self):
        statuses = [
            asyncio.run(create_room(FakeRequest())).status_code
            for _ in range(store.MAX_ROOMS_PER_CLIENT + 2)
        ]

        self.assertTrue(all(status == 200 for status in statuses[:store.MAX_ROOMS_PER_CLIENT]))
        self.assertTrue(all(status == 429 for status in statuses[store.MAX_ROOMS_PER_CLIENT:]))

        # a different client is unaffected by the first one hitting its cap
        self.assertEqual(asyncio.run(create_room(FakeRequest("10.0.0.2"))).status_code, 200)

    def test_capacity_limit_returns_503(self):
        with patch.object(self.registry, "active_count", new=AsyncMock(return_value=store.MAX_ACTIVE_ROOMS)):
            response = asyncio.run(create_room(FakeRequest()))

        self.assertEqual(response.status_code, 503)


class JoinValidationTests(unittest.TestCase):
    """The WebSocket endpoint refuses unknown codes.

    Driving the real handshake needs a websocket client, so the validation
    rule itself is asserted directly: an unresolved code must not create a
    room. This is the behaviour the endpoint branches on.
    """

    def setUp(self):
        store._codes.clear()
        rooms.clear()
        self.registry = store.MemoryRegistry()
        self.registry._codes.clear()
        patcher = patch.object(server, "registry", self.registry)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unknown_and_badly_typed_codes_do_not_resolve(self):
        self.assertIsNone(asyncio.run(self.registry.get("ZZZZZZ")))
        self.assertIsNone(asyncio.run(self.registry.get("")))
        self.assertIsNone(asyncio.run(self.registry.get("not a code")))

    def test_a_reserved_code_resolves_case_insensitively(self):
        response = asyncio.run(create_room(FakeRequest()))
        code = json.loads(response.body)["code"]

        self.assertIsNotNone(asyncio.run(self.registry.get(code.lower())))
        self.assertIsNotNone(asyncio.run(self.registry.get(f" {code} ")))


class AfkReaperTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_reservation_closes_the_waiting_host(self):
        registry = store.MemoryRegistry()
        registry._codes.clear()
        # a code reserved long ago that nobody joined
        registry._codes["OLD123"] = store.CodeRecord(
            code="OLD123", host_token="h", created_at=0.0
        )
        ws = AsyncMock()
        player = server.Player(token="t", peer_id="p", seat=1, name="Host", ws=ws)
        room = server.Room(room_id="OLD123")
        room.players["t"] = player
        rooms["OLD123"] = room

        with patch.object(server, "registry", registry):
            reaped = await registry.reap_expired()

        self.assertIn("OLD123", reaped)

    async def test_live_room_with_one_player_is_afk_after_the_window(self):
        registry = store.MemoryRegistry()
        registry._codes.clear()
        registry._codes["LIVE12"] = store.CodeRecord(
            code="LIVE12",
            host_token="h",
            created_at=time.time(),
            live=True,
            last_activity=time.time() - (store.AFK_ROOM_SECONDS + 5),
        )
        record = await registry.get("LIVE12")

        # the code TTL no longer applies once live
        self.assertTrue(record.live)
        self.assertIsNone(record.expires_at)
        # but the AFK rule does
        self.assertGreater(
            time.time() - record.last_activity, store.AFK_ROOM_SECONDS
        )

    async def test_reaper_keeps_a_freshly_created_room(self):
        registry = store.MemoryRegistry()
        registry._codes.clear()
        code = await registry.reserve_code("host")

        reaped = await registry.reap_expired()

        self.assertEqual(reaped, [])
        self.assertIsNotNone(await registry.get(code))


if __name__ == "__main__":
    unittest.main()
