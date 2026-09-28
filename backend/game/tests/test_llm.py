from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from game import llm
from game.llm import Verdict


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


class AnswerTest(SimpleTestCase):
    def test_strips_whitespace(self) -> None:
        with patch.object(llm, "OpenAI", return_value=_client("  bonjour  ")):
            self.assertEqual(llm.answer("salut"), "bonjour")
