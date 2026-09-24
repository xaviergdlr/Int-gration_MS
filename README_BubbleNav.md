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

**On corrige uniquement la station active**, c'est-à-dire celle de la vue A,
et seulement ceci :

| Correction | Geste | Où elle va |
|---|---|---|
| **Position XY** | Espace + glisser son point sur le **plan**, ou sa pastille dans la **vue B** | CSV de sortie |
| **Δ** | Alt + molette | CSV de sortie |
| **H** | Maj + molette | CSV de sortie |
| **Orientation** de l'image | Ctrl + molette (1°, ou 0,1° dans Réglages) | les **images** (jamais le CSV) |

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

## 4. Corriger la station active

1. **Choisir la station** : elle est active dans la vue A. Pour corriger une
   voisine, faites Ctrl+clic dessus (elle s'ouvre en B), puis **I** pour
   échanger A et B : elle devient active.
2. **Δ et H** : Alt + molette et Maj + molette, que le curseur soit dans A,
   dans B ou sur le plan. Le pas est de 5 cm (1 cm dans Réglages).
   * Le tronçon Δ monte du plancher ; H part du sol local et mène à la sphère.
     Alt fait donc monter Δ, puis H et la sphère avec lui ; Maj ne fait monter
     que H et la sphère.
   * **Dans la fiche**, présente en A comme en B, une **coupe** de la station
     active le montre à chaque cran : plancher, Δ en rose, mire H, sphère. La
     position du CSV reste en pointillé.
   * **Dans B**, la vraie sphère de la station active monte ou descend sur son
     mât, et l'ancrage au plancher reste fixe.
   * **Dans A**, qui est la vue prise *depuis* la station, changer Δ ou H
     déplace la caméra. Toutes les voisines se recalent sur la photo : c'est
     là qu'on juge si H et Δ sont justes (les pieds doivent tomber sur le sol
     de l'image).
3. **Position XY** : maintenez **Espace** et glissez :
   * **sur le plan**, le point cerclé de la station active : il suit la
     souris. Aucun autre point ne peut être déplacé ;
   * ou **dans B**, la pastille de la station active (sphère, mât ou ombre) :
     son pied suit le curseur sur le plancher, en direct, et son point de
     départ reste visible en transparence.

   Pendant le geste, la vue A reste figée ; elle se recale au lâcher. Dans A
   elle-même, le geste est refusé : on y est *dans* la station, tout s'y
   décalerait et l'on croirait les voisines modifiées. X / Y verrouillent un
   axe si besoin.
4. **Orientation** : Ctrl + molette, par pas de 1° (ou 0,1°). L'image tourne
   sous les pastilles ; le trait du centre de l'image montre le décalage.
5. **Suivi** : la **fiche**, en bas à gauche, en gros sur fond sombre, donne
   Z plancher, Δ, H, Z final, ΔX / ΔY et l'orientation. Elle se met à jour à
   chaque cran et surligne la valeur qui vient de changer. Les valeurs
   corrigées passent en orange, avec leur valeur du CSV.
6. **Revenir en arrière** : Ctrl+Z (une rafale de molette compte pour une
   seule étape). Le bouton **↺ CSV**, dans l'en-tête de A comme dans celui de
   B, rend à la station active ses valeurs du CSV.

**Règle unique** : les gestes agissent toujours sur la station active, où que
soit le curseur (A, B ou plan). Chaque vue montre ce qu'elle peut montrer :
* A (vue *depuis* la station) : l'orientation et le recalage de la caméra ;
* B (vue *sur* la station) : sa sphère, son mât, sa position ;
* le plan : sa position en XY.

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
| O / G / I | d'où l'on vient / face à face / inverser A et B |
| C | ouvrir / fermer la vue B |
| Liste « Voir », L / T | pastilles montrées : Local, Locaux voisins, **De proche en proche** (défaut), Distance, Plancher entier |
| F / M / V / F11 | filtres / module / visualiseur / plein écran |
| F1 ou ? | aide complète |

Le **plan** sert à naviguer : un clic va sur une station, un clic droit l'ouvre
en B. Espace + glisser le point de la station active la déplace en XY ; aucun
autre point ne peut être déplacé.

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
