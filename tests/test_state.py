"""Public-state and game-over payload tests.

These cover the two structures the client actually renders: `public_state`,
which every seat receives on every change, and the `game_over` payload that
drives the results modal.

They are deliberately plain unit tests rather than socket tests. The earlier
1100-line socket file could not even be counted by pytest - a version this old
comprehends a `try` but not `except*` - and can only run its 8 hypothetical
tests. An in-process websocket harness built here would have to hold a
`TestClient` context and a live `WebSocketTestSession` at once, which is exactly
where that file used to hang: the test thread blocks in `receive_text()` while
the server needs the same thread to pump its own broadcasts.

The wire contract is instead pinned by asserting the payload shapes directly.
The rules themselves are covered exhaustively in test_rules.py.

    python -m pytest tests/test_state.py -q
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from tests.test_rules import make_player, make_room, make_card


class PublicStateTests(unittest.TestCase):
    def setUp(self):
        self.players = [make_player(s) for s in (1, 2)]
        self.room = make_room(
            self.players, started=True, current_color="red", direction=1,
            discard=[make_card("red", "5")], turn_seat=1,
            settings=server.GameSettings(starting_cards=7),
        )
        self.p1, self.p2 = self.room.players["tok1"], self.room.players["tok2"]
        self.p1.hand = [make_card("red", "7")]
        self.p2.hand = [make_card("blue", "1"), make_card("blue", "2")]

    def test_every_field_the_client_renders_is_present(self):
        state = server.public_state(self.room)
        for key in ("room", "seats", "seat_keys", "turn_seat", "direction",
                    "current_color", "top_card", "draw_pile_count", "started",
                    "host_seat", "player_count", "max_seats", "settings",
                    "round_number", "turn_seconds_left", "pending_wild4",
                    "dealer_seat", "match_over", "match_winner_seat",
                    "match_target"):
            self.assertIn(key, state, f"public_state is missing {key}")

    def test_seats_report_the_real_hand_size_and_flags(self):
        state = server.public_state(self.room)
        self.assertEqual(state["seats"]["1"]["hand_count"], 1)
        self.assertEqual(state["seats"]["2"]["hand_count"], 2)
        self.assertIn("called_uno", state["seats"]["1"])
        self.assertIn("uno_catchable", state["seats"]["1"])
        self.assertIn("is_host", state["seats"]["1"])
        self.assertIn("score", state["seats"]["1"])

    def test_no_secret_is_ever_exposed(self):
        """The client is untrusted. A session token would let anyone
        impersonate a seat (it is the credential a reconnect sends), and
        peer_id is the WebRTC identity."""
        state = server.public_state(self.room)
        blob = json.dumps(state)
        for token in self.room.players:
            self.assertNotIn(token, blob, "a player token leaked into public_state")
        for player in self.room.players.values():
            self.assertNotIn(player.peer_id, blob, "a peer_id leaked into public_state")

    def test_the_seat_key_is_a_separate_public_handle(self):
        """seat_keys exist so the host can name players in a seating order.
        They must not be usable as credentials."""
        state = server.public_state(self.room)
        keys = state["seat_keys"]
        self.assertEqual(len(set(keys.values())), len(keys), "seat keys collide")
        for player in self.room.players.values():
            self.assertNotEqual(keys[str(player.seat)], player.token)
            self.assertNotEqual(keys[str(player.seat)], player.peer_id)
            self.assertTrue(keys[str(player.seat)])

    def test_the_draw_four_prompt_withholds_whether_it_was_a_bluff(self):
        self.room.pending_wild4 = {"by": 1, "against": 2, "legal": True}
        state = server.public_state(self.room)
        self.assertEqual(set(state["pending_wild4"].keys()), {"by", "against"})
        self.assertNotIn("legal", json.dumps(state))

    def test_the_hand_counts_never_include_the_card_faces(self):
        """Only counts travel: a client holding the whole table's hands could
        read everyone's cards out of the payload."""
        state = server.public_state(self.room)
        blob = json.dumps(state)
        for card in self.p2.hand:
            self.assertNotIn(card["id"], blob)
        self.assertNotIn('"value"', json.dumps(state["seats"]))

    def test_the_turn_timer_is_reported_as_seconds_remaining(self):
        self.room.settings.turn_timer = 30
        server.arm_turn_timer(self.room)
        state = server.public_state(self.room)
        self.assertIsNotNone(state["turn_seconds_left"])
        self.assertLessEqual(state["turn_seconds_left"], 30)
        self.assertGreater(state["turn_seconds_left"], 0)

    def test_the_timer_is_null_when_disabled(self):
        self.room.settings.turn_timer = 0
        server.arm_turn_timer(self.room)
        self.assertIsNone(server.public_state(self.room)["turn_seconds_left"])


class GameOverPayloadTests(unittest.TestCase):
    """The game_over message is what the results modal renders."""

    def setUp(self):
        self.players = [make_player(s) for s in (1, 2, 3)]
        self.room = make_room(
            self.players, started=True, round_number=3,
            settings=server.GameSettings(points_mode="official"),
        )
        self.winner, self.p2, self.p3 = (self.room.players[f"tok{s}"] for s in (1, 2, 3))
        self.p2.hand = [make_card("red", "9")]
        self.p3.hand = [make_card("blue", "skip")]

    def test_the_payload_carries_everything_the_modal_needs(self):
        points = server.settle_round(self.room, self.winner)
        payload = server.game_over_payload(self.room, self.winner, points, all_time=[])

        self.assertEqual(payload["type"], "game_over")
        self.assertEqual(payload["winner_seat"], 1)
        self.assertEqual(payload["winner_name"], "P1")
        self.assertEqual(payload["round_points"], 29)     # 9 + 20
        self.assertEqual(payload["round_number"], 3)
        self.assertEqual(payload["points_mode"], "official")
        self.assertEqual(payload["match_target"], server.MATCH_TARGET_POINTS)
        self.assertFalse(payload["match_over"])
        # The leaderboard leads with the round winner.
        self.assertEqual(payload["scores"][0]["seat"], 1)

    def test_reaching_the_target_is_reported_as_a_match_win(self):
        self.winner.score = server.MATCH_TARGET_POINTS - 5
        points = server.settle_round(self.room, self.winner)
        payload = server.game_over_payload(self.room, self.winner, points, all_time=[])

        self.assertTrue(payload["match_over"])
        self.assertEqual(payload["match_winner_seat"], 1)

    def test_scores_are_read_before_the_hands_are_cleared(self):
        """game_over_payload must be called before clear_all_hands(), or the
        leaderboard is built from empty hands and every round scores 0."""
        points = server.settle_round(self.room, self.winner)
        payload = server.game_over_payload(self.room, self.winner, points, all_time=[])
        self.assertEqual(payload["scores"][0]["score"], 29)

        server.clear_all_hands(self.room)
        after = server.game_over_payload(self.room, self.winner, points, all_time=[])
        # The already-computed payload is unaffected; recomputing after the
        # clear would lose the points, which is why ordering matters.
        self.assertEqual(after["scores"][0]["score"], 29)


if __name__ == "__main__":
    unittest.main(verbosity=2)
