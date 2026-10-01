from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.db.utils import IntegrityError
from django.db import models
from django.test import TestCase
from django.utils import timezone

from game.models import (
    Answer,
    Player,
    QuizAttempt,
    QuizQuestion,
    Rating,
    Room,
    Round,
)
from game.quiz import QUESTIONS, QUIZ_SECONDS
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


class QuizClockTests(TestCase):
    """The room's question and the room's window: the two fields the didacticiel now runs on.

    Tested HERE, on the model, and not only through the endpoints, because these are the two
    methods both callers share. `admin_start` opens question 0 and the admin's button opens every
    question after it, and the property worth pinning is that they are the SAME method — a second
    writer of `quiz_deadline` is how a class ends up with a fifteen-second question 3 and a
    thirty-second question 4.
    """

    def test_a_fresh_room_is_at_the_first_question_with_no_window(self) -> None:
        # The defaults, and they mean different things: index 0 is a POSITION, null deadline is NO
        # WINDOW. A deadline of the epoch would have said "expired", which is a different claim.
        room = Room.objects.create(code="ABCD")
        self.assertEqual(room.quiz_index, 0)
        self.assertIsNone(room.quiz_deadline)

    def test_the_two_columns_are_named_and_typed_for_the_contract(self) -> None:
        # `quiz_index` is an int the snapshot prints as `index` and the POST compares against;
        # `quiz_deadline` is NULLABLE because "no window" has to be storable — every existing row
        # is null after the migration, and a room relaunched before it is started again is null too.
        index = Room._meta.get_field("quiz_index")
        self.assertIsInstance(index, models.IntegerField)
        self.assertEqual(index.default, 0)
        self.assertTrue(Room._meta.get_field("quiz_deadline").null)

    def test_opening_a_question_writes_the_cursor_and_a_fifteen_second_window(self) -> None:
        room, _player, _questions = _quiz_fixture()
        room._open_quiz()
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 0)
        seconds = (room.quiz_deadline - timezone.now()).total_seconds()
        self.assertLessEqual(seconds, QUIZ_SECONDS)
        self.assertGreater(seconds, QUIZ_SECONDS - 5)
        # The same method opens any question, and it opens it at the same length: the parameter is
        # the position, not a way to ask for a longer window.
        room._open_quiz(4)
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, 4)
        seconds = (room.quiz_deadline - timezone.now()).total_seconds()
        self.assertLessEqual(seconds, QUIZ_SECONDS)
        self.assertGreater(seconds, QUIZ_SECONDS - 5)

    def test_the_next_question_walks_the_bank_and_then_refuses(self) -> None:
        # `_next_quiz` is the whole of the didacticiel's advance, and its return value is the
        # whole of the transition: True opened a question, False means the bank is behind the
        # cursor and the caller opens the game. Pinned on the boundary in both directions, because
        # off-by-one here either skips a question nobody asked or strands the room in the quiz with
        # nothing left to press.
        room, _player, _questions = _quiz_fixture()
        bank = room.questions.count()
        self.assertEqual(bank, len(QUESTIONS))
        for index in range(1, bank):
            self.assertTrue(room._next_quiz(), f"question {index} should have opened")
            room.refresh_from_db()
            self.assertEqual(room.quiz_index, index)
        # The bank is behind the cursor now: False, and no question 8 in a bank of eight.
        self.assertFalse(room._next_quiz())
        room.refresh_from_db()
        self.assertEqual(room.quiz_index, bank - 1)
        # Which is why the room can never be walked PAST the eighth, however many times the button
        # is pressed — `next_quiz` alone never moves the phase, so it has nothing else to break.
        self.assertEqual(room.phase, Room.Phase.WAITING)

    def test_an_unmaterialised_bank_has_no_next_question(self) -> None:
        # A room whose bank was never created: count 0, so nothing to walk to. The caller opens the
        # game, which is the only reading of a gate with nothing in it that does not strand the
        # admin on a button that does nothing.
        room = Room.objects.create(code="WXYZ")
        self.assertFalse(room._next_quiz())
        room.refresh_from_db()
        self.assertEqual((room.quiz_index, room.quiz_deadline), (0, None))

    def test_opening_a_question_does_not_touch_the_phase_or_the_round_cursor(self) -> None:
        # `save(update_fields=...)`, pinned because the phases write the same row: a bare `save()`
        # from a half-loaded instance is how a question cursor rewrites `current`, or an
        # `admin_restart`'s phase reset rewrites a window it never meant to see. The round cursor
        # here is deliberately off zero, so a clobber would be visible.
        room, _player, _questions = _quiz_fixture()
        room.phase = Room.Phase.QUIZ
        room.current = 4
        room.save(update_fields=["phase", "current"])
        room._open_quiz(2)
        room.refresh_from_db()
        self.assertEqual((room.phase, room.current, room.quiz_index), ("quiz", 4, 2))


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


class SettlementColumnTests(TestCase):
    """The four columns `settle_round` writes, and the one property the whole write-not-compute
    shape rests on: an unsettled round is INVISIBLE to a sum."""

    def test_a_fresh_answer_has_no_settled_score(self) -> None:
        # Four nulls, not four zeros. A default of 0 would make an answer that was never rated look
        # like a round scored 0, and the ranking would count a round nobody played.
        answer, _raters = _rating_fixture()
        self.assertEqual(
            (
                answer.quality_llm,
                answer.quality_peers,
                answer.quality,
                answer.round_score,
            ),
            (None, None, None, None),
        )

    def test_the_four_columns_are_nullable_and_named(self) -> None:
        # The names are a contract with `views.settle_round`, `room_state`'s summary block and the
        # final ranking's SUM, and `null=True` is what lets an in-progress round exist at all:
        # these columns were added to a table that already had rows.
        for name in ("quality_llm", "quality_peers", "quality", "round_score"):
            with self.subTest(field=name):
                self.assertTrue(Answer._meta.get_field(name).null, f"{name} must be nullable")
        for name in ("quality_llm", "quality_peers", "quality"):
            with self.subTest(field=name):
                self.assertIsInstance(Answer._meta.get_field(name), models.IntegerField)

    def test_the_round_score_is_a_float_because_the_specs_mean_is_one(self) -> None:
        # §7: `note_tour = (proximité + qualité) / 2`. The two halves are integers, so the mean is
        # a whole number only half the time, and an `IntegerField` can only store a FLOOR of it.
        # The floor was the schema rather than the rule, so it was invisible at the line that
        # computed it: ten rounds of proximity 7 and quality 6 settled to 60 on a scale the final
        # screen still calls « sur 100 », where the spec's arithmetic gives 65. Pinned as a type
        # because that is where the truncation lived, and then by a value that has to survive the
        # round trip: 6.5 in, 6.5 out, which an integer column could not do on SQLite either.
        self.assertIsInstance(Answer._meta.get_field("round_score"), models.FloatField)
        answer, _raters = _rating_fixture()
        Answer.objects.filter(pk=answer.pk).update(round_score=6.5)
        self.assertEqual(Answer.objects.get(pk=answer.pk).round_score, 6.5)
        # And the sum the ranking is built on keeps the half: this one round reads 6.5, and ten
        # such rounds would be the 65 the spec's arithmetic gives, where a floored column said 60.
        self.assertEqual(
            answer.player.answers.aggregate(total=Sum("round_score"))["total"],
            6.5,
        )

    def test_the_critics_comment_is_stored_beside_its_score(self) -> None:
        # The reason for the critic's number, stored with it. `blank=True` and NOT `null=True`,
        # like `comment` above the settlement block: a column that could be NULL would make "the
        # critic said nothing" and "nobody wrote a comment" the same stored value, and the summary
        # screen has to be able to print one and hide the other. It is NOT one of the four
        # settlement columns either — it is written on the submit, beside `quality_llm`, because
        # both come from the same billed call.
        field = Answer._meta.get_field("quality_comment")
        self.assertTrue(field.blank)
        self.assertFalse(field.null)
        answer, _raters = _rating_fixture()
        self.assertEqual(answer.quality_comment, "")  # a fresh row is empty, not null
        answer.quality_comment = "il manque un role et un format"
        answer.save(update_fields=["quality_comment"])
        self.assertEqual(Answer.objects.get(pk=answer.pk).quality_comment, "il manque un role et un format")
        # And a row nobody wrote a comment on is still empty, not null: the settlement walk in
        # `views.settle_round` must not fill it in, and its `update_fields` says so below.
        second = Answer.objects.create(
            round=Round.objects.create(room=answer.round.room, order=1, target="t2"),
            player=answer.player,
            prompt="c",
            output="d",
            score=5,
        )
        self.assertEqual(second.quality_comment, "")

    def test_an_unsettled_round_is_ignored_by_a_sum(self) -> None:
        # THE PROPERTY THE WRITES EXIST FOR. The final ranking is a `Sum("round_score")`, and `Sum`
        # skips NULL — so a player with one settled round and one open reads the settled total
        # rather than a zero for the round being played. A `null=False` column with a default of 0
        # would pass every test above and turn this into a silent under-count for the whole
        # duration of the round in progress.
        answer, _raters = _rating_fixture()
        # A second round, because one answer per player per round is the table's own rule and the
        # two rows below have to belong to different rounds to be legal at all.
        second = Round.objects.create(room=answer.round.room, order=1, target="t2")
        Answer.objects.filter(pk=answer.pk).update(round_score=6)
        Answer.objects.create(
            round=second, player=answer.player, prompt="c", output="d", score=4
        )
        self.assertEqual(
            answer.player.answers.aggregate(total=Sum("round_score"))["total"],
            6,
        )
        # And the open row is still readable on its own, so a null neighbour does not hide it.
        self.assertEqual(answer.player.answers.get(round=answer.round).round_score, 6)
        self.assertIsNone(answer.player.answers.get(round=second).round_score)

    def test_the_settlement_is_a_plain_update_and_leaves_the_rest_of_the_row_alone(self) -> None:
        # `settle_round` writes with `update_fields`, so the proximity score, the prompt, the
        # judge's comment and the CRITIC's comment are not part of its write and cannot be
        # clobbered by it. Pinned because a bare `answer.save()` would rewrite every column from a
        # possibly stale instance, and the prompt and the prose are the two things a player must
        # find byte-identical in the summary.
        answer, raters = _rating_fixture()
        # Stored first, on its own write, the way `answer` does it: the critic's prose is written
        # by the submit and read by the summary, with the settlement in between.
        answer.quality_comment = "role et format explicites"
        answer.save(update_fields=["quality_comment"])
        answer.quality_llm = 6
        answer.quality_peers = 7
        answer.quality = 7
        answer.round_score = 6
        answer.save(update_fields=["quality_llm", "quality_peers", "quality", "round_score"])
        answer.refresh_from_db()
        self.assertEqual(
            (answer.prompt, answer.output, answer.score, answer.comment),
            ("a", "b", 5, ""),
        )
        # The comment survived a settlement write that did not name it, which is the whole claim:
        # a bare `save()` from a stale instance would have rewritten it from memory.
        self.assertEqual(answer.quality_comment, "role et format explicites")
        # The ratings are still the ones the settlement read, and the whole graph is still there.
        self.assertEqual(answer.ratings.count(), 0)
        self.assertEqual(len(raters), 3)


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
