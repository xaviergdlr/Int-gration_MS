# BubbleNav-XPhase

Navigateur autonome de **bulles géoréférencées** : les panoramas d'un relevé sont
parcourus de proche en proche, en cliquant sur des **pastilles posées au sol** à la
position réelle des bulles voisines (principe Street View, appliqué à votre CSV).

L'outil est **indépendant** d'Orientation-XPhase : un seul fichier Python, rien à installer
à la main.

---

## 1. Démarrer

Double-cliquez sur `BubbleNav.bat` (Windows) ou lancez :

```
python BubbleNav_XPhase.py
```

Au premier lancement, l'outil demande :
1. le **CSV de relevé** ;
2. le **dossier des images bulles** (exploré récursivement ; on peut annuler et le
   choisir plus tard avec le bouton « Images… »).

Les deux chemins sont mémorisés pour les lancements suivants.

Options en ligne de commande :

```
python BubbleNav_XPhase.py --csv releve.csv --images D:\Bulles\GRA6
python BubbleNav_XPhase.py --selftest      # vérifications internes, sans interface
```

## 2. Le CSV attendu

Séparateur `;` `,` tabulation ou `|`, virgule ou point décimal, colonnes dans n'importe
quel ordre, accents indifférents :

| Colonne | Rôle | Obligatoire |
|---|---|---|
| `Fichier photo` | nom de l'image, avec ou sans extension | oui |
| `X` | coordonnée **Est** (m) | oui |
| `Y` | coordonnée **Nord** (m) | oui |
| `Z` | altitude de la caméra (m) | recommandé |
| `% NORD` | position du nord dans l'image, en % de la largeur (50 % = centre) | recommandé |
| `Nom du Locator` | nom affiché de la bulle | facultatif |
| `Plancher` | niveau, sert au plan et aux liens verticaux | facultatif |

Les lignes inexploitables sont ignorées et listées dans « Réglages… → Voir les
avertissements CSV » — jamais bloquantes.

## 3. Naviguer

| Action | Effet |
|---|---|
| Clic sur une pastille | aller sur cette bulle |
| Glisser | tourner la vue |
| Molette, `+` / `−` | champ de vision (30° à 130°, 105° par défaut) |
| Double-clic | recentrer la vue sur ce point |
| `Entrée` / `Espace` | avancer vers la pastille la plus centrale |
| `Retour arrière` | revenir à la bulle précédente |
| Flèches (`Maj` = pas large) | tourner |
| `Origine` | redresser la vue |
| `F11` / `Échap` | plein écran |

**Plan du plancher** (à droite) : clic gauche = aller à la bulle la plus proche,
molette = zoom, clic droit glissé = déplacer. Le cône jaune montre où vous regardez.
La liste « Plancher » change de niveau en rejoignant la bulle la plus proche à l'aplomb.

**Couleur des pastilles** : jaune = même plancher · bleu ▲ = niveau au-dessus ·
violet ▼ = niveau en dessous · rouge sombre = image absente du dossier · orange =
bulle corrigée.

**Taille des pastilles** : rayon à l'écran = focale × rayon physique ÷ distance —
une pastille quatre fois plus loin est quatre fois plus petite, et zoomer les
grossit exactement comme un disque posé au sol. Le rayon physique (0,42 m par
défaut) se règle dans « Réglages… », entre des bornes de 7 et 70 px.

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
* **Hauteur caméra** : hauteur de prise de vue au-dessus du sol (1,65 m par défaut),
  elle fixe la hauteur à laquelle les pastilles se posent.

Section *Réseau* : portée des liens, nombre maximal de pastilles, séparation angulaire
minimale (une seule pastille par direction, pour ne pas empiler les bulles alignées) et
portée des liaisons entre planchers.

## 5. Corriger le relevé depuis la vue (mode Édition)

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

### Le fichier de corrections
```
Fichier photo;Nom du Locator;X;Y;Z;Delta Nord (deg);dX;dY;dZ;Orientation appliquee;Date
CP1_GRA_TR6_BK_02_K256_20260416_01;K256_01;589.150;73.450;1.650;6.1077;+0.000;+0.000;+0.000;;2026-09-04 17:53
```
* une ligne par bulle corrigée, rien d'autre ;
* `X` `Y` `Z` sont les valeurs **corrigées**, `dX` `dY` `dZ` l'écart au relevé —
  relisible à l'œil pour contrôler une passe de correction ;
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

### Position X / Y / Z
Trois gestes, au choix :
* **glisser une pastille** dans la vue : elle se déplace au sol, gauche/droite =
  azimut, haut/bas = éloignement (intersection exacte du rayon avec le plan du
  sol, vérifiée au pixel près) ;
* **glisser un point sur le plan** : positionnement X/Y en vue de dessus ;
* **saisie numérique** X/Y/Z, avec pas réglable (1 cm à 50 cm) et Page haut/bas
  pour l'altitude.

**Ctrl + glisser** dans la vue déplace la **bulle active** elle-même : tout le
réseau de pastilles suit le curseur, ce qui permet de recaler une bulle mal
positionnée sur le décor qu'elle voit.

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
* **Ctrl+Z** annule ; un glisser complet compte pour une seule étape ;
* « Réinit. cible » et « Réinit. tout » ramènent aux valeurs du relevé ;
* une ligne du fichier de corrections dont la photo n'existe pas dans le relevé
  est signalée et ignorée, jamais bloquante.

### Survol d'une pastille
Une infobulle donne le nom, le fichier photo, la **distance 3D**, la distance
horizontale, le Δ altitude, l'azimut, les coordonnées, le plancher, la présence
de l'image et, le cas échéant, la correction déjà appliquée.

## 6. Performance

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

## 7. Vérifier l'installation

```
python BubbleNav_XPhase.py --selftest
```

Contrôle les angles, la réciprocité azimut ↔ image, la **cohérence entre la position
calculée des pastilles et le rendu réel** (écart mesuré < 2 px), la lecture du CSV, la
construction du réseau, les temps de rendu, l'aller-retour **écran ↔ sol** utilisé pour
déplacer une pastille, l'analyse des noms de fichiers (convention, variantes, noms incomplets, cohérence
avec les colonnes du relevé sur les 693 bulles), les filtres de pastilles et la loi de
taille en 1/distance, l'aller-retour du fichier de corrections (écriture, relecture, ligne orpheline,
date d'application) avec relevé source inchangé octet pour octet, l'écriture du relevé
complet corrigé (colonne Δ nord créée puis réutilisée, seules les lignes modifiées
changent), et l'équivalence **image tournée de Δ ≡ vue décalée de Δ** — autrement dit, ce que vous
voyez en réglant l'orientation est exactement ce que l'application par lot écrira dans
le JPEG.

## 8. Dépendances

`Pillow`, `opencv-python`, `numpy` — installées automatiquement au premier lancement si
elles manquent. Python 3.9 ou plus, avec Tkinter (inclus dans l'installateur Windows
officiel).
