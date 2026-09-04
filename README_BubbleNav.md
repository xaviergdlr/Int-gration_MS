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
violet ▼ = niveau en dessous · rouge sombre = image absente du dossier.

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

## 5. Performance

* Décodage JPEG réduit à la volée (`draft`) à 2048 / 4096 / 8192 px de large — réglable
  dans « Qualité » ;
* cache mémoire des bulles décodées + **préchargement des voisins** dès l'arrivée :
  le saut suivant est instantané ;
* rendu perspective par `cv2.remap` avec grilles de rayons et de remap mises en cache,
  exécuté hors du thread d'interface ; résolution réduite pendant la rotation, pleine
  résolution au repos.

Mesures sur source 4096×2048 : ~16 ms par image en 1600×900, ~4 ms pendant la rotation.

## 6. Vérifier l'installation

```
python BubbleNav_XPhase.py --selftest
```

Contrôle les angles, la réciprocité azimut ↔ image, la **cohérence entre la position
calculée des pastilles et le rendu réel** (écart mesuré < 2 px), la lecture du CSV, la
construction du réseau et les temps de rendu.

## 7. Dépendances

`Pillow`, `opencv-python`, `numpy` — installées automatiquement au premier lancement si
elles manquent. Python 3.9 ou plus, avec Tkinter (inclus dans l'installateur Windows
officiel).
