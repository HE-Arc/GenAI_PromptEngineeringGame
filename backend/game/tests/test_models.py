from django.db.utils import IntegrityError
from django.test import TestCase

from game.models import Answer, Player, Room, Round
from game.targets import ROUNDS_PER_GAME, TARGETS, pick


class RoomTest(TestCase):
    def test_next_round_walks_to_done(self) -> None:
        room = Room.objects.create(code="ABCD")
        for i in range(2):
            Round.objects.create(room=room, order=i, target=f"t{i}")

        self.assertEqual((room.status, room.current), (Room.Status.WAITING, 0))

        room.next_round()
        room.refresh_from_db()
        self.assertEqual((room.status, room.current), (Room.Status.PLAYING, 1))

        room.next_round()
        room.refresh_from_db()
        self.assertEqual((room.status, room.current), (Room.Status.DONE, 1))

    def test_status_is_a_text_enum(self) -> None:
        self.assertEqual(Room.Status.PLAYING, "playing")
        self.assertEqual(Room.Status.values, ["waiting", "playing", "done"])


class UniquenessTest(TestCase):
    def test_player_name_unique_per_room(self) -> None:
        room = Room.objects.create(code="ABCD")
        Player.objects.create(room=room, name="ana")
        with self.assertRaises(IntegrityError):
            Player.objects.create(room=room, name="ana")

    def test_player_name_reused_in_another_room(self) -> None:
        first = Room.objects.create(code="AAAA")
        second = Room.objects.create(code="BBBB")
        Player.objects.create(room=first, name="ana")
        # A global unique on `name` would raise here and break the two-rooms join.
        Player.objects.create(room=second, name="ana")
        self.assertEqual(Player.objects.filter(name="ana").count(), 2)

    def test_one_answer_per_player_per_round(self) -> None:
        room = Room.objects.create(code="ABCD")
        rnd = Round.objects.create(room=room, order=0, target="t")
        p = Player.objects.create(room=room, name="ana")
        Answer.objects.create(round=rnd, player=p, prompt="a", output="b", score=5)
        with self.assertRaises(IntegrityError):
            Answer.objects.create(round=rnd, player=p, prompt="c", output="d", score=9)

    def test_player_answers_two_rounds(self) -> None:
        room = Room.objects.create(code="ABCD")
        first = Round.objects.create(room=room, order=0, target="t0")
        second = Round.objects.create(room=room, order=1, target="t1")
        p = Player.objects.create(room=room, name="ana")
        # A global unique on `player` would raise here.
        Answer.objects.create(round=first, player=p, prompt="a", output="b", score=5)
        Answer.objects.create(round=second, player=p, prompt="c", output="d", score=7)

    def test_two_players_answer_one_round(self) -> None:
        room = Room.objects.create(code="ABCD")
        rnd = Round.objects.create(room=room, order=0, target="t")
        ana = Player.objects.create(room=room, name="ana")
        bob = Player.objects.create(room=room, name="bob")
        Answer.objects.create(round=rnd, player=ana, prompt="a", output="b", score=5)
        Answer.objects.create(round=rnd, player=bob, prompt="c", output="d", score=7)
        self.assertEqual(rnd.answers.count(), 2)


class TargetsTest(TestCase):
    def test_pick_returns_distinct_targets(self) -> None:
        picked = pick()
        self.assertEqual(len(picked), ROUNDS_PER_GAME)
        self.assertEqual(len(set(picked)), ROUNDS_PER_GAME)
        self.assertTrue(set(picked) <= set(TARGETS))

    def test_bank_size_is_pinned(self) -> None:
        self.assertEqual(len(TARGETS), 24)
        self.assertEqual(len(set(TARGETS)), 24)
        self.assertEqual(ROUNDS_PER_GAME, 10)
