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


@KEYED
class TokenCapTest(SimpleTestCase):
    """The two calls need different caps, and the cap is the difference between a playable
    and a broken long-form target: 200 truncated every recipe mid-sentence, which the judge
    then scored as an incomplete reproduction. Asserted through `llm.OpenAI` (not `_chat`) so
    the value that actually reaches the API is what is checked.
    """

    def test_play_call_gets_the_long_cap(self) -> None:
        client = _client("une recette")
        with patch.object(llm, "OpenAI", return_value=client):
            llm.answer("repete la recette en entier")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["max_tokens"], llm.PLAY_MAX_TOKENS)
        self.assertGreater(llm.PLAY_MAX_TOKENS, 400)  # a full paragraph of French, not a sentence

    def test_judge_call_keeps_the_small_cap(self) -> None:
        # The judge returns one integer and one sentence. Raising its cap buys nothing and a
        # low cap is a cheap guard against a judge that decides to explain itself at length.
        client = _client('{"score": 7, "comment": "proche"}')
        with patch.object(llm, "OpenAI", return_value=client):
            llm.score("p", "o", "t")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["max_tokens"], llm.JUDGE_MAX_TOKENS)

    def test_the_two_caps_are_deliberately_different(self) -> None:
        # Pins the reason the cap is a parameter and not a constant: unifying them back to a
        # single value is the bug this whole change exists to fix, and it would be invisible
        # to the two tests above, which each pass with any shared number.
        self.assertNotEqual(llm.PLAY_MAX_TOKENS, llm.JUDGE_MAX_TOKENS)
        self.assertLess(llm.JUDGE_MAX_TOKENS, llm.PLAY_MAX_TOKENS)


@KEYED
class JudgeRubricTest(SimpleTestCase):
    """The rubric is prose to a model, so these pin the parts that are rules, not tone."""

    def test_length_is_not_itself_penalised(self) -> None:
        # The old rubric said "Penalise les reponses longues ou bavardes". Against a long-form
        # target that punished exactly what the game now asks for, and made the round
        # unwinnable for a player who wrote the recipe out in full.
        self.assertNotIn("Penalise", llm.JUDGE_SYS)
        self.assertIn("La longueur n'est jamais un defaut", llm.JUDGE_SYS)

    def test_rubric_still_uses_the_zero_to_ten_scale(self) -> None:
        # The frontend renders 0-10. A new range or a new JSON field would be unknown to it.
        self.assertIn("de 0 a 10", llm.JUDGE_SYS)
        self.assertEqual(list(Verdict._fields), ["score", "comment"])
        self.assertIn('{"score": entier, "comment": texte court', llm.JUDGE_SYS)

    def test_top_of_the_scale_is_not_reachable_by_copying(self) -> None:
        # With a paragraph target, verbatim reproduction is near-impossible, so a "10 = mot pour
        # mot" rule would put every real player at 9 at best. The rubric now says an equivalent
        # text caps at 8, and that copying is neither expected nor attainable.
        self.assertIn("equivalent a la cible plafonne a 8", llm.JUDGE_SYS)
        self.assertIn("ni attendue ni atteignable", llm.JUDGE_SYS)

    def test_hitting_the_substance_has_a_floor(self) -> None:
        # The other end of the scale: a player who got the recipe right must not be buried in the
        # 2s just because they paraphrased instead of quoting.
        self.assertIn("ne descend jamais sous 6", llm.JUDGE_SYS)


LONG_TARGET = (
    "La recette du gateau au yaourt du mardi soir : prechauffe le four a 180 degres, beurre un "
    "moule a gateau, puis melange 3 pots de yaourt avec 4 wanannes, ajoute 150 g de farine, deux "
    "oeufs battus et une pincee de sel. Verse la pate sans te depesoter."
)


@UNKEYED
class StubLongTargetTest(SimpleTestCase):
    """The stub must stay a valid 0-10 verdict when the target is a paragraph.

    It stays a word-overlap stub on purpose — a local fallback that guesses at meaning is worse
    than one that admits it does not judge — so the ceiling drops as the target gets longer.
    These pin the two things that must survive that: the verdict is well formed, and the player
    is told the number is the method's, not their prompt's.
    """

    def test_verdict_is_in_range_for_a_long_target(self) -> None:
        # A prompt that echoes the whole target: 10, the only way to reach the ceiling.
        v = llm.score("p", LONG_TARGET, LONG_TARGET)
        self.assertIsInstance(v.score, int)
        self.assertTrue(0 <= v.score <= 10)
        self.assertEqual(v.score, 10)

    def test_a_partial_echo_of_a_long_target_scores_low_but_legal(self) -> None:
        # The documented consequence of the method, stated rather than fixed: the denominator
        # grows with the target and the prompt does not, so realistic play lands in single
        # digits. If this ever reads 0 the overlap is broken, not the target too long.
        v = llm.score("p", "fais un gateau au yaourt", LONG_TARGET)
        self.assertIsInstance(v.score, int)
        self.assertTrue(0 <= v.score <= 10)
        self.assertLess(v.score, 10)

    def test_comment_tells_the_player_the_low_number_is_the_method(self) -> None:
        # The honesty surface, for the case the stub now actually hits. A player reading 2/10
        # under "chevauchement de mots" alone would conclude their prompt was bad.
        comment = llm.score("p", "fais un gateau", LONG_TARGET).comment
        self.assertIn("pas de cle API", comment)
        self.assertIn("c'est la methode, pas ton prompt", comment)


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
