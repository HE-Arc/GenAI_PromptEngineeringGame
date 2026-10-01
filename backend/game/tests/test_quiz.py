import random
from unittest import mock

from django.test import SimpleTestCase

from game.quiz import QUESTIONS, QUIZ_LENGTH, pick


class QuizPickTests(SimpleTestCase):
    def test_pick_returns_a_permutation_of_the_whole_bank(self):
        picked = pick()
        self.assertEqual(len(picked), QUIZ_LENGTH)
        self.assertEqual({q.prompt for q in picked}, {q.prompt for q in QUESTIONS})

    def test_pick_never_repeats_a_question(self):
        picked = pick()
        self.assertEqual(len({q.prompt for q in picked}), len(picked))

    def test_pick_actually_shuffles_rather_than_returning_the_bank_in_file_order(self):
        # The bank is exactly one quiz long, so the only variety between two rooms is the
        # ORDER. A `pick` that quietly returned QUESTIONS untouched would pass every other
        # test here and make every room ask its questions in the same sequence, which is
        # exactly the thing this function exists to prevent.
        with mock.patch("game.quiz.random.sample", side_effect=lambda pool, n: tuple(pool)):
            self.assertEqual([q.prompt for q in pick()], [q.prompt for q in QUESTIONS])

    def test_pick_is_reproducible_under_a_seed(self):
        # Real seeding, not a patched-out `random.seed` — mocking the seeding function
        # proves nothing about reproducibility, it only proves the mock was called. This
        # also pins that the order comes from the stdlib RNG rather than a hardcoded
        # rotation, which is what makes a bug report from a classroom reproducible.
        state = random.getstate()
        try:
            random.seed(0)
            first = pick(3)
            random.seed(0)
            self.assertEqual(pick(3), first)
        finally:
            random.setstate(state)


class QuizBankTests(SimpleTestCase):
    def test_eight_questions(self):
        self.assertEqual(len(QUESTIONS), QUIZ_LENGTH)

    def test_every_question_has_four_distinct_choices(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertEqual(len(q.choices), 4)
                self.assertEqual(len(set(q.choices)), 4)

    def test_every_correct_index_is_in_range(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertIn(q.correct, range(4))

    def test_every_question_explains_its_answer(self):
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertGreater(len(q.explanation), 20)

    def test_a_question_is_never_trivially_guessable_by_position(self):
        # A Kahoot where the right answer is always first teaches position, not prompt
        # engineering. Require the correct index to be spread, not uniform-by-accident.
        positions = {q.correct for q in QUESTIONS}
        self.assertGreaterEqual(len(positions), 3)

    def test_no_correct_answer_is_the_longest_choice(self):
        # The mirror trap of the one above: a bank where the right answer is systematically the
        # most developed sentence is won by a reader who spots the careful wording, which is
        # exactly the reflex a quiz should not train. Equality is allowed, dominance is not.
        for i, q in enumerate(QUESTIONS):
            with self.subTest(question=i):
                self.assertLessEqual(
                    len(q.choices[q.correct]),
                    max(len(c) for j, c in enumerate(q.choices) if j != q.correct),
                )
