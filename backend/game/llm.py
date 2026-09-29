"""The only place that talks to the model. Two calls: play, then judge.

With no OPENAI_API_KEY in the environment both calls fall back to a local stub, so the
game runs with no configuration at all. The stub is not a model: it echoes the prompt and
scores by word overlap. It says so in the judge's comment, which the game screen already
renders, so a player is never left thinking a real model judged them.
"""

import json
import os
import re
from typing import NamedTuple

from openai import OpenAI

PLAY_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")

STUB_COMMENT = "Local, pas de cle API : chevauchement de mots, pas un vrai jury."


class Verdict(NamedTuple):
    """The judge's ruling. A NamedTuple so it unpacks where that reads better."""

    score: int
    comment: str


JUDGE_SYS = (
    "Tu es le jury d'un jeu de prompt engineering. On te donne un prompt joue par "
    "un joueur, la reponse obtenue du modele, et la reponse cible cachee. "
    "Note la reponse obtenue de 0 a 10 selon sa proximite semantique avec la cible : "
    "10 = identique ou equivalente, 5 = vaguement proche, 0 = sans rapport. "
    "Penalise les reponses longues ou bavardes qui ajoutent du bruit. "
    "Reponds uniquement avec un objet JSON {\"score\": entier, \"comment\": texte court en francais}."
)


def _has_key() -> bool:
    """True when a real model is reachable. An empty value counts as absent."""
    return bool(os.environ.get("OPENAI_API_KEY"))


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def _stub_answer(prompt: str) -> str:
    # Echo the prompt, so a player who puts the target's words into it is the one who can
    # score. That keeps the whole loop playable without a model.
    return f"(reponse locale) {prompt}"


def _stub_score(output: str, target: str) -> Verdict:
    want = _words(target)
    if not want:
        return Verdict(0, STUB_COMMENT)
    return Verdict(round(10 * len(want & _words(output)) / len(want)), STUB_COMMENT)


def _chat(model: str, messages: list[dict], temperature: float, json_mode: bool) -> str:
    # The client is built here, not at import: `views.py` imports this module on every
    # request, and `OpenAI()` raises if OPENAI_API_KEY is missing. Both public entry points
    # return before reaching this when there is no key, so the suite also runs with none.
    client = OpenAI()
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=200)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs).choices[0].message.content.strip()


def answer(prompt: str) -> str:
    if not _has_key():
        return _stub_answer(prompt)
    return _chat(PLAY_MODEL, [{"role": "user", "content": prompt}], 0.7, False)


def score(prompt: str, output: str, target: str) -> Verdict:
    if not _has_key():
        return _stub_score(output, target)
    payload = json.dumps({"prompt": prompt, "reponse_obtenue": output, "cible": target}, ensure_ascii=False)
    raw = _chat(JUDGE_MODEL, [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": payload}], 0.0, True)
    data = json.loads(raw)
    # `dict.get`, not `[...]`: the judge is a model and can omit or null the score, and a
    # KeyError there turns a 0 into a 500 for the player. Anything `int` cannot read as a
    # number is worth 0, but a quoted one ("7") is read, not discarded — a judge that quotes
    # its number is still making a judgement, and dropping it would be harsher than reading it.
    try:
        points = int(data.get("score") or 0)
    except (TypeError, ValueError):
        points = 0
    return Verdict(max(0, min(10, points)), str(data.get("comment", "")))
