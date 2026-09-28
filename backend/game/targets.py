"""Seed bank of hidden target answers. One game draws ROUNDS_PER_GAME of them at random."""

import random

ROUNDS_PER_GAME = 10

TARGETS: list[str] = [
    "un escargot sur un clavier",
    "42",
    "une licorne sous la pluie",
    "Jacques Brel",
    "un pingouin en smoking",
    "Nantes",
    "le mont Rougemont",
    "un chat qui code en Python",
    "Star Wars",
    "1969",
    "la Tour Eiffel vue d'en haut",
    "Bob Marley",
    "une souris sans fil",
    "un sandwich triangle",
    "le sens de la vie",
    "un parachute",
    "Michelangelo",
    "une baleine dans un ascenseur",
    "Leonardo DiCaprio",
    "une pizza a quatre fromages",
    "un ventilateur dans un aquarium",
    "Facebook",
    "un canard en peasant",
    "une fonction qui ne fait rien",
]


def pick(n: int = ROUNDS_PER_GAME) -> list[str]:
    """Return n distinct targets, in random order."""
    if n > len(TARGETS):
        raise ValueError(f"cannot draw {n} distinct targets from {len(TARGETS)}")
    return random.sample(TARGETS, n)
