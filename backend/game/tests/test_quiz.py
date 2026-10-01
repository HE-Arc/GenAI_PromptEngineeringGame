from django.test import SimpleTestCase

from game.quiz import QUESTIONS, QUIZ_LENGTH


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
