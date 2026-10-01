"""The cross-rating scale and the bound that keeps it honest. Pure functions: no Django, no
model, no I/O.

`half_up` is public rather than private because the tie it settles is the one rule in this file a
reviewer cannot check by reading: `round()` looks like it rounds and quietly does not.

Its own module rather than a corner of `views.py`, and that is the point rather than tidiness:
the bound IS the policy of the whole rating feature, and `settle_round` can only apply it
correctly if the rule itself is testable without a room, a database or a request. Every test
that exercises `quality` directly is a `SimpleTestCase` for that reason, and the two-room proof
in `tests/test_rating.py` pins the same function end to end so a rewrite of this file cannot
quietly change what the game awards.
"""

import math

# How far a group may move the critic's score, in either direction. Three points out of ten is
# about a third of the scale: enough that a room that read the prompt well has a real say, small
# enough that twenty people who all think a prompt is worthless cannot talk the ranking into
# something the critic — who read the same prompt alone, with no idea how the room felt about it
# — would never have said. The rating is a game of prompt engineering; a bound too wide would
# make the outcome a popularity contest, and a bound too narrow would make the group a decoration
# on a model's opinion.
QUALITY_BAND = 3

# The 1-10 scale, in one place, because three things have to agree on where it ends: the
# endpoint's validation, `Rating`'s check constraint and the visual scale the players tap. A
# scale that was 1-10 in the view and 0-9 in the model would reject honest input and store a
# wrong ranking, silently.
SCALE_MIN, SCALE_MAX = 1, 10


def half_up(mean: float) -> int:
    """`mean` rounded to an integer, HALF UP: 7.5 is 8 and 6.5 is 7.

    NOT `round()`, which is round-half-to-EVEN by the IEEE 754 default Python inherits. That makes
    a score a player reads depend on which way an exact tie happened to lean: a peer mean of exactly
    7.5 settles to 8 while a mean of exactly 6.5 settles to 6, so two prompts the room judged
    identically can land a point apart, and the one that lost the point cannot tell why. Half-up is
    what a classroom expects from a mean, and it is monotone — every tie in the range goes the same
    way, which is the property the tie test pins.

    `math.floor(x + 0.5)` rather than `Decimal(...).quantize(ROUND_HALF_UP)`: the inputs are a sum
    of integers over a count, so the only inexactness is binary floating point on a value that is
    already representable as a ratio of small integers, and the epsilon-free form is the one that
    cannot be broken by a rounding mode. It is the same expression on every branch below, so the
    `llm is None` path and the clamped path cannot disagree about the tie.
    """
    return math.floor(mean + 0.5)


def quality(llm: int | None, peers: list[int]) -> int:
    """The peers' average, held inside ±QUALITY_BAND of the critic's score.

    THE LLM ARBITRATES, THE PEERS ADJUST. The critic reads the prompt on its own and is the only
    one of the two judges that never saw the answer or the group, so it is the anchor; the group
    knows what it has just read and is allowed to move the number, but not to replace it. A
    prompt a room cannot stand, whatever the critic thinks of its construction, cannot be held
    at the bottom, and a prompt the room loves cannot be floated to the top.

    THE BAND IS A RAIL, NOT A VETO. A group whose average is inside the band is heard exactly,
    and the `min`/`max` pair below is what makes that true: a mean of 7 against a critic of 5
    returns 7, not 5. Clamping the critic's own score to the group's, or averaging the two, would
    both read as "the model has the last word" and neither is what this feature is for.

    `llm is None` is the offline case: with no API key the critic is a local stub, and a stub
    that silently became the score would hand a heuristic the arbiter's chair. With no critic
    the band has no anchor to hold, so the peers' mean is the quality, uncapped.

    No peers at all is the other degenerate case — a prompt nobody but its author ever saw. The
    critic's score stands alone rather than a mean of nothing, which is why the empty list is
    checked before the division.

    Both roundings go through `half_up`, never `round()`, for the reason its docstring gives: a
    settled score must not depend on which way an exact tie leans.
    """
    if not peers:
        return llm if llm is not None else 0
    mean = sum(peers) / len(peers)
    if llm is None:
        return half_up(mean)
    # Clamp the MEAN to the band, never the critic: symmetric by construction, and the result
    # cannot leave the band after `half_up` because the band's own ends are integers.
    return half_up(max(llm - QUALITY_BAND, min(mean, llm + QUALITY_BAND)))
