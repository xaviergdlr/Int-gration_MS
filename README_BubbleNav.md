# BubbleNav-XPhase

Navigateur autonome de **bulles géoréférencées** : les panoramas d'un relevé sont
parcourus de proche en proche, en cliquant sur des **pastilles posées au sol** à la
position réelle des bulles voisines (principe Street View, appliqué à votre CSV).

L'outil est **indépendant** d'Orientation-XPhase : un seul fichier Python, rien à installer
à la main.

---

## 1. Démarrer

Double-cliquez sur `BubbleNav_XPhase.py` (Windows, Python installé depuis
python.org) ou lancez :

```
python BubbleNav_XPhase.py
```

L'outil s'organise en **deux fenêtres** :

| Fenêtre | Contenu |
|---|---|
| **Module principal** (petite, toujours présente) | choix du **relevé CSV**, du **dossier des images** et du **fichier de corrections** ; état du chargement (bulles, planchers, images trouvées, corrections) ; boutons Réglages, Appliquer / enregistrer, Aide, Quitter |
| **Visualiseur** (grande, touche **V**) | la vue bulle A et, quand la comparaison est ouverte, la vue B **empilée dessous** (séparation ajustable) ; à droite le **panneau latéral** : plan du plancher, filtres, fiche de la bulle, voisins ou outils d'édition |

Le visualiseur s'ouvre de lui-même dès qu'un relevé est chargé, **en plein
écran** par défaut. Sa barre d'outils porte à droite **⛶** (plein écran oui /
non, comme F11) et **✕** (fermer le visualiseur) : en plein écran, la barre de
titre et sa croix disparaissent. Échap sort du plein écran et revient à la
dernière taille connue. « Réglages… → Visualiseur à l'ouverture » propose aussi *maximisé* ou
*mémorisé* (dernière taille et position). Le fermer ne fait que le masquer : le
bouton « Ouvrir le visualiseur » ou la touche V le ramène. Les raccourcis clavier
fonctionnent depuis l'une ou l'autre fenêtre. Le dossier des images est exploré
récursivement ; chemins mémorisés pour les lancements suivants.

Options en ligne de commande :

```
python BubbleNav_XPhase.py --csv releve.csv --images D:\Bulles\GRA6
python BubbleNav_XPhase.py --selftest      # vérifications internes, sans interface
```

## 2. Le CSV attendu

Le format est **souple** : il faut seulement un **identifiant** et **X / Y**.
Séparateur `;` `,` tabulation ou `|`, virgule ou point décimal, colonnes dans
n'importe quel ordre, accents et casse indifférents, nombreux synonymes
d'intitulés (`N° scan`, `Numéro de scan`, `Scan`, `Est`, `Nord`, `Altitude`…).

* **Pas de colonne « Fichier photo » ?** Le numéro de scan sert d'identifiant et
  de nom de photo (`1001` → `1001.jpg`) : c'est le cas d'un CSV num scan prêt,
  `Num scan;Nom du Locator;X;Y;Z;% NORD;Plancher`.
* **Local, index et étage** se déduisent alors du locator (`R110b_01` → local
  R110b, index 01) et du plancher (`PLANCHER 01 (-03.50m)` → étage 01) : filtres
  et fiche restent complets sans nom projeté.
* **Plancher non renseigné ?** L'étage est déduit du **chiffre des centaines du
  numéro de local** (`R712` → 07, `K058` → 00), et marqué « déduit du local ».
  Le plancher renseigné reste prioritaire : un local peut déborder sur le niveau
  supérieur (R732 au plancher 08) ou traverser les niveaux (gaines, escaliers),
  la fiche l'indique alors (« local du niveau 07, sur le plancher 08 »).
* **Contrôle de cohérence** : un étage déduit est confronté à l'altitude des
  bulles de ce plancher. S'il la contredit, ou si ce plancher n'existe pas dans
  le relevé, la bulle est signalée (⚠ dans la fiche, décompte dans la barre
  d'état, liste des locaux dans les avertissements). Rien n'est corrigé
  d'office : c'est la donnée source qu'il faut vérifier.
* **Pas de ligne d'en-tête ?** L'outil le détecte et propose une correspondance
  devinée (1re colonne = identifiant, puis X, Y, Z) à confirmer.
* **Intitulés inconnus ?** Une boîte « Colonnes du CSV » montre les premières
  lignes et demande quelle colonne est l'identifiant, X, Y, Z, le plancher…
  Le choix est **mémorisé pour ce format** : on ne le refait plus.

Colonnes reconnues :

| Colonne | Rôle | Obligatoire |
|---|---|---|
| `Fichier photo` ou `N° scan` | identifiant ; nom de l'image, avec ou sans extension | l'un des deux |
| `X` | coordonnée **Est** (m) | oui |
| `Y` | coordonnée **Nord** (m) | oui |
| `Z` | altitude du point de vue ; **recalculée** (voir ci-dessous), lue seulement pour une bulle sans altitude de plancher | facultatif |
| `% NORD` | position du nord dans l'image, en % de la largeur (50 % = centre) | recommandé |
| `Nom du Locator` | nom affiché de la bulle | facultatif |
| `Plancher` | niveau, sert au plan et aux liens verticaux | facultatif |
| `Z plancher` | altitude du plancher (m) ; à défaut, lue dans le libellé « PLANCHER 02 (+00.00m) » | recommandé |
| `Delta` | décalage **±** du sol local par rapport au plancher (marche, faux plancher, mezzanine) | facultatif (0) |
| `Hauteur instrument` | hauteur de l'appareil au-dessus du sol local | facultatif (Réglages, 1,65 m) |

Intitulés reconnus, entre autres : `Z plancher`, `Altitude plancher`, `Z dalle` ;
`Delta`, `Delta plancher` ; `Hauteur instrument`, `H instrument`, `HI`,
`H appareil` ; `Plancher`, `PlancherMS` (et tout intitulé commençant par
« plancher » ou « niveau »).

**Format terrain** (`Num scan;Locator;X;Y;Z;Delta;Hauteur/cm;Zcorrige;% NORD;PlancherMS`) :
* dès qu'une colonne **`Zcorrige`** (ou `Z final`, `Z appareil`) existe, c'est
  elle l'altitude du point de vue, et le **`Z` nu est l'altitude du plancher** ;
* l'unité se lit dans l'intitulé : **`Hauteur/cm`** est en centimètres (165 →
  1,65 m). Une hauteur de plus de 20 sans unité est aussi comprise en cm ;
* sur BUG_BR_TR2 terrain, Z + Delta + Hauteur/100 = Zcorrige pour les 852
  bulles. Les 8 planchers sont lus, et même les 27 bulles sans libellé de
  plancher ont leur altitude (colonne Z) ;
* le CSV corrigé réécrit dans les mêmes unités (hauteur en cm). Un Delta vide
  reste vide tant qu'il vaut 0. Sans correction, le fichier ressort identique
  à l'octet près.

**Altitude du point de vue** : toujours calculée,

```
Z = Z plancher + Delta + Hauteur instrument
```

Les corrections de l'édition portent sur ces composantes :
* **hauteur station** : seule la caméra bouge ;
* **delta plancher** : caméra et sol bougent.

Le Z se recalcule aussitôt. `X` et `Y` se corrigent à part, pour la position
et la cohérence d'orientation entre points de vue.

**Le CSV corrigé** (« Appliquer / enregistrer… », coché par défaut) reprend le
format du CSV chargé avec les **bonnes valeurs** sur chaque ligne :
* X et Y ;
* Z = plancher + Δ + H (un Z faux est remplacé, même sans correction) ;
* Delta et Hauteur instrument ;
* Δ nord.

Si une correction de delta, de hauteur ou d'orientation le demande, la colonne
correspondante est ajoutée en fin de ligne. Le CSV chargé n'est jamais modifié.

Les lignes inexploitables sont ignorées et listées dans « Réglages… → Voir les
avertissements CSV » — jamais bloquantes.

### Travailler avant renommage (mode « num scan »)

Quand les photos ne portent encore que leur **numéro de scan** (`0347.jpg`), le
CSV peut fournir trois colonnes facultatives :

| Colonne | Rôle |
|---|---|
| `Num scan` (ou `Cle`, `Scan`, `Numero`) | **clé immuable** : identifie la bulle quel que soit le nom du fichier, aujourd'hui et après renommage |
| `Nom projeté` (ou `Projection`, `Nom final`, `Nouveau nom`) | nom selon la convention, analysé pour obtenir local, étage, index, date |
| `Local`, `Étage`, `Date`, `Index` | attributs explicites, prioritaires sur l'analyse du nom |

Conséquences :

* les **filtres** (local, plancher…) et la **fiche** restent complets même avec
  des photos numérotées ;
* la photo est retrouvée sur disque sous le nom de la colonne « Fichier photo »,
  sinon sous son numéro de scan (`347`, `0347`, `00347`), sinon sous son nom
  projeté — le nombre de rattachements est indiqué au chargement ;
* le **fichier de corrections est écrit par clé** (colonne `Cle` en tête) : après
  renommage, on rouvre le relevé renommé avec le même fichier de corrections et
  tout est retrouvé, positions, orientations et dates d'application comprises ;
* une clé dupliquée est signalée et la ligne ignorée, jamais bloquante.

Sans colonne de clé, la clé est le nom de la photo : rien ne change pour un
relevé déjà renommé.

## 3. Naviguer

| Action | Effet |
|---|---|
| Clic sur une pastille | aller sur cette bulle |
| **Clic droit** sur une pastille | l'ouvrir dans l'**autre vue** (A → B, en ouvrant la comparaison au besoin ; B → A) |
| Glisser | tourner la vue |
| Molette, `+` / `−` | champ de vision, de 30° à **200°** (105° par défaut) ; au-delà de 110°, passage progressif en **grand angle** |
| Double-clic | recentrer la vue sur ce point |
| `Entrée` / `Espace` | avancer vers la pastille la plus centrale |
| `Retour arrière` | revenir à la bulle précédente |
| **`Ctrl+Z`** | **annuler la dernière opération**, quelle qu'elle soit : entrée dans une bulle (retour à la bulle quittée, avec le même cap, site et champ), correction, bulle ouverte dans la vue B |
| `T` | bascule **toutes les bulles du plancher** (défaut) ↔ **réseau élagué** |
| **`O`** | **regarder d'où l'on vient** : la vue se tourne vers la pastille de la bulle quittée |
| Flèches (`Maj` = pas large) | tourner |
| `Origine` | redresser la vue |
| `F11` / `Échap` | plein écran |

**Plan du plancher** (à droite) : le **survol** d'un point affiche le nom de la
station (et son n° de scan en mode num scan) ; clic gauche = aller à la bulle la plus proche,
molette = zoom, clic droit glissé = déplacer, **clic droit sans glisser = ouvrir la
bulle dans la vue B**. Le **camembert jaune** donne la
position, la direction de visée et l'ouverture du champ. Il est **entièrement
dynamique** : une veille compare 60 fois par seconde l'état dont il dépend (point
de vue, cap, champ, plancher affiché, cadrage du plan, calibration, position
corrigée) et le redessine dès qu'il change. Il ne dépend donc pas du rendu de
l'image : même pendant le décodage d'un panorama 16000×8000, il reste juste.
Coût mesuré : 9 µs par battement, soit 0,06 % d'un cœur. Les cercles jaunes
marquent les bulles retenues comme pastilles, filtres compris ; des tirets
jaunes les relient à la bulle courante.

**Réseau du plan** (liste « réseau », sous le plan) :
* **squelette** (par défaut) : le réseau de navigation compte jusqu'à 8 liens
  par bulle, jusqu'à 12 m. Tracé en entier, il couvre le plan d'une toile de
  diagonales qui traversent les murs : 2 832 traits sur GRA6, 3 624 sur
  BUG_BR. Le squelette omet un lien A–B dès qu'une bulle C, reliée à A et à B,
  est plus proche de chacune qu'elles ne le sont entre elles : le trajet A–C–B
  le remplace. Il reste 988 et 1 206 traits (trois fois moins), qui suivent
  les couloirs. Le squelette est connexe partout où le réseau l'est et se
  calcule en 5 ms ;
* **complet** : tous les liens de navigation, comme avant ;
* **aucun** : seulement les bulles et les liens de la bulle courante.

La navigation (pastilles de la vue) n'est pas touchée : seul le dessin du plan
change.
La liste « Plancher » change de niveau en rejoignant la bulle la plus proche à l'aplomb.

**Couleur des pastilles** (menu « Affichage ▾ ») :
* **par local** (défaut) : chaque local a sa couleur, tirée de son nom. Elle
  est donc toujours la même d'une session à l'autre, et identique sur le plan.
  Les ▲ ▼ signalent toujours les planchers voisins ;
* **par type de lien** : jaune = même plancher, bleu ▲ = niveau au-dessus,
  violet ▼ = niveau en dessous.

Dans les deux cas, le rouge sombre signale une image absente du dossier, et
l'orange une bulle corrigée.

**Infobulles** : chaque bouton, liste, curseur ou case affiche son aide après un
court survol.

**Toutes les bulles du plancher, par défaut.** Chaque bulle du même plancher a
sa pastille, quelle que soit sa distance. Seule limite : le champ de la vue ;
élargissez-le, jusqu'à 200°, pour voir presque tout le tour. S'y ajoutent les
pastilles ▲ ▼ vers les planchers voisins (la plus proche au-dessus et en
dessous, à moins de 5 m en plan).

Pour rester lisible :
* seules les pastilles **proches** portent leurs étiquettes (nom, distance,
  H / Δ / Z). Les lointaines, réduites à leur taille minimale, n'affichent que
  la sphère, et le survol en donne tout le détail ;
* une étiquette qui chevaucherait celle d'une pastille plus proche est omise ;
* le filtre de distance (panneau « Filtres ») limite au besoin l'affichage.

**Réseau élagué** (touche **T**, ou menu « Affichage ▾ ») : c'est l'ancien
comportement, fait pour naviguer de proche en proche. Il garde au plus
**8 pastilles**, à moins de **12 m**, et **une seule par direction** (écart de
25°) : dans un couloir, seule la plus proche des bulles alignées reste. Ces
trois valeurs se règlent dans *Réglages → Réseau*.

**Grand angle** : jusqu'à 110°, la vue est une perspective normale (lignes
droites). Au-delà, elle passe progressivement en projection
**stéréographique** (complète à 160°). Les bords ne s'étirent plus à l'infini :
on peut voir jusqu'à 200°, avec des lignes courbées mais des formes
préservées. Pastilles, clics et glisser suivent exactement la même projection.

**Hauteur des pastilles** (menu « Affichage ▾ ») :
* **au sol** (par défaut) : la pastille se pose sur le sol de la bulle cible,
  c'est-à-dire **plancher + Δ = Z − H** ;
* **au point de vue** : la sphère est à la hauteur de l'appareil (**Z**), et un
  **mât** la relie à son pied au sol. On lit ainsi H d'un coup d'œil.

Le point de vue se calcule toujours de la même façon, **Z = plancher + Δ + H**
(voir § 2). La fiche détaille le calcul :

```
sol      plancher -8.50 + Δ +0.00 = -8.50
caméra   sol + H 1.65 = -6.85 (point de vue)
```

**Étiquettes des pastilles** (menu « Affichage ▾ », chaque ligne se coche à
part, le choix est mémorisé et vaut aussi pour la vue B) :
* **au-dessus** : le **nom de la station**, en petit ;
* **dessous** : la **distance**, puis trois lignes discrètes :
  * `H` : **hauteur de l'appareil** (hauteur du relevé + correction) ;
  * `Δ` : **delta plancher** (colonne du relevé, ou déduit du plancher, + correction) ;
  * `Z` : **altitude finale du point de vue** (Z caméra = plancher + H + Δ).

Ces valeurs sont relues à chaque image : toute correction, au clavier, par
saisie ou en glissant un axe, s'affiche aussitôt. Une composante corrigée passe
en **orange**. En mode édition, les valeurs sont au millimètre, sinon au
centimètre. Les trois lignes d'altitude sont réservées aux pastilles proches,
c'est-à-dire celles qui ne sont pas réduites à leur taille minimale. La pastille
survolée et la cible d'édition les affichent toujours, pour ne pas encombrer
le lointain.

**Aspect des pastilles** : chaque pastille est une **sphère ombrée** posée sur son
ombre portée — éclairage en haut à gauche, reflet, ombre douce au pied — générée
une fois par couleur et par taille puis mise en cache (3 ms, aussi rapide qu'un
disque plat à l'affichage). Le point du sol projeté est le pied de la sphère ;
la sphère comme l'ombre sont cliquables. Au **survol**, la sphère grossit,
s'éclaircit et s'entoure d'un **halo lumineux**, et son infobulle apparaît —
dans la vue principale comme dans la vue de comparaison. « Réglages… →
Pastilles en relief » revient aux disques plats.

**Taille des pastilles** : rayon à l'écran = focale × rayon physique ÷ distance —
une pastille trois fois plus loin est trois fois plus petite, et zoomer les
grossit exactement comme un disque posé au sol. La taille est **bornée** :
jamais moins de **10 px** (elle reste visible et cliquable), jamais plus de
**36 px** (elle n'envahit pas la vue, soit 4,5 % de la largeur d'une vue de
1600 px). Rayon physique (0,32 m) et bornes mini/maxi se règlent dans
« Réglages… », qui affiche la plage de distances où la taille reste pleinement
proportionnelle — environ 5,5 m à 20 m avec les valeurs par défaut ; en deçà la
taille plafonne, au-delà elle atteint son plancher.

### Fiche de la bulle
Le panneau de droite décrit la bulle **visée** — celle qu'on survole ou qu'on
sélectionne dans la liste des voisins — et retombe sur la bulle courante sinon.
Le **nom de fichier est analysé** selon la convention
`campagne_site_tranche_ouvrage_étage_local_date_index` :

```
K256_36
photo    CP1_GRA_TR6_BK_02_K256_20260326_36
repère   CP1 · GRA · TR6 · BK
étage 02 · local K256 · index 36
prise de vue 2026-03-26
plancher PLANCHER 02 (+00.00m)
X/Y/Z    586.48 / 72.63 / 1.65
nord     50 %   ·   image présente
distance 2.79 m (3D) · 2.79 m (plan)
         Δz +0.00 m depuis K256_01
```

L'analyse s'ancre sur la date (8 chiffres) : elle reste juste si le nombre de
segments de tête change. Un nom incomplet est signalé (`nom incomplet : date
absente`) sans jamais bloquer, et le nombre de noms incomplets apparaît dans la
barre d'état au chargement.

Le survol d'une pastille ajoute une infobulle avec les mêmes attributs plus
l'azimut et le cap dans l'image.

### Filtres des pastilles  (touche F)
Un panneau repliable, à droite, restreint ce qui est affiché **en direct** — les
pastilles sont redessinées immédiatement, sans recharger l'image ni toucher au
réseau :

| Filtre | Effet |
|---|---|
| Plancher | tous · plancher courant · un plancher précis |
| Distance | pastilles au-delà de N mètres masquées (0 = illimité) |
| Local | `K256`, `K25`, `W25*` … motifs séparés par des virgules, préfixe suffisant |
| liens ▲▼ | garder ou non les pastilles vers les autres niveaux |
| images absentes | masquer les bulles dont le JPEG manque |

La case « Filtres des pastilles » les active ou les désactive d'un coup, sans
perdre les réglages ; un bandeau en haut à droite de la vue rappelle le filtre en
cours et le nombre de pastilles masquées, et la liste des voisins suit le même
filtre. Les réglages sont mémorisés d'une session à l'autre.

Le **cap terrain est conservé** d'une bulle à l'autre : on continue à regarder dans la
même direction réelle après chaque saut.

## 4. Si les pastilles tombent à côté

Ouvrez **Réglages…**, section *Calibration de l'azimut* — l'effet est immédiat, sans
recharger les images :

* **Interprétation du `% NORD`** : colonne du nord dans l'image (défaut, 50 % = image
  redressée nord au centre) ou azimut visé par le centre de l'image ;
* **Sens des azimuts** : horaire (standard) ou anti-horaire si l'image est en miroir ;
* **Correction nord** : décalage global en degrés ;
* **Hauteur instrument** : hauteur de l'appareil au-dessus du sol (1,65 m par
  défaut), pour les bulles sans colonne de hauteur. Elle entre dans le calcul
  Z = plancher + Δ + H, avec un effet immédiat dans les deux vues.

Les dialogues (Réglages, bilan…) s'ouvrent **devant le visualiseur**, même en
plein écran.

Section *Réseau* : portée des liens, nombre maximal de pastilles, séparation angulaire
minimale (une seule pastille par direction, pour ne pas empiler les bulles alignées) et
portée des liaisons entre planchers.

### Regarder d'où l'on vient (vérifier la cohérence)

Une fois arrivé sur une bulle, la pastille de la bulle **quittée** doit tomber
exactement sur l'endroit où se trouvait l'appareil dans l'image. Un écart
révèle une erreur de position (X/Y), d'orientation (Δ nord) ou d'altitude
(H, Δ).
* touche **O**, ou menu « Affichage ▾ » → *Regarder d'où l'on vient* : la vue
  se tourne et centre la pastille d'origine ;
* menu « Affichage ▾ » → *À l'arrivée, regarder d'où l'on vient* : à chaque
  clic sur une pastille, la vue arrive déjà tournée vers la bulle quittée (dans
  A, et dans B si la vue liée est coupée) ;
* **clic droit** sur une pastille : la bulle s'ouvre dans B, **tournée vers
  A**, et la vue liée est suspendue. A voit B, B voit A : les deux pastilles se
  font face ;
* repères : la bulle quittée est entourée en pointillé et marquée
  « ↩ origine » ; dans A, la pastille de la bulle ouverte en B porte « B » ;
  dans B, celle de A porte « A ».

## 5. Comparer deux points de vue  (touche C)

Sur le plan, la vue B a toujours son repère cyan et son cône. Si B est sur un
autre plancher que celui affiché, son repère devient un cercle pointillé
marqué « B · PLANCHER nn ».


Le bouton « Comparer » ouvre une **seconde vue bulle**, empilée sous la
première dans le visualiseur (séparation glissante, bouton ✕ pour la refermer).
Elle partage tout le modèle — relevé, réseau,
filtres, calibration, corrections, cache d'images — et n'a que son point de vue
en propre.

**Clic droit = ouvrir dans l'autre vue** :
* sur une pastille de **A**, la bulle s'ouvre dans **B**. Si la comparaison est
  fermée, elle s'ouvre directement sur cette bulle ; A ne bouge pas ;
* sur une pastille de **B**, la bulle s'ouvre dans **A** ;
* sur un point du **plan** (clic droit sans glisser), la bulle s'ouvre dans **B**.

L'infobulle des pastilles le rappelle (« clic droit : ouvrir dans la vue B / A »).
Sous macOS, le bouton 2 est aussi accepté.

* **Vue liée** (par défaut) : les deux vues regardent en permanence la **même
  direction terrain**, chacune corrigée de son propre nord. Tourner ou zoomer
  d'un côté agit sur les deux ; c'est ce qui rend la comparaison lisible.
  Décochez pour orienter la vue B librement.
* **Suivi de A** : à chaque déplacement dans la vue principale, la vue B se
  place automatiquement sur la bulle correspondante —
  *même local, autre plancher* (utile pour un local présent sur plusieurs
  niveaux, comme une gaine ou une trémie) ou *bulle la plus proche*.
  « aucun » laisse la vue B où elle est.
* **A → B** recopie la bulle courante, **⇄ Échanger** intervertit les deux
  points de vue.
* Les **pastilles de B sont cliquables** : on s'y déplace indépendamment.
* La barre d'état de B donne le cap, le nombre de pastilles, la **distance 3D
  et le Δz par rapport à la bulle A**.
* Sur le plan, la vue B a son propre repère cyan avec son camembert, relié à A
  par un trait pointillé.
* Les deux vues partagent la hauteur à parts égales à l'ouverture ; la
  séparation se déplace à la souris.

L'édition reste réservée à la vue principale ; la vue B affiche les corrections
en direct (position et orientation) puisque le modèle est partagé.

## 6. Corriger le relevé depuis la vue (mode Édition)

Touche **E** ou bouton « Édition ». Trois fichiers, trois rôles :

| Fichier | Rôle | Modifié par l'outil |
|---|---|---|
| Relevé chargé (`…MySurvey….csv`) | source, référence | **jamais** |
| Images bulles (JPEG) | source | **jamais** (sauf lot final, vers un autre dossier) |
| **`…_corrections.csv`** | corrections en cours | en continu |

Toutes les corrections — position **et** orientation — vont dans le **fichier de
corrections**, un CSV distinct écrit à côté du relevé. Elles s'appliquent en
direct à l'affichage, sont relues automatiquement à la réouverture du relevé, et
ne touchent ni le relevé ni les images.

### Le fichier de corrections = un patch pour QGIS
```
Cle;Fichier photo;Nom du Locator;X;Y;Z;dX;dY;dH station;dDelta plancher;dZ;Delta Nord (deg);H appareil;Delta plancher;Orientation appliquee;Date
0347;0347;K256_01;589.150;73.450;1.500;+0.000;+0.000;-0.150;+0.000;-0.150;6.1077;1.500;+0.000;;2026-09-04 17:53
```
Chaque correction est rangée **selon sa nature physique**, pour être appliquée
telle quelle au projet QGIS qui produit le relevé (jointure par la colonne
`Cle`, puis calculatrice de champs : `X + dX`, `Y + dY`, hauteur `+ dH`,
delta `+ dDelta`) :

| Nature | Colonnes | Effet dans la vue | Cible du patch |
|---|---|---|---|
| Orientation | `Delta Nord (deg)` | l'image tourne | l'image (ou l'attribut nord si vous le portez) |
| Position en plan | `dX`, `dY` | la pastille se déplace, en 2D | géométrie du point |
| **Hauteur de station** | `dH station` | la caméra monte ou descend, **le sol reste** | attribut hauteur appareil |
| **Delta plancher** | `dDelta plancher` | caméra **et** sol se déplacent : marche, faux plancher, local sur plusieurs niveaux | attribut delta / altitude locale |

Le modèle d'altitude est `Z caméra = altitude du plancher + hauteur appareil + delta`.
`dZ = dH + dDelta` est donné pour contrôle ; `X` `Y` `Z` `H appareil` `Delta
plancher` sont les valeurs absolues corrigées, pour une chaîne qui préfère
remplacer plutôt qu'ajouter. Si le relevé porte déjà des colonnes `Hauteur
appareil` / `Delta plancher`, elles sont lues au chargement et mises à jour dans
le relevé complet corrigé.

**Convention de signe de `Delta Nord`** : angle en degrés à appliquer à l'image
par rotation cyclique, **positif = le contenu de l'image glisse vers la droite**
(soit un décalage de `+angle/360 × largeur` pixels). Un ancien fichier de
corrections ne portant qu'un `Z` absolu est relu comme une correction de hauteur
de station.

* une ligne par bulle corrigée, rien d'autre, identifiée par sa **clé immuable**
  (numéro de scan, ou nom de photo à défaut) puis par son nom de photo ;
* `Delta Nord (deg)` est l'angle **restant à appliquer** à l'image ;
* `Orientation appliquee` porte la date une fois les images tournées ;
* écriture atomique et continue : rien n'est perdu si l'outil se ferme ;
* bouton « Fichier… » pour créer ou reprendre un autre fichier de corrections
  (le choix est mémorisé pour ce relevé).

### Cible d'édition
Le panneau affiche en permanence sur quoi vous travaillez : **bulle active**
(celle d'où vous regardez) ou **pastille** (cliquez-en une pour la prendre pour
cible). Le bouton « Bulle active » revient à la première.

### Orientation
* **Maj + glisser** dans la vue, ou le curseur Δ nord, ou les pas ±0,05° / ±0,5° ;
* l'**image tourne sous les pastilles** — les pastilles, géoréférencées, sont la
  référence : on aligne le décor sur elles ;
* les **croix bleues** sont les bulles voisines non retenues comme pastilles :
  elles élargissent le jeu de repères pour juger la cohérence avec le réseau ;
* « appliquer à ce plancher / tout le relevé » propage la même valeur si le
  décalage est systématique ;
* la colonne `% NORD` du relevé n'est jamais touchée : elle reste à 50.

### Position X / Y / Z : toujours le long d'un axe
En mode édition, la cible porte un **repère XYZ** : X Est en rouge, Y Nord en
vert, Z en bleu. Il est centré sur sa **position d'origine du CSV**. Les axes
sont **gradués** (5 cm à 1 m selon la distance, point plus gros au mètre).
Pour une pastille, l'axe Z monte jusqu'à la caméra de la station.

Dès que la cible a bougé :
* une **pastille fantôme**, semi-transparente, reste à sa position d'origine
  (« origine CSV ») ;
* le déplacement est **décomposé** : un segment rouge ΔX, puis un vert ΔY
  (puis un bleu ΔZ), chacun avec sa valeur au millimètre. Un fin pointillé
  orange trace la résultante ;
* **sur le plan**, un cercle pointillé marque l'origine, et deux segments
  donnent les composantes ΔX et ΔY avec leurs valeurs.

En bas de la vue, une
ligne donne le déplacement depuis le CSV, axe par axe : ΔX, ΔY, ΔH (hauteur
station), ΔΔ (delta plancher), avec la valeur du geste en cours.

Un déplacement n'est **jamais libre** : il suit un seul axe.
* **glisser une pastille** : en mode **Auto**, l'axe X ou Y est choisi par le
  début du geste (celui dont la direction à l'écran est la plus proche), puis
  reste fixe jusqu'au relâchement ;
* **saisir un axe du repère** (le trait coloré) : le geste suit cet axe ;
* **verrouiller un axe** : boutons *Auto X/Y · X Est · Y Nord · Z* du panneau,
  ou touches **X**, **Y**, **Z** (un second appui revient en Auto). Z n'est
  jamais choisi automatiquement, pour ne pas le confondre avec l'éloignement ;
* **« Z agit sur »** : *Δ plancher* (le sol et la caméra montent, la pastille
  suit) ou *H station* (seule la caméra bouge ; un trait orange marque la
  nouvelle hauteur, un blanc l'ancienne) ;
* **glisser un point sur le plan** : le long de X ou de Y aussi (axe verrouillé,
  ou le sens dominant du geste) ;
* **saisie numérique** X/Y, avec pas réglable (1 cm à 50 cm).

Le point suivi est le point de l'axe le plus proche du rayon sous le curseur
(calcul exact, contrôlé au 1e-12 m). La pastille reste donc sous le curseur le
long de son axe, arrondie au millimètre. Un rail pointillé prolonge l'axe
pendant le geste. Si l'axe est vu exactement dans l'axe du regard, le geste
est ignoré et un message invite à changer d'axe ou de point de vue.

L'**altitude** se corrige par ses deux composantes, dans le bloc « Altitude » :
**hauteur station** (Page haut/bas — la caméra bouge, le sol de la pastille
reste) ou **delta plancher** (Maj + Page haut/bas — caméra et sol bougent).
Depuis les bulles voisines, les deux se ressemblent : c'est la scène qui tranche
(une marche ou un faux plancher visible = delta ; sinon = hauteur). Le panneau
affiche en permanence Z caméra, altitude du sol, hauteur et delta résultants.

**Ctrl + glisser** dans la vue déplace la **bulle active** elle-même, sur un
axe elle aussi. Le réseau de pastilles suit le curseur et la station part en
sens inverse, ce qui permet de recaler une bulle mal positionnée sur le décor
qu'elle voit. Son repère (visible en regardant vers le sol) marque sa position
d'origine. En Z, vu d'aplomb, glisser vers le haut ou le bas règle l'altitude
(2 mm par pixel).

> Corrigez l'orientation **avant** les positions : sur une image mal orientée,
> déplacer une pastille pour la faire coïncider avec ce qu'on voit reporterait
> l'erreur d'angle dans les coordonnées.

### Appliquer en fin de vérification  (« Appliquer / enregistrer… », Ctrl+S)
Une boîte donne le bilan (positions corrigées, orientations corrigées, images à
tourner, fichiers concernés) et propose deux traitements par lot :

1. **Appliquer l'orientation aux images** — écrit les JPEG tournés dans un
   **autre dossier**, les originaux restant intacts. Rotation cyclique
   **arrondie au pixel entier** (0,0225° de pas sur 16000 px) : aucune
   interpolation, aucun flou ; tables de quantification et EXIF de la source
   réutilisés, donc ré-encodage quasi transparent (écart mesuré 0,14/255, taille
   de fichier inchangée). Comptez ~7 s et ~0,8 Go de mémoire par image
   16000×8000 et par tâche (nombre de tâches réglable). Les angles appliqués
   repassent alors à 0 dans le fichier de corrections, avec la date
   d'application — impossible de les appliquer deux fois.
2. **Écrire aussi un relevé complet corrigé** (facultatif) — relevé d'origine +
   corrections fusionnés en un CSV unique, pour une chaîne qui n'accepte qu'un
   seul fichier. Le relevé chargé et le fichier de corrections ne bougent pas.

Tant que le lot n'est pas lancé, tout reste réversible.

### Filet de sécurité
* **Ctrl+Z** annule la dernière opération, correction ou navigation, dans
  l'ordre inverse. Un glisser complet compte pour une seule étape, et un simple
  clic de sélection n'en crée aucune ;
* « Réinit. cible » et « Réinit. tout » ramènent aux valeurs du relevé ;
* une ligne du fichier de corrections dont la photo n'existe pas dans le relevé
  est signalée et ignorée, jamais bloquante.

### Survol d'une pastille
Une infobulle donne le nom, le fichier photo, la **distance 3D**, la distance
horizontale, le Δ altitude, l'azimut, les coordonnées, le plancher, la présence
de l'image et, le cas échéant, la correction déjà appliquée.

## 7. Performance

* Décodage JPEG réduit à la volée (`draft`) à 2048 / 4096 / 8192 px de large — réglable
  dans « Qualité » ;
* cache mémoire des bulles décodées + **préchargement des voisins** dès l'arrivée :
  le saut suivant est instantané ;
* rendu perspective par `cv2.remap` avec grilles de rayons et de remap mises en cache,
  exécuté hors du thread d'interface ; résolution réduite pendant la rotation, pleine
  résolution au repos.

Mesures sur un panorama **16000×8000** réel :

| Qualité | Décodage d'une bulle | Mémoire par bulle | Rendu 1600×900 |
|---|---|---|---|
| 2048 | 0,17 s | 6 Mo | 46 ms |
| **4096** (défaut) | 0,51 s | 25 Mo | 24 ms |
| 8192 | 6,2 s | 101 Mo | 11 ms |

Le nombre de bulles gardées en mémoire est **plafonné automatiquement** par une
enveloppe d'environ 1,1 Go : inutile de surveiller le réglage en montant la
qualité. Pendant une rotation de vue, le rendu passe en demi-résolution
(~4 ms), la pleine résolution revenant dès l'arrêt du geste.

## 8. Vérifier l'installation

```
python BubbleNav_XPhase.py --selftest
```

Contrôle les angles, la réciprocité azimut ↔ image, la **cohérence entre la position
calculée des pastilles et le rendu réel** (écart mesuré < 2 px), la lecture du CSV, la
construction du réseau, les temps de rendu, l'aller-retour **écran ↔ sol** utilisé pour
déplacer une pastille, l'analyse des noms de fichiers (convention, variantes, noms incomplets, cohérence
avec les colonnes du relevé sur les 693 bulles), les filtres de pastilles (également
appliqués à la vue de comparaison), le rendu des sphères ombrées (image, ombre, reflet, zone de clic), la loi de taille en
1/distance et ses bornes (taille toujours comprise entre 10 et 36 px, de
0,4 m à 200 m), les deux composantes d'altitude (hauteur station laissant le sol en place, delta
plancher le déplaçant, relevé complet mis à jour par composante, ancien format relu),
le mode num scan (clé immuable, nom projeté, attributs explicites, rattachement des
photos par numéro, corrections retrouvées après renommage), la lecture souple (N° scan sans colonne
photo, intitulés variés, fichier sans en-tête, intitulés inconnus, relevé corrigé
réécrit au même format), l'étage déduit du numéro de local et son contrôle
de cohérence avec Z, le glisser sur un axe (rayon écran ↔ point 3D,
abscisse retrouvée sous le curseur, axe vu de face), le squelette du plan (grille
sans diagonale, connexité identique au réseau), la précision du micromètre, l'aller-retour du fichier
de corrections (écriture, relecture, ligne orpheline,
date d'application) avec relevé source inchangé octet pour octet, l'écriture du relevé
complet corrigé (colonne Δ nord créée puis réutilisée, seules les lignes modifiées
changent), et l'équivalence **image tournée de Δ ≡ vue décalée de Δ** — autrement dit, ce que vous
voyez en réglant l'orientation est exactement ce que l'application par lot écrira dans
le JPEG.

## 9. Dépendances

`Pillow`, `opencv-python`, `numpy` — installées automatiquement au premier lancement si
elles manquent. Python 3.9 ou plus, avec Tkinter (inclus dans l'installateur Windows
officiel).
