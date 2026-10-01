"""The only place that talks to the model. Three calls: play, then judge, then critique.

With no OPENAI_API_KEY in the environment all three fall back to a local stub, so the game
runs with no configuration at all. No stub is a model: the answer stub echoes the prompt, the
judge stub scores by word overlap, the critic stub counts structure markers. Each says so in
its comment, which the game screen already renders, so a player is never left thinking a real
model judged them.
"""

import json
import os
import re
from typing import NamedTuple

from openai import OpenAI

PLAY_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "gpt-4o-mini")
# The critic gets its own env var for the same reason the judge does: arbitration is the
# call you want to be able to move to a bigger model on a day when the group's scores look
# wrong, without touching the two calls that are on the critical path of every round.
CRITIC_MODEL = os.environ.get("CRITIC_MODEL", "gpt-4o-mini")

# The player-visible honesty surface. It has to say two things, not one: this is not a model,
# AND the number it produces understates a long prompt. A comment that only admitted the first
# would leave a player reading a local rule over a score of 2 and concluding their prompt was
# bad, when the ceiling is a property of the method.
#
# Shared by all three stubs, so it names no one method in particular: the judge counts word
# overlap and the critic counts structure markers, and a comment claiming the first would be
# wrong under the second — and a player misled about how the number was produced is exactly the
# failure this constant exists to prevent.
STUB_COMMENT = (
    "Local, pas de cle API : regle locale, pas un vrai jury. "
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

# The critic is a third judge with a different job, so it gets its own rubric rather than a
# paragraph appended to JUDGE_SYS. It rates the METHOD — the prompt as written — and never the
# result. Two reasons, and the second is the important one:
#
# 1. The game already has a call that rates the result (the judge, by proximity to the target).
#    A critic that also looked at the answer would be that call twice, and the players would be
#    told the same rubric twice in two different words.
# 2. This verdict is the anchor of the ±3 peer band, so it has to be independent of the outcome.
#    A critic that rewarded a lucky result would teach players that vague prompts sometimes win
#    and to re-roll instead of iterating — the exact opposite of what the didacticiel teaches.
#
# So the rubric below never mentions the target except to forbid the comparison, and never says
# anything about how good the answer turned out.
CRITIC_SYS = (
    "Tu es le critique de prompt d'un jeu de prompt engineering. On te donne un prompt ecrit "
    "par un joueur. Tu notes la METHODE de ce prompt, pas son resultat : tu evalues la facon "
    "dont la demande est formulee, et rien d'autre. "
    "Tu ne compares pas la reponse a la cible, tu ne la vois pas, et un resultat chanceux ne "
    "te fait pas monter la note. "
    "Note de 0 a 10 sur cinq criteres, dans cet ordre d'importance : "
    "le role et le public sont-ils enonces (a qui l'on parle, pour qui l'on parle) ; "
    "le contexte est-il fourni (situation, materiel, donnees, ce que le modele ne peut pas "
    "deviner tout seul) ; "
    "le format de sortie est-il demande (liste, tableau, nombre d'elements, json) ; "
    "la longueur est-elle bornee sans etre sur-specifiee ni laissee implicite ; "
    "aucun exemple donne ne se contredit-il. "
    "9 ou 10 = les cinq criteres sont satisfaits. "
    "6 a 8 = quatre criteres, ou trois et un indifferent. "
    "3 a 5 = deux criteres seulement. "
    "0 a 2 = une consigne nue, sans role ni format ni contexte. "
    "Un critere absent ne se recompense pas par un autre : un prompt dont le role est "
    "excellent mais qui ne dit rien du format ne depasse pas 6. "
    "Reponds uniquement avec un objet JSON {\"score\": entier, \"comment\": texte court en "
    "francais, portant sur la methode du prompt et non sur le resultat qu'il a produit}."
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
# CRITIC_MAX_TOKENS equals JUDGE_MAX_TOKENS and must stay its own constant. Equal because both
# calls return the same shape — one integer and one short sentence — so the bound is a property
# of the output, not a coincidence between two tunings. Separate because the two numbers are
# reasoned about separately: unifying all three is exactly the bug PLAY_MAX_TOKENS exists to
# warn about, and a critic silently handed 800 would only ever waste tokens explaining itself.
CRITIC_MAX_TOKENS = 200


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


# The stub critic counts the markers of a well-formed prompt. One per rubric criterion, kept as
# plain lowercase substrings because a player writes "Public :" and "public :" and both count.
_STUB_ROLE = ("tu es", "role", "incarne", "agis comme", "parle en tant que")
_STUB_PUBLIC = ("public", "destinataire", "lecteur", "adresse", "pour un", "pour une", "niveau")
_STUB_CONTEXT = ("contexte", "situation", "cadre", "materiel", "donnees", "enonce", "ingredients")
_STUB_FORMAT = ("format", "liste", "tableau", "json", "markdown", "puces", "sous forme", "en 3", "en 2", "en 5")
# Length is a separate marker because it is the criterion most often missing: a prompt can be
# perfectly structured and still say nothing about how much it wants back.
_STUB_LENGTH = ("maximum", "moins de", "au plus", "mots", "lignes", "phrase", "court", "bref", "concis")


def _stub_grade(prompt: str) -> Verdict:
    # A heuristic, not a jury, and deliberately the smallest one that can order the two cases
    # the stub has to order. It counts four markers worth 2 points each (role, public,
    # contexte, format) and one worth 1 (length), over a base of 1 for having written something
    # at all — so a structured prompt reads 9, a bare one reads 1, and the ceiling of 10 is only
    # reachable by hitting every criterion including the one players forget.
    #
    # This is a word-counting stub in the same sense as `_stub_score`: it cannot tell a good
    # argument from a keyword salad, and it credits "public :" whether or not the audience is
    # named. It returns STUB_COMMENT for exactly that reason — the number is the method's, not
    # the player's, and a local fallback that pretended to judge would be worse than one that
    # admits it does not. Give it a real critic instead, via OPENAI_API_KEY.
    text = prompt.lower()
    if not text.strip():
        return Verdict(0, STUB_COMMENT)
    points = 1
    for markers in (_STUB_ROLE, _STUB_PUBLIC, _STUB_CONTEXT, _STUB_FORMAT):
        points += 2 * any(marker in text for marker in markers)
    points += 1 * any(marker in text for marker in _STUB_LENGTH)
    return Verdict(points, STUB_COMMENT)


def _chat(model: str, messages: list[dict], temperature: float, json_mode: bool, max_tokens: int) -> str:
    # The client is built here, not at import: `views.py` imports this module on every
    # request, and `OpenAI()` raises if OPENAI_API_KEY is missing. All three public entry points
    # return before reaching this when there is no key, so the suite also runs with none.
    #
    # `max_tokens` is a parameter, not a constant, because the callers want different caps —
    # see PLAY_MAX_TOKENS and JUDGE_MAX_TOKENS for why, and for why unifying them is the bug.
    client = OpenAI()
    kwargs = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return client.chat.completions.create(**kwargs).choices[0].message.content.strip()


def _verdict(raw: str, default_comment: str) -> Verdict:
    """Read one 0-10 JSON verdict. Shared by the judge and the critic.

    Both calls ask the same shape and both answers land in the same player-visible box, so the
    tolerance that keeps a chatty reply from becoming a 500 has to be identical for them: two
    copies of this logic is two places for the clamp and the null handling to drift apart, and
    the critic's number is what the peer band is clamped around.
    """
    data = json.loads(raw)
    # `dict.get`, not `[...]`: the judge is a model and can omit or null the score, and a
    # KeyError there turns a 0 into a 500 for the player. Anything `int` cannot read as a
    # number is worth 0, but a quoted one ("7") is read, not discarded — a judge that quotes
    # its number is still making a judgement, and dropping it would be harsher than reading it.
    try:
        points = int(data.get("score") or 0)
    except (TypeError, ValueError):
        points = 0
    return Verdict(max(0, min(10, points)), str(data.get("comment") or default_comment))


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
    return _verdict(raw, "")


def grade_prompt(prompt: str, target: str = "") -> Verdict:
    """The critic's ruling on the method. This is the anchor of the ±3 peer band.

    The payload carries the prompt and the target and nothing else, deliberately: the critic
    arbitrates between the model and the group, so it has to see neither the group's ratings
    nor the answer those ratings were given about. A critic that saw the peers would be pulled
    by them, and a rail that moves with the thing it is supposed to hold is not a rail.
    """
    if not _has_key():
        return _stub_grade(prompt)
    payload = json.dumps({"prompt": prompt, "cible": target}, ensure_ascii=False)
    raw = _chat(
        CRITIC_MODEL,
        messages=[{"role": "system", "content": CRITIC_SYS}, {"role": "user", "content": payload}],
        temperature=0.0,
        json_mode=True,
        max_tokens=CRITIC_MAX_TOKENS,
    )
    # STUB_COMMENT as the fallback, not "": a critic that answered with a bare score has still
    # not told the player why the number is low, and the honest reading of that number is the
    # local heuristic's. Better a truthful sentence than an empty box.
    return _verdict(raw, STUB_COMMENT)
