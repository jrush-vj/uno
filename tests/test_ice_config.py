import base64
import hashlib
import hmac
import unittest
from unittest.mock import AsyncMock

from server import Player, Room, broadcast_peers, build_ice_servers, public_state


class IceConfigTests(unittest.TestCase):
    def test_stun_only_when_turn_is_not_configured(self):
        servers = build_ice_servers("turn:turn.example.test:3478", None, now=1000)

        self.assertTrue(servers)
        self.assertTrue(all(server["urls"].startswith("stun:") for server in servers))

    def test_turn_uses_expiring_coturn_rest_credentials(self):
        urls = (
            "turn:turn.example.test:3478?transport=udp,"
            "turn:turn.example.test:3478?transport=tcp"
        )
        servers = build_ice_servers(urls, "test-shared-secret", 600, now=1000)
        turn_servers = [
            server for server in servers
            if server["urls"].startswith(("turn:", "turns:"))
        ]

        self.assertEqual(len(turn_servers), 3)
        username = "1600:uno"
        expected = base64.b64encode(
            hmac.new(b"test-shared-secret", username.encode(), hashlib.sha1).digest()
        ).decode("ascii")
        self.assertTrue(all(server["username"] == username for server in turn_servers))
        self.assertTrue(all(server["credential"] == expected for server in turn_servers))
        self.assertEqual(turn_servers[-1]["urls"], "turns:turn.example.test:5349")

    def test_turn_ttl_is_bounded(self):
        servers = build_ice_servers("turn:turn.example.test:3478", "secret", 999999, now=1000)
        turn_server = next(server for server in servers if server["urls"].startswith("turn:"))

        self.assertEqual(turn_server["username"], "87400:uno")


class PublicStateSecurityTests(unittest.TestCase):
    def setUp(self):
        self.room = Room(room_id="test")
        self.player = Player(
            token="private-reconnect-token",
            peer_id="ephemeral-webrtc-peer-id",
            seat=1,
            name="Player",
        )
        self.room.players[self.player.token] = self.player

    def test_public_state_does_not_expose_reconnect_token(self):
        seat = public_state(self.room)["seats"]["1"]

        self.assertNotIn("token", seat)

    def test_peer_id_is_separate_from_reconnect_token(self):
        self.assertNotEqual(self.player.peer_id, self.player.token)


class PeerRosterTests(unittest.IsolatedAsyncioTestCase):
    async def test_peer_roster_sends_peer_id_but_never_reconnect_token(self):
        room = Room(room_id="test")
        players = [
            Player(
                token=f"private-reconnect-token-{seat}",
                peer_id=f"ephemeral-peer-id-{seat}",
                seat=seat,
                name=f"Player {seat}",
                ws=AsyncMock(),
            )
            for seat in (1, 2)
        ]
        room.players = {player.token: player for player in players}

        await broadcast_peers(room)

        for player in players:
            sent = player.ws.send_text.await_args.args[0]
            self.assertIn(player.peer_id, sent)
            self.assertNotIn(player.token, sent)


if __name__ == "__main__":
    unittest.main()
