import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from game import llm
from game.llm import Verdict

# `answer` and `score` fall back to a local stub when no key is present, so the real-path
# tests pin one. The stub tests blank it instead, which holds whether or not the machine
# running the suite happens to have a real key in its environment.
KEYED = patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"})
UNKEYED = patch.dict(os.environ, {"OPENAI_API_KEY": ""})


def _completion(content: str) -> MagicMock:
    """A stand-in for one chat.completions response carrying `content`."""
    msg = MagicMock()
    msg.message.content = content
    resp = MagicMock()
    resp.choices = [msg]
    return resp


def _client(content: str) -> MagicMock:
    """An OpenAI client whose every completion returns `content`.

    Patched over `llm.OpenAI` rather than `llm._chat`: `_chat` is the function that
    reads the response, so mocking it would skip the `.strip()` and the json_mode
    handling these tests are here to cover.
    """
    client = MagicMock()
    client.chat.completions.create.return_value = _completion(content)
    return client


@KEYED
class ScoreTest(SimpleTestCase):
    def test_parses_json_and_returns_a_verdict(self) -> None:
        with patch.object(llm, "OpenAI", return_value=_client('{"score": 7, "comment": "proche"}')):
            self.assertEqual(llm.score("p", "o", "t"), Verdict(7, "proche"))

    def test_verdict_unpacks_like_a_tuple(self) -> None:
        with patch.object(llm, "OpenAI", return_value=_client('{"score": 7, "comment": "proche"}')):
            points, comment = llm.score("p", "o", "t")
        self.assertEqual((points, comment), (7, "proche"))

    def test_clamps_out_of_range_score(self) -> None:
        with patch.object(llm, "OpenAI", return_value=_client('{"score": 42, "comment": "x"}')):
            self.assertEqual(llm.score("p", "o", "t").score, 10)
        with patch.object(llm, "OpenAI", return_value=_client('{"score": -3, "comment": "x"}')):
            self.assertEqual(llm.score("p", "o", "t").score, 0)


    def test_missing_or_null_score_scores_zero(self) -> None:
        # The judge is a model: it can omit the key or send null. Both used to raise and
        # surface as a 500 to the player, for a round that was otherwise fully played.
        with patch.object(llm, "OpenAI", return_value=_client('{"comment": "je ne sais pas"}')):
            self.assertEqual(llm.score("p", "o", "t"), Verdict(0, "je ne sais pas"))
        with patch.object(llm, "OpenAI", return_value=_client('{"score": null, "comment": "peut-etre"}')):
            self.assertEqual(llm.score("p", "o", "t"), Verdict(0, "peut-etre"))

    def test_non_numeric_score_scores_zero(self) -> None:
        # The third unusable shape, and the one a bare `int(...)` still raised on: the judge
        # wrote prose where the number belongs. That is a 0, not a 500 for the player.
        with patch.object(llm, "OpenAI", return_value=_client('{"score": "proche", "comment": "x"}')):
            self.assertEqual(llm.score("p", "o", "t"), Verdict(0, "x"))

    def test_quoted_numeric_score_is_read_not_discarded(self) -> None:
        # Pins the judgement recorded in the try/except's comment: a quoted number is read,
        # not thrown away. It does NOT distinguish that guard from a bare `int(...)` — both
        # return 7 here; only the prose case ("proche") needs the except, and that is
        # test_non_numeric_score_scores_zero. Dropping the `or 0` also changes nothing
        # (`int(None or 0)` and `int(None) -> 0` agree), so it is not guarded here either.
        # What this does catch is a strict-type fix — an isinstance check, or treating a
        # quoted number as unusable — which would score a real judgement 0.
        with patch.object(llm, "OpenAI", return_value=_client('{"score": "7", "comment": "proche"}')):
            self.assertEqual(llm.score("p", "o", "t"), Verdict(7, "proche"))


@KEYED
class AnswerTest(SimpleTestCase):
    def test_strips_whitespace(self) -> None:
        with patch.object(llm, "OpenAI", return_value=_client("  bonjour  ")):
            self.assertEqual(llm.answer("salut"), "bonjour")


@UNKEYED
class StubTest(SimpleTestCase):
    """No key means no model. The game must still be playable, and must say so."""

    def test_answer_echoes_the_prompt_without_a_key(self) -> None:
        self.assertIn("un escargot", llm.answer("un escargot sur un clavier"))

    def test_answer_never_builds_a_client_without_a_key(self) -> None:
        with patch.object(llm, "OpenAI", side_effect=AssertionError("client built")):
            llm.answer("peu importe")

    def test_exact_target_scores_ten(self) -> None:
        self.assertEqual(llm.score("p", "un escargot sur un clavier", "un escargot sur un clavier").score, 10)

    def test_partial_overlap_scores_proportionally(self) -> None:
        # Target is 4 words {un, escargot, sur, clavier}; "un escargot" hits 2 of them.
        self.assertEqual(llm.score("p", "un escargot", "un escargot sur un clavier").score, 5)

    def test_unrelated_output_scores_zero(self) -> None:
        self.assertEqual(llm.score("p", "une voiture rouge", "un escargot sur un clavier").score, 0)

    def test_never_builds_a_client_without_a_key(self) -> None:
        with patch.object(llm, "OpenAI", side_effect=AssertionError("client built")):
            llm.score("p", "o", "t")

    def test_comment_tells_the_player_it_is_local(self) -> None:
        # The game's honesty surface: this string is what the game screen shows.
        self.assertEqual(llm.score("p", "o", "t").comment, llm.STUB_COMMENT)
        self.assertIn("pas de cle API", llm.STUB_COMMENT)

    def test_ignores_case_and_accents_are_matched_as_letters(self) -> None:
        self.assertEqual(llm.score("p", "UN ESCARGOT SUR UN CLAVIER", "un escargot sur un clavier").score, 10)
