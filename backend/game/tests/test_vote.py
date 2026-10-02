from django.test import SimpleTestCase

from game.prompts import majority


class MajorityTests(SimpleTestCase):
    def test_the_threshold_is_a_strict_majority(self):
        for n, expected in ((1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (20, 11), (21, 11)):
            with self.subTest(players=n):
                self.assertEqual(majority(n), expected)

    def test_a_room_with_nobody_connected_has_no_threshold(self):
        # 0 // 2 + 1 is 1, which would be a threshold one player could never reach in a room
        # with no players — and the caller that used it would advance a game nobody is in.
        self.assertEqual(majority(0), 0)

    def test_the_threshold_is_never_more_than_the_players(self):
        for n in range(1, 30):
            with self.subTest(players=n):
                self.assertLessEqual(majority(n), n)

    def test_the_threshold_is_always_reachable(self):
        # The property that makes the game autonomous: with every player ready, the room always
        # crosses the line. A threshold of n+1 would deadlock a unanimous class forever.
        for n in range(1, 30):
            with self.subTest(players=n):
                self.assertLessEqual(majority(n), n)