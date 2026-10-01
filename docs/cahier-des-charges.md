# Cahier des charges du jeu de prompt engineering

## Contexte

Le prompt engineering s'apprend en écrivant, pas en lisant. En TP, un élève qui découvre
le sujet écrit un prompt en une ligne, obtient un résultat approximatif, et conclut que le
modèle est faible. Le problème est presque toujours ailleurs : un prompt sans contexte,
sans rôle, sans consigne de format.

Le jeu existe déjà (Django + Vite, ouvert sur le réseau local, jouable depuis un
téléphone), et son principe tient : afficher une cible, laisser chacun écrire un prompt,
laisser le modèle répondre, noter la proximité entre la réponse obtenue et la cible sur
10. Dix manches, un classement final. Le jeu mesure donc déjà quelque chose de réel, la
capacité à mettre en œuvre ce qui fait la qualité d'un prompt.

Trois manques bloquent cet objectif en séance.

**Le jeu démarre avant que les élèves sachent quoi chercher.** Les cibles sont tirées au
sort, donc un élève qui n'a jamais vu les notions joue au hasard pendant dix tours. Sa note
ne mesure pas son niveau, elle mesure le sujet qui lui est tombé. Le didactique arrive trop
tart, quand il ne peut plus rien réparer.

**Le jeu ne note que le résultat, jamais le prompt.** Un prompt peut obtenir 10/10 en
contournant l'exercice : demander la cible outright, ou la deviner. À l'inverse, un prompt
remarquable qui rate sa cible est indiscernable d'un prompt paresseux. Le jury actuel regarde
la sortie, il ne regarde jamais la méthode.

**Le seul juge est le modèle.** Personne d'autre ne seprononce. Un cours learns en se
discutant entre pairs, pas en attendant le verdict d'une API.

## Le jeu

Une partie, un didacticiel, dix tours, un classement.

```
lobby ──▶ didacticiel ──▶ ┌─ tour 1 ─┐
          (QCM, 8 questions) │        │
                            ▼        │
                     rédaction ──▶ notation ──▶ résumé ──▶ (tour suivant)
                                                            └─▶ classement final
```

### 1. Lobby

L'admin crée la partie et choisit si elle comporte un didacticiel. Les joueurs rejoignent
avec le code. Le choix du didacticiel est figé pour toute la partie.

### 2. Didacticiel

Huit questions à quatre choix sur le prompt engineering : qu'est-ce qu'un contexte, à quoi
sert un rôle, que change une consigne de format, pourquoi un prompt trop court échoue, etc.
Les huit questions et leur ordre sont identiques pour tout le monde, tirés une fois par
partie comme les cibles.

- Le joueur répond, reçoit la bonne réponse et son explication, puis passe à la suivante.
- Le didacticiel bloque la partie : aucun joueur ne peut écrire un prompt tant qu'il n'a pas
  répondu aux huit questions.
- La partie démarre seule dès que tous les joueurs **connectés** ont répondu aux huit
  questions. L'admin peut forcer le passage à tout moment.
- Le score du QCM est affiché et classé à la fin du didacticiel, puis **n'entre pas** dans le
  classement de la partie. Un joueur rejoint en retard, ou une partie relancée sans
  didacticique, ne doit pas être pénalisé par un score qu'il n'a pas eu l'occasion de
  faire.

### 3. Rédaction

Les dix cibles sont tirées au sort au lancement, identiques et dans le même ordre pour tout
le monde : le classement mesure les joueurs, pas le tirage. À chaque tour, la cible est
affichée, chaque joueur écrit un prompt, le modèle répond.

- Une seule réponse par joueur et par tour.
- Le tour se referme dès que tous les joueurs **connectés** ont répondu. Un téléphone qui
  dort ou qui perd le réseau ne bloque donc pas la classe. L'admin peut forcer la fin.
- Aucun joueur ne voit le prompt d'un autre à ce stade.

### 4. Notation croisée

Le tour bascule en notation. Les prompts sont révélés **un à un**, jamais tous ensemble.

Pour chaque prompt révélé, le joueur voit la cible, le prompt, et rien d'autre : ni la
réponse obtenue, ni sa note, ni le nom de l'auteur si l'anonymat est actif. Il attribue une
note de 1 à 10 sur la conformité du prompt aux règles du prompt engineering. Pas de
commentaire : sur un téléphone, un champ libre à chaque prompt fait abandonner.

- Un joueur ne note jamais son propre prompt. Il note donc les neuf prompts d'un tour à dix
  joueurs, et le tour ne se referme pas avant qu'il les ait tous notés.
- Le prompt suivant s'affiche dès que tous les joueurs **connectés** ont noté le prompt
  précédent. L'admin peut forcer l'avance.
- **Le classement est masqué pendant toute la phase de notation** : ni score, ni prompt, ni
  réponse dans le flux, pour personne. Les noms, eux, restent : c'est la liste de classe, et
  levoir ne dit rien du prompt de qui que ce soit. Ce sont les scores qui font la fuite :
  je connais mon propre score du tour, et s'il est le seul absent de la colonne, il
  désigne ma ligne. Le prix est l'absence de tension pendant la notation ; le gain est une
  notation qui n'est pas un concours de prédiction.

### 5. Garde-fou LLM

Chaque prompt est aussi évalué par un LLM, sur la même chose que les pairs : la conformité
aux règles du prompt engineering. Le LLM ne voit pas les notes des pairs et ne se cale sur
personne.

La note de qualité d'un prompt est la moyenne des pairs **bornée** dans un intervalle de
±3 points autour de la note du LLM :

```
qualité = min(max(moyenne_pairs, note_llm − 3), note_llm + 3)
```

Le LLM est l'arbitre, les pairs corrigent. Une classe hostile ne peut pas maintenir un
prompt nul à 10, ni lyncher un prompt solide à 0. En revanche une classe qui juge sainement
peut remonter un prompt que le LLM sous-estime de 3 points, et le faire descendre d'autant.
La note du LLM n'est montrée aux joueurs qu'au résumé de tour, jamais pendant la notation :
l'afficher pendant ouvrirait l'ancrage et les pairs jugeraient la note du LLM au lieu du
prompt.

### 6. Résumé de tour

Le résumé lève l'anonymat. Pour chaque joueur : le prompt, la proximité obtenue, la note du
LLM, la moyenne des pairs, la note de qualité retenue, la note du tour. Un joueur absent
n'apparaît pas dans la notation mais reste au classement avec les points qu'il a eus.

### 7. Note de tour et classement

La note d'un tour est la moyenne des deux dimensions, sur 10 :

```
note_tour = (proximité + qualité) / 2
```

Le classement final additionne les dix tours, sur 100. Le classement est mis à jour en
direct pendant la rédaction et le résumé, et il est masqué pendant la notation.

### 8. Relance

L'admin peut relancer une partie avec les mêmes joueurs, avec ou sans didacticiel. Sans
didacticiel, la relance va directement dans la rédaction, et les dix cibles sont
retirées au sort.

## Règles transverses

- **Anonymat** : choix de l'admin au moment de créer la partie, actif ou non pour les dix
  tours. L'anonymat masque le nom, pas l'égalité : tout le monde est noté, l'auteur est
  seulement inconnu. Un pseudonyme stable par joueur et par partie (« Joueur 3 ») évite que
  le même prompt soit attribué à deux personnes de deux manières différentes. Il ne résiste pas à
  quelqu'un qui compte les arrivées ; il retire le biais visuel, qui est le but.
- **Présence** : portée par le flux temps réel lui-même, jamais stockée. Un joueur qui
  revient est retrouvé par son jeton. Toute avance de phase compte les joueurs connectés.
- **Hors ligne** : le jeu tourne sans clé d'API. Le modèle devient un écho, le jury devient
  un chevauchement de mots, et l'écran le dit explicitement. Jouable de bout en bout, pas
  classable, et ce n'est pas la faute du prompt.
- **Contenus en données** : questions du QCM, cibles, barèmes de notation vivent dans des
  fichiers de contenu, jamais dans la logique.
- **Une prise par joueur et par phase.** Une reconnexion ne rejoue rien et ne double rien.

## Objectifs primaires

1. **Interdire de jouer sans avoir appris.** Le didacticiel conditionne la partie : aucun
   joueur n'écrit de prompt avant d'avoir vu les huit questions.
2. **Noter la méthode, pas seulement le produit.** Chaque prompt reçoit une note de
   conformité aux règles du prompt engineering, en plus de la note de proximité déjà
   existante. Sans cela, la note reste mesurable par la ruse.
3. **Rendre le classement indépendant de la notation des autres.** La note LLM borne les
   notes humaines dans une bande de ±3 : la qualité d'un prompt ne dépend pas du niveau de
   ses cornotes.
4. **Supprimer le biais de dénotation.** L'anonymat est un choix d'admin par partie, et il
   est réellement tenu, ce qui impose de masquer le classement pendant la notation.
5. **Rendre la seconde partie évidente.** Une relance avec les mêmes joueurs, avec ou sans
   didacticiel, pour pratiquer immédiatement après un cours.

## Objectifs secondaires

- **Réutiliser le socle existant** plutôt que le réécrire : mêmes cibles, même modèle de
  données, même transport temps réel, mêmes tests. Le CDC décrit une évolution du jeu, pas
  un nouveau jeu.
- **Tenir sur un téléphone, debout.** Une prise par joueur et par phase, un écran lisible sans
  zoom, et la progression de la partie visible en continu.
- **Ne jamais bloquer la séance.** Un joueur qui décroche n'arrête pas la partie ; l'admin
  avance quand il le décide, à n'importe quelle phase.
- **Rester jouable sans clé d'API.** Le mode de repli local est conservé, y compris pour
  l'évaluation de la qualité des prompts.
- **Rester éditable sans toucher au code.** Questions, cibles, consignes : des données.
