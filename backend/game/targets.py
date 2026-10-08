"""Seed bank of the round exercises. A game plays the first ROUNDS_PER_GAME of them, in order.

The ten "Exercices de prompting", one per category, in the order of the handout: the file
order IS the round order. Categories stay backend-internal.

Plain text, shown as-is with its line breaks: no Markdown is rendered on the game screen.
"""

ROUNDS_PER_GAME: int = 10

# Category -> target texts. Only store; flat views are built at use sites.
TARGETS_BY_CATEGORY: dict[str, list[str]] = {
    "resume_email": [
        """Vous avez reçu un email. Faites un prompt pour résumer ce mail.

Bonjour Monsieur Jean Dupont,

Nous vous faisons parvenir ce message afin de confirmer votre rendez-vous auprès du Centre Médical des Tilleuls.

Votre consultation est prévue le 14 octobre 2026 à 10h30 au service de médecine interne. Dossier : MT-2026-1047.

Cette consultation concerne l'évaluation d'un syndrome neurovasculaire idiopathique à manifestations céphalalgiques intermittentes, nécessitant un bilan complémentaire.

Merci de vous présenter 15 minutes avant le rendez-vous avec vos éventuels documents médicaux récents.

Cordialement,

Dr Éléonore Montferrand
Centre Médical des Tilleuls
Tél. : +41 81 000 00 00
secretariat@medical.ch""",
    ],
    "maths_fourier": [
        r"""Faites un prompt afin de vous aider pour cet exercice de math.

Problème
On considère la fonction périodique f(x) définie sur [−π, π] par :
f(x) = x
et prolongée périodiquement avec une période 2π.

1. Montrer que sa série de Fourier ne contient que des termes en sinus.
2. Déterminer les coefficients bₙ.
3. En déduire que :
   x = 2 \sum_{n=1}^{\infty} (-1)^{n+1} \, \frac{\sin(nx)}{n}
4. Utiliser cette série pour obtenir une approximation de π.

Notions utilisées : intégrales, fonctions périodiques, coefficients de Fourier et séries infinies.""",
    ],
    "reformuler_ingenieur": [
        """Reformulez ce texte pour correspondre aux attentes d'un ingénieur.

La lumiere va super vite, elle peut faire environ 300 000 kilometres en une seconde.

On veut savoir combien de temp elle met pour parcourir une certaine distance. Par exemple, si elle doit parcourir 600 kilometres entre deux villes.

Vu que la lumiere très vite, le temp que sa prend est vraiment tres petit.

Apres on peut essayer de calculer le temp exact et voir ce que sa donne si on augmente la distance.

Ref : Wikipédia, « Vitesse de la lumière »

Notions : notation scientifique des nombres, référence plus explicite, format scientifique.""",
    ],
    "pseudo_code": [
        """Il y a un problème dans ce pseudo-code. Aidez-vous d'un prompt pour résoudre le problème.

DEBUT

distance = 600
vitesse = 300000
secondes = 60
facteur = 2

temps = distance / vitesse
afficher "Temps :", temps

secondes = secondes - (30 * facteur)

si secondes >= 0 alors
    vitesse = vitesse * secondes
    temps = distance / vitesse
    afficher "Nouveau temps :", temps
sinon
    afficher "Valeur invalide"
fin si

distance = distance + 200
temps = distance / vitesse

afficher "Temps final :", temps

FIN""",
    ],
    "comptage": [
        "À l'aide d'un prompt, comptez le nombre de « é » dans le mot suivant :\n\ncélestéiale",
    ],
    "meta_prompt": [
        "Vous voulez comprendre la physique quantique, mais vous n'êtes pas expert. "
        "Faites un méta-prompt pour comprendre le domaine.",
    ],
    "preparer_examen": [
        """Vous devez vous préparer pour un examen. Quels prompts faites-vous pour vous préparer à partir des notions ci-dessous ?

Nombres
- Nombre rationnel : nombre qui peut être écrit comme une fraction de deux nombres entiers. Par exemple, 3/4, −2 ou 0,5.
- Nombre irrationnel : nombre qui ne peut pas être écrit exactement sous forme de fraction. Par exemple, √2 ou π.
- Nombre réel : ensemble comprenant les nombres rationnels et irrationnels. Tous les nombres réels peuvent être représentés sur une droite graduée.
- Valeur absolue : représente la distance entre un nombre et zéro, sans tenir compte de son signe. Ainsi, ∣−5∣ = 5.

Calculs
- Puissance : permet d'écrire plus simplement une multiplication répétée. Par exemple, 2³ signifie 2 × 2 × 2.
- Racine carrée : opération qui permet de trouver le nombre qui, multiplié par lui-même, donne le nombre de départ. Par exemple, √25 = 5.

Algèbre
- Équation : égalité mathématique dans laquelle une ou plusieurs valeurs peuvent être inconnues. Le but est généralement de trouver ces valeurs.
- Inconnue : valeur que l'on cherche dans une équation. Elle est souvent représentée par une lettre comme x.
- Fonction : relation mathématique qui associe généralement une valeur d'entrée à une valeur de sortie.
- Variable : quantité dont la valeur peut changer. Elle est souvent représentée par une lettre.
- Coefficient : nombre placé devant une variable et qui la multiplie. Dans 5x, 5 est le coefficient de x.
- Constante : valeur qui reste fixe dans une expression ou un problème. Dans 3x + 7, 7 est une constante.""",
    ],
    "faire_deviner": [
        "Faites deviner à l'IA « Où est Charlie ? », sans donner le nom du jeu dans aucune langue.",
    ],
    "generer_image": [
        "Générez cette image à l'aide d'un prompt.",
    ],
    "graphique_donnees": [
        """À partir des données ci-dessous, générez ce graphique à l'aide d'un prompt. Les données sont fictives.

Mois : Précipitations (mm)
Janvier : 68
Février : 61
Mars : 66
Avril : 72
Mai : 98
Juin : 104
Juillet : 96
Août : 102
Septembre : 88
Octobre : 82
Novembre : 79
Décembre : 81""",
    ],
}

# Category -> image shown under the exercise, as a path served by the frontend (`public/`).
IMAGES_BY_CATEGORY: dict[str, str] = {
    "generer_image": "/exercices/trapeze.png",
    "graphique_donnees": "/exercices/precipitations.png",
}


def image_for(target: str | None) -> str | None:
    """The image that goes with this exercise, or None. Looked up by text, since a round
    stores only its target: an exercise whose text was edited after the game started loses
    its image, never gets someone else's."""
    for cat, texts in TARGETS_BY_CATEGORY.items():
        if target in texts:
            return IMAGES_BY_CATEGORY.get(cat)
    return None


def pick(n: int = ROUNDS_PER_GAME) -> list[str]:
    """Return the first n targets, in file order."""
    bank: list[str] = [t for texts in TARGETS_BY_CATEGORY.values() for t in texts]
    if n > len(bank):
        raise ValueError(f"cannot draw {n} distinct targets from {len(bank)}")
    return bank[:n]
