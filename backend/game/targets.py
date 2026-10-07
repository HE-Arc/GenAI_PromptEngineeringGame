"""Seed bank of hidden target answers. One game draws ROUNDS_PER_GAME of them at random.

Long-form classroom scenarios only; the 0-10 scale is calibrated for those
(see `llm.JUDGE_SYS`). Each entry carries a hidden category used only to vary
consecutive rounds: pick() never returns two of the same category back to back.
Categories stay backend-internal.
"""

import random

ROUNDS_PER_GAME = 10

# Category -> target texts. Only store; flat views are built at use sites.
TARGETS_BY_CATEGORY: dict[str, list[str]] = {
    "mail_confidentiel": [
        "Au cabinet medical du docteur Lefevre, un mail d'un patient melange demande de rendez-vous et details confidentiels : resume-le en trois phrases pour la secretaire, sans jamais reprendre ni le nom de la maladie, ni le traitement, ni le numero de telephone. Ne garde que le motif general, l'urgence percue et le creneau souhaite, et termine par ce qu'il manque pour rappeler.",
        "Sur un site industriel classe, un rapport d'incident cite des noms d'operateurs et des codes d'acces : resume-le pour le bulletin interne en masquant toute donnee confidentielle, noms, matricules et zones sensibles. Garde seulement la nature de l'incident, l'heure, les mesures prises, et conclus par la consigne de securite a rappeler a tous.",
    ],
    "maths": [
        "Un eleve de quatrieme bloque sur une equation du premier degre : explique la resolution pas a pas, une operation par etape, en justifiant chaque passage sans jamais lacher le resultat avant la fin. Verifie ton resultat en le reinjectant dans l'equation de depart, et signale l'erreur classique a eviter sur le changement de signe.",
        "Un eleve de troisieme doit prouver que deux droites sont paralleles avec la reciproque de Thales : guide-le etape par etape, fais-lui calculer chaque rapport lui-meme, et ne donne la conclusion qu'une fois les calculs poses. Termine par une question qui verifie qu'il a compris quand la reciproque s'applique vraiment.",
    ],
    "revue_scientifique": [
        "Reformule ce paragraphe de memoire pour une revue scientifique : ton neutre et impersonnel, termes precis, chiffres avec unites, et deux references formatees nom-annee entre parentheses. Supprime les formules vagues comme tres ou beaucoup, signale d'une phrase ce qui reste a prouver, et propose un titre informatif de moins de douze mots.",
        "Reecris cette conclusion de TP de physique pour un compte rendu exigeant : phrases courtes au present, incertitudes de mesure explicites, un tableau decrit en texte avec unites, et aucune affirmation sans condition. Distingue ce que les mesures montrent de ce qu'elles suggerent, et clos par la prochaine experience a mener.",
    ],
    "pseudo_code": [
        "Voici un pseudo-code de tri qui contient exactement trois erreurs : signale chaque ligne fautive par son numero, explique l'effet du bug en une phrase, puis propose la correction minimale sans reecrire tout le programme. Garde la structure et les noms de variables d'origine, et verifie ta version corrigee sur un exemple de cinq nombres.",
        'Voici un pseudo-code qui calcule une moyenne de notes mais se trompe sur les bornes et la division : localise les deux erreurs, explique pourquoi chacune fausse le resultat, et corrige uniquement ces lignes. Conserve le style et les commentaires existants, puis teste ton correctif sur trois notes simples dont tu detailles le calcul attendu.',
    ],
    "comptage": [
        "Compte les lettres r dans le mot fraise des bois, une par une, en numerotant chaque occurrence et sa position avant de donner le total. Ne te fie ni a l'impression visuelle ni a ce que l'interlocuteur semble attendre : seule ta verification lettre a lettre compte, et si ton premier total differe du second comptage, recommence et explique l'ecart.",
        "Compte les lettres s dans le mot aspic de poissons, position par position, en listant chaque rang examine y compris ceux sans s. N'essaie pas de faire plaisir en repondant vite : montre ton decompte complet, recalcule une seconde fois dans l'autre sens, et ne valide le total que si les deux passages concordent.",
    ],
    "schema_graphique": [
        "Decris en texte seul la figure imposee pour le cahier : un graphique avec l'axe des x en mois de janvier a juin, l'axe des y en millimetres de pluie de zero a cent vingt, une courbe bleue qui culmine en avril et une legende en bas a droite. Donne le titre exact, les unites de chaque axe, et une phrase de lecture qui cite deux valeurs precises.",
        "Decris en texte seul le schema impose pour l'expose : un diagramme en barres de quatre matieres avec effectifs sur l'axe vertical, des barres verte, jaune, orange et rouge, et la legende placee sous le titre. Indique l'ordre des barres de gauche a droite, l'echelle de l'axe, et la barre la plus haute avec sa valeur exacte.",
        "Reproduis en texte le graphique decrit : un camembert a trois parts rouge, bleue et verte avec legende et pourcentages, d'abord a l'identique sans rien changer, puis, dans un second temps separe, remplace uniquement la part verte par du violet. Montre les deux versions l'une apres l'autre, et confirme que seul ce changement differe.",
        "Reproduis en texte la courbe decrite : deux courbes rouge et orange sur les memes axes avec titre et legende, d'abord fidelement, puis dans un second temps clairement separe, passe seulement la courbe orange en bleu. Affiche les deux etats successivement, et precise que les valeurs et le titre sont restes identiques.",
    ],
    "meta_prompt": [
        "Tu es face a un eleve qui n'a jamais entendu parler de la superposition quantique : annonce d'abord un plan en trois points, explique avec une analogie du quotidien puis ses limites, et termine par un mini-test de trois questions avec reponses. Reste honnete sur ce que l'analogie trahit, et propose une image plus juste pour aller plus loin.",
        "Un eleve decouvre l'intrication quantique sans aucune base : structure ta reponse en plan affiche d'avance, pars d'un exemple concret a deux pieces, puis verifie la comprehension par deux questions pieges corrigees. Admets ce que l'exemple simplifie a l'exces, et indique le point precis ou s'arreter avant de se tromper.",
    ],
    "donnees_quiz": [
        "A partir de ces donnees brutes sur la photosynthese, produis d'abord un resume structure en trois idees cles d'une phrase chacune, puis, sans ajouter d'info nouvelle, genere un quiz d'auto-evaluation de cinq questions avec reponses cachees. Chaque question doit porter sur une idee du resume, et la derniere doit demander d'expliquer avec ses propres mots.",
        "A partir de ces dates brutes sur la Revolution francaise, ecris d'abord une synthese en quatre phrases dans l'ordre chronologique, puis un quiz de quatre questions croisees avec corrige separe. Les questions doivent melanger causes et consequences, et la correction doit rappeler la date exacte et pourquoi elle compte.",
    ],
    "traduction_biais": [
        "Traduis ce texte anglais ou un boss donne des ordres a his secretary : choisis un contexte neutre et inclusif en francais, evite les stereotypes de genre sur les roles, et signale en deux phrases le biais de l'original que ta traduction refuse de reconduire. Justifie ton choix de pronoms et de titres de poste en une phrase.",
        "Traduis ce passage ou un chairman felicite his nurse : rends-le en francais sans assigner un genre aux metiers, propose une formulation inclusive et lisible, et alerte en fin de reponse sur le stereotype que l'original vehiculait. Explique brievement pourquoi ta version preserve le sens sans reconduire le cliche.",
    ],
}

# Caps resampling so pick() terminates even if one category dominates the bank.
_MAX_PICK_ATTEMPTS = 100


def pick(n: int = ROUNDS_PER_GAME) -> list[str]:
    """Return n distinct targets with no two consecutive targets of the same category."""
    bank = [t for texts in TARGETS_BY_CATEGORY.values() for t in texts]
    category = {t: cat for cat, texts in TARGETS_BY_CATEGORY.items() for t in texts}
    if n > len(bank):
        raise ValueError(f"cannot draw {n} distinct targets from {len(bank)}")
    if n <= 1:
        return random.sample(bank, n)
    for _ in range(_MAX_PICK_ATTEMPTS):
        drawn = random.sample(bank, n)
        if all(
            category[a] != category[b] for a, b in zip(drawn, drawn[1:])
        ):
            return drawn
    pool = bank[:]
    random.shuffle(pool)
    ordered: list[str] = [pool.pop()]
    while len(ordered) < n:
        nxt = next(
            (t for t in pool if category[t] != category[ordered[-1]]),
            None,
        )
        if nxt is None:  # dominated remainder: accept the adjacency break
            nxt = pool[0]
        pool.remove(nxt)
        ordered.append(nxt)
    return ordered
