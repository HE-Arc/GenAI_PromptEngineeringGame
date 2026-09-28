from django.db.utils import IntegrityError
from django.test import TestCase

from game.models import Answer, Player, Room, Round


class RoomTest(TestCase):
    def test_next_round_walks_to_done(self):
        room = Room.objects.create(code="ABCD")
        for i in range(2):
            Round.objects.create(room=room, order=i, target=f"t{i}")

        self.assertEqual((room.status, room.current), (Room.Status.WAITING, 0))

        room.next_round()
        self.assertEqual((room.status, room.current), (Room.Status.PLAYING, 1))

        room.next_round()
        self.assertEqual((room.status, room.current), (Room.Status.DONE, 1))

    def test_status_is_a_text_enum(self):
        self.assertEqual(Room.Status.PLAYING, "playing")
        self.assertEqual(Room.Status.values, ["waiting", "playing", "done"])


class UniquenessTest(TestCase):
    def test_player_name_unique_per_room(self):
        room = Room.objects.create(code="ABCD")
        Player.objects.create(room=room, name="ana")
        with self.assertRaises(IntegrityError):
            Player.objects.create(room=room, name="ana")

    def test_one_answer_per_player_per_round(self):
        room = Room.objects.create(code="ABCD")
        rnd = Round.objects.create(room=room, order=0, target="t")
        p = Player.objects.create(room=room, name="ana")
        Answer.objects.create(round=rnd, player=p, prompt="a", output="b", score=5)
        with self.assertRaises(IntegrityError):
            Answer.objects.create(round=rnd, player=p, prompt="c", output="d", score=9)


class TargetsTest(TestCase):
    def test_pick_returns_distinct_targets(self):
        from game.targets import ROUNDS_PER_GAME, TARGETS, pick

        picked = pick()
        self.assertEqual(len(picked), ROUNDS_PER_GAME)
        self.assertEqual(len(set(picked)), ROUNDS_PER_GAME)
        self.assertTrue(set(picked) <= set(TARGETS))
