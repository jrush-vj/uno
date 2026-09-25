"""Tests for game settings, seating, scoring extras and lifetime stats."""

import asyncio
import time
import unittest

import store
from server import (
    GameSettings,
    Player,
    Room,
    apply_seat_order,
    arm_turn_timer,
    leaderboard,
    record_round_stats,
    settle_round,
    shuffle_seats,
)
import server


def make_room(host="host", others=()):
    room = Room(room_id="TEST")
    players = [Player(token=host, peer_id=f"p-{host}", seat=1, name="Host", score=0)]
    for index, name in enumerate(others, start=2):
        players.append(Player(token=name, peer_id=f"p-{name}", seat=index, name=name.title()))
    room.players = {p.token: p for p in players}
    room.host_token = host
    return room


class SettingsValidationTests(unittest.TestCase):
    def test_invalid_values_are_rejected_not_trusted(self):
        settings = GameSettings.from_json({
            "starting_cards": 99,
            "points_mode": "chaos",
            "turn_timer": 7,
        })

        self.assertEqual(settings.starting_cards, 7)
        self.assertEqual(settings.points_mode, "official")
        self.assertEqual(settings.turn_timer, 0)

    def test_valid_values_survive_a_round_trip(self):
        settings = GameSettings.from_json({
            "starting_cards": 5,
            "points_mode": "wins",
            "turn_timer": 30,
        })

        self.assertEqual(settings.starting_cards, 5)
        self.assertEqual(settings.points_mode, "wins")
        self.assertEqual(settings.turn_timer, 30)
        self.assertEqual(GameSettings.from_json(settings.to_json()).to_json(), settings.to_json())

    def test_every_supported_option_is_accepted(self):
        for cards in server.VALID_STARTING_CARDS:
            self.assertEqual(GameSettings.from_json({"starting_cards": cards}).starting_cards, cards)
        for mode in server.VALID_POINTS_MODES:
            self.assertEqual(GameSettings.from_json({"points_mode": mode}).points_mode, mode)
        for timer in server.VALID_TURN_TIMERS:
            self.assertEqual(GameSettings.from_json({"turn_timer": timer}).turn_timer, timer)


class SeatOrderingTests(unittest.TestCase):
    def test_apply_seat_order_reseats_everyone(self):
        room = make_room(others=["bob", "cara", "dan"])
        room.settings.seat_order = ["dan", "host", "cara", "bob"]

        apply_seat_order(room)

        self.assertEqual(room.players["dan"].seat, 1)
        self.assertEqual(room.players["host"].seat, 2)
        self.assertEqual(room.players["cara"].seat, 3)
        self.assertEqual(room.players["bob"].seat, 4)

    def test_players_missing_from_the_order_are_appended(self):
        room = make_room(others=["bob", "cara"])
        # only one player is named; the rest must still get a seat
        room.settings.seat_order = ["cara"]

        apply_seat_order(room)

        seats = sorted(p.seat for p in room.players.values())
        self.assertEqual(seats, [1, 2, 3])
        self.assertEqual(room.players["cara"].seat, 1)

    def test_a_stale_token_is_ignored(self):
        room = make_room(others=["bob"])
        room.settings.seat_order = ["ghost-token", "bob", "host"]

        apply_seat_order(room)

        self.assertEqual(sorted(p.seat for p in room.players.values()), [1, 2])

    def test_shuffle_seats_keeps_every_player_seated(self):
        room = make_room(others=["bob", "cara", "dan", "eve"])

        shuffle_seats(room)

        seats = sorted(p.seat for p in room.players.values())
        self.assertEqual(seats, [1, 2, 3, 4, 5])
        self.assertEqual(len(room.settings.seat_order), 5)


class PointsModeTests(unittest.TestCase):
    def test_official_mode_scores_the_opponents_cards(self):
        room = make_room(others=["bob"])
        room.settings.points_mode = "official"
        room.players["bob"].hand = [
            {"color": "red", "value": "9"},
            {"color": "black", "value": "wild"},
        ]

        points = settle_round(room, room.players["host"])

        self.assertEqual(points, 9 + server.WILD_CARD_POINTS)
        self.assertEqual(room.players["host"].score, points)

    def test_wins_mode_awards_a_flat_bonus(self):
        room = make_room(others=["bob"])
        room.settings.points_mode = "wins"
        room.players["bob"].hand = [{"color": "black", "value": "wild4"}] * 5

        points = settle_round(room, room.players["host"])

        self.assertEqual(points, store.WIN_BONUS_POINTS)
        self.assertLess(points, 5 * server.WILD_CARD_POINTS)

    def test_round_stats_mark_exactly_one_winner(self):
        room = make_room(others=["bob", "cara"])
        winner = room.players["host"]
        winner.round_points = 42

        entries = record_round_stats(room, winner, 42)

        self.assertEqual(len(entries), 3)
        self.assertEqual(sum(1 for entry in entries if entry["won"]), 1)
        self.assertEqual([e["points"] for e in entries if e["won"]], [42])
        self.assertTrue(all(e["rounds"] if "rounds" in e else True for e in entries))


class TurnTimerTests(unittest.TestCase):
    def test_no_deadline_when_the_timer_is_off(self):
        room = make_room(others=["bob"])
        room.started = True
        room.turn_seat = 1
        room.settings.turn_timer = 0

        arm_turn_timer(room)

        self.assertIsNone(room.turn_deadline)

    def test_deadline_is_set_relative_to_now(self):
        room = make_room(others=["bob"])
        room.started = True
        room.turn_seat = 1
        room.settings.turn_timer = 30

        before = time.time()
        arm_turn_timer(room)

        self.assertIsNotNone(room.turn_deadline)
        self.assertGreaterEqual(room.turn_deadline, before + 29)
        self.assertLessEqual(room.turn_deadline, time.time() + 31)

    def test_no_deadline_before_the_round_starts(self):
        room = make_room(others=["bob"])
        room.started = False
        room.settings.turn_timer = 60

        arm_turn_timer(room)

        self.assertIsNone(room.turn_deadline)


class PublicStateTests(unittest.TestCase):
    def test_settings_and_round_travel_with_the_state(self):
        room = make_room(others=["bob"])
        room.settings.starting_cards = 5
        room.settings.turn_timer = 60
        room.round_number = 3

        state = server.public_state(room)

        self.assertEqual(state["settings"]["starting_cards"], 5)
        self.assertEqual(state["settings"]["turn_timer"], 60)
        self.assertEqual(state["round_number"], 3)
        self.assertEqual(state["max_seats"], 6)

    def test_seat_tokens_let_the_host_send_an_order(self):
        room = make_room(others=["bob"])

        state = server.public_state(room)

        self.assertEqual(state["seat_tokens"]["1"], "host")
        self.assertEqual(state["seat_tokens"]["2"], "bob")

    def test_seconds_left_is_sent_rather_than_an_absolute_deadline(self):
        room = make_room(others=["bob"])
        room.started = True
        room.settings.turn_timer = 30
        room.turn_deadline = time.time() + 25

        state = server.public_state(room)

        self.assertIsNotNone(state["turn_seconds_left"])
        self.assertLessEqual(state["turn_seconds_left"], 25)
        self.assertGreater(state["turn_seconds_left"], 20)

    def test_no_countdown_when_the_timer_is_off(self):
        room = make_room(others=["bob"])
        room.turn_deadline = None

        self.assertIsNone(server.public_state(room)["turn_seconds_left"])

    def test_leaderboard_includes_score_and_host_flag(self):
        room = make_room(others=["bob"])
        room.players["bob"].score = 30

        rows = leaderboard(room)

        self.assertEqual(rows[0]["name"], "Bob")
        self.assertTrue(any(row["is_host"] for row in rows))


class StatsStoreTests(unittest.TestCase):
    def setUp(self):
        store._player_stats.clear()
        self.stats = store.MemoryStats()
        self.stats._stats.clear()

    def test_a_round_records_wins_and_points(self):
        asyncio.run(self.stats.record_round([
            {"name": "Host", "points": 40, "round_points": 40, "won": True, "uno_calls": 1},
            {"name": "Bob", "points": 0, "round_points": 0, "won": False, "uno_calls": 0},
        ]))

        host = asyncio.run(self.stats.get("host"))
        bob = asyncio.run(self.stats.get("bob"))

        self.assertEqual(host.points, 40)
        self.assertEqual(host.wins, 1)
        self.assertEqual(host.rounds, 1)
        self.assertEqual(host.best_round, 40)
        self.assertEqual(bob.rounds, 1)
        self.assertEqual(bob.wins, 0)

    def test_names_accumulate_across_case_differences(self):
        asyncio.run(self.stats.record_round([
            {"name": "Jerush", "points": 10, "won": True},
            {"name": "jerush", "points": 5, "won": False},
        ]))

        stats = asyncio.run(self.stats.get("JERUSH"))

        self.assertEqual(stats.rounds, 2)
        self.assertEqual(stats.points, 15)

    def test_leaderboard_is_ordered_by_points_then_wins(self):
        asyncio.run(self.stats.record_round([
            {"name": "Ann", "points": 50, "won": True},
            {"name": "Ben", "points": 90, "won": False},
            {"name": "Cid", "points": 50, "won": False},
        ]))

        rows = asyncio.run(self.stats.top_players())

        self.assertEqual([row["name"] for row in rows], ["Ben", "Ann", "Cid"])
        self.assertAlmostEqual(rows[1]["win_rate"], 1.0)

    def test_win_rate_is_zero_with_no_rounds(self):
        asyncio.run(self.stats.record_round([{"name": "Solo", "points": 0, "won": False}]))
        stats = asyncio.run(self.stats.get("solo"))
        # one round played, no wins
        self.assertEqual(stats.win_rate, 0.0)


if __name__ == "__main__":
    unittest.main()
