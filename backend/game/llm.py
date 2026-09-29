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

# The player-visible honesty surface. It has to say two things, not one: this is not a model,
# AND the number it produces understates a long prompt. A comment that only admitted the first
# would leave a player reading "chevauchement de mots" over a score of 2 and concluding their
# prompt was bad, when the ceiling is a property of the method.
STUB_COMMENT = (
    "Local, pas de cle API : chevauchement de mots, pas un vrai jury. "
    "Avec une cible longue, les scores sont bas par construction : c'est la methode, pas ton prompt."
)


class Verdict(NamedTuple):
    """The judge's ruling. A NamedTuple so it unpacks where that reads better."""

    score: int
    comment: str


JUDGE_SYS = (
    "Tu es le jury d'un jeu de prompt engineering. On te donne un prompt joue par "
    "un joueur, la reponse obtenue du modele, et la reponse cible cachee. "
    "La cible est un texte long : une recette, une description, un recit, une enigme. "
    "Note la reponse obtenue de 0 a 10 selon sa proximite semantique avec la cible, "
    "c'est-a-dire la quantite d'information de la cible que la reponse fait retrouver au lecteur. "
    "La longueur n'est jamais un defaut en soi : ne retire aucun point pour une reponse longue, "
    "seulement pour ce qu'elle ajoute et qui n'y figure pas. "
    "9 ou 10 = la cible est la redite dans sa substance et dans son detail, sans contrediction "
    "ni invention ; un texte seulement equivalent a la cible plafonne a 8, et la recopie mot pour "
    "mot n'est ni attendue ni atteignable sur un paragraphe. "
    "7 ou 8 = meme sujet, meme contenu, mais un element notable manque ou est deforme. "
    "5 ou 6 = l'intention est la bonne, la moitie du contenu manque, ou c'est generique. "
    "3 ou 4 = voisin, un rapport de theme, tres peu de substance reprise. "
    "0 a 2 = sans rapport, ou le joueur a seulement ecrit autre chose. "
    "Une reponse qui restitue la substance de la cible ne descend jamais sous 6, quels que soient "
    "les mots choisis et la longueur du texte. "
    "Reponds uniquement avec un objet JSON {\"score\": entier, \"comment\": texte court en francais}."
)

# The two calls need different caps. Do not unify them again: the number is not a preference,
# it is a property of what each call has to return.
#
# The play call has to REPRODUCE a long-form target — a 40-60 word recipe, a description — and a
# generated answer that runs to several times the target is the normal, correct case. French
# tokenises at roughly 1.6 tokens per word, so a faithful 250-word answer already costs ~400
# tokens; 200 truncated every such answer mid-sentence, which the judge then scored as an
# incomplete reproduction. 800 covers a ~500 word answer with margin, which is far past anything
# a player needs and still bounds a runaway generation.
#
# The judge returns a fixed JSON object with one integer and one short sentence — under 60 tokens
# in any sane reply. Its cap stays at 200: ten times what the format can use. Raising it buys
# nothing, and a low cap is a cheap guard against a judge that decides to explain itself at
# length, which would both waste the call and push real content out of the player's comment box.
PLAY_MAX_TOKENS = 800
JUDGE_MAX_TOKENS = 200


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
    # Overlap divided by the TARGET's word count, so the ceiling is only reached by reproducing
    # every word. That was fair when a target was two to four words and a player could hold the
    # whole thing in their head. It stops being fair now that a target is 40-60 words of recipe:
    # the denominator grows with the target and the prompt does not, so the reachable ceiling
    # falls as the content gets longer. Realistic play lands in the 1-4 band however good the
    # prompt is.
    #
    # So the honest reading of a low stub score is "this method cannot see past a paragraph",
    # NOT "your prompt was bad" — which is why STUB_COMMENT above says so where the player will
    # actually read it, not just here. Deliberately left as a word-overlap stub: making it
    # smarter would mean inventing a similarity heuristic that guesses at meaning, and a local
    # fallback that pretends to judge is worse than one that admits it does not. Give it a real
    # judge instead, via OPENAI_API_KEY.
    want = _words(target)
    if not want:
        return Verdict(0, STUB_COMMENT)
    return Verdict(round(10 * len(want & _words(output)) / len(want)), STUB_COMMENT)


def _chat(model: str, messages: list[dict], temperature: float, json_mode: bool, max_tokens: int) -> str:
    # The client is built here, not at import: `views.py` imports this module on every
    # request, and `OpenAI()` raises if OPENAI_API_KEY is missing. Both public entry points
    # return before reaching this when there is no key, so the suite also runs with none.
    #
    # `max_tokens` is a parameter, not a constant, because the two callers want different caps —
    # see PLAY_MAX_TOKENS and JUDGE_MAX_TOKENS for why, and for why unifying them is the bug.
    client = OpenAI()
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs).choices[0].message.content.strip()


def answer(prompt: str) -> str:
    if not _has_key():
        return _stub_answer(prompt)
    return _chat(PLAY_MODEL, [{"role": "user", "content": prompt}], 0.7, False, PLAY_MAX_TOKENS)


def score(prompt: str, output: str, target: str) -> Verdict:
    if not _has_key():
        return _stub_score(output, target)
    payload = json.dumps({"prompt": prompt, "reponse_obtenue": output, "cible": target}, ensure_ascii=False)
    raw = _chat(
        JUDGE_MODEL,
        [{"role": "system", "content": JUDGE_SYS}, {"role": "user", "content": payload}],
        0.0,
        True,
        JUDGE_MAX_TOKENS,
    )
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
