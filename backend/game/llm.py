"""The only place that talks to the model. Two calls: play, then judge."""

import json
import os
from typing import NamedTuple

from openai import OpenAI

PLAY_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")


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


def _chat(model: str, messages: list[dict], temperature: float, json_mode: bool) -> str:
    # The client is built here, not at import: `views.py` imports this module on every
    # request, and `OpenAI()` raises if OPENAI_API_KEY is missing. Deferring it keeps the
    # test suite runnable with no key at all.
    client = OpenAI()
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=200)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs).choices[0].message.content.strip()


def answer(prompt: str) -> str:
    return _chat(PLAY_MODEL, [{"role": "user", "content": prompt}], 0.7, False)


def score(prompt: str, output: str, target: str) -> Verdict:
    payload = json.dumps({"prompt": prompt, "reponse_obtenue": output, "cible": target}, ensure_ascii=False)
    raw = _chat(JUDGE_MODEL, [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": payload}], 0.0, True)
    data = json.loads(raw)
    # `dict.get`: the judge is a model and can omit or null the score, and a KeyError there
    # turns a 0 into a 500 for the player. A missing, null or unparseable score scores 0.
    return Verdict(max(0, min(10, int(data.get("score", 0) or 0))), str(data.get("comment", "")))
