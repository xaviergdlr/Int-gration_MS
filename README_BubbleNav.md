# BubbleNav-XPhase

Navigateur et correcteur de **bulles géoréférencées**. On parcourt les panoramas
d'un relevé de proche en proche, en cliquant sur les **pastilles** des stations
voisines (principe Street View, appliqué à votre CSV). On y vérifie, puis on
corrige, la position de chaque station.

Un seul fichier Python, indépendant d'Orientation-XPhase ; les dépendances
s'installent d'elles-mêmes au premier lancement.

---

## 1. Principe

**En entrée**, un CSV qui donne pour chaque station :

| Donnée | Colonne (exemples d'intitulés reconnus) |
|---|---|
| identifiant | `Num scan` (sert aussi de nom d'image : `1001` → `1001.jpg`), ou `Fichier photo` |
| nom | `Locator` (`R110b_01` → local R110b, index 01) |
| position en plan | `X`, `Y` |
| **Z plancher** | `Z` (un Z nu est toujours le plancher), ou `Z plancher` |
| **Delta** (Δ) | `Delta` — vide = 0 ; en m, ou en cm / mm si l'intitulé le dit |
| **Hauteur instrument** (H) | `Hauteur/cm`, `Hauteur instrument`… |
| Z final (contrôle) | `Zcorrige`, `Z final` |
| orientation | `% NORD` |
| plancher | `PlancherMS`, `Plancher` |

**Un seul modèle altimétrique :**

```
Z final (point de vue) = Z plancher + Δ + H
```

Le Z plancher est **fixe**. Au chargement, le Z final lu (`Zcorrige`) est
comparé au Z recalculé. Tout écart de plus de 5 mm est signalé (Réglages ›
avertissements). Sur le relevé terrain fourni (852 stations), il n'y a aucun
écart.

**A et B sont deux points de vue fixes** : on ne corrige jamais la station
où l'on se trouve. On corrige **une voisine**, que l'on voit sous deux angles
(depuis A et depuis B) pour croiser l'information.

La **station active** est celle que l'on **survole** : sa sphère, son mât ou
son cercle au plancher, dans A, dans B ou sur le plan. Elle est cerclée, et
sa fiche et sa mire s'affichent dans les deux vues. Elle reste active
jusqu'au survol d'une autre.

| Correction de la station survolée | Geste | Où elle va |
|---|---|---|
| **Position XY** | Espace + glisser sa pastille (dans A ou B), ou son point sur le plan | CSV de sortie |
| **Δ** | Alt + molette | CSV de sortie |
| **H** | Maj + molette | CSV de sortie |

Seule exception : l'**orientation** concerne l'image elle-même. Ctrl + molette
tourne l'image **de la vue où est le curseur** (1°, ou 0,1° dans Réglages).
Elle s'applique aux images, jamais au CSV.

Toutes les corrections sont **bornées** et **arrondies** :

| Donnée | Borne | Arrondi |
|---|---|---|
| XY | 5 m au plus de la position du CSV | déplacement au micromètre (la coordonnée d'origine reste exacte) ; au mm pour un glisser |
| H | entre 0,10 et 3 m | mm |
| Δ | ±3 m | mm |
| Orientation | ramenée dans ]−180°, 180°] | 0,01° |

Une borne atteinte est signalée dans la barre d'état. Z est toujours recalculé
par la même formule, sans bruit numérique : 25 crans aller-retour reviennent
exactement à la valeur du CSV.

## 2. Démarrer

```
python BubbleNav_XPhase.py
python BubbleNav_XPhase.py --csv releve.csv --images D:\Bulles
python BubbleNav_XPhase.py --selftest      # vérifications internes, sans interface
```

Il y a deux fenêtres :
* le **module principal** : CSV, dossier des images, fichier de corrections,
  Réglages, Appliquer / enregistrer, Aide ;
* le **visualiseur** (touche **V**) : vue A, vue B en dessous quand la
  comparaison est ouverte, plan du plancher et fiche à droite.

## 3. Lire la 3D dans la bulle

Chaque station voisine est dessinée à sa position exacte :

* **la sphère** : le point de vue (Z final), **à sa taille réelle**. Plus elle
  est petite, plus elle est loin. Les stations lointaines sont estompées.

  Jusqu'ici, une taille minimale de 10 px faisait paraître toutes les stations
  situées au-delà de 14 m à la même distance, trop près. À l'inverse, les plus
  proches étaient plafonnées et paraissaient trop loin. La distance se lit
  désormais juste ;
* **le mât, l'ombre et l'empreinte** (cercle de 50 cm) sont **ancrés au Z
  plancher**. Au-dessus, le **tronçon Δ** (rose) monte jusqu'au sol local, puis
  la **mire H**, graduée tous les 10 cm, monte jusqu'à la sphère. Δ et H ne
  font monter ou descendre que la sphère ;
* **le réseau au sol** : les liens entre stations, tracés sur le plancher. Ils
  fuient vers l'horizon comme un carrelage, et la place de chaque station dans
  le réseau XYZ importé se lit d'un coup d'œil. Pour le masquer : Affichage ›
  Réseau au sol ;
* **le trait pointillé vertical** marque le centre de l'image (le « nord
  image »). Il tourne avec l'image quand on corrige l'orientation, alors que le
  « N » rouge reste le nord terrain.

Sur des panoramas de synthèse exacts, où chaque voisine est dessinée comme un
trépied à sa vraie place, sphères et pieds tombent exactement sur les
trépieds.

## 4. Corriger une station

1. **Deux points de vue** : A est la bulle où l'on se trouve. Ouvrez B sur
   une autre voisine (Ctrl+clic ou clic droit sur sa pastille). A et B
   restent fixes pendant les corrections.
2. **Désigner la station** : survolez-la (sphère, mât ou cercle au plancher),
   dans A, dans B ou sur le plan. Elle devient la **station active** : cerclée,
   avec sa mire et sa fiche dans les deux vues. La touche **J** (ou
   « ◎ Station active ») tourne B vers elle, pour la voir sous le second angle.
3. **Δ et H** : Alt + molette et Maj + molette sur elle. Le pas est de 5 cm
   (1 cm dans Réglages).
   * Le **cercle au plancher ne bouge jamais**.
   * Le **tronçon Δ** (rose) est fixé en bas au plancher et varie par le haut.
   * La **mire H** part du haut de Δ et varie par le haut, jusqu'à la sphère.
   * Seule **la sphère** monte ou descend, dans A comme dans B. Les points de
     vue et les autres stations ne bougent pas.
   * Pendant une rafale de crans, la molette reste sur la même station même si
     sa sphère quitte le curseur : son mât et son cercle restent dessous.
   * La **fiche** en montre aussi une coupe : plancher, Δ, mire H, sphère, et
     la position du CSV en pointillé.
4. **Position XY** : maintenez **Espace** et glissez sa pastille, dans A ou
   dans B, ou son point sur le plan. Elle suit le curseur, et son point de
   départ reste en transparence. Rien d'autre ne bouge. X / Y verrouillent un
   axe si besoin.
5. **Orientation** : Ctrl + molette tourne l'image de la vue où est le
   curseur. Le trait du centre de l'image montre le décalage.
6. **Revenir en arrière** : Ctrl+Z (une rafale de molette compte pour une
   seule étape). **Suppr** rend à la station active ses valeurs du CSV.

**Vérifier d'abord les points de vue.** Un point de vue corrigé fausse tout
ce que l'on voit depuis lui, par exemple un reste d'essai relu dans le fichier
de corrections.

Exemple : avec A relevé de 1,07 m (Δ +1,04, H +0,03), le pied d'une voisine à
3 m est vu 42° sous l'horizon au lieu de 29°. Il paraît donc à 1,8 m au lieu
de 3 m, et la sphère n'est plus au-dessus de l'emplacement de l'appareil
visible dans la photo. Une orientation fausse de 3° décale en plus de 16 cm à
3 m.

Les valeurs corrigées d'un point de vue sont en orange dans son en-tête.
**↺ CSV**, dans l'en-tête de A ou de B, remet **ce point de vue** aux valeurs
du CSV (annulable).

## Vue du sol  (touche N, bouton « Sol »)

Une fenêtre séparée montre le sol **vu de dessus**, nord en haut :
* chaque bulle est **projetée à la verticale** sur son sol local ;
* les projections sont assemblées **en pavage** : chaque point du sol vient
  de la bulle la plus proche.

Le sol (dalles, joints, marquages) doit **se raccorder** d'une tuile à
l'autre :

| Ce qu'on voit au raccord | Erreur probable |
|---|---|
| un décalage | position XY |
| un pivotement | orientation |
| un changement d'échelle | hauteur instrument |

Chaque station est marquée d'un point, et un trait pointillé montre la
direction du centre de son image. Pour une station déplacée, la position du
CSV reste visible.

Les gestes sont les mêmes que dans les bulles, sur la tuile survolée :

| Geste | Effet |
|---|---|
| Survol | la station devient active |
| Alt / Maj + molette | Δ / H |
| Espace + glisser | position XY |
| Ctrl + molette | orientation de son image : la tuile tourne |
| Molette | zoom |
| Clic droit glissé | déplacer la vue |
| Clic | aller sur la station |

### Projection : gnomonique sur le plan du sol

La vue du sol est une **projection gnomonique** (centrale) de chaque bulle sur
le plan de son sol : le rayon qui part de l'objectif, à la hauteur H, et
traverse un pixel de l'image est prolongé jusqu'au sol (distance = H /
tan(site)). C'est exactement la vue « rectilinéaire » que l'on obtiendrait en
visant droit vers le bas, mise à l'échelle du terrain : **les lignes droites
du sol restent droites**, et sur des panoramas parfaits les tuiles se
raccordent à 1,6/255 près (joints, traits continus d'une tuile à l'autre).

Ce qui empêche une jointure de se faire sur des photos réelles :
* **ce qui n'est pas au sol** (murs, pieds de mobilier, marches) : projeté
  comme s'il était au sol, il s'étire en rayons ;
* une bulle **pas de niveau** : l'erreur grandit très vite en rasant — 1° de
  défaut décale le sol d'environ 5 cm à 1,5 m de la station, mais de 16 cm à
  3,5 m (H = 1,65 m) ;
* une **hauteur H fausse** (échelle de la tuile, voir plus bas), une position
  ou une orientation fausse.

D'où le réglage **« Portée des tuiles (m) »** : plus court, chaque point du sol
vient d'une bulle qui le voit de plus haut, et les jointures sont plus
fiables.

### Couture

* **Centre des stations grisé** (par défaut) : sur 0,6 m sous chaque bulle
  (trépied, opérateur), la photo ne voit pas le sol ; rien n'y est affiché
  plutôt qu'une texture trompeuse. La case **« Nadir des voisines »** le fait
  remplir par une bulle voisine qui le voit (ce n'est alors pas la photo de
  la station).
* **Coutures** : un trait fin suit exactement la limite entre deux tuiles (la
  médiatrice des deux stations) : une cassure du sol sur ce trait signale
  l'erreur.
* **Fondu** : 20 cm de part et d'autre de la couture, les deux bulles sont
  mélangées ; un décalage y apparaît **en double**.
* Une tuile ne s'étend jamais au-delà de ce que sa bulle voit du sol (station
  posée bas : portée réduite).

### Îlots

Un **îlot** est un groupe de stations reliées de proche en proche : deux
stations plus proches que la **liaison max** (4 m par défaut) sont dans le
même îlot. Avec **« séparer par local »** (par défaut), seules les stations
d'un même local (R133, R110b…) sont reliées : un îlot ne passe pas à travers
les murs, où le sol n'est pas commun. Le sol ne se transmet pas d'un îlot à
l'autre : **chaque îlot a sa propre référence ★** et se contrôle / s'ajuste
seul.

Dans la vue du sol (case **Îlots**), chaque îlot a un contour de couleur, son
numéro et son effectif, et « sans ★ » tant qu'il n'a pas de référence.

### Réactivité

Le sol de chaque bulle est gardé en mémoire (bande réduite à 2048 px), sans
chasser les bulles du cache de la visite. Après une correction (molette,
Espace + glisser), **seule la zone de la tuile touchée** est recalculée
(≈ 60 ms) : la tuile suit le geste. Déplacement et zoom montrent aussitôt
l'image existante, recadrée ; le calcul complet suit en arrière-plan
(≈ 0,2 s). À la première ouverture, le pavage apparaît au fur et à mesure du
chargement.

### Ce que corrige le pavage

La vue du sol **ne corrige rien d'elle-même** : elle montre. Ce qui corrige :
* les **gestes** sur une tuile (Espace + glisser, Alt / Maj + molette ;
  Ctrl + molette pour l'orientation, à la main seulement) ;
* l'**ajustement** ci-dessous, et seulement après « Appliquer ».

L'ajustement ne corrige que la **position XY** de la station et, sur demande,
sa **hauteur d'instrument H** (les deux écrites dans le CSV de sortie).
**L'orientation des images est mesurée et affichée, jamais modifiée.** Les
**pastilles** des bulles voisines sont calculées à partir de la position et
de la hauteur : les corriger remet donc les pastilles à leur place. Δ ne se
voit pas au sol (il décale plancher et sol ensemble) : il n'est jamais
modifié.

Sur le pavage, chaque station porte son **nom** (Locator) et son numéro de
scan, sur fond sombre (case **Noms**). La référence porte une **★ dorée**.

### Facteur d'échelle : valider la hauteur H

Une bulle se projette au sol à la distance H / tan(site) : si sa hauteur
d'instrument est fausse, **toute sa tuile est trop grande ou trop petite**,
autour de la station (H trop forte de 10 % : tuile 10 % trop grande). Le
recalage laisse donc aussi l'**échelle** libre : le facteur trouvé donne la
hauteur qui raccorde le sol, **H probable = H × échelle**.

* L'échelle n'est mesurée que si le sol commun **entoure** la station : d'un
  seul côté, grossir la tuile ressemble à la décaler, elle n'est pas
  mesurable.
* Sans ce degré de liberté, une hauteur fausse se déguiserait en faux
  décalage XY et ferait chuter la corrélation.
* En ajustement de proche en proche, l'échelle n'est libérée que si le
  raccord sans elle est médiocre et qu'elle l'améliore nettement : libre à
  chaque maillon, son léger bruit s'accumulerait le long de la chaîne.

### Contrôler / ajuster

Bouton **« ⇄ Contrôler / ajuster… »** de la vue du sol.

**Îlots** — la liste en tête donne chaque îlot : effectif, référence ★, bilan
du dernier contrôle / ajustement. Clic sur un îlot : la vue du sol le cadre.
**Traiter : l'îlot choisi / tous les îlots.** Un îlot sans référence est
ignoré par l'ajustement (« ignoré : pas de référence ★ ») ; une station
isolée ne peut pas être contrôlée.

**★ Référence de l'îlot** — la station qui ne bouge jamais : son îlot
s'aligne sur elle ; elle doit donc être juste. Boutons **= A**, **= station
active**, **= conseillée** (la plus cohérente de l'îlot après un contrôle).
Par défaut, l'îlot de A a A pour référence ; les autres sont à choisir. Les
références sont gardées d'une séance à l'autre pour ce relevé.

**🔍 Contrôler — ne modifie jamais rien.** Chaque photo est confrontée au sol
des voisines **de son îlot**, telles qu'elles sont :
* **rouge** : mal placée ou mal orientée **à elle seule**, avec l'écart (cm,
  degrés) ;
* **violet** : échelle incohérente, donc **hauteur H douteuse** (« H probable
  1,65 m (saisie 1,80 m) », échelle ×0,917) ;
* orange pointillé : douteuse (sol peu lisible).

Une photo fausse fait paraître ses voisines justes légèrement décalées vers
elle : les suspectes sont donc écartées et leurs voisines recontrôlées, pour
ne garder que les vraies fautives. Le contrôle dit aussi si la **référence
est cohérente** et **conseille** la station la plus sûre.

**▶ Ajuster depuis la référence** — la référence est d'abord contrôlée : si
elle semble fausse, l'ajustement est **suspendu** avec un avertissement (un
second clic l'utilise quand même). Ensuite, la bulle la plus proche des
stations déjà ajustées est recalée sur le sol de ses voisines, puis sert à son
tour de référence ; deux passes d'affinage répartissent l'erreur de tous
côtés. Les corrections proposées sont entourées d'**orange**.

Le **tableau** donne, pour chaque bulle : ΔX, ΔY, écart, orientation
(mesurée), ΔH et échelle, corrélation, nombre de voisines et état.
Double-clic : la vue du sol se centre dessus.

**✔ Appliquer** (après un ajustement seulement) — position XY, et H si
**« Hauteur H (échelle) »** est cochée (décochée par défaut : H est alors
seulement signalée). **Un seul Ctrl+Z annule tout.**

Garde-fous : rien ne change avant « Appliquer » ; une bulle est refusée si la
corrélation est faible ou la correction excessive (0,8 m, 5°, H ± 0,5 m) ;
sous 5 mm (XY) et 1 cm (H), elle est jugée en place et n'est pas retouchée ;
les bornes habituelles (5 m du CSV, H entre 0,10 et 3 m) s'appliquent. Seuils
du contrôle : 2 cm, 0,2°, 3 cm de hauteur.

### Vérifications

Sur des panoramas de synthèse d'un même sol :
* le pavage reconstitue le sol, avec un écart de 1,6/255, trépieds effacés ;
* une erreur de 3° d'orientation, de 30 cm de position ou de 25 cm de hauteur
  casse nettement les raccords ;
* une image réellement tournée de 4° se raccorde avec une correction de +4° ;
* recalage d'une bulle faussée de (+18, −12) cm ou de 2° : retrouvé au
  millimètre et au centième de degré ;
* de proche en proche sur 29 bulles faussées jusqu'à 27 cm et 2,3° : erreur
  résiduelle 4 mm et 0,06° ; les stations justes ne bougent pas ;
* contrôle de 28 bulles dont 4 faussées (jusqu'à 20 cm et 3°) : exactement
  les 4 repérées, aucune fausse alerte ; une référence faussée est refusée ;
* hauteur d'instrument faussée de +15 cm / −20 cm : échelle ×0,916 / ×1,137,
  H retrouvée à 2 mm près, position de la station juste à 3 mm ; en
  ajustement avec hauteurs fausses, XY et H justes à 5 mm près.

La **vue B** peut aussi se détacher dans sa propre fenêtre (bouton ⧉ de son
en-tête), par exemple sur un second écran.

Les gestes sur un point de vue (A ou B) sont refusés avec un message. Ailleurs
que sur une station, Alt / Maj + molette ne fait rien : pas de zoom par
erreur.

Les corrections s'enregistrent en continu dans un **fichier de corrections**
séparé (`…_corrections.csv`, écrit par numéro de scan). Il est relu à la
réouverture. Le CSV d'entrée et les images d'origine ne sont jamais modifiés.

## 5. Enregistrer  (« Appliquer / enregistrer… », Ctrl+S)

* **CSV de sortie** : **le même CSV qu'à l'entrée**.
  * Même format : mêmes colonnes, pas une de plus, même ordre, même
    séparateur, même encodage, mêmes unités (Hauteur en cm), mêmes décimales.
  * Seules les **lignes des stations corrigées** changent, et dans ces lignes
    seulement X, Y, Delta, Hauteur et Z final (`Zcorrige`). Le Z plancher et
    le % NORD restent intacts.
  * Sans correction, le fichier de sortie est identique octet pour octet au
    fichier d'entrée.
* **Orientation appliquée aux images** (en option) : les JPEG tournés sont
  écrits dans un autre dossier, les originaux restent intacts. La rotation se
  fait au pixel entier et réutilise les tables JPEG de la source.
* **Tout réinitialiser** : retour aux valeurs du CSV pour toutes les stations.

## 6. Naviguer

| Geste | Effet |
|---|---|
| Clic sur une pastille | y aller (sens de navigation conservé) |
| Ctrl+clic ou clic droit | **sonder** : la bulle s'ouvre dans l'autre vue, face à face |
| Glisser / double-clic | tourner la vue / recentrer |
| Molette, + / − | champ de vision (30° à 200°, cran à 120°). Au-delà de 110°, le grand angle est « droit » (projection **Pannini**) : les verticales restent verticales et droites. L'ancien rendu fisheye reste disponible dans Réglages |
| Entrée ou Espace bref | avancer vers la pastille la plus centrale |
| Retour arrière | bulle précédente |
| O / G / I / J | d'où l'on vient / face à face / inverser A et B / B regarde la station active |
| C | ouvrir / fermer la vue B |
| Liste « Voir », L / T | pastilles montrées : Local, Locaux voisins, **De proche en proche** (défaut), Distance, Plancher entier |
| F / M / V / F11 | filtres / module / visualiseur / plein écran |
| F1 ou ? | aide complète |

Le **plan** sert à naviguer : un clic va sur une station, un clic droit l'ouvre
en B. Le survol d'un point en fait la station active ; Espace + glisser le
déplace en XY, Alt / Maj + molette corrige son Δ / H. Les points de vue A et B
ne se déplacent pas.

## 7. Performance

* Les JPEG sont décodés en taille réduite, à 2048, 4096 ou 8192 px de large
  (réglable).
* Les bulles décodées restent en cache, plafonné à environ 1,1 Go, et les
  voisines sont préchargées.
* Le rendu perspective (`cv2.remap`) tourne hors du fil d'interface ; il passe
  en demi-résolution pendant une rotation.
* Sur un panorama 16000×8000 réel, en qualité 4096 : décodage 0,5 s, rendu
  1600×900 en 24 ms.

## 8. Vérifier l'installation

```
python BubbleNav_XPhase.py --selftest [--csv releve.csv]
```

Ce contrôle couvre :
* les angles et la cohérence entre la position calculée des pastilles et le
  rendu réel ;
* le réseau et la lecture souple du CSV ;
* le modèle **Z = plancher + Δ + H**, les bornes, les arrondis et
  l'annulation ;
* le **CSV de sortie identique à l'entrée** (octet pour octet sans correction ;
  seules les lignes corrigées changent) ;
* l'aller-retour du fichier de corrections ;
* la rotation d'image (une image tournée de Δ équivaut à une vue décalée de Δ) ;
* la taille à l'échelle des pastilles et l'estompage.

## 9. Dépendances

`Pillow`, `opencv-python`, `numpy` : elles sont installées automatiquement si
elles manquent. Il faut Python 3.9 ou plus, avec Tkinter (inclus dans
l'installateur Windows officiel).
