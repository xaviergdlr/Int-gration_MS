# -*- coding: utf-8 -*-
"""
BubbleNav-XPhase v1.0.0
Navigateur de bulles georeferencees (panoramas equirectangulaires).

Principe :
  * un CSV de releve donne la position (X, Y, Z), l'orientation nord et le
    plancher de chaque bulle ;
  * l'outil calcule un reseau de voisinage (de proche en proche) ;
  * chaque voisin est projete dans la vue comme une pastille cliquable,
    posee au sol a sa position reelle ;
  * un clic (ou la touche Entree) deplace l'observateur sur cette bulle,
    en conservant le cap regarde.

Outil autonome : aucune dependance au programme Orientation-XPhase.

Auteur  : XPhase
Version : 1.0.0
Python  : 3.9+
Deps    : Pillow, opencv-python, numpy
Usage   : python BubbleNav_XPhase.py [--csv CHEMIN] [--images DOSSIER]
          python BubbleNav_XPhase.py --selftest   (verifications sans interface)
"""

from __future__ import annotations

import os

os.environ.setdefault('OPENCV_LOG_LEVEL', 'ERROR')

import argparse
import csv
import json
import math
import queue
import sys
import threading
import time
import unicodedata
from bisect import insort
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__version__ = "1.0.0"
__author__ = "XPhase"

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────────────────────

APP_NAME = "BubbleNav-XPhase"
CONFIG_NAME = ".bubblenav_xphase.json"

IMG_EXTS = ('.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp', '.bmp')

# Rendu
FOV_MIN, FOV_MAX = 30.0, 130.0
FOV_DEFAULT = 105.0            # vue large demandee
PITCH_MIN, PITCH_MAX = -89.0, 89.0
PITCH_DEFAULT = -20.0          # les pastilles au sol sont sous l'horizon
DRAG_SCALE = 0.5               # sous-echantillonnage pendant la manipulation
IDLE_FULL_MS = 110             # delai avant rendu pleine resolution
UI_PUMP_MS = 15                # periode de drainage des messages threads
MAP_CACHE_SIZE = 24            # LRU des grilles de remap
RAY_CACHE_SIZE = 8             # LRU des grilles de rayons (par fov/taille)

# Sources images
SRC_WIDTH_CHOICES = (2048, 4096, 8192)
SRC_WIDTH_DEFAULT = 4096
IMG_CACHE_DEFAULT = 12         # bulles decodees gardees en memoire
PREFETCH_WORKERS = 4

# Reseau
RADIUS_DEFAULT = 12.0          # m — portee max d'un lien
KMAX_DEFAULT = 8               # nb max de pastilles par bulle
ANG_MIN_DEFAULT = 25.0         # deg — separation angulaire mini entre pastilles
FLOOR_RADIUS_DEFAULT = 5.0     # m — portee horizontale d'un lien inter-plancher
FLOOR_DZ_MAX = 12.0            # m — denivele max d'un lien inter-plancher
EYE_HEIGHT_DEFAULT = 1.65      # m — hauteur de la camera au-dessus du sol

# Pastilles
DISC_RADIUS_M = 0.42           # m — rayon physique de la pastille au sol
DISC_PX_MIN, DISC_PX_MAX = 7.0, 70.0
HIT_SLACK_PX = 10.0            # tolerance de clic autour de la pastille

# Couleurs (theme sombre, coherent avec Orientation-XPhase)
COLORS = {
    'bg_dark': '#1e1e1e',
    'bg_medium': '#2d2d2d',
    'bg_light': '#3c3c3c',
    'card': '#252525',
    'border': '#444444',
    'accent': '#0078d4',
    'text': '#e0e0e0',
    'text_muted': '#888888',
    'ok': '#4caf50',
    'warning': '#ff9800',
    'error': '#f44336',
    'hot': '#ffd24a',          # pastille meme plancher
    'hot_up': '#7fd4ff',       # pastille montante
    'hot_down': '#c58bff',     # pastille descendante
    'hot_edge': '#101010',
    'plan_link': '#3a5a75',
    'plan_pt': '#6f7f8f',
    'plan_missing': '#5a4040',
    'plan_here': '#ffd24a',
    'plan_cone': '#ffd24a',
}

F_UI = ('Segoe UI', 9)
F_UI_B = ('Segoe UI', 9, 'bold')
F_TITLE = ('Segoe UI', 11, 'bold')
F_MONO = ('Consolas', 9)


# ─────────────────────────────────────────────────────────────────────────────
# OUTILS GENERAUX
# ─────────────────────────────────────────────────────────────────────────────

def wrap180(a: float) -> float:
    """Ramene un angle en degres dans ]-180, +180]."""
    a = (a + 180.0) % 360.0 - 180.0
    return a + 360.0 if a <= -180.0 else a


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn')


def norm_key(s: str) -> str:
    """Normalise un intitule de colonne : minuscules, sans accents ni espaces."""
    s = _strip_accents(str(s or '')).lower().replace('%', 'pct')
    return ''.join(c for c in s if c.isalnum())


def parse_float(s: str) -> Optional[float]:
    """Lit un nombre tolerant : virgule decimale, espaces, espaces insecables."""
    if s is None:
        return None
    t = str(s).strip().replace(' ', '').replace(' ', '').replace(',', '.')
    if not t:
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def base_name(path_or_name: str) -> str:
    """Nom de fichier sans dossier ni extension."""
    b = os.path.basename(str(path_or_name).strip().strip('"'))
    root, ext = os.path.splitext(b)
    return root if ext.lower() in IMG_EXTS else b


def human_dist(d: float) -> str:
    return f"{d:.1f} m" if d < 100 else f"{d:.0f} m"


# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION PERSISTANTE
# ─────────────────────────────────────────────────────────────────────────────

def config_path() -> str:
    home = os.path.expanduser('~')
    if not os.path.isdir(home):
        home = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(home, CONFIG_NAME)


DEFAULT_CONFIG = {
    'csv_path': '',
    'images_dir': '',
    'fov': FOV_DEFAULT,
    'src_width': SRC_WIDTH_DEFAULT,
    'cache_size': IMG_CACHE_DEFAULT,
    # Calibration azimut
    'north_mode': 'colonne',   # 'colonne' | 'centre'
    'north_sense': 1,          # +1 : azimut croissant vers la droite de l'image
    'north_offset': 0.0,       # deg — correction manuelle globale
    'eye_height': EYE_HEIGHT_DEFAULT,
    # Reseau
    'radius': RADIUS_DEFAULT,
    'kmax': KMAX_DEFAULT,
    'ang_min': ANG_MIN_DEFAULT,
    'floor_radius': FLOOR_RADIUS_DEFAULT,
    'show_labels': True,
    'keep_heading': True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            for k, v in data.items():
                if k not in cfg:
                    continue
                ref = cfg[k]
                if isinstance(ref, bool):
                    cfg[k] = bool(v)
                elif isinstance(ref, (int, float)) and isinstance(v, (int, float)):
                    cfg[k] = type(ref)(v)
                elif isinstance(ref, str) and isinstance(v, str):
                    cfg[k] = v
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        tmp = config_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(cfg, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, config_path())
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# MODELE : STATIONS ET LECTURE CSV
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Station:
    idx: int
    photo: str          # nom de base du fichier image
    locator: str
    x: float            # Est (m)
    y: float            # Nord (m)
    z: float            # Altitude camera (m)
    north_pct: float    # colonne du nord dans l'image, en % de la largeur
    floor: str


COL_ALIASES = {
    'photo': ('fichierphoto', 'fichier', 'photo', 'image', 'nomimage',
              'nomphoto', 'filename', 'file', 'name'),
    'locator': ('nomdulocator', 'locator', 'nomlocator', 'station', 'point',
                'nomdupoint', 'nom', 'id'),
    'x': ('x', 'e', 'est', 'easting', 'xm', 'coordx'),
    'y': ('y', 'n', 'nord', 'northing', 'ym', 'coordy'),
    'z': ('z', 'altitude', 'alt', 'elevation', 'h', 'hauteur', 'zm', 'coordz'),
    'north': ('pctnord', 'nordpct', 'pct', 'nordpourcent', 'cap', 'heading',
              'azimut', 'orientation'),
    'floor': ('plancher', 'niveau', 'etage', 'level', 'floor', 'dalle'),
}


def _sniff_delimiter(sample: str) -> str:
    """Choisit le separateur le plus present sur la premiere ligne."""
    line = sample.splitlines()[0] if sample else ''
    counts = {d: line.count(d) for d in (';', '\t', ',', '|')}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ';'


def _read_text(path: str) -> str:
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            with open(path, 'r', encoding=enc, newline='') as fh:
                return fh.read()
        except UnicodeDecodeError:
            continue
    with open(path, 'r', encoding='latin-1', errors='replace', newline='') as fh:
        return fh.read()


def read_survey_csv(path: str) -> Tuple[List[Station], List[str]]:
    """Lit le CSV de releve.

    Tolerant : BOM, separateur ; , tab |, virgule decimale, colonnes dans
    n'importe quel ordre, intitules accentues ou non.

    Retourne (stations, avertissements). Les lignes inexploitables sont
    ignorees et signalees, jamais fatales.
    """
    warns: List[str] = []
    text = _read_text(path)
    if not text.strip():
        raise ValueError("Fichier CSV vide.")

    delim = _sniff_delimiter(text)
    reader = csv.reader(text.splitlines(), delimiter=delim)
    rows = [r for r in reader if any((c or '').strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("CSV sans donnees exploitables (moins de 2 lignes).")

    header = [norm_key(c) for c in rows[0]]
    col: Dict[str, int] = {}
    for field_name, aliases in COL_ALIASES.items():
        for alias in aliases:
            if alias in header:
                col[field_name] = header.index(alias)
                break

    missing = [f for f in ('photo', 'x', 'y') if f not in col]
    if missing:
        raise ValueError(
            "Colonnes introuvables : " + ', '.join(missing) +
            "\nIntitules lus : " + ', '.join(rows[0]) +
            "\nAttendu au minimum : « Fichier photo », « X », « Y »."
        )

    def cell(row: Sequence[str], key: str, default: str = '') -> str:
        i = col.get(key, -1)
        return row[i].strip() if 0 <= i < len(row) else default

    stations: List[Station] = []
    seen: Dict[str, int] = {}
    for lineno, row in enumerate(rows[1:], start=2):
        photo = base_name(cell(row, 'photo'))
        if not photo:
            warns.append(f"ligne {lineno} : nom de photo vide — ignoree")
            continue
        x = parse_float(cell(row, 'x'))
        y = parse_float(cell(row, 'y'))
        if x is None or y is None:
            warns.append(f"ligne {lineno} ({photo}) : X/Y illisibles — ignoree")
            continue
        z = parse_float(cell(row, 'z'))
        north = parse_float(cell(row, 'north'))
        if north is None:
            north = 50.0
        key = photo.lower()
        if key in seen:
            warns.append(f"ligne {lineno} : doublon de « {photo} » — ignoree")
            continue
        seen[key] = len(stations)
        stations.append(Station(
            idx=len(stations),
            photo=photo,
            locator=cell(row, 'locator') or photo,
            x=x, y=y, z=(0.0 if z is None else z),
            north_pct=north,
            floor=cell(row, 'floor') or '—',
        ))

    if not stations:
        raise ValueError("Aucune station exploitable dans le CSV.")
    return stations, warns


# ─────────────────────────────────────────────────────────────────────────────
# GEOMETRIE : AZIMUTS, CAP PANORAMA, PROJECTION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Calib:
    """Conversion azimut terrain <-> cap dans le panorama."""
    mode: str = 'colonne'    # 'colonne' : le % donne la colonne du nord
    sense: int = 1           # +1 : azimut croissant vers la droite de l'image
    offset: float = 0.0      # correction manuelle (deg)

    def pano_yaw(self, azimuth_deg: float, north_pct: float) -> float:
        """Cap dans l'image (0 = centre de l'image, positif vers la droite)."""
        p = north_pct / 100.0
        if self.mode == 'centre':
            # le % donne l'azimut vise par le centre de l'image
            return wrap180(self.sense * (azimuth_deg - p * 360.0) + self.offset)
        # mode 'colonne' : le nord est a la colonne p de l'image
        return wrap180(self.sense * azimuth_deg + (p - 0.5) * 360.0 + self.offset)

    def azimuth(self, pano_yaw_deg: float, north_pct: float) -> float:
        """Reciproque exacte de pano_yaw (utilisee pour conserver le cap)."""
        p = north_pct / 100.0
        if self.mode == 'centre':
            return wrap180((pano_yaw_deg - self.offset) / self.sense + p * 360.0)
        return wrap180(((pano_yaw_deg - self.offset) - (p - 0.5) * 360.0) / self.sense)


def azimuth_elev(dx_east: float, dy_north: float, dz: float) -> Tuple[float, float, float]:
    """Azimut (deg, horaire depuis le nord), elevation (deg), distance 3D (m)."""
    dh = math.hypot(dx_east, dy_north)
    az = math.degrees(math.atan2(dx_east, dy_north))
    el = math.degrees(math.atan2(dz, dh)) if (dh > 1e-9 or abs(dz) > 1e-9) else 0.0
    return az, el, math.hypot(dh, dz)


@dataclass
class View:
    """Etat de la camera perspective."""
    yaw: float = 0.0       # cap dans l'image (deg)
    pitch: float = PITCH_DEFAULT  # tangage (deg, negatif = vers le bas)
    fov: float = FOV_DEFAULT
    width: int = 1280
    height: int = 720

    def focal(self) -> float:
        return (self.width / 2.0) / math.tan(math.radians(self.fov) / 2.0)


def project(view: View, pano_yaw_deg: float, elev_deg: float
            ) -> Optional[Tuple[float, float, float]]:
    """Projette une direction (cap image, elevation) en pixels ecran.

    Convention identique au rendu : X avant, Y droite ecran, Z haut ;
    l'axe X du monde correspond au centre de l'image equirectangulaire.
    Retourne (col, row, cos_angle_axe) ou None si la direction est derriere.
    """
    ps = math.radians(pano_yaw_deg)
    th = math.radians(elev_deg)
    ct = math.cos(th)
    wx, wy, wz = ct * math.cos(ps), ct * math.sin(ps), math.sin(th)

    yr = math.radians(view.yaw)
    pr = math.radians(view.pitch)
    cy, sy = math.cos(yr), math.sin(yr)
    cp, sp = math.cos(pr), math.sin(pr)

    fwd = cy * wx + sy * wy                 # composante dans le plan de visee
    xc = cp * fwd + sp * wz                 # avant camera
    if xc <= 1e-6:
        return None
    yc = -sy * wx + cy * wy                 # droite ecran
    zc = -sp * fwd + cp * wz                # haut ecran

    f = view.focal()
    col = view.width / 2.0 + f * yc / xc
    row = view.height / 2.0 - f * zc / xc
    return col, row, xc


# ─────────────────────────────────────────────────────────────────────────────
# RESEAU DE NAVIGATION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Link:
    """Lien oriente vers une bulle voisine."""
    target: int
    dist: float          # distance 3D (m)
    dist_h: float        # distance horizontale (m)
    azimuth: float       # deg, horaire depuis le nord
    dz: float            # denivele (m)
    kind: str = 'same'   # 'same' | 'up' | 'down'


@dataclass
class GraphParams:
    radius: float = RADIUS_DEFAULT
    kmax: int = KMAX_DEFAULT
    ang_min: float = ANG_MIN_DEFAULT
    floor_radius: float = FLOOR_RADIUS_DEFAULT
    floor_dz_max: float = FLOOR_DZ_MAX


class SpatialIndex:
    """Grille reguliere — recherche de voisins en O(1) amorti.

    Dimensionnee pour rester efficace bien au-dela des quelques milliers de
    bulles d'un releve courant.
    """

    def __init__(self, stations: Sequence[Station], cell: float):
        self.cell = max(0.5, float(cell))
        self.buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
        self.stations = stations
        inv = 1.0 / self.cell
        for st in stations:
            self.buckets[(int(math.floor(st.x * inv)),
                          int(math.floor(st.y * inv)))].append(st.idx)

    def around(self, x: float, y: float, radius: float) -> Iterable[int]:
        inv = 1.0 / self.cell
        r = max(1, int(math.ceil(radius * inv)))
        cx, cy = int(math.floor(x * inv)), int(math.floor(y * inv))
        for i in range(cx - r, cx + r + 1):
            for j in range(cy - r, cy + r + 1):
                bucket = self.buckets.get((i, j))
                if bucket:
                    yield from bucket


def build_graph(stations: Sequence[Station], params: GraphParams
                ) -> List[List[Link]]:
    """Construit le reseau « de proche en proche ».

    Regles :
      * liens dans le meme plancher, tries par distance ;
      * elagage angulaire : une seule pastille par direction (la plus proche),
        ce qui evite d'empiler les bulles alignees dans un couloir ;
      * un lien montant et un lien descendant vers le plancher adjacent le
        plus proche (escaliers, tremies).
    """
    n = len(stations)
    links: List[List[Link]] = [[] for _ in range(n)]
    if n == 0:
        return links

    index = SpatialIndex(stations, max(params.radius, params.floor_radius))
    r2 = params.radius * params.radius
    fr2 = params.floor_radius * params.floor_radius

    for st in stations:
        same: List[Tuple[float, float, float, float, int]] = []
        by_floor: Dict[str, Tuple[float, int]] = {}

        for j in index.around(st.x, st.y, max(params.radius, params.floor_radius)):
            if j == st.idx:
                continue
            other = stations[j]
            dx, dy = other.x - st.x, other.y - st.y
            d2 = dx * dx + dy * dy
            if other.floor == st.floor:
                if d2 <= r2:
                    dz = other.z - st.z
                    az, _, d3 = azimuth_elev(dx, dy, dz)
                    insort(same, (d3, az, math.sqrt(d2), dz, j))
            elif d2 <= fr2:
                dz = other.z - st.z
                if abs(dz) <= params.floor_dz_max:
                    prev = by_floor.get(other.floor)
                    if prev is None or d2 < prev[0]:
                        by_floor[other.floor] = (d2, j)

        out: List[Link] = []
        for d3, az, dh, dz, j in same:
            if len(out) >= params.kmax:
                break
            if any(abs(wrap180(az - lk.azimuth)) < params.ang_min for lk in out):
                continue        # deja une pastille dans cette direction
            out.append(Link(j, d3, dh, az, dz, 'same'))

        # Planchers voisins : le plus proche au-dessus et le plus proche en dessous
        best_up: Optional[Tuple[float, int]] = None
        best_dn: Optional[Tuple[float, int]] = None
        for _floor, (d2, j) in by_floor.items():
            dz = stations[j].z - st.z
            slot = 'up' if dz >= 0 else 'down'
            cur = best_up if slot == 'up' else best_dn
            score = (abs(dz), d2)
            if cur is None or score < cur[0]:
                if slot == 'up':
                    best_up = (score, j)
                else:
                    best_dn = (score, j)
        for slot, best in (('up', best_up), ('down', best_dn)):
            if best is None:
                continue
            j = best[1]
            other = stations[j]
            dx, dy, dz = other.x - st.x, other.y - st.y, other.z - st.z
            az, _, d3 = azimuth_elev(dx, dy, dz)
            out.append(Link(j, d3, math.hypot(dx, dy), az, dz, slot))

        links[st.idx] = out
    return links


def nearest_station(stations: Sequence[Station], x: float, y: float,
                    floor: Optional[str] = None) -> Optional[int]:
    """Station la plus proche d'un point du plan (optionnellement d'un plancher)."""
    best, best_d2 = None, float('inf')
    for st in stations:
        if floor is not None and st.floor != floor:
            continue
        d2 = (st.x - x) ** 2 + (st.y - y) ** 2
        if d2 < best_d2:
            best, best_d2 = st.idx, d2
    return best


# ─────────────────────────────────────────────────────────────────────────────
# CHARGEMENT DES IMAGES (cache LRU + prechargement)
# ─────────────────────────────────────────────────────────────────────────────

def index_images(root: str, progress=None) -> Dict[str, str]:
    """Indexe recursivement les images d'un dossier : nom de base -> chemin."""
    found: Dict[str, str] = {}
    if not root or not os.path.isdir(root):
        return found
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for fn in filenames:
            root_name, ext = os.path.splitext(fn)
            if ext.lower() not in IMG_EXTS:
                continue
            key = root_name.lower()
            # une seule entree par nom de base : la premiere rencontree
            found.setdefault(key, os.path.join(dirpath, fn))
            count += 1
            if progress and count % 500 == 0:
                progress(count)
    return found


class ImageStore:
    """Cache LRU de panoramas decodes + prechargement en arriere-plan.

    Les images sont decodees en RGB numpy, reduites a la volee (draft JPEG,
    tres rapide) a la largeur cible. Thread-safe.
    """

    def __init__(self, src_width: int = SRC_WIDTH_DEFAULT,
                 cache_size: int = IMG_CACHE_DEFAULT):
        self.src_width = int(src_width)
        self.cache_size = max(2, int(cache_size))
        self._cache: "OrderedDict[str, object]" = OrderedDict()
        self._lock = threading.RLock()
        self._loading: set = set()
        self._paths: Dict[str, str] = {}
        self._pool = None
        self._closed = False

    # ── configuration ────────────────────────────────────────────────
    def set_paths(self, paths: Dict[str, str]) -> None:
        with self._lock:
            self._paths = dict(paths)
            self._cache.clear()

    def set_src_width(self, width: int) -> None:
        with self._lock:
            if int(width) != self.src_width:
                self.src_width = int(width)
                self._cache.clear()

    def set_cache_size(self, size: int) -> None:
        with self._lock:
            self.cache_size = max(2, int(size))
            self._trim()

    def has(self, photo: str) -> bool:
        return photo.lower() in self._paths

    def path_of(self, photo: str) -> Optional[str]:
        return self._paths.get(photo.lower())

    def count(self) -> int:
        return len(self._paths)

    # ── acces ────────────────────────────────────────────────────────
    def peek(self, photo: str):
        """Image deja en cache, ou None (jamais de decodage ici)."""
        key = photo.lower()
        with self._lock:
            img = self._cache.get(key)
            if img is not None:
                self._cache.move_to_end(key)
        return img

    def load(self, photo: str):
        """Decode l'image (bloquant) et la met en cache. None si absente."""
        key = photo.lower()
        cached = self.peek(photo)
        if cached is not None:
            return cached
        path = self._paths.get(key)
        if not path:
            return None
        try:
            arr = self._decode(path)
        except Exception:
            arr = None
        if arr is None:
            return None
        with self._lock:
            self._cache[key] = arr
            self._cache.move_to_end(key)
            self._trim()
        return arr

    def prefetch(self, photos: Sequence[str]) -> None:
        """Precharge en arriere-plan (voisins de la bulle courante)."""
        if self._closed:
            return
        with self._lock:
            if self._pool is None:
                from concurrent.futures import ThreadPoolExecutor
                self._pool = ThreadPoolExecutor(
                    max_workers=PREFETCH_WORKERS,
                    thread_name_prefix='bubblenav-prefetch')
            todo = []
            for photo in photos:
                key = photo.lower()
                if key in self._cache or key in self._loading or key not in self._paths:
                    continue
                self._loading.add(key)
                todo.append(photo)
            pool = self._pool
        for photo in todo:
            try:
                pool.submit(self._prefetch_one, photo)
            except Exception:
                with self._lock:
                    self._loading.discard(photo.lower())

    def close(self) -> None:
        self._closed = True
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # ── interne ──────────────────────────────────────────────────────
    def _prefetch_one(self, photo: str) -> None:
        try:
            self.load(photo)
        finally:
            with self._lock:
                self._loading.discard(photo.lower())

    def _trim(self) -> None:
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)

    def _decode(self, path: str):
        import numpy as np
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(path) as im:
            target = self.src_width
            try:
                # draft() : sous-echantillonnage DCT natif du JPEG (tres rapide)
                im.draft('RGB', (target, max(1, target // 2)))
            except Exception:
                pass
            im = im.convert('RGB')
            if im.width > target:
                h = max(1, round(im.height * target / im.width))
                im = im.resize((target, h), Image.BILINEAR)
            return np.asarray(im)


# ─────────────────────────────────────────────────────────────────────────────
# RENDU GNOMONIQUE (equirectangulaire -> perspective)
# ─────────────────────────────────────────────────────────────────────────────

class PanoRenderer:
    """Projection perspective d'un panorama equirectangulaire.

    Optimisations :
      * grille de rayons precalculee par (fov, largeur, hauteur) — la rotation
        se reduit alors a un produit matriciel 3x3 ;
      * cache LRU des grilles de remap (cv2.remap) ;
      * float32 partout, aucune allocation superflue par image.
    """

    def __init__(self):
        self._rays: "OrderedDict[tuple, object]" = OrderedDict()
        self._maps: "OrderedDict[tuple, tuple]" = OrderedDict()
        self._lock = threading.RLock()
        self._np = None
        self._cv2 = None

    def _mods(self):
        if self._np is None:
            import numpy as np
            self._np = np
        if self._cv2 is None:
            import cv2
            self._cv2 = cv2
        return self._np, self._cv2

    def _ray_grid(self, fov: float, w: int, h: int):
        """Rayons camera unitaires (3, N) : X avant, Y droite, Z haut."""
        np, _ = self._mods()
        key = (round(fov, 3), w, h)
        with self._lock:
            rays = self._rays.get(key)
            if rays is not None:
                self._rays.move_to_end(key)
                return rays
        f = (w / 2.0) / math.tan(math.radians(fov) / 2.0)
        xs = np.linspace(-w / 2.0, w / 2.0, w, dtype=np.float32)
        ys = np.linspace(-h / 2.0, h / 2.0, h, dtype=np.float32)
        gx, gy = np.meshgrid(xs, ys)
        norm = np.sqrt(f * f + gx * gx + gy * gy, dtype=np.float32)
        rays = np.empty((3, w * h), dtype=np.float32)
        rays[0] = (f / norm).reshape(-1)
        rays[1] = (gx / norm).reshape(-1)
        rays[2] = (-gy / norm).reshape(-1)
        with self._lock:
            self._rays[key] = rays
            while len(self._rays) > RAY_CACHE_SIZE:
                self._rays.popitem(last=False)
        return rays

    @staticmethod
    def _rotation(yaw_deg: float, pitch_deg: float, np):
        """Camera -> monde : Rz(yaw) . Ry(-pitch)."""
        yr, pr = math.radians(yaw_deg), math.radians(pitch_deg)
        cy, sy = math.cos(yr), math.sin(yr)
        cp, sp = math.cos(pr), math.sin(pr)
        return np.array([
            [cy * cp, -sy, -cy * sp],
            [sy * cp,  cy, -sy * sp],
            [sp,      0.0,  cp],
        ], dtype=np.float32)

    def _remap_grids(self, view: View, sw: int, sh: int):
        np, _ = self._mods()
        key = (round(view.yaw, 2), round(view.pitch, 2), round(view.fov, 2),
               view.width, view.height, sw, sh)
        with self._lock:
            maps = self._maps.get(key)
            if maps is not None:
                self._maps.move_to_end(key)
                return maps

        rays = self._ray_grid(view.fov, view.width, view.height)
        world = self._rotation(view.yaw, view.pitch, np) @ rays   # (3, N)

        mx = np.arctan2(world[1], world[0])
        mx += math.pi
        mx *= (sw / (2.0 * math.pi))
        mz = np.clip(world[2], -1.0, 1.0)
        my = np.arcsin(mz)
        my *= (-1.0 / math.pi)
        my += 0.5
        my *= sh
        maps = (mx.reshape(view.height, view.width),
                my.reshape(view.height, view.width))
        with self._lock:
            self._maps[key] = maps
            while len(self._maps) > MAP_CACHE_SIZE:
                self._maps.popitem(last=False)
        return maps

    def render(self, src, view: View):
        """Rend la vue perspective. `src` : tableau RGB (H, W, 3)."""
        np, cv2 = self._mods()
        sh, sw = src.shape[0], src.shape[1]
        mx, my = self._remap_grids(view, sw, sh)
        return cv2.remap(src, mx, my, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_WRAP)

    def clear(self) -> None:
        with self._lock:
            self._rays.clear()
            self._maps.clear()


# ─────────────────────────────────────────────────────────────────────────────
# INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _TK_OK = True
except Exception:                                   # pragma: no cover
    tk = ttk = filedialog = messagebox = None
    _TK_OK = False

_TkBase = tk.Tk if _TK_OK else object


@dataclass
class Hotspot:
    """Pastille projetee dans la vue."""
    link: Link
    col: float
    row: float
    radius: float
    label: str


class BubbleNavApp(_TkBase):
    """Fenetre principale : vue bulle + pastilles cliquables + plan."""

    def __init__(self, cfg: dict, csv_path: str = '', images_dir: str = ''):
        super().__init__()
        self.cfg = cfg
        self.stations: List[Station] = []
        self.links: List[List[Link]] = []
        self.floors: List[str] = []
        self.current: int = -1
        self.history: List[int] = []
        self.csv_path: str = ''
        self.images_dir: str = ''
        self.warnings: List[str] = []

        self.calib = Calib(mode=str(cfg.get('north_mode', 'colonne')),
                           sense=1 if int(cfg.get('north_sense', 1)) >= 0 else -1,
                           offset=float(cfg.get('north_offset', 0.0)))
        self.params = GraphParams(
            radius=float(cfg.get('radius', RADIUS_DEFAULT)),
            kmax=int(cfg.get('kmax', KMAX_DEFAULT)),
            ang_min=float(cfg.get('ang_min', ANG_MIN_DEFAULT)),
            floor_radius=float(cfg.get('floor_radius', FLOOR_RADIUS_DEFAULT)),
        )
        self.store = ImageStore(int(cfg.get('src_width', SRC_WIDTH_DEFAULT)),
                                int(cfg.get('cache_size', IMG_CACHE_DEFAULT)))
        self.renderer = PanoRenderer()

        self.view = View(fov=float(cfg.get('fov', FOV_DEFAULT)))
        self.hotspots: List[Hotspot] = []
        self._hover: Optional[int] = None
        self._frame_view: Optional[View] = None
        self._tk_img = None
        self._drag: Optional[Tuple[int, int, float, float]] = None
        self._interactive = False
        self._idle_job = None
        self._plan_view = {'scale': 1.0, 'ox': 0.0, 'oy': 0.0, 'fitted': False}
        self._plan_drag = None
        self._plan_floor = ''

        # Rendu asynchrone : 1 thread, derniere demande gagnante
        self._req: Optional[tuple] = None
        self._req_seq = 0
        self._shown_seq = -1
        self._cv = threading.Condition()
        self._stop = threading.Event()
        # Les threads ne touchent jamais Tk : ils deposent ici, le thread
        # principal draine la file (seul mecanisme garanti thread-safe).
        self._ui_queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._worker = threading.Thread(target=self._render_worker,
                                        name='bubblenav-render', daemon=True)

        self._build_ui()
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self._worker.start()
        self.after(UI_PUMP_MS, self._pump_ui)

        if csv_path:
            self.after(60, lambda: self.load_csv(csv_path, images_dir))
        elif images_dir:
            self.after(60, lambda: self.set_images_dir(images_dir))

    # ═════════════════════════════════════════════════════════════════
    # CONSTRUCTION DE L'INTERFACE
    # ═════════════════════════════════════════════════════════════════
    def _build_ui(self) -> None:
        self.title(f"{APP_NAME} v{__version__}")
        self.configure(bg=COLORS['bg_dark'])
        self.geometry("1500x900")
        self.minsize(900, 560)

        style = ttk.Style(self)
        try:
            style.theme_use('clam')
        except Exception:
            pass
        style.configure('BN.TCombobox', fieldbackground=COLORS['bg_light'],
                        background=COLORS['bg_light'], foreground=COLORS['text'],
                        arrowcolor=COLORS['text'], bordercolor=COLORS['border'],
                        lightcolor=COLORS['bg_light'], darkcolor=COLORS['bg_light'],
                        padding=3)
        style.map('BN.TCombobox',
                  fieldbackground=[('readonly', COLORS['bg_light'])],
                  foreground=[('readonly', COLORS['text'])],
                  background=[('readonly', COLORS['bg_light'])],
                  selectbackground=[('readonly', COLORS['bg_light'])],
                  selectforeground=[('readonly', COLORS['text'])])
        self.option_add('*TCombobox*Listbox.background', COLORS['card'])
        self.option_add('*TCombobox*Listbox.foreground', COLORS['text'])
        self.option_add('*TCombobox*Listbox.selectBackground', COLORS['accent'])
        self.option_add('*TCombobox*Listbox.selectForeground', 'white')
        style.configure('BN.Horizontal.TScale', background=COLORS['bg_medium'])

        self._build_toolbar()

        body = tk.Frame(self, bg=COLORS['bg_dark'])
        body.pack(fill='both', expand=True)

        # Vue bulle
        left = tk.Frame(body, bg=COLORS['bg_dark'])
        left.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(left, bg='#101010', highlightthickness=0,
                                cursor='fleur')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', self._on_canvas_resize)
        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Motion>', self._on_motion)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', lambda e: self._on_wheel(e, +1))
        self.canvas.bind('<Button-5>', lambda e: self._on_wheel(e, -1))
        self.canvas.bind('<Double-Button-1>', self._on_double)

        # Panneau lateral
        self._build_side_panel(body)
        self._build_status()
        self._bind_keys()

    def _mk_button(self, parent, text, cmd, bg=None, width=None):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg or COLORS['bg_light'], fg=COLORS['text'],
                      activebackground=COLORS['accent'], activeforeground='white',
                      relief='flat', bd=0, padx=10, pady=4, font=F_UI,
                      cursor='hand2', highlightthickness=0)
        if width:
            b.config(width=width)
        return b

    def _build_toolbar(self) -> None:
        bar = tk.Frame(self, bg=COLORS['bg_medium'])
        bar.pack(fill='x', side='top')

        tk.Label(bar, text="BubbleNav", font=F_TITLE, bg=COLORS['bg_medium'],
                 fg=COLORS['accent']).pack(side='left', padx=(10, 12), pady=5)

        self._mk_button(bar, "CSV…", self._open_csv).pack(side='left', padx=3, pady=4)
        self._mk_button(bar, "Images…", self._open_images).pack(side='left', padx=3, pady=4)

        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y',
                                                         padx=8, pady=6)

        tk.Label(bar, text="Plancher", bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                 font=F_UI).pack(side='left', padx=(2, 4))
        self.floor_var = tk.StringVar()
        self.floor_cb = ttk.Combobox(bar, textvariable=self.floor_var, width=24,
                                     state='readonly', style='BN.TCombobox')
        self.floor_cb.pack(side='left', padx=2, pady=4)
        self.floor_cb.bind('<<ComboboxSelected>>', self._on_floor_selected)

        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y',
                                                         padx=8, pady=6)

        tk.Label(bar, text="Champ", bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                 font=F_UI).pack(side='left', padx=(2, 2))
        self.fov_var = tk.DoubleVar(value=self.view.fov)
        self.fov_scale = tk.Scale(bar, from_=FOV_MIN, to=FOV_MAX, resolution=1,
                                  orient='horizontal', length=150, showvalue=False,
                                  variable=self.fov_var, command=self._on_fov,
                                  bg=COLORS['text_muted'], fg=COLORS['text'],
                                  troughcolor=COLORS['bg_dark'], highlightthickness=0,
                                  bd=0, sliderrelief='flat',
                                  activebackground=COLORS['accent'])
        self.fov_scale.pack(side='left', padx=2)
        self.fov_lbl = tk.Label(bar, text=f"{self.view.fov:.0f}°", width=5,
                                bg=COLORS['bg_medium'], fg=COLORS['text'], font=F_MONO)
        self.fov_lbl.pack(side='left')

        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y',
                                                         padx=8, pady=6)

        tk.Label(bar, text="Qualité", bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                 font=F_UI).pack(side='left', padx=(2, 4))
        self.qual_var = tk.StringVar(value=str(self.store.src_width))
        qual = ttk.Combobox(bar, textvariable=self.qual_var, width=6, state='readonly',
                            style='BN.TCombobox',
                            values=[str(v) for v in SRC_WIDTH_CHOICES])
        qual.pack(side='left', padx=2)
        qual.bind('<<ComboboxSelected>>', self._on_quality)

        self.labels_var = tk.BooleanVar(value=bool(self.cfg.get('show_labels', True)))
        tk.Checkbutton(bar, text="Étiquettes", variable=self.labels_var,
                       command=lambda: self._draw_overlay(),
                       bg=COLORS['bg_medium'], fg=COLORS['text'], font=F_UI,
                       selectcolor=COLORS['bg_light'], activebackground=COLORS['bg_medium'],
                       activeforeground=COLORS['text'], bd=0, highlightthickness=0
                       ).pack(side='left', padx=8)

        self._mk_button(bar, "Aide", self._dlg_help).pack(side='right', padx=(3, 10), pady=4)
        self._mk_button(bar, "Réglages…", self._dlg_settings).pack(side='right', padx=3, pady=4)

    def _build_side_panel(self, parent) -> None:
        side = tk.Frame(parent, bg=COLORS['bg_medium'], width=360)
        side.pack(side='right', fill='y')
        side.pack_propagate(False)

        tk.Label(side, text="Plan du plancher", font=F_UI_B, bg=COLORS['bg_medium'],
                 fg=COLORS['text']).pack(anchor='w', padx=10, pady=(8, 2))
        self.plan = tk.Canvas(side, bg='#161616', height=330, highlightthickness=1,
                              highlightbackground=COLORS['border'])
        self.plan.pack(fill='x', padx=10)
        self.plan.bind('<Configure>', lambda e: self._draw_plan())
        self.plan.bind('<Button-1>', self._on_plan_click)
        self.plan.bind('<ButtonPress-3>', self._on_plan_press)
        self.plan.bind('<B3-Motion>', self._on_plan_drag)
        self.plan.bind('<MouseWheel>', self._on_plan_wheel)
        self.plan.bind('<Button-4>', lambda e: self._on_plan_wheel(e, +1))
        self.plan.bind('<Button-5>', lambda e: self._on_plan_wheel(e, -1))

        btns = tk.Frame(side, bg=COLORS['bg_medium'])
        btns.pack(fill='x', padx=10, pady=6)
        self._mk_button(btns, "Recadrer", self._plan_fit).pack(side='left')
        self._mk_button(btns, "◀ Retour", self.go_back).pack(side='left', padx=6)

        tk.Label(side, text="Bulle courante", font=F_UI_B, bg=COLORS['bg_medium'],
                 fg=COLORS['text']).pack(anchor='w', padx=10, pady=(6, 2))
        self.info = tk.Label(side, text="—", justify='left', anchor='nw', font=F_MONO,
                             bg=COLORS['card'], fg=COLORS['text'], padx=8, pady=6)
        self.info.pack(fill='x', padx=10)

        tk.Label(side, text="Voisins (double-clic pour y aller)", font=F_UI_B,
                 bg=COLORS['bg_medium'], fg=COLORS['text']
                 ).pack(anchor='w', padx=10, pady=(10, 2))
        wrap = tk.Frame(side, bg=COLORS['bg_medium'])
        wrap.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        sb = tk.Scrollbar(wrap, orient='vertical')
        sb.pack(side='right', fill='y')
        self.nb_list = tk.Listbox(wrap, bg=COLORS['card'], fg=COLORS['text'],
                                  font=F_MONO, activestyle='none', bd=0,
                                  highlightthickness=0, selectbackground=COLORS['accent'],
                                  yscrollcommand=sb.set)
        self.nb_list.pack(side='left', fill='both', expand=True)
        sb.config(command=self.nb_list.yview)
        self.nb_list.bind('<Double-Button-1>', self._on_nb_activate)
        self.nb_list.bind('<Return>', self._on_nb_activate)

    def _build_status(self) -> None:
        bar = tk.Frame(self, bg=COLORS['bg_medium'])
        bar.pack(fill='x', side='bottom')
        self.status = tk.Label(bar, text="Chargez un CSV puis le dossier des images.",
                               anchor='w', bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                               font=F_UI, padx=10, pady=4)
        self.status.pack(side='left', fill='x', expand=True)
        self.heading_lbl = tk.Label(bar, text="", bg=COLORS['bg_medium'],
                                    fg=COLORS['text'], font=F_MONO, padx=10)
        self.heading_lbl.pack(side='right')

    def _bind_keys(self) -> None:
        self.bind('<Left>', lambda e: self._nudge(yaw=-6))
        self.bind('<Right>', lambda e: self._nudge(yaw=+6))
        self.bind('<Up>', lambda e: self._nudge(pitch=+5))
        self.bind('<Down>', lambda e: self._nudge(pitch=-5))
        self.bind('<Shift-Left>', lambda e: self._nudge(yaw=-25))
        self.bind('<Shift-Right>', lambda e: self._nudge(yaw=+25))
        self.bind('<plus>', lambda e: self._zoom(-6))
        self.bind('<KP_Add>', lambda e: self._zoom(-6))
        self.bind('<minus>', lambda e: self._zoom(+6))
        self.bind('<KP_Subtract>', lambda e: self._zoom(+6))
        self.bind('<Return>', lambda e: self._go_forward())
        self.bind('<space>', lambda e: self._go_forward())
        self.bind('<BackSpace>', lambda e: self.go_back())
        self.bind('<Home>', lambda e: self._reset_view())
        self.bind('<F11>', lambda e: self._toggle_fullscreen())
        self.bind('<Escape>', lambda e: self.attributes('-fullscreen', False))

    # ═════════════════════════════════════════════════════════════════
    # DONNEES
    # ═════════════════════════════════════════════════════════════════
    def _post(self, fn, *args) -> None:
        """Demande l'execution d'une fonction sur le thread principal.

        Appelable depuis n'importe quel thread : rien de Tk n'est touche ici.
        """
        if not self._stop.is_set():
            self._ui_queue.put((fn, args))

    def _pump_ui(self) -> None:
        """Draine la file des threads de travail (thread principal)."""
        while True:
            try:
                fn, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as exc:
                try:
                    self._set_status(f"Erreur interne : {exc}", COLORS['error'])
                except Exception:
                    pass
        if not self._stop.is_set():
            try:
                self.after(UI_PUMP_MS, self._pump_ui)
            except Exception:
                pass

    def _set_status(self, text: str, color: str = None) -> None:
        try:
            self.status.config(text=text, fg=color or COLORS['text_muted'])
        except Exception:
            pass

    def load_csv(self, path: str, images_dir: str = '') -> bool:
        try:
            stations, warns = read_survey_csv(path)
        except Exception as exc:
            messagebox.showerror("Lecture du CSV", f"{path}\n\n{exc}")
            return False

        self.stations = stations
        self.warnings = warns
        self.csv_path = path
        self.cfg['csv_path'] = path
        self.history.clear()
        self.current = -1

        self.floors = sorted({s.floor for s in stations})
        self.floor_cb.config(values=self.floors)
        self.rebuild_graph()

        msg = f"{len(stations)} bulles · {len(self.floors)} planchers · {os.path.basename(path)}"
        if warns:
            msg += f" · {len(warns)} ligne(s) ignorée(s)"
        self._set_status(msg, COLORS['ok'] if not warns else COLORS['warning'])

        target = images_dir or self.cfg.get('images_dir', '')
        if target and os.path.isdir(target):
            self.set_images_dir(target)
        else:
            self._plan_view['fitted'] = False
            self.goto(0, keep_heading=False)
        save_config(self.cfg)
        return True

    def set_images_dir(self, path: str) -> None:
        """Indexe le dossier d'images en arriere-plan (arborescence quelconque)."""
        self.images_dir = path
        self.cfg['images_dir'] = path
        self._set_status(f"Indexation des images : {path} …")

        def work():
            paths = index_images(path)
            self._post(self._images_indexed, paths)

        threading.Thread(target=work, name='bubblenav-index', daemon=True).start()

    def _images_indexed(self, paths: Dict[str, str]) -> None:
        self.store.set_paths(paths)
        found = sum(1 for s in self.stations if self.store.has(s.photo))
        total = len(self.stations)
        color = COLORS['ok'] if found == total else COLORS['warning']
        self._set_status(f"{found}/{total} images trouvées dans « {self.images_dir} » "
                         f"({len(paths)} fichiers indexés)", color)
        save_config(self.cfg)
        if self.current < 0:
            start = next((s.idx for s in self.stations if self.store.has(s.photo)), 0)
            self._plan_view['fitted'] = False
            self.goto(start, keep_heading=False)
        else:
            self._request_render(force=True)
            self._refresh_side()
            self._draw_plan()

    def rebuild_graph(self) -> None:
        t0 = time.perf_counter()
        self.links = build_graph(self.stations, self.params)
        dt = (time.perf_counter() - t0) * 1000.0
        n_links = sum(len(v) for v in self.links)
        if self.current >= 0:
            self._refresh_side()
            self._draw_overlay()
            self._draw_plan()
        self._set_status(f"Réseau : {n_links} liens ({dt:.0f} ms)")

    def station(self) -> Optional[Station]:
        return self.stations[self.current] if 0 <= self.current < len(self.stations) else None

    # ═════════════════════════════════════════════════════════════════
    # NAVIGATION
    # ═════════════════════════════════════════════════════════════════
    def goto(self, idx: int, keep_heading: bool = True, push: bool = True) -> None:
        if not (0 <= idx < len(self.stations)) or idx == self.current:
            return
        prev = self.station()
        if keep_heading and prev is not None and self.cfg.get('keep_heading', True):
            # conserve le cap terrain : azimut vise avant -> apres
            az = self.calib.azimuth(self.view.yaw, prev.north_pct)
            self.view.yaw = self.calib.pano_yaw(az, self.stations[idx].north_pct)
        if push and self.current >= 0:
            self.history.append(self.current)
            del self.history[:-200]
        self.current = idx
        st = self.stations[idx]
        if self.floor_var.get() != st.floor:
            self.floor_var.set(st.floor)
            self._plan_view['fitted'] = False
        self._request_render(force=True)
        self._refresh_side()
        self._draw_plan()
        if idx < len(self.links):      # prechargement des voisins immediats
            self.store.prefetch([self.stations[lk.target].photo for lk in self.links[idx]])

    def go_back(self) -> None:
        if self.history:
            self.goto(self.history.pop(), keep_heading=True, push=False)

    def _go_forward(self) -> None:
        """Rejoint la pastille la plus proche du centre de la vue."""
        if not self.hotspots or self._frame_view is None:
            return
        cx, cy = self._frame_view.width / 2.0, self._frame_view.height / 2.0
        best = min(self.hotspots, key=lambda h: (h.col - cx) ** 2 + (h.row - cy) ** 2)
        self.goto(best.link.target)

    def _reset_view(self) -> None:
        self.view.pitch = PITCH_DEFAULT
        self._request_render(force=True)

    def _toggle_fullscreen(self) -> None:
        try:
            self.attributes('-fullscreen', not bool(self.attributes('-fullscreen')))
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════════════
    # RENDU
    # ═════════════════════════════════════════════════════════════════
    def _on_canvas_resize(self, event) -> None:
        self.view.width = max(64, int(event.width))
        self.view.height = max(64, int(event.height))
        self._request_render(force=True)

    def _request_render(self, force: bool = False, interactive: bool = False) -> None:
        if self.current < 0:
            return
        scale = DRAG_SCALE if interactive else 1.0
        w = max(64, int(self.view.width * scale))
        h = max(64, int(self.view.height * scale))
        rv = View(self.view.yaw, self.view.pitch, self.view.fov, w, h)
        with self._cv:
            self._req_seq += 1
            self._req = (self._req_seq, self.current, rv, scale)
            self._cv.notify()
        if interactive:
            if self._idle_job:
                self.after_cancel(self._idle_job)
            self._idle_job = self.after(IDLE_FULL_MS, self._render_full)

    def _render_full(self) -> None:
        self._idle_job = None
        self._interactive = False
        self._request_render(force=True, interactive=False)

    def _render_worker(self) -> None:
        """Thread de rendu : toujours la demande la plus recente."""
        from PIL import Image
        while not self._stop.is_set():
            with self._cv:
                while self._req is None and not self._stop.is_set():
                    self._cv.wait(0.3)
                req, self._req = self._req, None
            if req is None or self._stop.is_set():
                continue
            seq, idx, rv, scale = req
            try:
                st = self.stations[idx]
            except Exception:
                continue
            src = self.store.peek(st.photo)
            if src is None:
                if not self.store.has(st.photo):
                    self._post(self._publish_missing, seq, idx)
                    continue
                self._post(self._set_status, f"Chargement de {st.photo} …")
                src = self.store.load(st.photo)
                with self._cv:
                    superseded = self._req is not None
                if superseded:
                    continue                    # une demande plus recente existe
                if src is None:
                    self._post(self._publish_missing, seq, idx)
                    continue
            try:
                out = self.renderer.render(src, rv)
                img = Image.fromarray(out)
            except Exception as exc:
                self._post(self._set_status, f"Erreur de rendu : {exc}", COLORS['error'])
                continue
            self._post(self._publish, img, seq, rv, idx, scale)

    def _publish(self, img, seq: int, rv: View, idx: int, scale: float) -> None:
        """Affiche une image rendue (thread principal uniquement)."""
        if self._stop.is_set() or seq <= self._shown_seq or idx != self.current:
            return
        try:
            from PIL import Image, ImageTk
            if scale != 1.0 and (rv.width != self.view.width or rv.height != self.view.height):
                img = img.resize((max(1, self.view.width), max(1, self.view.height)),
                                 Image.BILINEAR)
            self._shown_seq = seq
            self._frame_view = View(rv.yaw, rv.pitch, rv.fov,
                                    self.view.width, self.view.height)
            self._tk_img = ImageTk.PhotoImage(img)
            self.canvas.delete('frame')
            self.canvas.create_image(0, 0, anchor='nw', image=self._tk_img, tags='frame')
            self.canvas.tag_lower('frame')
            self._draw_overlay()
            if scale == 1.0:
                st = self.stations[idx]
                n = len(self.links[idx]) if idx < len(self.links) else 0
                self._set_status(f"{st.locator} · {n} voisin(s) · "
                                 f"{len(self.hotspots)} pastille(s) en vue · "
                                 f"{rv.width}×{rv.height}")
        except Exception as exc:
            self._set_status(f"Affichage impossible : {exc}", COLORS['error'])

    def _publish_missing(self, seq: int, idx: int) -> None:
        """Bulle sans image : fond neutre, pastilles conservees."""
        if self._stop.is_set() or seq <= self._shown_seq or idx != self.current:
            return
        self._shown_seq = seq
        self._frame_view = View(self.view.yaw, self.view.pitch, self.view.fov,
                                self.view.width, self.view.height)
        self._tk_img = None
        self.canvas.delete('frame')
        self.canvas.create_rectangle(0, 0, self.view.width, self.view.height,
                                     fill='#181818', outline='', tags='frame')
        st = self.stations[idx]
        self.canvas.create_text(self.view.width // 2, self.view.height // 2,
                                text=f"Image introuvable\n{st.photo}",
                                fill=COLORS['warning'], font=('Segoe UI', 13), tags='frame')
        self.canvas.tag_lower('frame')
        self._draw_overlay()

    # ═════════════════════════════════════════════════════════════════
    # PASTILLES
    # ═════════════════════════════════════════════════════════════════
    def _compute_hotspots(self, view: View) -> List[Hotspot]:
        st = self.station()
        if st is None or self.current >= len(self.links):
            return []
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        f = view.focal()
        out: List[Hotspot] = []
        for lk in self.links[self.current]:
            tgt = self.stations[lk.target]
            dz = (tgt.z - eye) - st.z          # pastille posee au sol de la cible
            dh = lk.dist_h
            elev = math.degrees(math.atan2(dz, dh)) if dh > 1e-6 else (90.0 if dz > 0 else -90.0)
            psi = self.calib.pano_yaw(lk.azimuth, st.north_pct)
            pr = project(view, psi, elev)
            if pr is None:
                continue
            col, row, _ = pr
            if not (-80 <= col <= view.width + 80 and -80 <= row <= view.height + 80):
                continue
            radius = clamp(f * DISC_RADIUS_M / max(lk.dist, 0.35), DISC_PX_MIN, DISC_PX_MAX)
            out.append(Hotspot(lk, col, row, radius, tgt.locator))
        out.sort(key=lambda h: -h.link.dist)     # les plus lointaines dessinees d'abord
        return out

    def _draw_overlay(self) -> None:
        view = self._frame_view
        self.canvas.delete('hs')
        if view is None or self.current < 0:
            return
        self.hotspots = self._compute_hotspots(view)
        show_lbl = bool(self.labels_var.get())
        for i, hs in enumerate(self.hotspots):
            lk = hs.link
            color = {'same': COLORS['hot'], 'up': COLORS['hot_up'],
                     'down': COLORS['hot_down']}[lk.kind]
            missing = not self.store.has(self.stations[lk.target].photo)
            if missing:
                color = COLORS['plan_missing']
            hovered = (i == self._hover)
            r = hs.radius * (1.25 if hovered else 1.0)
            self.canvas.create_oval(hs.col - r, hs.row - r * 0.55,
                                    hs.col + r, hs.row + r * 0.55,
                                    fill=color, outline=COLORS['hot_edge'],
                                    width=2 if hovered else 1, tags='hs')
            self.canvas.create_oval(hs.col - r * 0.22, hs.row - r * 0.12,
                                    hs.col + r * 0.22, hs.row + r * 0.12,
                                    fill=COLORS['hot_edge'], outline='', tags='hs')
            if lk.kind != 'same':
                self.canvas.create_text(hs.col, hs.row - r * 0.9,
                                        text='▲' if lk.kind == 'up' else '▼',
                                        fill=color, font=('Segoe UI', 11, 'bold'), tags='hs')
            if show_lbl or hovered:
                txt = human_dist(lk.dist)
                if hovered:
                    txt = f"{hs.label} · {txt}"
                    if missing:
                        txt += " · image absente"
                ty = hs.row + r * 0.55 + 10
                self.canvas.create_text(hs.col + 1, ty + 1, text=txt, fill='#000000',
                                        font=F_UI, tags='hs')
                self.canvas.create_text(hs.col, ty, text=txt,
                                        fill='white' if hovered else '#e8e8e8',
                                        font=F_UI_B if hovered else F_UI, tags='hs')
        self._draw_hud(view)

    def _draw_hud(self, view: View) -> None:
        st = self.station()
        if st is None:
            return
        az = self.calib.azimuth(view.yaw, st.north_pct)
        self.heading_lbl.config(
            text=f"cap {az:+07.1f}°  |  site {view.pitch:+05.1f}°  |  champ {view.fov:.0f}°")
        title = f"{st.locator}   ({st.floor})"
        self.canvas.create_text(15, 13, text=title, anchor='nw', fill='#000000',
                                font=('Segoe UI', 12, 'bold'), tags='hs')
        self.canvas.create_text(14, 12, text=title, anchor='nw', fill=COLORS['hot'],
                                font=('Segoe UI', 12, 'bold'), tags='hs')
        # rose des vents : direction du nord dans la vue
        pr = project(view, self.calib.pano_yaw(0.0, st.north_pct), 0.0)
        if pr is not None:
            col, row, _ = pr
            if 0 <= col <= view.width:
                self.canvas.create_text(col, 34, text="N", fill='#ff6b6b',
                                        font=('Segoe UI', 12, 'bold'), tags='hs')
                self.canvas.create_line(col, 44, col, 56, fill='#ff6b6b', width=2, tags='hs')

    def _hotspot_at(self, x: float, y: float) -> Optional[int]:
        best, best_d = None, float('inf')
        for i, hs in enumerate(self.hotspots):
            rx = hs.radius + HIT_SLACK_PX
            ry = hs.radius * 0.55 + HIT_SLACK_PX
            dx, dy = (x - hs.col) / rx, (y - hs.row) / ry
            d = dx * dx + dy * dy
            if d <= 1.0 and d < best_d:
                best, best_d = i, d
        return best

    # ═════════════════════════════════════════════════════════════════
    # EVENEMENTS SOURIS / CLAVIER
    # ═════════════════════════════════════════════════════════════════
    def _on_press(self, event) -> None:
        self.focus_set()
        self._drag = (event.x, event.y, self.view.yaw, self.view.pitch)
        self._press_xy = (event.x, event.y)

    def _on_drag(self, event) -> None:
        if self._drag is None:
            return
        x0, y0, yaw0, pitch0 = self._drag
        deg_per_px = self.view.fov / max(1, self.view.width)
        self.view.yaw = wrap180(yaw0 - (event.x - x0) * deg_per_px)
        self.view.pitch = clamp(pitch0 + (event.y - y0) * deg_per_px,
                                PITCH_MIN, PITCH_MAX)
        self._interactive = True
        self._request_render(interactive=True)

    def _on_release(self, event) -> None:
        moved = 0
        if getattr(self, '_press_xy', None):
            moved = abs(event.x - self._press_xy[0]) + abs(event.y - self._press_xy[1])
        self._drag = None
        if moved <= 4:
            hit = self._hotspot_at(event.x, event.y)
            if hit is not None:
                self.goto(self.hotspots[hit].link.target)
                return
        if self._interactive:
            self._render_full()

    def _on_motion(self, event) -> None:
        hit = self._hotspot_at(event.x, event.y)
        if hit != self._hover:
            self._hover = hit
            self.canvas.config(cursor='hand2' if hit is not None else 'fleur')
            self._draw_overlay()

    def _on_wheel(self, event, direction: int = 0) -> None:
        step = direction if direction else (1 if getattr(event, 'delta', 0) > 0 else -1)
        self._zoom(-6 * step)

    def _on_double(self, event) -> None:
        """Double-clic dans le vide : recentre la vue sur ce point."""
        if self._hotspot_at(event.x, event.y) is not None:
            return
        view = self._frame_view or self.view
        f = view.focal()
        dx = event.x - view.width / 2.0
        dy = event.y - view.height / 2.0
        self.view.yaw = wrap180(self.view.yaw + math.degrees(math.atan2(dx, f)))
        self.view.pitch = clamp(self.view.pitch - math.degrees(math.atan2(dy, f)),
                                PITCH_MIN, PITCH_MAX)
        self._request_render(force=True)

    def _nudge(self, yaw: float = 0.0, pitch: float = 0.0) -> None:
        if yaw:
            self.view.yaw = wrap180(self.view.yaw + yaw)
        if pitch:
            self.view.pitch = clamp(self.view.pitch + pitch, PITCH_MIN, PITCH_MAX)
        self._request_render(force=True)

    def _zoom(self, delta: float) -> None:
        self.view.fov = clamp(self.view.fov + delta, FOV_MIN, FOV_MAX)
        self.fov_var.set(self.view.fov)
        self.fov_lbl.config(text=f"{self.view.fov:.0f}°")
        self.cfg['fov'] = self.view.fov
        self._request_render(interactive=True)

    def _on_fov(self, _val=None) -> None:
        self.view.fov = float(self.fov_var.get())
        self.fov_lbl.config(text=f"{self.view.fov:.0f}°")
        self.cfg['fov'] = self.view.fov
        self._request_render(interactive=True)

    def _on_quality(self, _evt=None) -> None:
        try:
            width = int(self.qual_var.get())
        except ValueError:
            return
        self.store.set_src_width(width)
        self.cfg['src_width'] = width
        save_config(self.cfg)
        self._request_render(force=True)
        st = self.station()
        if st is not None and st.idx < len(self.links):
            self.store.prefetch([self.stations[lk.target].photo
                                 for lk in self.links[st.idx]])

    def _on_floor_selected(self, _evt=None) -> None:
        floor = self.floor_var.get()
        st = self.station()
        self._plan_view['fitted'] = False
        if st is not None and st.floor != floor:
            idx = nearest_station(self.stations, st.x, st.y, floor)
            if idx is not None:
                self.goto(idx)
                return
        self._draw_plan()

    def _on_nb_activate(self, _evt=None) -> None:
        sel = self.nb_list.curselection()
        if not sel or self.current < 0:
            return
        links = self.links[self.current]
        i = sel[0]
        if 0 <= i < len(links):
            self.goto(links[i].target)

    # ═════════════════════════════════════════════════════════════════
    # PANNEAU LATERAL
    # ═════════════════════════════════════════════════════════════════
    def _refresh_side(self) -> None:
        st = self.station()
        if st is None or self.current >= len(self.links):
            return
        img_state = "présente" if self.store.has(st.photo) else "ABSENTE"
        self.info.config(text=(
            f"{st.locator}\n"
            f"photo   {st.photo}\n"
            f"image   {img_state}\n"
            f"X/Y/Z   {st.x:.2f} / {st.y:.2f} / {st.z:.2f}\n"
            f"nord    {st.north_pct:g} %\n"
            f"plancher {st.floor}"
        ))
        self.nb_list.delete(0, 'end')
        for lk in self.links[self.current]:
            tgt = self.stations[lk.target]
            mark = {'same': ' ', 'up': '▲', 'down': '▼'}[lk.kind]
            flag = '' if self.store.has(tgt.photo) else '  (img?)'
            self.nb_list.insert('end',
                                f"{mark} {tgt.locator:<12} {lk.dist:5.1f} m  "
                                f"az {lk.azimuth:+06.1f}°{flag}")

    # ═════════════════════════════════════════════════════════════════
    # PLAN (mini-carte)
    # ═════════════════════════════════════════════════════════════════
    def _plan_stations(self) -> List[Station]:
        floor = self.floor_var.get()
        return [s for s in self.stations if s.floor == floor] or self.stations

    def _plan_fit(self) -> None:
        self._plan_view['fitted'] = False
        self._draw_plan()

    def _plan_transform(self, pts: Sequence[Station], w: int, h: int):
        pv = self._plan_view
        if not pv['fitted'] or self._plan_floor != self.floor_var.get():
            xs = [s.x for s in pts]
            ys = [s.y for s in pts]
            span_x = max(1e-3, max(xs) - min(xs))
            span_y = max(1e-3, max(ys) - min(ys))
            pv['scale'] = min((w - 30) / span_x, (h - 30) / span_y)
            pv['ox'] = pv['oy'] = 0.0
            pv['cx'] = (max(xs) + min(xs)) / 2.0
            pv['cy'] = (max(ys) + min(ys)) / 2.0
            pv['fitted'] = True
            self._plan_floor = self.floor_var.get()
        s = pv['scale']
        cx, cy = pv.get('cx', 0.0), pv.get('cy', 0.0)
        ox, oy = pv['ox'], pv['oy']

        def to_screen(x: float, y: float) -> Tuple[float, float]:
            return (w / 2.0 + (x - cx) * s + ox,
                    h / 2.0 - (y - cy) * s + oy)

        def to_world(px: float, py: float) -> Tuple[float, float]:
            return (cx + (px - w / 2.0 - ox) / s,
                    cy - (py - h / 2.0 - oy) / s)

        return to_screen, to_world

    def _draw_plan(self) -> None:
        self.plan.delete('all')
        if not self.stations:
            return
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        pts = self._plan_stations()
        to_screen, _ = self._plan_transform(pts, w, h)
        floor = self.floor_var.get()

        # liens du plancher
        seen = set()
        for st in pts:
            for lk in self.links[st.idx] if self.links else []:
                if lk.kind != 'same':
                    continue
                key = (min(st.idx, lk.target), max(st.idx, lk.target))
                if key in seen:
                    continue
                seen.add(key)
                tgt = self.stations[lk.target]
                x1, y1 = to_screen(st.x, st.y)
                x2, y2 = to_screen(tgt.x, tgt.y)
                self.plan.create_line(x1, y1, x2, y2, fill=COLORS['plan_link'], width=1)

        for st in pts:
            x, y = to_screen(st.x, st.y)
            if -10 <= x <= w + 10 and -10 <= y <= h + 10:
                col = COLORS['plan_pt'] if self.store.has(st.photo) else COLORS['plan_missing']
                self.plan.create_oval(x - 2.5, y - 2.5, x + 2.5, y + 2.5,
                                      fill=col, outline='')

        cur = self.station()
        if cur is not None:
            # voisins mis en evidence
            for lk in (self.links[cur.idx] if self.links else []):
                tgt = self.stations[lk.target]
                if tgt.floor != floor:
                    continue
                x, y = to_screen(tgt.x, tgt.y)
                self.plan.create_oval(x - 4, y - 4, x + 4, y + 4,
                                      outline=COLORS['hot'], width=1)
            if cur.floor == floor:
                x, y = to_screen(cur.x, cur.y)
                view = self._frame_view or self.view
                az = self.calib.azimuth(view.yaw, cur.north_pct)
                half = view.fov / 2.0
                rad = 34.0
                plist = [x, y]
                for k in range(9):
                    a = math.radians(az - half + k * (2 * half / 8))
                    plist += [x + rad * math.sin(a), y - rad * math.cos(a)]
                self.plan.create_polygon(plist, fill=COLORS['plan_cone'], outline='',
                                         stipple='gray25')
                self.plan.create_oval(x - 5, y - 5, x + 5, y + 5,
                                      fill=COLORS['plan_here'], outline='#000000')

        self.plan.create_text(w - 16, 16, text="N", fill='#ff6b6b', font=F_UI_B)
        self.plan.create_line(w - 16, 26, w - 16, 40, fill='#ff6b6b', width=2)
        self.plan.create_text(8, h - 10, anchor='w', font=F_UI, fill=COLORS['text_muted'],
                              text=f"{len(pts)} bulles · molette: zoom · clic droit: déplacer")

    def _on_plan_click(self, event) -> None:
        pts = self._plan_stations()
        if not pts:
            return
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        to_screen, _ = self._plan_transform(pts, w, h)
        best, best_d = None, 400.0
        for st in pts:
            x, y = to_screen(st.x, st.y)
            d = (x - event.x) ** 2 + (y - event.y) ** 2
            if d < best_d:
                best, best_d = st.idx, d
        if best is not None:
            self.goto(best)

    def _on_plan_press(self, event) -> None:
        self._plan_drag = (event.x, event.y, self._plan_view['ox'], self._plan_view['oy'])

    def _on_plan_drag(self, event) -> None:
        if not self._plan_drag:
            return
        x0, y0, ox, oy = self._plan_drag
        self._plan_view['ox'] = ox + (event.x - x0)
        self._plan_view['oy'] = oy + (event.y - y0)
        self._draw_plan()

    def _on_plan_wheel(self, event, direction: int = 0) -> None:
        step = direction if direction else (1 if getattr(event, 'delta', 0) > 0 else -1)
        self._plan_view['scale'] *= (1.25 if step > 0 else 0.8)
        self._draw_plan()

    # ═════════════════════════════════════════════════════════════════
    # BOITES DE DIALOGUE
    # ═════════════════════════════════════════════════════════════════
    def _open_csv(self) -> None:
        path = filedialog.askopenfilename(
            title="CSV de relevé (Fichier photo ; X ; Y ; Z ; % NORD ; Plancher)",
            initialdir=os.path.dirname(self.csv_path or self.cfg.get('csv_path', '')) or None,
            filetypes=[("Fichiers CSV", "*.csv *.txt"), ("Tous les fichiers", "*.*")])
        if path:
            self.load_csv(path)

    def _open_images(self) -> None:
        path = filedialog.askdirectory(
            title="Dossier des images bulles (exploration récursive)",
            initialdir=self.images_dir or self.cfg.get('images_dir', '') or None)
        if path:
            self.set_images_dir(path)

    def _dlg_settings(self) -> None:
        win = tk.Toplevel(self)
        win.title("Réglages")
        win.configure(bg=COLORS['bg_dark'])
        win.transient(self)
        win.resizable(False, False)

        def section(title: str) -> tk.Frame:
            tk.Label(win, text=title, font=F_UI_B, bg=COLORS['bg_dark'],
                     fg=COLORS['accent']).pack(anchor='w', padx=12, pady=(12, 2))
            frame = tk.Frame(win, bg=COLORS['bg_dark'])
            frame.pack(fill='x', padx=12)
            return frame

        def slider(parent, label, var, lo, hi, res, cmd):
            row = tk.Frame(parent, bg=COLORS['bg_dark'])
            row.pack(fill='x', pady=1)
            tk.Label(row, text=label, width=22, anchor='w', font=F_UI,
                     bg=COLORS['bg_dark'], fg=COLORS['text']).pack(side='left')
            sc = tk.Scale(row, from_=lo, to=hi, resolution=res, orient='horizontal',
                          variable=var, command=cmd, length=230,
                          bg=COLORS['bg_dark'], fg=COLORS['text'],
                          troughcolor=COLORS['bg_light'], highlightthickness=0, bd=0,
                          sliderrelief='flat', activebackground=COLORS['accent'])
            sc.pack(side='left')
            return sc

        # ── Calibration azimut ──────────────────────────────────────
        cal = section("Calibration de l'azimut (effet immédiat sur les pastilles)")
        mode_var = tk.StringVar(value=self.calib.mode)
        sense_var = tk.IntVar(value=self.calib.sense)
        off_var = tk.DoubleVar(value=self.calib.offset)
        eye_var = tk.DoubleVar(value=float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)))

        def apply_calib(_=None):
            self.calib.mode = mode_var.get()
            self.calib.sense = 1 if sense_var.get() >= 0 else -1
            self.calib.offset = float(off_var.get())
            self.cfg['north_mode'] = self.calib.mode
            self.cfg['north_sense'] = self.calib.sense
            self.cfg['north_offset'] = self.calib.offset
            self.cfg['eye_height'] = float(eye_var.get())
            self._draw_overlay()
            self._draw_plan()

        for text, value in (("« % NORD » = colonne du nord dans l'image (50 % = centre)", 'colonne'),
                            ("« % NORD » = azimut visé par le centre de l'image", 'centre')):
            tk.Radiobutton(cal, text=text, variable=mode_var, value=value,
                           command=apply_calib, font=F_UI, anchor='w',
                           bg=COLORS['bg_dark'], fg=COLORS['text'],
                           selectcolor=COLORS['bg_light'], activebackground=COLORS['bg_dark'],
                           activeforeground=COLORS['text'], bd=0, highlightthickness=0
                           ).pack(fill='x')
        srow = tk.Frame(cal, bg=COLORS['bg_dark'])
        srow.pack(fill='x', pady=(4, 0))
        tk.Label(srow, text="Sens des azimuts", width=22, anchor='w', font=F_UI,
                 bg=COLORS['bg_dark'], fg=COLORS['text']).pack(side='left')
        for text, value in (("horaire (standard)", 1), ("anti-horaire (image miroir)", -1)):
            tk.Radiobutton(srow, text=text, variable=sense_var, value=value,
                           command=apply_calib, font=F_UI,
                           bg=COLORS['bg_dark'], fg=COLORS['text'],
                           selectcolor=COLORS['bg_light'], activebackground=COLORS['bg_dark'],
                           activeforeground=COLORS['text'], bd=0, highlightthickness=0
                           ).pack(side='left', padx=4)
        slider(cal, "Correction nord (°)", off_var, -180, 180, 0.5, apply_calib)
        slider(cal, "Hauteur caméra (m)", eye_var, 0.0, 3.0, 0.05, apply_calib)

        # ── Reseau ──────────────────────────────────────────────────
        net = section("Réseau de navigation")
        rad_var = tk.DoubleVar(value=self.params.radius)
        kmax_var = tk.IntVar(value=self.params.kmax)
        ang_var = tk.DoubleVar(value=self.params.ang_min)
        fr_var = tk.DoubleVar(value=self.params.floor_radius)
        pending = {'job': None}

        def apply_graph(_=None):
            if pending['job']:
                self.after_cancel(pending['job'])

            def run():
                pending['job'] = None
                self.params.radius = float(rad_var.get())
                self.params.kmax = int(kmax_var.get())
                self.params.ang_min = float(ang_var.get())
                self.params.floor_radius = float(fr_var.get())
                self.cfg.update({'radius': self.params.radius, 'kmax': self.params.kmax,
                                 'ang_min': self.params.ang_min,
                                 'floor_radius': self.params.floor_radius})
                self.rebuild_graph()

            pending['job'] = self.after(220, run)

        slider(net, "Portée des liens (m)", rad_var, 2, 40, 0.5, apply_graph)
        slider(net, "Pastilles max", kmax_var, 1, 24, 1, apply_graph)
        slider(net, "Séparation angulaire (°)", ang_var, 0, 60, 1, apply_graph)
        slider(net, "Portée inter-plancher (m)", fr_var, 0, 20, 0.5, apply_graph)

        # ── Performance ─────────────────────────────────────────────
        perf = section("Performance")
        cache_var = tk.IntVar(value=self.store.cache_size)

        def apply_cache(_=None):
            self.store.set_cache_size(int(cache_var.get()))
            self.cfg['cache_size'] = int(cache_var.get())

        slider(perf, "Bulles en mémoire", cache_var, 3, 60, 1, apply_cache)
        keep_var = tk.BooleanVar(value=bool(self.cfg.get('keep_heading', True)))
        tk.Checkbutton(perf, text="Conserver le cap en changeant de bulle",
                       variable=keep_var, font=F_UI, anchor='w',
                       command=lambda: self.cfg.__setitem__('keep_heading', keep_var.get()),
                       bg=COLORS['bg_dark'], fg=COLORS['text'], selectcolor=COLORS['bg_light'],
                       activebackground=COLORS['bg_dark'], activeforeground=COLORS['text'],
                       bd=0, highlightthickness=0).pack(fill='x', pady=(4, 0))

        foot = tk.Frame(win, bg=COLORS['bg_dark'])
        foot.pack(fill='x', padx=12, pady=12)

        def close():
            apply_calib()
            save_config(self.cfg)
            win.destroy()

        self._mk_button(foot, "Fermer", close, bg=COLORS['accent']).pack(side='right')
        if self.warnings:
            self._mk_button(foot, f"Voir les {len(self.warnings)} avertissement(s) CSV",
                            self._dlg_warnings).pack(side='left')
        win.bind('<Escape>', lambda e: close())

    def _dlg_warnings(self) -> None:
        messagebox.showwarning("Lignes CSV ignorées",
                               '\n'.join(self.warnings[:40]) +
                               ('\n…' if len(self.warnings) > 40 else ''))

    def _dlg_help(self) -> None:
        messagebox.showinfo(f"{APP_NAME} v{__version__}", (
            "NAVIGATION\n"
            "  • Clic sur une pastille  : aller sur cette bulle\n"
            "  • Glisser                : tourner la vue\n"
            "  • Molette / + −          : champ de vision\n"
            "  • Double-clic            : recentrer la vue\n"
            "  • Entrée ou Espace       : avancer vers la pastille centrale\n"
            "  • Retour arrière         : revenir à la bulle précédente\n"
            "  • Flèches                : tourner (Maj = pas large)\n"
            "  • Origine (Home)         : redresser la vue\n"
            "  • F11 / Échap            : plein écran\n\n"
            "PLAN\n"
            "  • Clic gauche            : aller sur la bulle la plus proche\n"
            "  • Molette                : zoom · clic droit glissé : déplacer\n"
            "  • Liste « Plancher »     : changer de niveau (bulle la plus proche)\n\n"
            "PASTILLES\n"
            "  jaune = même plancher · bleu ▲ = niveau au-dessus\n"
            "  violet ▼ = niveau en dessous · rouge sombre = image absente\n\n"
            "Si les pastilles ne tombent pas au bon endroit, ouvrez « Réglages… »\n"
            "et ajustez la calibration de l'azimut (effet immédiat)."
        ))

    # ═════════════════════════════════════════════════════════════════
    # FERMETURE
    # ═════════════════════════════════════════════════════════════════
    def _on_close(self) -> None:
        try:
            self.cfg['fov'] = self.view.fov
            self.cfg['show_labels'] = bool(self.labels_var.get())
            save_config(self.cfg)
        except Exception:
            pass
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        try:
            self.store.close()
        except Exception:
            pass
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# DEPENDANCES
# ─────────────────────────────────────────────────────────────────────────────

def ensure_deps(interactive: bool = True) -> None:
    """Installe silencieusement Pillow / OpenCV / numpy si absents."""
    import subprocess
    deps = {'PIL': 'Pillow', 'cv2': 'opencv-python', 'numpy': 'numpy'}
    missing = []
    for mod, pkg in deps.items():
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print(f"[{APP_NAME}] installation de : {', '.join(missing)}")
    splash = None
    if interactive and _TK_OK:
        try:
            splash = tk.Tk()
            splash.title(APP_NAME)
            tk.Label(splash, text="Installation des composants manquants…\n"
                                  + ', '.join(missing), padx=30, pady=20).pack()
            splash.update()
        except Exception:
            splash = None
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--quiet'] + missing)
    except Exception as exc:
        msg = f"Impossible d'installer : {', '.join(missing)}\n{exc}"
        if splash is not None:
            messagebox.showerror(APP_NAME, msg)
        print(msg, file=sys.stderr)
        sys.exit(1)
    finally:
        if splash is not None:
            splash.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# AUTO-VERIFICATIONS (sans interface)
# ─────────────────────────────────────────────────────────────────────────────

def selftest(csv_path: str = '') -> int:
    """Verifie la geometrie, la lecture CSV, le reseau et les performances.

    Retourne 0 si tout passe, 1 sinon. Aucune fenetre n'est ouverte.
    """
    import numpy as np

    failures: List[str] = []

    def check(name: str, ok: bool, detail: str = '') -> None:
        print(f"  [{'OK ' if ok else 'ECHEC'}] {name}" + (f" — {detail}" if detail else ''))
        if not ok:
            failures.append(name)

    print(f"{APP_NAME} v{__version__} — auto-vérifications\n")

    # 1. Angles
    print("1) Angles et calibration")
    check("wrap180", all(abs(wrap180(a) - b) < 1e-9 for a, b in
                         ((0, 0), (180, 180), (-180, 180), (190, -170), (540, 180), (-190, 170))))
    max_err = 0.0
    for mode in ('colonne', 'centre'):
        for sense in (1, -1):
            cal = Calib(mode, sense, 17.5)
            for pct in (0.0, 25.0, 50.0, 87.5, 100.0):
                for az in range(-180, 180, 7):
                    psi = cal.pano_yaw(az, pct)
                    back = cal.azimuth(psi, pct)
                    max_err = max(max_err, abs(wrap180(back - az)))
    check("azimut <-> cap panorama (aller-retour)", max_err < 1e-9, f"erreur max {max_err:.2e}°")

    cal = Calib('colonne', 1, 0.0)
    check("nord au centre quand % NORD = 50",
          abs(cal.pano_yaw(0.0, 50.0)) < 1e-9)
    check("est à +90° de l'image quand % NORD = 50",
          abs(cal.pano_yaw(90.0, 50.0) - 90.0) < 1e-9)
    check("% NORD = 25 décale le nord d'un quart de tour",
          abs(cal.pano_yaw(0.0, 25.0) + 90.0) < 1e-9)

    # 2. Azimut / elevation
    print("\n2) Azimut et élévation")
    az, el, d = azimuth_elev(0.0, 10.0, 0.0)
    check("nord pur -> azimut 0", abs(az) < 1e-9 and abs(d - 10) < 1e-9)
    az, el, d = azimuth_elev(10.0, 0.0, 0.0)
    check("est pur -> azimut 90", abs(az - 90.0) < 1e-9)
    az, el, d = azimuth_elev(0.0, -10.0, 10.0)
    check("sud + montée -> azimut 180, site 45",
          abs(abs(az) - 180.0) < 1e-9 and abs(el - 45.0) < 1e-9)

    # 3. Projection vs rendu reel (chaine complete)
    print("\n3) Cohérence projection ↔ rendu (image de synthèse)")
    renderer = PanoRenderer()
    sw, sh = 1024, 512
    worst = 0.0
    for (yaw, pitch, fov, psi, elev) in (
            (0, 0, 90, 12.0, 5.0), (30, -15, 100, 55.0, -20.0),
            (-120, 25, 70, -95.0, 30.0), (170, -40, 120, 155.0, -35.0),
            (75, 0, 60, 60.0, 0.0)):
        src = np.zeros((sh, sw, 3), dtype=np.uint8)
        u = (psi + 180.0) / 360.0 * sw
        v = (0.5 - elev / 180.0) * sh
        yy, xx = np.mgrid[0:sh, 0:sw]
        dx = np.minimum(np.abs(xx - u), sw - np.abs(xx - u))
        blob = np.exp(-((dx ** 2 + (yy - v) ** 2) / 8.0)) * 255.0
        src[..., 0] = blob.astype(np.uint8)

        view = View(yaw, pitch, fov, 640, 400)
        out = renderer.render(src, view)
        pred = project(view, psi, elev)
        found = np.unravel_index(int(np.argmax(out[..., 0])), out.shape[:2])
        if pred is None:
            check(f"projection (yaw={yaw}, pitch={pitch})", False, "direction jugée hors champ")
            continue
        err = math.hypot(pred[0] - found[1], pred[1] - found[0])
        worst = max(worst, err)
        check(f"pastille yaw={yaw:>4} pitch={pitch:>3} fov={fov:>3}", err < 2.0,
              f"écart {err:.2f} px")
    check("écart maximal projection/rendu < 2 px", worst < 2.0, f"{worst:.2f} px")

    behind = project(View(0, 0, 90, 640, 400), 179.0, 0.0)
    check("direction opposée rejetée", behind is None)

    # 4. CSV + reseau
    print("\n4) Lecture CSV et réseau")
    if not csv_path:
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = sorted(f for f in os.listdir(here) if f.lower().endswith('.csv'))
        csv_path = os.path.join(here, candidates[0]) if candidates else ''
    if csv_path and os.path.isfile(csv_path):
        t0 = time.perf_counter()
        stations, warns = read_survey_csv(csv_path)
        t_csv = (time.perf_counter() - t0) * 1000
        check(f"lecture de {os.path.basename(csv_path)}", len(stations) > 0,
              f"{len(stations)} bulles, {len(warns)} ignorée(s), {t_csv:.0f} ms")

        t0 = time.perf_counter()
        links = build_graph(stations, GraphParams())
        t_graph = (time.perf_counter() - t0) * 1000
        n_links = sum(len(v) for v in links)
        isolated = [s.locator for s, lk in zip(stations, links) if not lk]
        check("réseau construit", n_links > 0,
              f"{n_links} liens, {n_links / len(stations):.1f} par bulle, {t_graph:.0f} ms")
        check("aucune bulle isolée", not isolated,
              f"{len(isolated)} isolée(s) : {', '.join(isolated[:5])}" if isolated else '')

        # reciprocite : un voisin proche doit se voir des deux cotes
        recip = sum(1 for i, lks in enumerate(links) for lk in lks
                    if lk.kind == 'same' and lk.dist < 4.0
                    and any(b.target == i for b in links[lk.target]))
        total_close = sum(1 for lks in links for lk in lks
                          if lk.kind == 'same' and lk.dist < 4.0)
        ratio = recip / total_close if total_close else 0.0
        check("liens proches réciproques", ratio > 0.85, f"{ratio * 100:.0f} %")

        # elagage angulaire respecte
        bad = 0
        for lks in links:
            same = [lk for lk in lks if lk.kind == 'same']
            for a in range(len(same)):
                for b in range(a + 1, len(same)):
                    if abs(wrap180(same[a].azimuth - same[b].azimuth)) < GraphParams().ang_min - 1e-6:
                        bad += 1
        check("une seule pastille par direction", bad == 0, f"{bad} conflit(s)")

        floors = sorted({s.floor for s in stations})
        inter = sum(1 for lks in links for lk in lks if lk.kind != 'same')
        check("liaisons inter-planchers présentes", inter > 0 or len(floors) == 1,
              f"{inter} liens sur {len(floors)} planchers")
    else:
        print("  (aucun CSV trouvé à côté du script — étape ignorée)")

    # 5. Performances de rendu
    print("\n5) Performances de rendu (source 4096×2048)")
    src = (np.random.default_rng(0).random((2048, 4096, 3)) * 255).astype(np.uint8)
    renderer.clear()
    for w, h, label in ((1600, 900, 'pleine résolution'), (800, 450, 'pendant rotation')):
        view = View(10.0, -10.0, FOV_DEFAULT, w, h)
        renderer.render(src, view)                    # amorce les caches
        t0 = time.perf_counter()
        n = 8
        for k in range(n):
            renderer.render(src, View(10.0 + k * 1.3, -10.0, FOV_DEFAULT, w, h))
        dt = (time.perf_counter() - t0) / n * 1000
        check(f"rendu {w}×{h} ({label})", dt < 120.0, f"{dt:.1f} ms/image ≈ {1000/dt:.0f} i/s")

    print("\n" + ("Toutes les vérifications passent." if not failures
                  else f"{len(failures)} échec(s) : " + ', '.join(failures)))
    return 0 if not failures else 1


# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTREE
# ─────────────────────────────────────────────────────────────────────────────

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog='BubbleNav_XPhase.py',
        description="Navigateur de bulles géoréférencées (CSV + panoramas).")
    parser.add_argument('--csv', default='', help="CSV de relevé à ouvrir")
    parser.add_argument('--images', default='', help="dossier des images bulles")
    parser.add_argument('--selftest', action='store_true',
                        help="vérifications internes, sans interface")
    args = parser.parse_args(argv)

    if args.selftest:
        ensure_deps(interactive=False)
        return selftest(args.csv)

    if not _TK_OK:
        print("Tkinter est absent de cette installation Python — interface impossible.\n"
              "Sous Windows, réinstallez Python en cochant « tcl/tk ».", file=sys.stderr)
        return 2

    ensure_deps(interactive=True)
    cfg = load_config()

    csv_path = args.csv or cfg.get('csv_path', '')
    if csv_path and not os.path.isfile(csv_path):
        csv_path = ''
    images_dir = args.images or cfg.get('images_dir', '')
    if images_dir and not os.path.isdir(images_dir):
        images_dir = ''

    if not csv_path:
        root = tk.Tk()
        root.withdraw()
        csv_path = filedialog.askopenfilename(
            title="CSV de relevé (Fichier photo ; X ; Y ; Z ; % NORD ; Plancher)",
            filetypes=[("Fichiers CSV", "*.csv *.txt"), ("Tous les fichiers", "*.*")])
        root.destroy()
        if not csv_path:
            return 0
    if not images_dir:
        root = tk.Tk()
        root.withdraw()
        images_dir = filedialog.askdirectory(
            title="Dossier des images bulles (laisser vide pour le choisir plus tard)")
        root.destroy()

    app = BubbleNavApp(cfg, csv_path, images_dir)
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
