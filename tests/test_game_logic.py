"""Unit tests for the server-side UNO rules engine.

These exercise the pure game logic in `server.py` directly (no WebSocket,
no async) so rules regressions are caught without spinning up the app.
"""

import pytest

from server import (
    ACTION_CARD_POINTS,
    ACTION_VALUES,
    COLORS,
    WILD_CARD_POINTS,
    Player,
    Room,
    advance_turn,
    build_deck,
    card_playable,
    card_points,
    clean_name,
    handle_call_uno,
    handle_catch_uno,
    handle_draw,
    handle_play,
    leaderboard,
    settle_round,
    start_game,
    sync_uno_flag,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def card(color: str, value: str, cid: str = None) -> dict:
    return {"color": color, "value": value, "id": cid or f"{color}_{value}_x"}


def make_room(n_players: int = 3, started: bool = True) -> Room:
    """A room with `n_players` seated at 1..n, a red 5 on the pile, and seat 1
    to play."""
    room = Room(room_id="test")
    for i in range(1, n_players + 1):
        p = Player(token=f"tok{i}", seat=i, name=f"P{i}")
        room.players[p.token] = p
    room.started = started
    room.discard = [card("red", "5", "top")]
    room.current_color = "red"
    room.turn_seat = 1
    return room


def p1(room: Room) -> Player:
    return room.player_by_seat(1)


# --------------------------------------------------------------------------
# Deck
# --------------------------------------------------------------------------

def test_build_deck_has_108_unique_cards():
    deck = build_deck()
    assert len(deck) == 108
    assert len({c["id"] for c in deck}) == 108


def test_build_deck_color_composition():
    deck = build_deck()
    for color in COLORS:
        colored = [c for c in deck if c["color"] == color]
        assert len(colored) == 25  # one 0, 1-9 twice, 3 actions twice
        assert sum(1 for c in colored if c["value"] == "0") == 1
        for v in ACTION_VALUES:
            assert sum(1 for c in colored if c["value"] == v) == 2

    wilds = [c for c in deck if c["color"] == "black"]
    assert len(wilds) == 8
    assert sum(1 for c in wilds if c["value"] == "wild") == 4
    assert sum(1 for c in wilds if c["value"] == "wild4") == 4


# --------------------------------------------------------------------------
# Playability
# --------------------------------------------------------------------------

def test_wild_cards_are_always_playable():
    room = make_room()
    assert card_playable(room, card("black", "wild"))
    assert card_playable(room, card("black", "wild4"))


def test_match_by_color_or_value():
    room = make_room()  # red 5 on top, current color red
    assert card_playable(room, card("red", "3"))
    assert card_playable(room, card("blue", "5"))
    assert not card_playable(room, card("blue", "3"))


def test_colored_card_playable_on_empty_pile():
    room = make_room()
    room.discard = []
    assert card_playable(room, card("green", "9"))


# --------------------------------------------------------------------------
# Turn rotation
# --------------------------------------------------------------------------

def test_advance_turn_one_seat():
    room = make_room(3)
    advance_turn(room, 1)
    assert room.turn_seat == 2


def test_advance_turn_skip_hops_two_seats():
    room = make_room(3)
    advance_turn(room, 2)
    assert room.turn_seat == 3


def test_advance_turn_wraps_around():
    room = make_room(3)
    room.turn_seat = 3
    advance_turn(room, 1)
    assert room.turn_seat == 1


def test_advance_turn_respects_direction():
    room = make_room(3)
    room.direction = -1
    advance_turn(room, 1)
    assert room.turn_seat == 3


def test_advance_turn_falls_back_when_holder_gone():
    room = make_room(3)
    room.turn_seat = 99
    advance_turn(room, 1)
    assert room.turn_seat == 1


def test_advance_turn_empty_room_clears_turn():
    room = Room(room_id="empty")
    room.turn_seat = 1
    advance_turn(room, 1)
    assert room.turn_seat is None


# --------------------------------------------------------------------------
# Starting a round
# --------------------------------------------------------------------------

def test_start_game_deals_seven_and_opens_with_colored_card():
    room = make_room(4, started=False)
    start_game(room)

    assert room.started
    assert all(len(p.hand) == 7 for p in room.players.values())
    assert len(room.discard) == 1
    assert room.discard[0]["color"] in COLORS
    assert room.current_color in COLORS
    # The opening card may be an action card that moves the turn, so just
    # require a valid seat rather than a fixed one.
    assert room.turn_seat in room.seat_order()

    total = len(room.deck) + len(room.discard) + sum(len(p.hand) for p in room.players.values())
    assert total == 108  # opening draw2 still preserves the deck


# --------------------------------------------------------------------------
# Playing cards
# --------------------------------------------------------------------------

def test_play_rejects_when_not_your_turn():
    room = make_room(3)
    room.turn_seat = 2
    p1(room).hand = [card("red", "3")]
    with pytest.raises(ValueError):
        handle_play(room, p1(room), "red_3_x", None)


def test_play_rejects_when_game_not_started():
    room = make_room(3, started=False)
    p1(room).hand = [card("red", "3")]
    with pytest.raises(ValueError):
        handle_play(room, p1(room), "red_3_x", None)


def test_play_rejects_card_not_in_hand():
    room = make_room(3)
    p1(room).hand = [card("red", "3")]
    with pytest.raises(ValueError):
        handle_play(room, p1(room), "red_9_x", None)


def test_play_rejects_unplayable_card():
    room = make_room(3)
    p1(room).hand = [card("blue", "3")]
    with pytest.raises(ValueError):
        handle_play(room, p1(room), "blue_3_x", None)


def test_wild_requires_chosen_color():
    room = make_room(3)
    p1(room).hand = [card("black", "wild", "w1"), card("red", "2")]
    with pytest.raises(ValueError):
        handle_play(room, p1(room), "w1", None)


def test_wild_sets_current_color_and_advances():
    room = make_room(3)
    p1(room).hand = [card("black", "wild", "w1"), card("red", "2")]
    handle_play(room, p1(room), "w1", "green")
    assert room.current_color == "green"
    assert room.turn_seat == 2


def test_colored_play_ignores_supplied_chosen_color():
    room = make_room(3)
    p1(room).hand = [card("red", "3", "r3"), card("red", "2")]
    handle_play(room, p1(room), "r3", "blue")
    assert room.current_color == "red"


def test_skip_hops_one_opponent():
    room = make_room(3)
    p1(room).hand = [card("red", "skip", "s1"), card("red", "2")]
    handle_play(room, p1(room), "s1", None)
    assert room.turn_seat == 3


def test_reverse_flips_direction_and_advances_with_three_players():
    room = make_room(3)
    p1(room).hand = [card("red", "reverse", "rv"), card("red", "2")]
    handle_play(room, p1(room), "rv", None)
    assert room.direction == -1
    assert room.turn_seat == 3


def test_reverse_in_two_player_game_keeps_turn():
    room = make_room(2)
    p1(room).hand = [card("red", "reverse", "rv"), card("red", "2")]
    handle_play(room, p1(room), "rv", None)
    assert room.direction == -1
    assert room.turn_seat == 1


def test_draw2_makes_victim_draw_and_skips_them():
    room = make_room(3)
    room.deck = [card("blue", "9", "d2a"), card("blue", "8", "d2b")]
    p1(room).hand = [card("red", "draw2", "d2"), card("red", "2")]
    _, victim = handle_play(room, p1(room), "d2", None)
    assert victim is room.player_by_seat(2)
    assert len(room.player_by_seat(2).hand) == 2
    assert room.turn_seat == 3


def test_wild4_makes_victim_draw_four():
    room = make_room(3)
    room.deck = [card("blue", str(n), f"w4{n}") for n in range(4)]
    p1(room).hand = [card("black", "wild4", "w4"), card("red", "2")]
    _, victim = handle_play(room, p1(room), "w4", "blue")
    assert victim is room.player_by_seat(2)
    assert len(room.player_by_seat(2).hand) == 4


def test_win_ends_round_and_skips_card_effect():
    room = make_room(3)
    p1(room).hand = [card("red", "skip", "last")]
    is_winner, victim = handle_play(room, p1(room), "last", None)
    assert is_winner is True
    assert victim is None
    assert room.started is False
    assert room.turn_seat == 1  # no advance applied on the winning card


def test_after_drawing_you_may_only_play_the_drawn_card():
    room = make_room(3)
    drawn = card("red", "7", "drawn")
    other = card("red", "8", "other")
    p1(room).hand = [drawn, other]
    p1(room).drew_this_turn = True
    p1(room).last_drawn_card_id = "drawn"

    with pytest.raises(ValueError):
        handle_play(room, p1(room), "other", None)

    handle_play(room, p1(room), "drawn", None)
    assert p1(room).drew_this_turn is False
    assert p1(room).last_drawn_card_id is None


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def test_draw_unplayable_card_passes_turn():
    room = make_room(3)
    room.deck = [card("blue", "3", "u1")]
    p1(room).hand = []
    drawn, playable = handle_draw(room, p1(room))
    assert playable is False
    assert len(drawn) == 1
    assert room.turn_seat == 2
    assert p1(room).drew_this_turn is False


def test_draw_playable_card_keeps_turn_and_locks_card():
    room = make_room(3)
    room.deck = [card("red", "9", "p1")]
    p1(room).hand = []
    drawn, playable = handle_draw(room, p1(room))
    assert playable is True
    assert room.turn_seat == 1
    assert p1(room).drew_this_turn is True
    assert p1(room).last_drawn_card_id == "p1"


def test_cannot_draw_twice_in_a_turn():
    room = make_room(3)
    room.deck = [card("red", "9", "p1"), card("red", "8", "p2")]
    p1(room).hand = []
    handle_draw(room, p1(room))
    with pytest.raises(ValueError):
        handle_draw(room, p1(room))


def test_draw_with_empty_deck_passes_turn():
    room = make_room(3)
    room.deck = []
    room.discard = [card("red", "5", "only")]  # cannot be recycled
    p1(room).hand = []
    drawn, playable = handle_draw(room, p1(room))
    assert drawn == []
    assert playable is False
    assert room.turn_seat == 2


# --------------------------------------------------------------------------
# UNO call / catch
# --------------------------------------------------------------------------

def test_call_uno_allowed_at_one_or_two_cards():
    player = Player(token="t", seat=1, name="P")
    player.hand = [card("red", "1"), card("red", "2")]
    handle_call_uno(player)
    assert player.called_uno is True


def test_call_uno_ignored_with_too_many_cards():
    player = Player(token="t", seat=1, name="P")
    player.hand = [card("red", "1"), card("red", "2"), card("red", "3")]
    handle_call_uno(player)
    assert player.called_uno is False


def test_catch_uno_penalizes_silent_one_card_hand():
    room = make_room(3)
    room.deck = [card("blue", "1", "c1"), card("blue", "2", "c2")]
    room.player_by_seat(2).hand = [card("red", "4", "one")]
    room.player_by_seat(2).called_uno = False

    assert handle_catch_uno(room, 2) is True
    victim = room.player_by_seat(2)
    assert len(victim.hand) == 3
    assert victim.called_uno is True
    # A second catch on the same target does nothing.
    assert handle_catch_uno(room, 2) is False


def test_catch_uno_ignores_two_card_hand():
    room = make_room(3)
    room.player_by_seat(2).hand = [card("red", "4"), card("red", "6")]
    room.player_by_seat(2).called_uno = False
    assert handle_catch_uno(room, 2) is False


def test_sync_uno_flag_clears_outside_one_or_two_cards():
    player = Player(token="t", seat=1, name="P")
    player.called_uno = True
    player.hand = [card("red", "1"), card("red", "2"), card("red", "3")]
    sync_uno_flag(player)
    assert player.called_uno is False


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------

def test_card_points_official_values():
    assert card_points(card("red", "7")) == 7
    assert card_points(card("blue", "0")) == 0
    assert card_points(card("green", "skip")) == ACTION_CARD_POINTS
    assert card_points(card("yellow", "draw2")) == ACTION_CARD_POINTS
    assert card_points(card("black", "wild")) == WILD_CARD_POINTS
    assert card_points(card("black", "wild4")) == WILD_CARD_POINTS


def test_settle_round_scores_all_opponent_cards():
    room = make_room(3)
    room.player_by_seat(1).hand = []
    room.player_by_seat(2).hand = [card("red", "7"), card("black", "wild")]  # 57
    room.player_by_seat(3).hand = [card("green", "skip")]  # 20
    winner = room.player_by_seat(1)

    points = settle_round(room, winner)
    assert points == 77
    assert winner.score == 77


def test_leaderboard_sorts_by_score_then_seat():
    room = make_room(3)
    room.player_by_seat(1).score = 10
    room.player_by_seat(2).score = 50
    room.player_by_seat(3).score = 50

    board = leaderboard(room)
    assert [row["seat"] for row in board] == [2, 3, 1]


# --------------------------------------------------------------------------
# Name sanitization
# --------------------------------------------------------------------------

def test_clean_name_strips_control_chars_and_caps_length():
    assert clean_name("  Bob  ") == "Bob"
    assert clean_name("Bad\x00\x1fName") == "BadName"
    assert len(clean_name("x" * 100)) == 24
    assert clean_name("") == "Player"
    assert clean_name(None) == "Player"
