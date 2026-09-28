"""Rules-engine tests for the UNO server.

Nothing here exercised the actual game rules before: the suite covered room
codes, the registry and settings validation, but not handle_play, handle_draw,
advance_turn, the UNO call/catch window, reshuffling or scoring. Every bug
these tests now pin down - the last-card penalty skip, the unenforceable Draw
Four, the self-catch, the eternal turn timer - was live in production.

Tables are built directly from make_card() rather than by dealing from a
shuffled deck, so each case states exactly the hand it needs and a failure is
readable. These are plain unit tests: no sockets, no server, no timing.

    python -m pytest tests/test_rules.py -q
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
from server import (
    Room,
    Player,
    GameSettings,
    make_card,
    card_playable,
    wild4_was_legal,
    handle_play,
    handle_draw,
    handle_call_uno,
    handle_catch_uno,
    handle_challenge,
    advance_turn,
    start_game,
    settle_round,
    reset_match,
    build_deck,
    reshuffle_discard_into_deck,
    card_points,
    most_common_colour,
    next_dealer_seat,
    MATCH_TARGET_POINTS,
    DRAW2_PENALTY,
    DRAW4_PENALTY,
    CHALLENGE_FAIL_PENALTY,
)


def make_player(seat, name=None, hand=None, **kwargs):
    p = Player(
        token=f"tok{seat}",
        peer_id=f"peer{seat}",
        seat=seat,
        name=name or f"P{seat}",
        hand=list(hand or []),
    )
    for key, value in kwargs.items():
        setattr(p, key, value)
    return p


def make_room(players, **kwargs):
    room = Room(room_id="TEST")
    for p in players:
        room.players[p.token] = p
    for key, value in kwargs.items():
        setattr(room, key, value)
    return room


# --------------------------------------------------------------------------
# Deck
# --------------------------------------------------------------------------

class DeckTests(unittest.TestCase):
    def test_deck_is_the_standard_108_cards(self):
        deck = build_deck()
        self.assertEqual(len(deck), 108)
        counts = {}
        for c in deck:
            counts[(c["color"], c["value"])] = counts.get((c["color"], c["value"]), 0) + 1
        for colour in server.COLORS:
            # One zero, and two of every other number and action.
            self.assertEqual(counts[(colour, "0")], 1)
            for value in [str(n) for n in range(1, 10)]:
                self.assertEqual(counts[(colour, value)], 2, f"{colour} {value}")
            for value in ("skip", "reverse", "draw2"):
                self.assertEqual(counts[(colour, value)], 2, f"{colour} {value}")
        self.assertEqual(counts[("black", "wild")], 4)
        self.assertEqual(counts[("black", "wild4")], 4)

    def test_every_card_id_is_unique(self):
        deck = build_deck()
        self.assertEqual(len({c["id"] for c in deck}), 108)


class ReshuffleTests(unittest.TestCase):
    def test_reshuffle_keeps_the_top_discard_and_recycles_the_rest(self):
        top = make_card("red", "5")
        buried = [make_card("blue", "1"), make_card("green", "skip")]
        room = make_room([make_player(1)], deck=[], discard=buried + [top])

        reshuffle_discard_into_deck(room)

        self.assertEqual(room.discard, [top])
        self.assertEqual(len(room.deck), 2)
        self.assertNotIn(top, room.deck)

    def test_drawing_more_than_the_pile_holds_reshuffles_and_supplies_what_exists(self):
        """A Draw Four on a nearly empty pile consumes the deck, reshuffles the
        discard, and deals what remains. Staying inside what the pile can give
        is what stops a penalty from raising mid-turn."""
        room = make_room([make_player(1)], deck=[], discard=[make_card("red", "1"),
                                                            make_card("blue", "2"),
                                                            make_card("red", "5")])
        drawn = server.draw_from_deck(room, 4)
        self.assertEqual(len(drawn), 2)          # everything but the kept top card
        self.assertEqual(len(room.discard), 1)   # the top card stays put

    def test_drawing_from_an_empty_pile_reshuffles_rather_than_failing(self):
        top = make_card("red", "5")
        room = make_room([make_player(1)], deck=[], discard=[make_card("red", "1"), top])
        drawn = server.draw_from_deck(room, 1)
        self.assertEqual(len(drawn), 1)
        self.assertEqual(len(room.discard), 1)

    def test_drawing_a_penalty_larger_than_the_pile_deals_what_exists(self):
        """A Draw Four on a nearly empty pile must not raise - it deals what
        the deck and discard can supply."""
        room = make_room([make_player(1)], deck=[make_card("red", "1")], discard=[make_card("red", "2")])
        drawn = server.draw_from_deck(room, 4)
        self.assertEqual(len(drawn), 1)   # the single deck card; the discard is only the kept top


# --------------------------------------------------------------------------
# Legality
# --------------------------------------------------------------------------

class PlayabilityTests(unittest.TestCase):
    def setUp(self):
        self.room = make_room([make_player(1)], current_color="red",
                              discard=[make_card("red", "5")])

    def test_colour_match_is_playable(self):
        self.assertTrue(card_playable(self.room, make_card("red", "9")))

    def test_value_match_across_colours_is_playable(self):
        self.assertTrue(card_playable(self.room, make_card("blue", "5")))

    def test_action_card_matches_its_symbol(self):
        # The pile top must be the action card too, or the colour comparison in
        # card_playable() makes every red card playable on a red pile and the
        # symbol match is never exercised.
        self.room.discard = [make_card("green", "skip")]
        self.room.current_color = "green"
        self.assertTrue(card_playable(self.room, make_card("red", "skip")))
        self.assertFalse(card_playable(self.room, make_card("red", "reverse")))

    def test_nothing_else_is_playable(self):
        self.assertFalse(card_playable(self.room, make_card("blue", "9")))

    def test_wilds_are_always_playable(self):
        self.assertTrue(card_playable(self.room, make_card("black", "wild")))
        self.assertTrue(card_playable(self.room, make_card("black", "wild4")))

    def test_a_card_matching_the_declared_colour_is_playable(self):
        """After a wild the live colour is the declared one, not the pile's."""
        self.room.current_color = "blue"
        self.room.discard = [make_card("black", "wild")]
        self.assertTrue(card_playable(self.room, make_card("blue", "3")))
        self.assertFalse(card_playable(self.room, make_card("red", "3")))


class Wild4LegalityTests(unittest.TestCase):
    def test_legal_when_no_card_of_the_live_colour_is_held(self):
        room = make_room([make_player(1)], current_color="red")
        player = room.players["tok1"]
        player.hand = [make_card("black", "wild4"), make_card("blue", "7")]
        card = player.hand[0]
        self.assertTrue(wild4_was_legal(room, player, card))

    def test_illegal_when_a_card_of_the_live_colour_is_held(self):
        room = make_room([make_player(1)], current_color="red")
        player = room.players["tok1"]
        player.hand = [make_card("black", "wild4"), make_card("red", "7")]
        card = player.hand[0]
        self.assertFalse(wild4_was_legal(room, player, card))

    def test_the_played_card_is_not_counted_against_itself(self):
        """The card being played is black, but the helper must not treat it as
        a black 'match' either way - it is excluded by id."""
        room = make_room([make_player(1)], current_color="black")
        player = room.players["tok1"]
        player.hand = [make_card("black", "wild4")]
        self.assertTrue(wild4_was_legal(room, player, player.hand[0]))


# --------------------------------------------------------------------------
# Turn order
# --------------------------------------------------------------------------

class TurnOrderTests(unittest.TestCase):
    def setUp(self):
        self.players = [make_player(s) for s in (1, 2, 3)]
        self.room = make_room(self.players, turn_seat=1, direction=1, started=True,
                              settings=GameSettings(turn_timer=0))

    def test_advances_to_the_next_seat(self):
        advance_turn(self.room, 1)
        self.assertEqual(self.room.turn_seat, 2)

    def test_two_steps_skips_one_player(self):
        advance_turn(self.room, 2)
        self.assertEqual(self.room.turn_seat, 3)

    def test_wraps_around_the_table(self):
        self.room.turn_seat = 3
        advance_turn(self.room, 1)
        self.assertEqual(self.room.turn_seat, 1)

    def test_counter_clockwise_runs_the_other_way(self):
        self.room.direction = -1
        advance_turn(self.room, 1)
        self.assertEqual(self.room.turn_seat, 3)

    def test_the_catch_window_closes_when_the_next_player_is_due(self):
        """The official rule: an uncalled player is catchable only until the
        next player acts. Sitting on one card across a turn must not stay
        catchable forever."""
        target = self.room.players["tok2"]
        target.hand = [make_card("red", "3")]
        target.called_uno = False
        target.uno_catchable = True
        # Anchored to the seat that follows them, which is seat 3 here.
        target.uno_grace_holder = 3

        self.room.turn_seat = 3
        advance_turn(self.room, 1)          # seat 3 acts, so the window shuts
        self.assertFalse(target.uno_catchable)

    def test_the_catch_window_stays_open_while_the_target_is_still_next(self):
        target = self.room.players["tok2"]
        target.hand = [make_card("red", "3")]
        target.uno_catchable = True
        target.uno_grace_holder = 3

        self.room.turn_seat = 1
        advance_turn(self.room, 1)          # turn moves to 2, not past 3
        self.assertTrue(target.uno_catchable)

    def test_two_player_reverse_acts_as_a_skip(self):
        """With two players, reversing direction lands back on whoever played,
        which is the official behaviour."""
        room = make_room([make_player(1), make_player(2)], turn_seat=1, direction=1,
                         started=True, settings=GameSettings(turn_timer=0))
        room.discard = [make_card("red", "5")]
        room.current_color = "red"
        room.players["tok1"].hand = [make_card("red", "reverse")]

        handle_play(room, room.players["tok1"], room.players["tok1"].hand[0]["id"], None)

        self.assertEqual(room.direction, -1)
        self.assertEqual(room.turn_seat, 1)   # same player goes again


# --------------------------------------------------------------------------
# Playing cards
# --------------------------------------------------------------------------

class PlayTests(unittest.TestCase):
    def setUp(self):
        self.players = [make_player(s) for s in (1, 2, 3)]
        self.room = make_room(
            self.players,
            turn_seat=1,
            direction=1,
            started=True,
            current_color="red",
            discard=[make_card("red", "5")],
            settings=GameSettings(turn_timer=0),
        )
        self.p1, self.p2, self.p3 = (self.room.players[f"tok{s}"] for s in (1, 2, 3))

    def play(self, player, card, colour=None):
        return handle_play(self.room, player, card["id"], colour)

    def test_a_normal_play_moves_the_turn_on_and_lays_the_card(self):
        card = make_card("red", "9")
        self.p1.hand = [card, make_card("blue", "1")]
        winner, victim = self.play(self.p1, card)
        self.assertFalse(winner)
        self.assertIsNone(victim)
        self.assertEqual(self.room.discard[-1], card)
        self.assertEqual(self.room.turn_seat, 2)

    def test_an_illegal_card_is_rejected_without_changing_anything(self):
        card = make_card("green", "9")
        self.p1.hand = [card]
        with self.assertRaises(ValueError):
            self.play(self.p1, card)
        self.assertEqual(self.p1.hand, [card])
        self.assertEqual(self.room.turn_seat, 1)

    def test_playing_out_of_turn_is_rejected(self):
        card = make_card("red", "9")
        self.p2.hand = [card]
        with self.assertRaises(ValueError):
            self.play(self.p2, card)

    def test_skip_skips_exactly_one_player(self):
        card = make_card("red", "skip")
        self.p1.hand = [card, make_card("blue", "1")]
        self.play(self.p1, card)
        self.assertEqual(self.room.turn_seat, 3)

    def test_draw_two_makes_the_next_player_draw_and_loses_their_turn(self):
        card = make_card("red", "draw2")
        self.p1.hand = [card, make_card("blue", "1")]
        self.p2.hand = [make_card("green", "1")]
        self.room.deck = [make_card("yellow", "9"), make_card("yellow", "8")]

        winner, victim = self.play(self.p1, card)

        self.assertIs(victim, self.p2)
        self.assertEqual(len(self.p2.hand), 3)          # 1 + 2 drawn
        self.assertEqual(self.room.turn_seat, 3)        # p2 is skipped

    def test_wild_requires_a_chosen_colour(self):
        card = make_card("black", "wild")
        self.p1.hand = [card, make_card("blue", "1")]
        with self.assertRaises(ValueError):
            self.play(self.p1, card, None)
        self.play(self.p1, card, "green")
        self.assertEqual(self.room.current_color, "green")

    def test_a_colour_on_a_non_wild_is_ignored(self):
        card = make_card("red", "9")
        self.p1.hand = [card, make_card("blue", "1")]
        self.play(self.p1, card, "green")
        self.assertEqual(self.room.current_color, "red")

    def test_after_drawing_only_the_drawn_card_may_be_played(self):
        drawn = make_card("red", "7")
        other = make_card("red", "8")
        self.p1.hand = [drawn, other]
        self.p1.drew_this_turn = True
        self.p1.last_drawn_card_id = drawn["id"]

        with self.assertRaises(ValueError):
            self.play(self.p1, other)
        self.play(self.p1, drawn)

    def test_nothing_may_be_played_while_a_draw_four_is_pending(self):
        self.room.pending_wild4 = {"by": 1, "against": 2, "legal": False}
        card = make_card("red", "9")
        self.p1.hand = [card, make_card("blue", "1")]
        with self.assertRaises(ValueError):
            self.play(self.p1, card)

    def test_going_out_wins_the_round(self):
        card = make_card("red", "9")
        self.p1.hand = [card]
        winner, _ = self.play(self.p1, card)
        self.assertTrue(winner)
        self.assertFalse(self.room.started)
        self.assertEqual(self.p1.hand, [])

    def test_going_out_on_a_draw_two_still_makes_the_next_player_draw(self):
        """This was the headline bug: the win check ran BEFORE the action-card
        branch, so finishing on a Draw Two or Draw Four silently skipped the
        penalty the official rules still require."""
        card = make_card("red", "draw2")
        self.p1.hand = [card]
        self.p2.hand = [make_card("green", "1")]
        self.room.deck = [make_card("yellow", "9"), make_card("yellow", "8")]

        winner, victim = self.play(self.p1, card)

        self.assertTrue(winner)
        self.assertIs(victim, self.p2)
        self.assertEqual(len(self.p2.hand), 3)

    def test_going_out_on_a_wild_four_still_penalises_the_next_player(self):
        card = make_card("black", "wild4")
        self.p1.hand = [card]
        self.p2.hand = [make_card("green", "1")]
        self.room.deck = [make_card("yellow", str(n)) for n in range(4)]

        winner, victim = self.play(self.p1, card, "blue")

        self.assertTrue(winner)
        self.assertIs(victim, self.p2)
        self.assertEqual(len(self.p2.hand), 5)

    def test_going_out_clears_every_catch_window(self):
        self.p2.hand = [make_card("red", "1")]
        self.p2.uno_catchable = True
        card = make_card("red", "9")
        self.p1.hand = [card]
        self.play(self.p1, card)
        self.assertFalse(self.p2.uno_catchable)


# --------------------------------------------------------------------------
# Wild Draw Four challenge
# --------------------------------------------------------------------------

class Wild4ChallengeTests(unittest.TestCase):
    def setUp(self):
        self.players = [make_player(s) for s in (1, 2, 3)]
        self.room = make_room(
            self.players, turn_seat=1, direction=1, started=True,
            current_color="red", discard=[make_card("red", "5")],
            settings=GameSettings(turn_timer=0),
        )
        self.p1, self.p2, self.p3 = (self.room.players[f"tok{s}"] for s in (1, 2, 3))
        self.room.deck = [make_card("yellow", str(n)) for n in range(10)]

    def test_a_legal_draw_four_applies_the_penalty_immediately(self):
        card = make_card("black", "wild4")
        self.p1.hand = [card, make_card("blue", "1")]     # no red = legal
        self.p2.hand = [make_card("green", "1")]

        winner, victim = handle_play(self.room, self.p1, card["id"], "blue")

        self.assertIs(victim, self.p2)
        self.assertEqual(len(self.p2.hand), 5)
        self.assertIsNone(self.room.pending_wild4)       # nothing to dispute
        self.assertEqual(self.room.turn_seat, 3)

    def test_a_bluff_opens_a_challenge_instead_of_penalising(self):
        card = make_card("black", "wild4")
        self.p1.hand = [card, make_card("red", "1")]      # holds red = a bluff
        self.p2.hand = [make_card("green", "1")]

        winner, victim = handle_play(self.room, self.p1, card["id"], "blue")

        self.assertIsNone(victim)                         # penalty is held
        self.assertIsNotNone(self.room.pending_wild4)
        self.assertEqual(self.room.pending_wild4["against"], 2)
        self.assertEqual(len(self.p2.hand), 1)            # not yet drawn
        # The turn stays on the bluffer: the victim answers out of turn, and
        # parking it on an empty seat would freeze the table.
        self.assertEqual(self.room.turn_seat, 1)

    def test_accepting_draws_four_and_continues_from_the_victim(self):
        card = make_card("black", "wild4")
        self.p1.hand = [card, make_card("red", "1")]
        self.p2.hand = [make_card("green", "1")]
        handle_play(self.room, self.p1, card["id"], "blue")

        caught, charged = handle_challenge(self.room, self.p2, accept=True)

        self.assertFalse(caught)
        self.assertIs(charged, self.p2)
        self.assertEqual(len(self.p2.hand), 1 + DRAW4_PENALTY)
        self.assertIsNone(self.room.pending_wild4)

    def test_a_successful_challenge_moves_the_four_to_the_bluffer(self):
        card = make_card("black", "wild4")
        self.p1.hand = [card, make_card("red", "1")]
        self.p2.hand = [make_card("green", "1")]
        handle_play(self.room, self.p1, card["id"], "blue")

        caught, charged = handle_challenge(self.room, self.p2, accept=False)

        self.assertTrue(caught)
        self.assertIs(charged, self.p1)
        self.assertEqual(len(self.p1.hand), 1 + DRAW4_PENALTY)   # the 1 red + 4
        self.assertEqual(len(self.p2.hand), 1)                    # unharmed
        self.assertEqual(self.room.turn_seat, 2)                  # plays on

    def test_only_the_target_can_answer(self):
        self.room.pending_wild4 = {"by": 1, "against": 2, "legal": False}
        with self.assertRaises(ValueError):
            handle_challenge(self.room, self.p3, accept=True)

    def test_answering_with_nothing_pending_is_rejected(self):
        with self.assertRaises(ValueError):
            handle_challenge(self.room, self.p2, accept=True)

    def test_nobody_can_play_while_the_challenge_is_open(self):
        self.room.pending_wild4 = {"by": 1, "against": 2, "legal": False}
        self.p2.hand = [make_card("green", "1"), make_card("blue", "2")]
        with self.assertRaises(ValueError):
            handle_play(self.room, self.p2, self.p2.hand[0]["id"], None)


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

class DrawTests(unittest.TestCase):
    def setUp(self):
        self.players = [make_player(s) for s in (1, 2)]
        self.room = make_room(
            self.players, turn_seat=1, direction=1, started=True,
            current_color="red", discard=[make_card("red", "5")],
            settings=GameSettings(turn_timer=0),
        )
        self.p1, self.p2 = self.room.players["tok1"], self.room.players["tok2"]

    def test_an_unplayable_draw_passes_the_turn_automatically(self):
        self.p1.hand = []
        self.room.deck = [make_card("green", "9")]
        drawn, playable = handle_draw(self.room, self.p1)
        self.assertEqual(len(drawn), 1)
        self.assertFalse(playable)
        self.assertEqual(self.room.turn_seat, 2)

    def test_a_playable_draw_keeps_the_turn_and_locks_the_card(self):
        self.p1.hand = []
        self.room.deck = [make_card("red", "9")]
        drawn, playable = handle_draw(self.room, self.p1)
        self.assertTrue(playable)
        self.assertEqual(self.room.turn_seat, 1)
        self.assertTrue(self.p1.drew_this_turn)
        self.assertEqual(self.p1.last_drawn_card_id, drawn[0]["id"])

    def test_drawing_twice_in_a_turn_is_rejected(self):
        self.p1.hand = []
        self.room.deck = [make_card("red", "9"), make_card("red", "8")]
        handle_draw(self.room, self.p1)
        with self.assertRaises(ValueError):
            handle_draw(self.room, self.p1)

    def test_drawing_is_rejected_off_turn(self):
        self.p2.hand = []
        with self.assertRaises(ValueError):
            handle_draw(self.room, self.p2)

    def test_drawing_is_rejected_while_a_draw_four_is_pending(self):
        self.room.pending_wild4 = {"by": 1, "against": 2, "legal": False}
        self.room.turn_seat = 2
        self.p2.hand = []
        with self.assertRaises(ValueError):
            handle_draw(self.room, self.p2)

    def test_drawing_drops_a_stale_uno_declaration(self):
        """You announced a one-card hand; drawing took that away."""
        self.p1.hand = [make_card("blue", "1")]
        self.p1.called_uno = True
        self.room.deck = [make_card("green", "9")]
        handle_draw(self.room, self.p1)
        self.assertFalse(self.p1.called_uno)

    def test_an_empty_deck_and_discard_passes_the_turn(self):
        self.p1.hand = []
        self.room.deck = []
        self.room.discard = [make_card("red", "5")]
        drawn, playable = handle_draw(self.room, self.p1)
        self.assertEqual(drawn, [])
        self.assertFalse(playable)
        self.assertEqual(self.room.turn_seat, 2)


# --------------------------------------------------------------------------
# UNO call and catch
# --------------------------------------------------------------------------

class UnoCallTests(unittest.TestCase):
    def setUp(self):
        self.players = [make_player(s) for s in (1, 2, 3)]
        self.room = make_room(
            self.players, turn_seat=1, direction=1, started=True,
            current_color="red", discard=[make_card("red", "5")],
            settings=GameSettings(turn_timer=0),
        )
        self.p1, self.p2, self.p3 = (self.room.players[f"tok{s}"] for s in (1, 2, 3))
        self.room.deck = [make_card("yellow", str(n)) for n in range(10)]

    def test_playing_down_to_one_card_opens_the_catch_window(self):
        card = make_card("red", "9")
        self.p1.hand = [card, make_card("blue", "1")]
        handle_play(self.room, self.p1, card["id"], None)
        self.assertTrue(self.p1.uno_catchable)

    def test_declaring_before_the_play_closes_the_window(self):
        card = make_card("red", "9")
        self.p1.hand = [card, make_card("blue", "1")]
        handle_call_uno(self.p1)
        handle_play(self.room, self.p1, card["id"], None)
        self.assertFalse(self.p1.uno_catchable)

    def test_a_declared_player_cannot_be_caught(self):
        self.p2.hand = [make_card("green", "1")]
        self.p2.called_uno = True
        self.p2.uno_catchable = False
        self.assertFalse(handle_catch_uno(self.room, self.p3, 2))
        self.assertEqual(len(self.p2.hand), 1)

    def test_an_uncalled_player_is_caught_for_two_cards(self):
        self.p2.hand = [make_card("green", "1")]
        self.p2.called_uno = False
        self.p2.uno_catchable = True
        self.assertTrue(handle_catch_uno(self.room, self.p3, 2))
        self.assertEqual(len(self.p2.hand), 1 + server.UNO_CATCH_PENALTY)
        self.assertFalse(self.p2.uno_catchable)

    def test_a_player_cannot_catch_themselves(self):
        """The old handler never compared the caller to the target, so a player
        could fire the penalty at their own seat."""
        self.p2.hand = [make_card("green", "1")]
        self.p2.uno_catchable = True
        self.assertFalse(handle_catch_uno(self.room, self.p2, 2))
        self.assertEqual(len(self.p2.hand), 1)

    def test_a_catch_outside_the_window_is_rejected(self):
        """Sitting on one uncalled card past the next player's turn is safe."""
        self.p2.hand = [make_card("green", "1")]
        self.p2.called_uno = False
        self.p2.uno_catchable = False
        self.assertFalse(handle_catch_uno(self.room, self.p3, 2))
        self.assertEqual(len(self.p2.hand), 1)

    def test_catching_a_player_with_two_cards_is_rejected(self):
        self.p2.hand = [make_card("green", "1"), make_card("blue", "2")]
        self.p2.uno_catchable = True
        self.assertFalse(handle_catch_uno(self.room, self.p3, 2))

    def test_declaring_twice_is_harmless(self):
        self.p1.hand = [make_card("blue", "1")]
        handle_call_uno(self.p1)
        before = self.p1.called_uno
        handle_call_uno(self.p1)
        self.assertTrue(before)
        self.assertTrue(self.p1.called_uno)

    def test_no_points_are_awarded_for_calling(self):
        """The custom +5 bonus per call was not a UNO rule and made the
        scoreboard meaningless, so it is gone."""
        self.p1.hand = [make_card("blue", "1")]
        before = self.p1.score
        handle_call_uno(self.p1)
        self.assertEqual(self.p1.score, before)


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

class ScoringTests(unittest.TestCase):
    def test_card_values_follow_the_official_table(self):
        self.assertEqual(card_points(make_card("red", "7")), 7)
        self.assertEqual(card_points(make_card("red", "0")), 0)
        for value in ("skip", "reverse", "draw2"):
            self.assertEqual(card_points(make_card("red", value)), 20)
        self.assertEqual(card_points(make_card("black", "wild")), 50)
        self.assertEqual(card_points(make_card("black", "wild4")), 50)

    def test_a_round_is_scored_from_the_cards_left_in_opponents_hands(self):
        players = [make_player(s) for s in (1, 2, 3)]
        room = make_room(players, started=True, settings=GameSettings(points_mode="official"))
        winner, other, third = (room.players[f"tok{s}"] for s in (1, 2, 3))
        winner.hand = []
        other.hand = [make_card("red", "9"), make_card("blue", "skip")]   # 9 + 20
        third.hand = [make_card("black", "wild")]                          # 50

        points = settle_round(room, winner)

        self.assertEqual(points, 79)
        self.assertEqual(winner.score, 79)

    def test_scores_accumulate_across_rounds(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, started=True, settings=GameSettings(points_mode="official"))
        winner, other = room.players["tok1"], room.players["tok2"]
        other.hand = [make_card("red", "9")]
        settle_round(room, winner)
        other.hand = [make_card("red", "5")]
        settle_round(room, winner)
        self.assertEqual(winner.score, 14)

    def test_reaching_the_target_ends_the_match(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, started=True, settings=GameSettings(points_mode="official"))
        winner, other = room.players["tok1"], room.players["tok2"]
        winner.score = MATCH_TARGET_POINTS - 5
        other.hand = [make_card("red", "9")]

        settle_round(room, winner)

        self.assertTrue(room.match_over)
        self.assertEqual(room.match_winner_seat, 1)

    def test_a_round_below_the_target_does_not_end_the_match(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, started=True, settings=GameSettings(points_mode="official"))
        winner, other = room.players["tok1"], room.players["tok2"]
        other.hand = [make_card("red", "9")]
        settle_round(room, winner)
        self.assertFalse(room.match_over)

    def test_wins_mode_does_not_end_a_match_on_points(self):
        """'wins' is a round count, so the 500-point target does not apply."""
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, started=True, settings=GameSettings(points_mode="wins"))
        winner, other = room.players["tok1"], room.players["tok2"]
        winner.score = MATCH_TARGET_POINTS + 100
        other.hand = [make_card("black", "wild")]
        settle_round(room, winner)
        self.assertFalse(room.match_over)

    def test_resetting_a_match_clears_scores_and_rounds(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, started=True, settings=GameSettings(points_mode="official"))
        winner, other = room.players["tok1"], room.players["tok2"]
        winner.score = 600
        room.match_over = True
        room.match_winner_seat = 1
        room.round_number = 7
        other.hand = [make_card("red", "9")]

        reset_match(room)

        self.assertFalse(room.match_over)
        self.assertIsNone(room.match_winner_seat)
        self.assertEqual(room.round_number, 0)
        self.assertEqual(winner.score, 0)
        self.assertEqual(other.hand, [])


# --------------------------------------------------------------------------
# Round setup
# --------------------------------------------------------------------------

class StartGameTests(unittest.TestCase):
    def test_a_round_deals_the_configured_hand_to_everyone(self):
        # GameSettings has no 5-card option of its own: VALID_STARTING_CARDS is
        # (5, 7) and the host chooses. This pins the deal to whatever it holds.
        players = [make_player(s) for s in (1, 2, 3)]
        room = make_room(players, settings=GameSettings(starting_cards=5))

        start_game(room)

        self.assertTrue(room.started)
        for p in room.players.values():
            self.assertEqual(len(p.hand), 5)
        # 3 players * 5 cards + 1 discard leaves 92 in the pile.
        self.assertEqual(len(room.deck), 108 - 15 - 1)

    def test_the_opening_card_is_never_a_wild(self):
        """A wild cannot start play: there would be no colour to match."""
        players = [make_player(s) for s in (1, 2)]
        for _ in range(40):
            room = make_room(players, settings=GameSettings(starting_cards=7))
            start_game(room)
            self.assertNotEqual(room.discard[-1]["color"], "black")
            self.assertIsNotNone(room.current_color)
            self.assertNotEqual(room.current_color, "black")

    def test_the_round_starts_on_a_colour_that_matches_the_pile(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, settings=GameSettings(starting_cards=7))
        start_game(room)
        self.assertEqual(room.current_color, room.discard[-1]["color"])

    def test_the_dealer_rotates_every_round(self):
        players = [make_player(s) for s in (1, 2, 3)]
        room = make_room(players, settings=GameSettings(starting_cards=7))
        dealers = []
        for _ in range(4):
            start_game(room)
            dealers.append(room.dealer_seat)
            # A round must be over before the next one can start.
            room.started = False

        self.assertEqual(dealers[0], 1)                 # lowest seat deals first
        self.assertEqual(dealers[1], 2)
        self.assertEqual(dealers[2], 3)
        self.assertEqual(dealers[3], 1)                 # wraps
        self.assertEqual(len(set(dealers[:3])), 3)      # everyone gets a turn

    def test_the_leader_is_not_always_the_first_seat(self):
        # starting_cards must stay small: the deck is 108 cards, so a large
        # hand for every player exhausts it and the opening-card draw fails.
        players = [make_player(s) for s in (1, 2, 3)]
        room = make_room(players, settings=GameSettings(starting_cards=5))
        leaders = set()
        for _ in range(6):
            start_game(room)
            leaders.add(room.turn_seat)
            room.started = False
        self.assertGreater(len(leaders), 1, "one seat leads every round")

    def test_previous_uno_flags_do_not_leak_into_the_next_round(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, settings=GameSettings(starting_cards=7))
        room.players["tok1"].called_uno = True
        room.players["tok1"].uno_catchable = True
        start_game(room)
        self.assertFalse(room.players["tok1"].called_uno)
        self.assertFalse(room.players["tok1"].uno_catchable)

    def test_a_round_clears_any_pending_draw_four(self):
        players = [make_player(s) for s in (1, 2)]
        room = make_room(players, settings=GameSettings(starting_cards=7),
                         pending_wild4={"by": 1, "against": 2, "legal": False})
        start_game(room)
        self.assertIsNone(room.pending_wild4)


class DealerTests(unittest.TestCase):
    def test_next_dealer_is_the_next_seat_round_the_table(self):
        players = [make_player(s) for s in (1, 2, 3)]
        room = make_room(players, dealer_seat=2)
        self.assertEqual(next_dealer_seat(room), 3)

    def test_next_dealer_wraps_and_handles_a_fresh_room(self):
        players = [make_player(s) for s in (1, 2, 3)]
        room = make_room(players, dealer_seat=3)
        self.assertEqual(next_dealer_seat(room), 1)
        room.dealer_seat = None
        self.assertEqual(next_dealer_seat(room), 1)


class AutoPlayColourTests(unittest.TestCase):
    def test_the_most_common_colour_is_declared_for_a_wild(self):
        p = make_player(1, hand=[
            make_card("blue", "1"), make_card("blue", "2"),
            make_card("red", "3"), make_card("black", "wild"),
        ])
        self.assertEqual(most_common_colour(p), "blue")

    def test_an_all_wild_hand_falls_back_to_a_real_colour(self):
        p = make_player(1, hand=[make_card("black", "wild"), make_card("black", "wild4")])
        self.assertIn(most_common_colour(p), server.COLORS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
