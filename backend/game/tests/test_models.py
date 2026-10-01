from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.utils import IntegrityError
from django.test import TestCase

from game.models import (
    Answer,
    Player,
    QuizAttempt,
    QuizQuestion,
    Rating,
    Room,
    Round,
)
from game.quiz import QUESTIONS
from game.targets import ROUNDS_PER_GAME, TARGETS, pick


def _quiz_fixture() -> tuple[Room, Player, list[QuizQuestion]]:
    """A room, a player and the materialised bank.

    The whole bank, not one row: `admin_create_room` materialises all of `quiz.QUESTIONS`, and
    a fixture with a single question would let a bug that only handles question 0 pass. The rows
    carry the room, because the bank belongs to the room — that is what `admin_create_room`
    writes and what every read of it scopes to.
    """
    room = Room.objects.create(code="ABCD")
    player = Player.objects.create(room=room, name="ana")
    questions = [
        QuizQuestion.objects.create(
            room=room,
            order=i,
            prompt=q.prompt,
            choices=list(q.choices),
            correct=q.correct,
            explanation=q.explanation,
        )
        for i, q in enumerate(QUESTIONS)
    ]
    return room, player, questions


def _rating_fixture() -> tuple[Answer, list[Player]]:
    """A prompt to rate and the three players around it. `answer.player` is the author, so
    raters[0] is deliberately someone else: the self-rating test needs a legal rater first."""
    room = Room.objects.create(code="ABCD")
    rnd = Round.objects.create(room=room, order=0, target="t")
    author = Player.objects.create(room=room, name="ana")
    others = [Player.objects.create(room=room, name=n) for n in ("bob", "clea", "dan")]
    answer = Answer.objects.create(round=rnd, player=author, prompt="a", output="b", score=5)
    return answer, others


class RoomTest(TestCase):
    def test_next_round_walks_to_done(self) -> None:
        room = Room.objects.create(code="ABCD")
        for i in range(2):
            Round.objects.create(room=room, order=i, target=f"t{i}")

        self.assertEqual((room.phase, room.current), (Room.Phase.WAITING, 0))

        room.next_round()
        room.refresh_from_db()
        self.assertEqual((room.phase, room.current), (Room.Phase.PLAYING, 1))

        room.next_round()
        room.refresh_from_db()
        self.assertEqual((room.phase, room.current), (Room.Phase.DONE, 1))

    def test_current_round_is_the_one_being_played(self) -> None:
        room = Room.objects.create(code="ABCD")
        first = Round.objects.create(room=room, order=0, target="t0")
        Round.objects.create(room=room, order=1, target="t1")
        self.assertEqual(room.current_round(), first)
        room.current = 1
        self.assertEqual(room.current_round().target, "t1")

    def test_current_round_is_none_before_the_game_starts(self) -> None:
        # None is a real answer, not a bug: every phase transition before `admin_start` asks
        # for the current round of a room that has no rounds yet.
        room = Room.objects.create(code="ABCD")
        self.assertIsNone(room.current_round())


class PhaseTests(TestCase):
    def test_status_is_replaced_by_phase(self) -> None:
        # A TestCase, not a SimpleTestCase: the enum test alone needs no database, but the
        # defaults below are a row's worth of truth and only the database has defaults.
        self.assertEqual(
            [p for p, _ in Room.Phase.choices],
            ["waiting", "quiz", "playing", "rating", "summary", "done"],
        )

    def test_status_is_gone(self) -> None:
        # Not "deprecated but still readable": two fields for one instant is the contradiction
        # the rename exists to remove, and a leftover `status` would invite writes to it.
        self.assertFalse(hasattr(Room, "status"))
        self.assertFalse(hasattr(Room, "Status"))

    def test_room_defaults_to_waiting_without_tutorial(self) -> None:
        room = Room.objects.create(code="ABCD")
        self.assertEqual(room.phase, Room.Phase.WAITING)
        self.assertTrue(room.tutorial)
        self.assertTrue(room.anonymous)

    def test_the_two_flags_are_per_game_and_survive_a_restart(self) -> None:
        # Fixed at creation, kept across restarts: relaunching "without" means without, twice.
        room = Room.objects.create(code="ABCD", tutorial=False, anonymous=False)
        room.current = 9
        room.next_round()
        room.refresh_from_db()
        self.assertFalse(room.tutorial)
        self.assertFalse(room.anonymous)


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


class QuizAttemptTests(TestCase):
    def test_one_attempt_per_player_and_question(self) -> None:
        room, player, questions = _quiz_fixture()
        question = questions[0]
        QuizAttempt.objects.create(player=player, question=question, choice=0, correct=True)
        with self.assertRaises(IntegrityError):
            QuizAttempt.objects.create(player=player, question=question, choice=1, correct=False)

    def test_quiz_score_counts_correct_answers(self) -> None:
        room, player, questions = _quiz_fixture()
        for i, q in enumerate(questions[:3]):
            QuizAttempt.objects.create(player=player, question=q, choice=0, correct=i % 2 == 0)
        self.assertEqual(player.quiz_score(), 2)

    def test_quiz_score_is_zero_before_the_first_answer(self) -> None:
        # An integer, never None: the client formats it into "3 sur 8" without a null check
        # that only a player who never answered would ever hit.
        _, player, _ = _quiz_fixture()
        self.assertEqual(player.quiz_score(), 0)

    def test_question_order_is_unique_within_a_room(self) -> None:
        # `(room, order)` is the quiz endpoint's address for a question, and the endpoint
        # resolves it inside one room. Two rows of one room sharing an order make "which question
        # is this" ambiguous, so the database refuses it.
        room, _, questions = _quiz_fixture()
        with self.assertRaises(IntegrityError):
            QuizQuestion.objects.create(
                room=room,
                order=questions[0].order,
                prompt="p",
                choices=["a", "b", "c", "d"],
                correct=0,
                explanation="e",
            )

    def test_the_same_order_in_two_rooms_is_fine(self) -> None:
        # The flip of the one above, and the whole reason `order` lost its global unique: two
        # rooms facing the same order is the ordinary case, not a collision. A global unique here
        # is what made the second room in an install impossible to give its own bank.
        first, _, first_questions = _quiz_fixture()
        second = Room.objects.create(code="WXYZ")
        QuizQuestion.objects.create(
            room=second,
            order=first_questions[0].order,
            prompt="p",
            choices=["a", "b", "c", "d"],
            correct=0,
            explanation="e",
        )
        self.assertEqual(first.questions.count(), len(first_questions))
        self.assertEqual(second.questions.count(), 1)

    def test_a_rooms_questions_cascade_with_it(self) -> None:
        # The bank belongs to the room, so deleting the room takes the eight with it. Nothing else
        # references them: a restart deletes rounds, never the room, which is how the bank survives
        # one.
        room, _, questions = _quiz_fixture()
        self.assertEqual(room.questions.count(), len(questions))
        room.delete()
        self.assertEqual(QuizQuestion.objects.count(), 0)


class RatingTests(TestCase):
    def test_one_rating_per_rater_per_answer(self) -> None:
        answer, raters = _rating_fixture()
        Rating.objects.create(answer=answer, rater=raters[0], score=7)
        with self.assertRaises(IntegrityError):
            Rating.objects.create(answer=answer, rater=raters[0], score=3)

    def test_a_player_cannot_rate_their_own_answer(self) -> None:
        # ValidationError, not IntegrityError: this is the `save()` guard, not a database
        # constraint — Django cannot express the rule in SQL, see the note in models.py.
        answer, raters = _rating_fixture()
        with self.assertRaises(ValidationError):
            Rating.objects.create(answer=answer, rater=answer.player, score=7)

    def test_rating_score_is_bounded(self) -> None:
        answer, raters = _rating_fixture()
        for bad in (0, 11, -1):
            with self.subTest(score=bad):
                # A savepoint per bad score: a CHECK violation aborts the transaction it runs
                # in, so without one the second subTest would fail on a broken transaction
                # instead of on the constraint.
                with transaction.atomic(), self.assertRaises(IntegrityError):
                    Rating.objects.create(answer=answer, rater=raters[0], score=bad)

    def test_every_peer_may_rate_the_same_answer(self) -> None:
        answer, raters = _rating_fixture()
        for i, rater in enumerate(raters):
            Rating.objects.create(answer=answer, rater=rater, score=i + 1)
        self.assertEqual(answer.ratings.count(), len(raters))

    def test_the_bounds_are_one_and_ten_inclusive(self) -> None:
        answer, raters = _rating_fixture()
        Rating.objects.create(answer=answer, rater=raters[0], score=1)
        Rating.objects.create(answer=answer, rater=raters[1], score=10)
        self.assertEqual(answer.ratings.count(), 2)


class TargetsTest(TestCase):
    def test_pick_returns_distinct_targets(self) -> None:
        picked = pick()
        self.assertEqual(len(picked), ROUNDS_PER_GAME)
        self.assertEqual(len(set(picked)), ROUNDS_PER_GAME)
        self.assertTrue(set(picked) <= set(TARGETS))

    def test_bank_size_is_pinned(self) -> None:
        # A floor, not an exact count: the bank grew from 24 when the targets became long-form,
        # and what must not regress is that it is comfortably larger than a game so `pick` never
        # runs the same targets twice in a row for a player. Pinning the exact number would make
        # every addition a test edit for no gain; dropping below ROUNDS_PER_GAME would break it.
        self.assertGreaterEqual(len(TARGETS), 24)
        self.assertEqual(len(set(TARGETS)), len(TARGETS))  # no duplicates to draw
        self.assertEqual(ROUNDS_PER_GAME, 10)

    def test_every_target_is_long_form_text(self) -> None:
        # The point of the change: a target has to be a paragraph, not a noun phrase, or the
        # judge's proximity scale has nothing to be proximal to. The threshold is well under the
        # shortest entry in the bank, so it fails on a reintroduced two-word target.
        self.assertTrue(all(len(t) > 200 for t in TARGETS))

    def test_every_target_fits_the_target_column(self) -> None:
        # A `max_length` raised past the content is a length nothing enforces — and `admin_start`
        # writes rounds with `bulk_create`, which skips `full_clean()`, so nothing would reject a
        # target that outgrew the column. Asserted against the field itself, so shortening it
        # fails here rather than in production.
        self.assertLessEqual(max(len(t) for t in TARGETS), Round._meta.get_field("target").max_length)
