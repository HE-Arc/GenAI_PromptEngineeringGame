"""Seed bank of hidden target answers. One game draws ROUNDS_PER_GAME of them at random.

Every target is long-form text — a recipe, a description, a riddle — never a noun phrase. That
is the whole game now: getting a model to reproduce a 300-character paragraph is a different
exercise from getting it to say "Nantes", and the 0-10 scale is calibrated for the former (see
`llm.JUDGE_SYS`). A target short enough to simply be guessed was a target the player did not
have to prompt for.

The mix of registers is deliberate and worth preserving when editing: recipes, proper nouns,
bare numbers, real places, and absurd images. The registers are what stop 10 rounds from feeling
like 10 of the same round, and the absurd ones in particular give a weak prompt somewhere
obvious to fail, which is what makes a low score legible.
"""

import random

ROUNDS_PER_GAME = 10

TARGETS: list[str] = [
    # --- Recettes ---
    "La recette du gateau au yaourt du mardi soir : prechauffe le four a 180 degres, beurre un "
    "moule a gateau, puis melange 3 pots de yaourt avec 4 wanannes, ajoute 150 g de farine, deux "
    "oeufs battus et une pincee de sel. Verse la pate sans te depesoter. Le secret n'est pas "
    "l'horloge mais le couteau : plante-le au centre et il doit ressortir propre, pas humide. "
    "Personne n'en dit jamais du mal, et personne n'en refait deux fois.",

    "Comment preparer un cassoulet qui tient jusqu'au dimanche : prends une cocotte en fonte, un "
    "kilo de haricots blancs trempes la veille dans l'eau froide avec une feuille de laurier, "
    "400 g de gorge de porc en gros cubes, deux saucisses et un pied de veau. Fais colorer la "
    "viande au lard, ajoute une carotte et un oignon tranches, recouvre de bouillon, et laisse "
    "cuire trois heures a petit feu. Emporte le plat chez le voisin, ou ne le fais pas.",

    "La tourte aux pommes de ma mere, expliquee sans balance : deux pommes par personne, "
    "rinceres, coupees en quartiers fins, ni ecorees ni pelees. Pour la farce, 250 g de farine, "
    "125 g de beurre froid, un demi-verre de sucre, un jaune d'oeuf. Roule la pate en deux "
    "ronds, pose la farce, ajoute les pommes, couvre, pince les bords a la fourchette. "
    "45 minutes a 200 degres, jusqu'a ce que le jus qui perle sur la plaque caramelise.",

    "Le bouillon qui n'est jamais amer, explique par un chef qui se trompe tout le temps : ne "
    "mets jamais la viande directement dans l'eau bouillante, le froid la saisit doucement et "
    "garde le jus dedans. Coupe l'oignon en deux, face contre le fond, et retire-le quand il "
    "est dore : c'est lui qui est amer, pas le bouillon. Desarrobe toutes les 20 minutes, jamais "
    "tout d'un coup, et ecoute la casserole plutot que l'horloge.",

    "Le gateau au citron qui n'est pas sec : le secret est de melanger deux jaunes d'oeufs avec "
    "150 g de sucre jusqu'a ce que le melange soit blanc epais, puis d'incorporer le zeste d'un "
    "citron entier, deux cuillères a soupe de jus frais, et enfin 125 g de farine levee en deux "
    "fois. Beurre le moule et presse-le jusqu'au doigt. 35 minutes au four, et surtout "
    "refroidi complet avant le glaçage, sinon le citron s'evade.",

    "Les crepes de sarrasin, version tuesday : 250 g de farine de sarrasin, 3 oeufs, 500 ml de "
    "lait, 20 g de beurre fondu, une pincee de sel. La pate doit etre lisse comme de la creme. "
    "Laisse-la reposer une heure, c'est la que la farine decide de ton caractere. La premiere "
    "crepe sera mauvaise et la dixieme meilleure. Garde la pate au refrigerateur : elle "
    "vieillit mieux que le jour meme.",

    # --- Noms propres ---
    "Jacques Brel, vu de l'arriere de la scene au moment ou il coupe la musique : un homme qui "
    "vient d'arreter de chanter devant plusieurs milliers de personnes, la tete basse, et qui "
    "cherche dans le noir ou est la sortie. Le silence dure assez longtemps pour etre un choix. "
    "Puis il leve les yeux, et ce n'est plus le meme homme qui reprend le refrain.",

    "Bob Marley, photogravure de 1980 : debout dans son tailleur noir et son bonnet rouge, deux "
    "leaders apparaissent en surimpression a cote de lui, la banane du peace toujours dirigee "
    "vers le bas, alors que le regard est leve et tourne vers toi. Rien dans l'image n'explique "
    "le mot qu'il porte, et c'est precisement ce qui en fait une vignette.",

    "Le poster officiel de Star Wars en 1977, le premier : une lune en plein cadre, une ligne "
    "d'asteroides en diagonale, et l'un des deux personnages baisse les yeux vers quelqu'un "
    "pendant que l'autre regarde droit devant, vers toi, pour rien. La vignette est plus "
    "mysterieuse que le resume, et c'est la promesse que le resume a oubliee.",

    "Facebook : une facade bleue d'ecole, le nom en clair, et la fenetre. Une entreprise pour "
    "qui le monde entier est un annuaire, dont on ne peut rien supprimer, ou les photos de "
    "vacances sont publiques par defaut, et ou un bouton J'aime remplace tout le vocabulaire de "
    "l'affection dont on dispose.",

    "Michelangelo au sommet de la chapelle Sixtine, en 1564, dix-sept ans de travail, peint a la "
    "frappe et non au pinceau : un homme de quarante ans sur un echafaudage, la tete en arriere, "
    "un oeil releve vers la voute qu'il vient de decouvrir, et devant lui un personnage nu de "
    "pres de dix metres de haut. Il a passe deux ans allonge sur le dos, les yeux infectes, et il "
    "a dit que son corps avait change de nature.",

    "Leonardo DiCaprio en 1997, la scene du naufrage, sa nomination aux Oscars l'ayant fait "
    "blaguer pendant des mois : un homme en manteau trempe de suie qui leve la tete vers une "
    "femme en robe de bal, et la fumee de la machine a cafe derriere eux ne l'a pas encore "
    "rattrape. Les critiques l'ont trouve en roue libreuse pendant six mois, et c'est "
    "exactement ce qu'il etait.",

    "Walt Disney en 1937, dans un studio de Burbank qu'il vient de batir en brique : la photo "
    "montre un homme en pull qui dessine de petites figures sur un mur blanc, et les murs sont "
    "couverts d'esquisses de ses propres films, encore en noir et blanc. Il a vendu sa voiture "
    "pour payer Mickey, et obtenu un succes que personne, lui le premier, n'avait prevu.",

    # --- Nombres, dates, lieux ---
    "Le 42 : ni une reponse trouvee, ni une cle de dictee, mais l'origine exacte du nombre dans "
    "la phrase que tout le monde cite de travers. Il est ecrit dans le roman de Douglas Adams en "
    "1978, il tient lieu de reponse a la question sur la vie, l'univers et tout, et son auteur "
    "ne l'a jamais explique. Tout le reste est interpretation, y compris ce que vous lisez "
    "la-dessus en ce moment.",

    "1969 : trois evenements que l'annee a portees en meme temps. Le premier vol humain sur la "
    "Lune le 20 juillet, un concert de cent mille personnes a Bethune le 26 aout, et un "
    "festival de Wight le 30 aout, dont l'organisation fut un desenroi. Le premier, on le voit "
    "en direct a la television ; les deux autres, on y va par la route ou en bateau. On peut "
    "faire les trois en quatre semaines, et personne n'y croyait.",

    "Nantes, un samedi de novembre, 18 h 20 : la pluie fine ne tombe pas, elle flotte entre les "
    "tours du centre et fait luire les paves. Les quais de la Loire sont gris comme le ciel, un "
    "tram passe sur le pont et tout le monde a deja l'habitude. Nantes n'est pas une belle "
    "ville, c'est une ville ou l'on a deja decide qu'on y resterait.",

    "La tour Eiffel vue d'en haut, la nuit : la forme n'apparait pas, il n'y a pas de tour, il "
    "y a un point rouge clignotant au sommet d'une colonne de lumiere doree, des faisceaux qui "
    "partent vers l'exterieur, un fragment de l'anneau de fer dans le noir, le vide entre les "
    "branches. On voit la Seine comme un reflet, pas comme un fleuve. Le point rouge est la "
    "seule chose qui batte dans l'image.",

    "Le mont Rougemont, au sommet, un soir d'aout : de la houle verte de chaque cote, la mer "
    "qui bat comme une claquerie, une pointe, un grand lee, un toboggan en bois, une piste de "
    "luge qui date de 1969 et n'a jamais ete Quite, et de la creme glacee dans un pot qui a "
    "coeur d'y voir. La Bretagne est la seule chose que la France sait faire en plus court, et "
    "c'est ce qui fait tout le reste du pays.",

    "Le chateau des Ducs de Bretagne, un apres-midi de pluie : sept hectares de remparts au "
    "milieu de la ville, une forteresse entouree d'un parc, et la difficulte a etre surpris qu'el"
    "le soit la. Le musee est gratuit, la grande cour sert de parking, et les familles s'y "
    "promenent sans avoir l'impression d'avoir rien vu. C'est la visite la plus courte du "
    "voyage, et celle dont on se souvient le mieux.",

    # --- Images absurdes ---
    "Un escargot sur un clavier d'ordinateur portable, vu de trois quarts, la coquille "
    "bourreutee de condensation, il est pose sur la touche P, et les trois lettres voisines "
    "portent la trace de ses passages. Le document ouvert contient exactement un mot, la "
    "batterie est a moins de dix pour cent. Personne ne l'a deplace depuis qu'il a choisi la "
    "touche P, et il y est reste toute la nuit.",

    "Un pingouin en smoking, sur la banquise antarctique, a l'heure bleue : le costume est "
    "parfaitement ajuste, le noeud papillon est de travers, il tient une flute de champagne a "
    "deux pattes, et il a trace son propre cercle de patins dans la neige, qui converge vers "
    "lui. Autour, la banquise est parfaitement lisse jusqu'a l'horizon. Il a l'air d'avoir "
    "reservé la table.",

    "Une licorne sous la pluie, sur un parking de centre commercial, la nuit : la pluie tombe "
    "verticalement dans les halos des lampadaires, l'animal est trempe jusqu'a la criniere et "
    "ne tente pas de s'abriter, la corne vrille, la criniere colle aux flancs, elle regarde les "
    "enseignes luminees. Aucune voiture, personne, et la pluie qui a commence a la meme heure "
    "qu'elle.",

    "Une baleine dans un ascenseur : elle occupe la majeure partie de la cabine, sa queue est "
    "coincee entre les portes, le panneau affiche la surcharge en rouge, et les rampes "
    "d'illumination ont change de couleur au passage d'un etage. Un voyageur appuie sur le "
    "bouton du troisieme. L'ascenseur hesite, puis continue.",

    "Un ventilateur de table tourne a fond, dans un aquarium d'eau douce : des poissons nagent "
    "dans les branches creuses comme des algues, le courant de l'eau suit exactement le sens de "
    "l'helice, et la mousse s'est coincee dans la grille a l'arriere. Il a ete pose la comme "
    "cela, et il fonctionne. Quelqu'un l'a choisi precisement pour ca.",

    "Un chat qui code en Python, assis devant un portable dans une cuisine qui sent le "
    "ratatouille, une patte sur le clavier et l'autre, levee, en l'air. L'ecran affiche une "
    "boucle for qui tourne depuis quarante minutes, et une fenetre de terminale derriere, ou le "
    "traceback est deja si long qu'il a ete nettoye deux fois. Le chat attend le programme. On "
    "l'a ecrit un jour, et depuis le programme fait ce qu'il veut.",

    "Une souris sans fil, seule au milieu d'une table en bois de quatre metres, dans un bureau "
    "vide : une souris d'ordinateur sans le bureau qui va avec, branchee a rien, allumee, posee "
    "pile au centre de la surface et parfaitement alignee avec le bord. Elle a atterri sur un "
    "autre continent que l'ordinateur auquel elle appartient.",

    "Un canard en peasant sur un trottoir, qui tient un panneau de la main gauche. C'est sa "
    "main gauche : il a donc inverse le geste, et c'est le detail le plus perturbant de l'image. "
    "Il est plus large que la poubelle a cote de lui. Il ne vend rien et ne demande pas d'argent, "
    "et il a plus de monde a ses pieds que le vendeur.",

    "Une fonction qui ne fait rien : une piece de code qui recoit ses arguments, ne modifie "
    "rien, ne renvoie rien, ne leve pas, et pourtant s'appelle deux fois par seconde depuis le "
    "coeur du programme. On l'a laissee en place pendant onze mois. Le teste est verte. Personne "
    "ne sait pourquoi le programme n'est pas plus rapide sans elle.",

    "Un parachute ouvert en dedans comme une fleur, sur un balcon du troisieme etage, les "
    "lanieres a plat sur le bitume, le vent siffle dedans comme dans une gouttiere. Personne "
    "n'attendait de ce parachute-la. Il est la depuis 2019, le proprietaire dit qu'il n'a jamais "
    "eu besoin de lui et qu'il ne le jettera pas, on ne sait pas pourquoi, et il a l'air "
    "d'avoir raison.",

    # --- Absurdes, mais decrits comme des recettes ---
    "La recette du sandwich triangle, en coupe : deux bandes de pain de mie toastees, une "
    "tranche de fromage fondue qui coule, un peu de moutarde, deux rondelles de cornichon, et "
    "la coupure en deux qui tient debout grace au carton. Ce qu'il y a dedans est meilleur "
    "qu'il n'y a a l'exterieur. On ne peut pas le couper autrement qu'en deux triangles, et "
    "c'est le seul aliment qui refuse categoriquement d'etre rond.",

    "La pizza a quatre fromages, decrite par la salle ou on la mange : la part a la bordure la "
    "plus brulee, la base qui plie un peu sous le poids, la mozzarella qui file en filet long "
    "quand on la prend avec la pince, le bleu qui s'ouvre en surflet au centre, le petit lapin "
    "d'origene verte, et l'homme au comptoir qui n'a rien dit depuis dix ans. Le four ne s'ouvre "
    "jamais devant le client, c'est une regle qu'il s'est donnee lui-meme.",

    "Le sens de la vie, explique comme une recette. Commencez par un reve qui dure plus de trois "
    "ans. Cassez-le avant qu'il ne devienne trop solide. Gardez le morceau qui vous ressemble le "
    "moins, c'est le plus honnete. Ajoutez un ami qui vous dit les choses quand vous avez tort, "
    "sans vous demander ce que vous en pensez. Ne cherchez pas la reponse : la recette n'a pas "
    "de fin, c'est la cuisson qui compte, et le four doit etre surfeu.",

    "La recette d'un parachute, en version longue : coupez la corde, comptez jusqu'a dix, et ne "
    "regardez pas en bas. Pour la preparation complete, ajoutez un auditeur, des toilettes, "
    "et dix minutes pendant lesquelles il ne se passe strictement rien. Servez froid, sans "
    "ceremonie, et gardez une cordelette de rechange au cas ou. Ce n'est pas un plat, c'est une "
    "procedure de survie, ce qui revient au meme en cuisine quand on a oublie d'ouvrir la "
    "fenetre.",
]


def pick(n: int = ROUNDS_PER_GAME) -> list[str]:
    """Return n distinct targets, in random order."""
    if n > len(TARGETS):
        raise ValueError(f"cannot draw {n} distinct targets from {len(TARGETS)}")
    return random.sample(TARGETS, n)
