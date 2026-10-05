"""The doubles the view tests share. One place, because twenty copies of a stub is twenty
places for them to drift apart — and a drift here is silent: the view would read a member the
double does not have, and only the test that happened to exercise that path would fail.
"""

import json
import time
from collections.abc import Callable, Iterator
from unittest.mock import MagicMock


class Chunks:
    """What `llm.answer_stream` hands back: deltas, and whether the call was cut short.

    The same two-member surface as `llm.AnswerStream` — iterate it, read `truncated` once it is
    over — so a test double cannot quietly diverge from the shape the view reads.
    """

    def __init__(self, *texts: str, truncated: bool = False) -> None:
        self._texts = texts
        self.truncated = truncated

    def __iter__(self) -> Iterator[str]:
        return iter(self._texts)

    def __next__(self) -> str:
        return next(iter(self._texts))


def answer_chunks(*texts: str, truncated: bool = False) -> MagicMock:
    """A stand-in for `llm.answer_stream`, used with `patch(..., new=...)`.

    `new=` and not `return_value=`: `return_value` hands every caller the SAME iterator, and the
    second submit of a test that posts twice would read an exhausted generator and see an empty
    answer. Here the mock is re-entered and each call gets its own `Chunks`.

    It is a `MagicMock` rather than a plain function so that the two assertions which exist
    about the play call survive the migration — `assert_not_called` on a refused submit, and
    `call_count == 1` on a refused duplicate. Keep the reference to assert on it:

        play = answer_chunks("42")
        with patch("game.llm.answer_stream", new=play), ...:
            ...
        play.assert_not_called()
    """
    mock = MagicMock(side_effect=lambda *_a, **_kw: Chunks(*texts, truncated=truncated))
    return mock


def read_frames(response) -> list[tuple[str, dict]]:
    """Every frame of a stream response, as (event, data) pairs.

    Walks the whole body rather than trusting one read: the test client hands back a
    `StreamingHttpResponse` whose iterator has not been touched, and a reader that stopped at
    the first chunk would pass on the happy path and fail on the third delta.
    """
    raw = b"".join(response.streaming_content).decode()
    frames: list[tuple[str, dict]] = []
    for block in raw.split("\n\n"):
        name, data = "message", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data:
            frames.append((name, json.loads(data)))
    return frames


def drain(response) -> None:
    """Read a streaming response to its end, for the tests that assert on something else.

    The work behind `/answer` runs in a thread, so a test that returned while it was still
    running would race the fixture teardown — and under `TransactionTestCase` that teardown is a
    flush, which a late write would land in. Draining is also the only thing that guarantees the
    row exists by the time the next line asserts on it.
    """
    if response.streaming:
        list(response.streaming_content)


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Wait for a condition a worker thread will satisfy, and report whether it did.

    Returns the outcome instead of only asserting: a poll loop that gave up quietly would turn a
    slow machine into a passing test, which is the one failure mode a wait cannot have.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()
