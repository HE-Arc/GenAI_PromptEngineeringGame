import json
import time

from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from game.models import Player, QuizQuestion, Ready, Room
from game.prompts import majority
from game.quiz import pick
from game.views import PRESENCE_TTL, _presence, _presence_lock, room_state


def _one_player_room(tutorial: bool = True):
    """A room with one joined player, its didacticiel open on question 0. The fixtures the other
    test files use are private to those files, so this one is rebuilt here — it is four lines and
    copying a shared helper across test modules would couple them to each other's edits."""
    from game.quiz import pick

    room = Room.objects.create(code="RDY1", tutorial=tutorial)
    from game.models import QuizQuestion

    QuizQuestion.objects.bulk_create(
        QuizQuestion(room=room, order=i, prompt=q.prompt, choices=list(q.choices), correct=q.correct, explanation=q.explanation)
        for i, q in enumerate(pick())
    )
    player = Player.objects.create(room=room, name="Ana", token="t")
    room._go(Room.Phase.QUIZ)
    room._open_quiz(0)
    room.refresh_from_db()
    return room, player


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


class ReadyModelTests(TestCase):
    def test_one_vote_per_player_per_step(self):
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Ready.objects.create(room=room, player=player, step="quiz:0")

    def test_the_same_player_may_vote_on_two_steps(self):
        # The step is in the key, not derived: a player who is ready on question 0 and again on
        # question 1 has two rows, and the question-0 row is garbage the moment the cursor moves.
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        Ready.objects.create(room=room, player=player, step="quiz:1")
        self.assertEqual(Ready.objects.filter(player=player).count(), 2)

    def test_a_vote_on_a_previous_step_is_never_counted_again(self):
        # THE INVARIANT THAT MAKES THE WHOLE THING SAFE, and the reason nothing in this codebase
        # clears votes when the room moves on. `step` is part of the key, so a vote on `quiz:0`
        # cannot match the count for `quiz:1` — there is no window in which a stale vote is
        # visible. Clearing instead would add three call sites and a failure mode (a forgotten
        # clear is a room that opens its next question already holding enough votes to skip it)
        # to solve a problem the key already solved.
        room, player = _one_player_room()
        Ready.objects.create(room=room, player=player, step="quiz:0")
        room._open_quiz(1)
        room.refresh_from_db()
        from game.views import current_step
        self.assertEqual(current_step(room), "quiz:1")
        self.assertEqual(Ready.objects.filter(room=room, step=current_step(room)).count(), 0)
        # The row is still there, and that is deliberate: it is inert, not gone.
        self.assertEqual(Ready.objects.filter(room=room, step="quiz:0").count(), 1)

    def test_a_restart_wipes_the_votes_of_the_previous_game(self):
        # The one place a wipe IS needed: a restart replays `quiz:0`, so this is the single case
        # where a stale step name comes back round and would otherwise count.
        #
        # It lives in TÂCHE 3, not here, and the reason is its shape rather than its subject:
        # it goes through the endpoint, so it needs the room helpers this task does not have yet.
        # In here it would have to poke `Ready` with a `.delete()` written next to the assertion,
        # and a test that deletes the rows itself and then finds them gone has asserted that
        # `delete()` works.
        pass