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
import fnmatch
import itertools
import json
import math
import queue
import re
import sys
import threading
import time
import unicodedata
from collections import OrderedDict, defaultdict
from datetime import datetime
from dataclasses import dataclass, field, replace as dc_replace
from functools import lru_cache
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
FOV_MIN, FOV_MAX = 30.0, 200.0
# Au-delà de 110°, la perspective normale étire trop les bords : on passe
# progressivement à une projection stéréographique (pleine à 160°), qui garde
# les formes et permet de voir bien plus large, jusqu'à 200°.
WIDE_START, WIDE_FULL = 110.0, 160.0
FOV_DEFAULT = 105.0            # vue large demandee
FOV_SNAP = 120.0               # cran aimanté du champ (bouton « 120° »)
FOV_SNAP_TOL = 5.0
# Ce que l'on voit : du plus serré au plus large
SCOPE_LABELS = {'local': "Local", 'voisins': "Locaux voisins",
                'distance': "Distance", 'plancher': "Plancher entier"}
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
MEMORY_BUDGET_MB = 1100        # enveloppe memoire visee pour le cache
JPEG_QUALITY_FALLBACK = 95     # si les tables de la source sont illisibles
PREFETCH_WORKERS = 4

# Reseau
RADIUS_DEFAULT = 12.0          # m — portee max d'un lien
KMAX_DEFAULT = 8               # nb max de pastilles par bulle
ANG_MIN_DEFAULT = 25.0         # deg — separation angulaire mini entre pastilles
FLOOR_RADIUS_DEFAULT = 5.0     # m — portee horizontale d'un lien inter-plancher
FLOOR_DZ_MAX = 12.0            # m — denivele max d'un lien inter-plancher
EYE_HEIGHT_DEFAULT = 1.65      # m — hauteur de la camera au-dessus du sol

# Pastilles
DISC_RADIUS_M = 0.32           # m — rayon physique de la pastille au sol
                               # (le rayon a l'ecran vaut f x rayon / distance)
DISC_PX_MIN, DISC_PX_MAX = 10.0, 36.0   # bornes d'affichage (px) : jamais
                               # minuscule (donc cliquable), jamais envahissante
DISC_PX_LIMITS = (5.0, 90.0)   # bornes admises pour le reglage utilisateur
HIT_SLACK_PX = 10.0            # tolerance de clic autour de la pastille

PLAN_H = 250                   # hauteur du plan (px)
PLAN_H_EDIT = 190              # reduite en mode edition, pour loger le panneau

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
    'edit': '#ff9f43',         # mode edition / bulle modifiee
    'sel': '#00e5ff',          # cible d'edition
    'tip_bg': '#0d0d0d',
}

F_UI = ('Segoe UI', 9)
F_UI_B = ('Segoe UI', 9, 'bold')
F_TITLE = ('Segoe UI', 11, 'bold')
F_MONO = ('Consolas', 9)
F_TINY = ('Segoe UI', 8)
F_TINY_B = ('Segoe UI', 8, 'bold')
MARK_TEXT = '#d9dee2'          # étiquettes discrètes des pastilles


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
    'show_names': True,        # nom de la station au-dessus des pastilles
    'show_heights': True,      # hauteur appareil / delta / altitude sous les pastilles
    'keep_heading': True,
    'disc_radius': DISC_RADIUS_M,
    'disc_min_px': DISC_PX_MIN,
    'disc_max_px': DISC_PX_MAX,
    'disc_3d': True,           # pastilles en relief (sphere ombree)
    'filter_active': False,
    'filter_floor': 'tous',
    'filter_dist': 0.0,
    'filter_local': '',
    'filter_inter': True,
    'filter_hide_missing': False,
    'filter_same_local': False,
    'export_workers': 2,       # panoramas 16000x8000 : ~800 Mo par tache
    'corr_paths': {},          # releve -> fichier de corrections choisi
    'viewer_geometry': '',     # taille/position du visualiseur hors plein ecran
    'viewer_start': 'plein écran',   # 'plein écran' | 'maximisé' | 'mémorisé'
    'plan_links': 'squelette',       # réseau du plan : 'squelette' | 'complet' | 'aucun'
    'drag_axis': 'auto',             # axe du glisser en édition : 'auto' | 'x' | 'y' | 'z'
    'drag_z': 'ddelta',              # l'axe Z agit sur 'ddelta' (sol + caméra) ou 'dh'
    'bubble_anchor': 'vue',          # bulle au point de 'vue' (appareil, mât, ombre) ou au 'sol'
    'hotspots_mode': 'plancher',     # 'plancher' : toutes les bulles du plancher ;
                                     # 'reseau' : réseau élagué (portée, nombre, direction)
    'color_mode': 'local',           # couleur des pastilles : 'local' ou 'lien'
    'show_mire': True,               # mire de hauteur sur les bulles comparées
    'show_tooltip': True,            # infobulle au survol des pastilles
    'wheel_step': 0.05,              # pas de la molette pour H / Δ (m) : 0,05 ou 0,01
    'view_scope': 'voisins',         # pastilles : 'local' | 'voisins' | 'distance' | 'plancher'
    'scope_dist': 6.0,               # distance de voisinage (m) : local + second plan
    'focus_origin': False,           # à l'arrivée, regarder la bulle d'où l'on vient
    'csv_mappings': {},        # format de CSV -> correspondance de colonnes choisie
}

# clic droit (bouton 2 sous macOS)
RIGHT_CLICK = ('<Button-2>', '<Button-3>') if sys.platform == 'darwin' else ('<Button-3>',)
PLAN_LINK_MODES = ('squelette', 'complet', 'aucun')
VIEWER_START_MODES = ('plein écran', 'maximisé', 'mémorisé')


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
                elif isinstance(ref, dict) and isinstance(v, dict):
                    cfg[k] = {str(a): str(b) for a, b in v.items()
                              if isinstance(b, str)}
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
class NameParts:
    """Découpage du nom de fichier photo.

    Convention observée : CP1_GRA_TR6_BK_02_K256_20260416_01
                          campagne_site_tranche_ouvrage_étage_local_date_index
    L'analyse s'ancre sur la date (8 chiffres) : elle reste juste même si le
    nombre de segments de tête change d'un chantier à l'autre.
    """
    campagne: str = ''
    site: str = ''
    tranche: str = ''
    ouvrage: str = ''
    etage: str = ''
    local: str = ''
    date: str = ''
    index: str = ''
    reste: Tuple[str, ...] = ()
    reconnu: bool = False
    niveau_local: str = ''   # niveau selon le n° du local : chiffre des centaines
    etage_deduit: bool = False   # étage tiré du n° de local faute de plancher

    def date_lisible(self) -> str:
        d = self.date
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d

    def locator(self) -> str:
        return f"{self.local}_{self.index}" if self.local and self.index else self.local

    def anomalies(self) -> List[str]:
        """Champs attendus mais absents du nom de fichier."""
        manque = []
        if not self.local:
            manque.append('local')
        if not self.index:
            manque.append('index')
        if not self.date:
            manque.append('date')
        if not self.etage:
            manque.append('étage')
        return manque

    def lignes(self) -> List[Tuple[str, str]]:
        """Champs renseignés, prêts à afficher (libellé, valeur)."""
        out = [('campagne', self.campagne), ('site', self.site),
               ('tranche', self.tranche), ('ouvrage', self.ouvrage),
               ('étage', self.etage), ('local', self.local),
               ('index', self.index), ('prise de vue', self.date_lisible())]
        if self.reste:
            out.append(('autres', ' '.join(self.reste)))
        return [(k, v) for k, v in out if v]


_DATE_RE = re.compile(r'^(?:19|20)\d{6}$')
_NUM_RE = re.compile(r'^\d{1,3}$')


@lru_cache(maxsize=8192)
def parse_photo_name(photo: str) -> NameParts:
    """Extrait étage / local / index / date d'un nom de fichier photo.

    Tolérant : un nom hors convention renvoie ce qui a pu être reconnu, avec
    `reconnu = False`, sans jamais lever.
    """
    toks = [t for t in str(photo or '').split('_') if t]
    if not toks:
        return NameParts()
    date_at = next((i for i in range(len(toks) - 1, -1, -1)
                    if _DATE_RE.match(toks[i])), -1)
    if date_at >= 0:
        date = toks[date_at]
        after = toks[date_at + 1:]
        index = after[0] if after and _NUM_RE.match(after[0]) else (after[0] if after else '')
        before = toks[:date_at]
        local = before[-1] if before else ''
        has_etage = len(before) >= 2 and _NUM_RE.match(before[-2])
        etage = before[-2] if has_etage else ''
        head = before[:-2] if has_etage else before[:-1]
        reste = tuple(after[1:])
    else:                                    # sans date : on se rabat sur la fin
        index = toks[-1] if _NUM_RE.match(toks[-1]) else ''
        local = toks[-2] if index and len(toks) >= 2 else (toks[-1] if not index else '')
        head = toks[:-2] if index and len(toks) >= 2 else toks[:-1]
        etage = head[-1] if head and _NUM_RE.match(head[-1]) else ''
        if etage:
            head = head[:-1]
        date, reste = '', ()
    if local and local.isdigit():                # « 0349 » n'est pas un local
        local = ''
    reconnu = bool(local and index)
    champs = ('campagne', 'site', 'tranche', 'ouvrage')
    valeurs = {champs[i]: head[i] for i in range(min(len(head), len(champs)))}
    return NameParts(etage=etage, local=local, date=date, index=index,
                     reste=tuple(head[len(champs):]) + reste, reconnu=reconnu,
                     **valeurs)


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
    yaw_fix: float = 0.0   # correction d'orientation, PORTEE PAR LE CSV (deg)
    ox: float = 0.0        # valeurs lues dans le CSV (reference pour annuler)
    oy: float = 0.0
    oz: float = 0.0
    oyaw: float = 0.0
    # Altitude : Z camera = altitude du plancher + hauteur appareil + delta.
    #   h0     : hauteur appareil lue dans le CSV (None = reglage global)
    #   delta0 : decalage local du sol lu dans le CSV (marche, faux plancher)
    #   dh     : correction « hauteur station »  -> la camera bouge, le sol reste
    #   ddelta : correction « delta plancher »   -> camera ET sol bougent
    h0: Optional[float] = None
    delta0: float = 0.0
    delta_col: bool = False     # True si le delta vient d'une colonne du CSV
    floor_alt: Optional[float] = None   # altitude du plancher (colonne, sinon libellé)
    floor_alt_src: str = ''     # 'colonne' | 'libellé' | ''
    z_csv: Optional[float] = None       # Z lu dans le CSV (repli sans altitude de plancher)
    dh: float = 0.0
    ddelta: float = 0.0
    key: str = ''          # cle immuable (numero de scan) ; = photo si absente
    key_explicit: bool = False   # True si la cle vient d'une colonne du CSV
    target: str = ''       # nom projete selon la convention, s'il est fourni
    attrs: Dict[str, str] = field(default_factory=dict)   # local/etage/date/index
    _parts: Optional[NameParts] = field(default=None, repr=False, compare=False)

    def height(self, eye: float = EYE_HEIGHT_DEFAULT) -> float:
        """Hauteur de l'appareil au-dessus du sol local, correction comprise."""
        return (self.h0 if self.h0 is not None else eye) + self.dh

    def ground(self, eye: float = EYE_HEIGHT_DEFAULT) -> float:
        """Altitude du sol sous la station (là où se pose la pastille)."""
        return self.z - self.height(eye)

    def delta(self, eye: float = EYE_HEIGHT_DEFAULT) -> float:
        """Décalage du sol local par rapport au plancher, correction comprise.

        Sans colonne delta dans le relevé, il se déduit du modèle
        Z caméra = altitude du plancher + hauteur appareil + delta, avec
        l'altitude lue dans le libellé du plancher (« PLANCHER 02 (+00.00m) »).
        """
        if not self.delta_col and self.floor_alt is not None:
            # avec Z calculé, oz = plancher + H : le delta se réduit à sa correction
            return self.z - self.floor_alt - self.height(eye)
        return self.delta0 + self.ddelta

    def moved(self, tol: float = 1e-4) -> bool:
        """Position en plan différente de celle lue dans le CSV (2D)."""
        return abs(self.x - self.ox) > tol or abs(self.y - self.oy) > tol

    def raised(self, tol: float = 1e-4) -> bool:
        """Hauteur de station corrigée."""
        return abs(self.dh) > tol

    def shifted(self, tol: float = 1e-4) -> bool:
        """Delta plancher corrigé."""
        return abs(self.ddelta) > tol

    def z_changed(self, tol: float = 1e-4) -> bool:
        return self.raised(tol) or self.shifted(tol)

    def turned(self, tol: float = 1e-4) -> bool:
        """Orientation différente de celle lue dans le CSV."""
        return abs(self.yaw_fix - self.oyaw) > tol

    def has_yaw(self, tol: float = 1e-4) -> bool:
        """Porte une correction d'orientation non encore appliquée à l'image."""
        return abs(self.yaw_fix) > tol

    def modified(self) -> bool:
        """Modifiée depuis la lecture du CSV (donc non enregistrée)."""
        return self.moved() or self.z_changed() or self.turned()

    def parts(self) -> NameParts:
        """Attributs de la bulle : nom projeté s'il existe, sinon nom de la
        photo ; les colonnes explicites du CSV l'emportent sur l'analyse."""
        if self._parts is None:
            base = parse_photo_name(self.target or self.photo)
            import dataclasses
            # Num scan sans nom projeté : le locator (« R110b_01 ») donne local et
            # index, le libellé de plancher (« PLANCHER 01 (-03.50m) ») l'étage.
            if not base.local and self.locator and self.locator != self.photo:
                m = re.match(r'^(.+?)[_-](\d{1,3})$', self.locator.strip())
                if m and not m.group(1).isdigit():
                    base = dataclasses.replace(base, local=m.group(1), index=m.group(2))
            if not base.etage and self.floor:
                m = re.search(r'(?:plancher|niveau|etage|étage|level|floor)\s*(-?\d{1,3})',
                              self.floor, re.I)
                if m:
                    base = dataclasses.replace(base, etage=m.group(1))
            # Niveau d'après le numéro du local (R712 -> 7, K058 -> 0). Il sert
            # de repli quand le plancher n'est pas renseigné ; sinon le plancher
            # fait foi (un local peut s'étendre sur le niveau supérieur).
            if base.local:
                m = re.search(r'(\d{3,})', base.local)
                if m:
                    niveau = f"{int(m.group(1)) // 100:02d}"
                    base = dataclasses.replace(base, niveau_local=niveau)
                    if not base.etage:
                        base = dataclasses.replace(base, etage=niveau, etage_deduit=True)
            if base.local and base.index:
                base = dataclasses.replace(base, reconnu=True)
            if self.attrs:
                base = dataclasses.replace(base, **{k: v for k, v in self.attrs.items()
                                                    if v and hasattr(base, k)})
                if base.local and base.index:
                    base.reconnu = True
            self._parts = base
        return self._parts

    def label(self) -> str:
        """Nom lisible : locator, ou nom projeté, ou clé."""
        return self.locator or self.target or self.key or self.photo

    def name_candidates(self) -> List[str]:
        """Noms de fichier plausibles sur disque, du plus sûr au moins sûr."""
        out: List[str] = []
        for n in (self.photo, self.key, self.target):
            n = (n or '').strip()
            if not n:
                continue
            out.append(n)
            if n.isdigit():                      # 347 / 0347 / 00347
                out += [n.zfill(4), n.zfill(5), n.lstrip('0') or '0']
        seen, uniq = set(), []
        for n in out:
            if n.lower() not in seen:
                seen.add(n.lower())
                uniq.append(n)
        return uniq


COL_ALIASES = {
    'photo': ('fichierphoto', 'nomdufichier', 'nomfichier', 'fichierimage',
              'nomdelaphoto', 'fichier', 'photo', 'image', 'nomimage',
              'nomphoto', 'filename', 'file', 'name'),
    'locator': ('nomdulocator', 'locator', 'nomlocator', 'station', 'point',
                'nomdupoint', 'nom', 'id'),
    'x': ('x', 'e', 'est', 'easting', 'xm', 'coordx'),
    'y': ('y', 'n', 'nord', 'northing', 'ym', 'coordy'),
    # Z du point de vue. Un « Z corrigé / final / appareil » passe avant un Z
    # nu : dans ce cas le Z nu est l'altitude du plancher (voir 'zfloor').
    'z': ('zcorrige', 'zcorriger', 'zcorr', 'zfinal', 'zappareil', 'zcamera',
          'zinstrument', 'zpointdevue', 'zpdv', 'zstation',
          'z', 'altitude', 'alt', 'elevation', 'zm', 'coordz'),
    'hcam': ('hauteurappareil', 'hauteurcamera', 'hauteurstation', 'hcam',
             'hauteurinstrument', 'hauteurdinstrument', 'hinstrument', 'hinst', 'hi',
             'hauteurinstrumentm', 'hauteurappareilm',
             'hauteurcm', 'hcm', 'hauteurinstrumentcm', 'hauteurappareilcm', 'hicm',
             'hauteurmm', 'hmm',
             'hauteur', 'h', 'hcamera', 'happareil'),
    'delta': ('delta', 'deltaplancher', 'decalageplancher', 'deltasol',
              'surelevation', 'marche', 'deltaz', 'deltam', 'deltacm', 'deltamm'),
    # Altitude du plancher : base du calcul Z = plancher + delta + hauteur
    'zfloor': ('zplancher', 'altitudeplancher', 'altplancher', 'zdalle',
               'altitudedalle', 'zniveau', 'altitudeniveau', 'niveauplancher',
               'coteplancher', 'cotedalle', 'zplancherm', 'altitudeplancherm',
               'zfloor', 'floorz', 'floorelevation',
               # Z nu resté libre : le point de vue a sa propre colonne (Z corrigé)
               'z', 'altitude'),
    'dh': ('dhstation', 'dh', 'dhauteur', 'correctionhauteur', 'dhauteurstation'),
    'ddelta': ('ddeltaplancher', 'ddelta', 'correctiondelta', 'ddeltasol'),
    'north': ('pctnord', 'nordpct', 'pct', 'nordpourcent', 'cap', 'heading',
              'azimut', 'orientation'),
    'floor': ('plancher', 'plancherms', 'planchers', 'nomplancher', 'libelleplancher',
              'niveau', 'etage', 'level', 'floor', 'dalle'),
    # Correction d'orientation : colonne dediee, ajoutee par l'outil si absente.
    'dnord': ('deltanorddeg', 'deltanord', 'dnord', 'correctionnord',
              'rotationimage', 'nordcorrection', 'deltanordo'),
    # Cle immuable (numero de scan) : survit au renommage des photos.
    'key': ('numscan', 'numeroscan', 'numerodescan', 'nodescan', 'ndescan',
            'noscan', 'nscan', 'scanno', 'scannumber', 'scanid', 'numscans', 'no', 'nr',
            'scan', 'numero', 'num', 'cle',
            'clef', 'uid', 'identifiant', 'idscan'),
    # Nom projete (nom final selon la convention) : porte local/etage/date
    # quand la photo sur disque ne s'appelle encore que par son numero.
    'target': ('nomprojete', 'projection', 'nomfinal', 'nomcible', 'nouveaunom',
               'renommage', 'fichierfinal', 'nomconvention', 'fichierprojete',
               'photoprojetee', 'nomphotoprojete'),
    # Attributs explicites, prioritaires sur l'analyse du nom.
    'local': ('local', 'piece', 'salle', 'zone', 'room'),
    'etage': ('etage', 'etg', 'stage'),
    'date': ('date', 'datepdv', 'dateprisedevue', 'prisedevue', 'datephoto'),
    'index': ('index', 'indice', 'numimage'),
}

YAW_COLUMN = 'Delta Nord (deg)'
ROUNDING_TOL = 5e-4   # m — sous le demi-millimètre, un écart relu est un arrondi   # intitule ecrit si la colonne n'existe pas


def _sniff_delimiter(sample: str) -> str:
    """Choisit le separateur le plus present sur la premiere ligne."""
    line = sample.splitlines()[0] if sample else ''
    counts = {d: line.count(d) for d in (';', '\t', ',', '|')}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ';'


def _read_text_enc(path: str) -> Tuple[str, str]:
    """Lit un fichier texte et retourne (contenu, encodage retenu)."""
    for enc in ('utf-8-sig', 'utf-8', 'cp1252', 'latin-1'):
        try:
            with open(path, 'r', encoding=enc, newline='') as fh:
                return fh.read(), enc
        except UnicodeDecodeError:
            continue
    with open(path, 'r', encoding='latin-1', errors='replace', newline='') as fh:
        return fh.read(), 'latin-1'


def _read_text(path: str) -> str:
    return _read_text_enc(path)[0]


class ColumnMappingNeeded(ValueError):
    """Les colonnes indispensables n'ont pas pu être reconnues d'elles-mêmes.

    Porte de quoi proposer une correspondance à l'utilisateur : intitulés,
    premières lignes, colonnes déjà reconnues (ou devinées) et signature du
    fichier pour mémoriser son choix.
    """

    def __init__(self, message: str, header: List[str], sample: List[List[str]],
                 guess: Dict[str, int], signature: str, headerless: bool):
        super().__init__(message)
        self.header = header
        self.sample = sample
        self.guess = guess
        self.signature = signature
        self.headerless = headerless


# Ordre d'attribution : une colonne ne sert qu'a un seul champ, les plus
# importants choisissent en premier.
_FIELD_ORDER = ('photo', 'key', 'x', 'y', 'z', 'zfloor', 'target', 'locator', 'north',
                'floor',
                'dnord', 'hcam', 'delta', 'dh', 'ddelta', 'local', 'etage', 'date', 'index')

# Champs proposes dans la boite de correspondance (champ, libelle, obligatoire)
MAPPING_FIELDS = (('key', "Identifiant / N° scan", True),
                  ('photo', "Fichier photo (si différent)", False),
                  ('x', "X (Est)", True), ('y', "Y (Nord)", True), ('z', "Z", False),
                  ('north', "% Nord", False), ('floor', "Plancher", False),
                  ('zfloor', "Z plancher", False), ('hcam', "Hauteur instrument", False),
                  ('delta', "Delta ±", False),
                  ('target', "Nom projeté", False), ('locator', "Nom du locator", False))


def auto_columns(header: Sequence[str]) -> Dict[str, int]:
    """Colonnes reconnues d'après leurs intitulés normalisés.

    Une colonne ne sert qu'à un champ, dans l'ordre d'importance. Un plancher
    à l'intitulé inattendu (« PlancherMS », « Niveau bâtiment »…) est repris
    par son préfixe s'il reste libre.
    """
    col: Dict[str, int] = {}
    used: set = set()
    for field_name in _FIELD_ORDER:
        for alias in COL_ALIASES.get(field_name, ()):
            if alias in header:
                idx = header.index(alias)
                if idx in used:
                    continue
                col[field_name] = idx
                used.add(idx)
                break
    if 'floor' not in col:
        for idx, name in enumerate(header):
            if idx not in used and name.startswith(('plancher', 'niveau', 'etage', 'floor')):
                col['floor'] = idx
                break
    return col


def unit_scale(name: str) -> float:
    """Facteur vers le mètre d'après l'intitulé : « Hauteur/cm » → 0,01."""
    n = norm_key(name)
    if n.endswith('mm'):
        return 0.001
    if n.endswith('cm'):
        return 0.01
    return 1.0


def csv_signature(header_cells: Sequence[str], headerless: bool) -> str:
    """Empreinte d'un format de CSV, pour retrouver la correspondance choisie."""
    if headerless:
        return f"#sans-entete:{len(header_cells)}"
    return '|'.join(norm_key(c) for c in header_cells)


def read_survey_csv(path: str, mapping: Optional[Dict[str, str]] = None
                    ) -> Tuple[List[Station], List[str]]:
    """Lit le CSV de releve.

    Tolerant : BOM, separateur ; , tab |, virgule decimale, colonnes dans
    n'importe quel ordre, intitules accentues ou non, nombreux synonymes.
    Seuls un identifiant (nom de photo OU numero de scan) et X / Y sont
    indispensables : sans colonne « Fichier photo », le numero de scan sert
    de nom de photo. Un CSV sans ligne d'en-tete est reconnu.

    `mapping` (champ -> intitule de colonne, ou « #n » pour la n-ieme colonne)
    impose une correspondance, typiquement choisie par l'utilisateur.

    Leve ColumnMappingNeeded si l'identifiant ou X / Y restent introuvables,
    ou si le fichier n'a pas d'en-tete (correspondance devinee a confirmer).

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
    if not rows:
        raise ValueError("CSV sans donnees exploitables.")

    # En-tete present ? Une premiere ligne majoritairement numerique n'en est pas un.
    first = [c.strip() for c in rows[0]]
    n_num = sum(1 for c in first if parse_float(c) is not None)
    headerless = n_num >= 2 and n_num >= len([c for c in first if c]) - 1
    if headerless:
        header_cells = [f"colonne {k + 1}" for k in range(max(len(r) for r in rows[:50]))]
        data = rows
    else:
        header_cells = list(rows[0])
        data = rows[1:]
    if not data:
        raise ValueError("CSV sans donnees exploitables (une seule ligne).")
    signature = csv_signature(header_cells, headerless)

    col: Dict[str, int] = {}
    if mapping:
        norm_cells = [norm_key(c) for c in header_cells]
        for field_name, ref in mapping.items():
            if not ref:
                continue
            if ref.startswith('#') and ref[1:].isdigit():
                idx = int(ref[1:])
            elif norm_key(ref) in norm_cells:
                idx = norm_cells.index(norm_key(ref))
            else:
                continue
            if 0 <= idx < len(header_cells):
                col[field_name] = idx
    elif not headerless:
        col.update(auto_columns([norm_key(c) for c in header_cells]))

    # Unités : hauteur et delta peuvent être en cm ou en mm (« Hauteur/cm »)
    def scale_of(field_name: str) -> float:
        i = col.get(field_name, -1)
        return unit_scale(header_cells[i]) if 0 <= i < len(header_cells) and not headerless \
            else 1.0
    h_scale, d_scale = scale_of('hcam'), scale_of('delta')

    # Identifiant : le nom de photo, a defaut le numero de scan, a defaut le nom projete
    if 'photo' not in col:
        if 'key' in col:
            col['photo'] = col['key']
            warns.append("pas de colonne « Fichier photo » : le numéro de scan "
                         "sert de nom de photo")
        elif 'target' in col:
            col['photo'] = col['target']

    if headerless and not mapping:
        # Correspondance devinee : 1re colonne = identifiant, puis les trois
        # premieres colonnes numeriques (hors identifiant) = X, Y, Z.
        guess: Dict[str, int] = {'key': 0}
        numeric = [k for k in range(1, len(header_cells))
                   if sum(1 for r in data[:20] if k < len(r)
                          and parse_float(r[k]) is not None) >= min(len(data), 20) * 0.8]
        for field_name, k in zip(('x', 'y', 'z'), numeric):
            guess[field_name] = k
        raise ColumnMappingNeeded(
            "CSV sans ligne d'en-tête : correspondance des colonnes à confirmer.",
            header_cells, data[:6], guess, signature, True)

    missing = [f for f in ('photo', 'x', 'y') if f not in col]
    if missing:
        noms = {'photo': "identifiant (Fichier photo ou N° scan)", 'x': "X", 'y': "Y"}
        raise ColumnMappingNeeded(
            "Colonnes introuvables : " + ', '.join(noms[f] for f in missing) +
            "\nIntitules lus : " + ', '.join(header_cells) +
            "\nIl faut au minimum un identifiant (« Fichier photo » ou « N° scan »), "
            "« X » et « Y ».",
            header_cells, data[:6], dict(col), signature, False)

    rows = [header_cells] + data          # la boucle ci-dessous saute la 1re ligne

    def cell(row: Sequence[str], key: str, default: str = '') -> str:
        i = col.get(key, -1)
        return row[i].strip() if 0 <= i < len(row) else default

    stations: List[Station] = []
    seen: Dict[str, int] = {}
    for lineno, row in enumerate(rows[1:], start=1 if headerless else 2):
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
        dnord = parse_float(cell(row, 'dnord')) or 0.0
        h0 = parse_float(cell(row, 'hcam'))
        if h0 is not None:
            h0 *= h_scale
            if h0 > 20.0:            # unité non dite : une hauteur de 165 est en cm
                h0 /= 100.0
        zfl = parse_float(cell(row, 'zfloor'))
        delta_val = parse_float(cell(row, 'delta'))
        if delta_val is not None:
            delta_val *= d_scale
        delta0 = delta_val or 0.0
        cle = cell(row, 'key') or photo
        target = base_name(cell(row, 'target')) if cell(row, 'target') else ''
        attrs = {k: cell(row, k) for k in ('local', 'etage', 'date', 'index')
                 if cell(row, k)}
        if 'date' in attrs:
            attrs['date'] = re.sub(r'\D', '', attrs['date'])[:8]   # 2026-04-16 -> 20260416
        key = cle.lower()
        if key in seen:
            warns.append(f"ligne {lineno} : clé « {cle} » déjà utilisée — ignoree")
            continue
        if photo.lower() != key and photo.lower() in {s.photo.lower() for s in stations}:
            warns.append(f"ligne {lineno} : photo « {photo} » déjà utilisée — ignoree")
            continue
        seen[key] = len(stations)
        zv = 0.0 if z is None else z
        stations.append(Station(
            idx=len(stations),
            photo=photo,
            locator=(cell(row, 'locator')
                     or (parse_photo_name(target).locator() if target else '')
                     or (parse_photo_name(photo).locator()
                         if parse_photo_name(photo).reconnu else '')
                     or photo),
            x=x, y=y, z=zv,
            north_pct=north,
            floor=cell(row, 'floor') or '—',
            yaw_fix=wrap180(dnord),
            ox=x, oy=y, oz=zv, oyaw=wrap180(dnord),
            key=cle, target=target, attrs=attrs, key_explicit='key' in col,
            h0=h0, delta0=delta0, delta_col=delta_val is not None,
            floor_alt=zfl if zfl is not None else floor_altitude(cell(row, 'floor')),
            floor_alt_src=('colonne' if zfl is not None else
                           'libellé' if floor_altitude(cell(row, 'floor')) is not None
                           else ''),
            z_csv=z,
        ))

    if not stations:
        raise ValueError("Aucune station exploitable dans le CSV.")
    return stations, warns


def apply_altimetry(stations: Sequence[Station], eye: float = EYE_HEIGHT_DEFAULT) -> None:
    """Altitude du point de vue de chaque bulle, avant corrections (`oz`) :
    Z = Z plancher + delta + hauteur instrument.

    Le Z lu dans le CSV n'entre pas dans le calcul ; il ne sert que pour une
    bulle sans altitude de plancher (ni colonne, ni libellé). Les corrections
    (hauteur station, delta plancher) s'ajoutent ensuite.
    """
    for st in stations:
        if st.floor_alt is not None:
            st.oz = st.floor_alt + st.delta0 + (st.h0 if st.h0 is not None else eye)
        elif st.z_csv is not None:
            st.oz = st.z_csv
        st.z = st.oz + st.dh + st.ddelta


_FLOOR_ALT_RE = re.compile(r'\(\s*([+-]?\d+(?:[.,]\d+)?)\s*m?\s*\)')


def floor_altitude(label: str) -> Optional[float]:
    """Altitude d'un plancher d'après son libellé : « PLANCHER 02 (+00.00m) » → 0.0."""
    m = _FLOOR_ALT_RE.search(label or '')
    return float(m.group(1).replace(',', '.')) if m else None


def floor_z_ranges(stations: Sequence[Station]) -> Dict[str, Tuple[float, float]]:
    """Plage d'altitudes observée par étage, d'après les planchers renseignés."""
    zs: Dict[str, List[float]] = defaultdict(list)
    for st in stations:
        p = st.parts()
        if p.etage and not p.etage_deduit:
            zs[p.etage.zfill(2)].append(st.z)
    return {e: (min(v), max(v)) for e, v in zs.items() if v}


def check_floor_coherence(stations: Sequence[Station], tol: float = 1.0
                          ) -> Dict[int, str]:
    """Bulles dont l'étage, déduit du numéro de local, contredit l'altitude.

    Retourne {index de station: explication}. Ne modifie rien : c'est un
    signalement, la donnée source doit être vérifiée.
    """
    plages = floor_z_ranges(stations)
    out: Dict[int, str] = {}
    for st in stations:
        p = st.parts()
        if not p.etage_deduit:
            continue
        plage = plages.get(p.etage.zfill(2))
        if plage is None:
            out[st.idx] = (f"étage {p.etage} déduit du local {p.local}, "
                           f"mais aucun plancher {p.etage} dans le relevé")
        elif not (plage[0] - tol <= st.z <= plage[1] + tol):
            out[st.idx] = (f"étage {p.etage} déduit du local {p.local}, mais Z "
                           f"{st.z:.2f} hors de la plage de ce plancher "
                           f"({plage[0]:.2f} … {plage[1]:.2f})")
    return out


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

    def lens(self) -> float:
        """0 = perspective normale, 1 = stéréographique (grand angle)."""
        return lens_mix(self.fov)

    def focal(self) -> float:
        """Échelle au centre de l'image (px par radian)."""
        return lens_focal(self.fov, self.width)


def lens_mix(fov: float) -> float:
    return clamp((fov - WIDE_START) / (WIDE_FULL - WIDE_START), 0.0, 1.0)


def lens_g(theta: float, d: float) -> float:
    """Rayon image (en focales) d'une direction à `theta` de l'axe.

    Perspective générale : d = 0 donne tan θ (perspective normale), d = 1
    donne 2 tan(θ/2) (stéréographique) ; entre les deux, transition continue.
    """
    return (d + 1.0) * math.sin(theta) / (d + math.cos(theta))


def lens_inv(rho: float, d: float) -> float:
    """Angle à l'axe d'un point image à `rho` focales du centre (inverse exact)."""
    r = math.hypot(d + 1.0, rho)
    return math.atan2(rho, d + 1.0) + math.asin(clamp(rho * d / r, -1.0, 1.0))


def lens_focal(fov: float, width: int) -> float:
    return (width / 2.0) / lens_g(math.radians(fov) / 2.0, lens_mix(fov))


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
    yc = -sy * wx + cy * wy                 # droite ecran
    zc = -sp * fwd + cp * wz                # haut ecran
    d = view.lens()
    margin = d + xc                         # > 0 : direction représentable
    if margin <= 1e-6:
        return None
    f = view.focal()
    if d == 0.0:
        col = view.width / 2.0 + f * yc / xc
        row = view.height / 2.0 - f * zc / xc
        return col, row, xc
    side = math.hypot(yc, zc)
    if side < 1e-12:
        return view.width / 2.0, view.height / 2.0, margin
    rho = f * lens_g(math.acos(clamp(xc, -1.0, 1.0)), d)
    return (view.width / 2.0 + rho * yc / side,
            view.height / 2.0 - rho * zc / side, margin)


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
class HotspotFilter:
    """Filtrage vivant des pastilles (n'affecte jamais le réseau ni les données).

    Désactivé, il laisse tout passer : le rendu retrouve son comportement
    normal sans qu'aucun réglage ne soit perdu.
    """
    active: bool = False
    floor_mode: str = 'tous'      # 'tous' | 'courant' | nom exact d'un plancher
    max_dist: float = 0.0         # m — 0 = pas de limite
    local: str = ''               # motifs séparés par des virgules, * accepté
    inter_floor: bool = True      # garder les pastilles ▲ / ▼
    hide_missing: bool = False    # masquer les bulles sans image
    same_local: bool = False      # seulement le local de la bulle courante (touche L)
    # Portée de la vue (toujours appliquée, filtres actifs ou non) : 'local',
    # 'voisins' (le local et les locaux proches), 'distance' ou 'plancher'.
    scope: str = 'plancher'
    scope_dist: float = 6.0
    near_locals: object = None    # fonction : indice de bulle -> locaux voisins

    def match_local(self, target: Station) -> bool:
        motifs = [m.strip().lower()
                  for m in self.local.replace(';', ',').split(',') if m.strip()]
        if not motifs:
            return True
        name = (target.parts().local or target.locator).lower()
        for motif in motifs:
            if not any(c in motif for c in '*?['):
                motif += '*'          # « K25 » retient K256, K257…
            if fnmatch.fnmatch(name, motif):
                return True
        return False

    def in_scope(self, current: Station, target: Station, link: "Link") -> bool:
        """Portée de la vue ; les liens ▲▼ vers les planchers voisins passent toujours."""
        if link.kind != 'same' or self.scope == 'plancher':
            return True
        if self.scope == 'distance':
            return link.dist_h <= self.scope_dist
        mine = current.parts().local or current.locator
        theirs = target.parts().local or target.locator
        if self.scope == 'local':
            return theirs == mine
        near = self.near_locals(current.idx) if callable(self.near_locals) else {mine}
        return theirs in near or link.dist_h <= self.scope_dist * 0.5

    def accepts(self, current: Station, target: Station, link: "Link",
                has_image: bool = True) -> bool:
        if not self.in_scope(current, target, link):
            return False
        if not self.active:
            return True
        if not self.inter_floor and link.kind != 'same':
            return False
        if self.floor_mode == 'courant':
            if target.floor != current.floor:
                return False
        elif self.floor_mode != 'tous' and target.floor != self.floor_mode:
            return False
        if self.max_dist > 0 and link.dist > self.max_dist:
            return False
        if self.hide_missing and not has_image:
            return False
        if self.same_local:
            mine = current.parts().local or current.locator
            if (target.parts().local or target.locator) != mine:
                return False
        return self.match_local(target)

    def resume(self) -> str:
        if not self.active:
            return "inactifs"
        bits = []
        if self.floor_mode == 'courant':
            bits.append("plancher courant")
        elif self.floor_mode != 'tous':
            bits.append(self.floor_mode)
        if self.max_dist > 0:
            bits.append(f"≤ {self.max_dist:g} m")
        if self.local.strip():
            bits.append(f"local {self.local.strip()}")
        if not self.inter_floor:
            bits.append("sans ▲▼")
        if self.hide_missing:
            bits.append("images présentes")
        if self.same_local:
            bits.append("local courant")
        return ' · '.join(bits) if bits else "actifs (tout passe)"

    def scope_text(self) -> str:
        if self.scope == 'distance':
            return f"≤ {self.scope_dist:g} m"
        if self.scope == 'voisins':
            return f"locaux voisins ({self.scope_dist:g} m)"
        return {'local': "local", 'plancher': "plancher entier"}.get(self.scope, self.scope)


@dataclass
class GraphParams:
    radius: float = RADIUS_DEFAULT
    kmax: int = KMAX_DEFAULT
    ang_min: float = ANG_MIN_DEFAULT
    floor_radius: float = FLOOR_RADIUS_DEFAULT
    floor_dz_max: float = FLOOR_DZ_MAX
    whole_floor: bool = False     # toutes les bulles du plancher, sans portée ni tri


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

    whole = params.whole_floor
    reach = params.floor_radius if whole else max(params.radius, params.floor_radius)
    index = SpatialIndex(stations, max(0.5, reach))
    r2 = params.radius * params.radius
    fr2 = params.floor_radius * params.floor_radius
    floors: Dict[str, List[int]] = defaultdict(list)
    if whole:
        for st in stations:
            floors[st.floor].append(st.idx)

    for st in stations:
        same: List[Tuple[float, float, float, float, int]] = []
        by_floor: Dict[str, Tuple[float, int]] = {}

        # plancher entier : toutes ses bulles (liste du plancher), les autres
        # planchers restant cherchés dans le voisinage ; sinon, la portée
        if whole:
            cands = itertools.chain(
                floors[st.floor],
                (j for j in index.around(st.x, st.y, reach)
                 if stations[j].floor != st.floor))
        else:
            cands = index.around(st.x, st.y, reach)
        for j in cands:
            if j == st.idx:
                continue
            other = stations[j]
            dx, dy = other.x - st.x, other.y - st.y
            d2 = dx * dx + dy * dy
            if other.floor == st.floor:
                if whole or d2 <= r2:
                    dz = other.z - st.z
                    az, _, d3 = azimuth_elev(dx, dy, dz)
                    same.append((d3, az, math.sqrt(d2), dz, j))
            elif d2 <= fr2:
                dz = other.z - st.z
                if abs(dz) <= params.floor_dz_max:
                    prev = by_floor.get(other.floor)
                    if prev is None or d2 < prev[0]:
                        by_floor[other.floor] = (d2, j)

        same.sort()
        out: List[Link] = []
        prune = params.ang_min > 0.0
        for d3, az, dh, dz, j in same:
            if len(out) >= params.kmax:
                break
            if prune and any(abs(wrap180(az - lk.azimuth)) < params.ang_min for lk in out):
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
# CORRECTIONS : POSITION XYZ (CSV) ET ORIENTATION (IMAGE)
# ─────────────────────────────────────────────────────────────────────────────

def _pano_ray(view: View, col: float, row: float) -> Tuple[float, float, float]:
    """Rayon unitaire d'un pixel, dans le repère de l'image (X = centre image)."""
    f = view.focal()
    xg = col - view.width / 2.0
    yg = row - view.height / 2.0
    d = view.lens()
    if d == 0.0:
        norm = math.sqrt(f * f + xg * xg + yg * yg)
        xc, yc, zc = f / norm, xg / norm, -yg / norm
    else:
        rpx = math.hypot(xg, yg)
        if rpx < 1e-9:
            xc, yc, zc = 1.0, 0.0, 0.0
        else:
            th = lens_inv(rpx / f, d)
            st_ = math.sin(th)
            xc, yc, zc = math.cos(th), st_ * xg / rpx, -st_ * yg / rpx
    yr, pr = math.radians(view.yaw), math.radians(view.pitch)
    cy, sy = math.cos(yr), math.sin(yr)
    cp, sp = math.cos(pr), math.sin(pr)
    # monde = Rz(yaw) . Ry(-pitch) . camera   (même convention que le rendu)
    return (cy * cp * xc - sy * yc - cy * sp * zc,
            sy * cp * xc + cy * yc - sy * sp * zc,
            sp * xc + cp * zc)


def screen_ray(view: View, col: float, row: float, calib: Calib,
               north_pct: float) -> Tuple[float, float, float]:
    """Rayon unitaire d'un pixel en coordonnées terrain (Est, Nord, Haut)."""
    wx, wy, wz = _pano_ray(view, col, row)
    horiz = math.hypot(wx, wy)
    a = math.radians(calib.azimuth(math.degrees(math.atan2(wy, wx)), north_pct))
    return horiz * math.sin(a), horiz * math.cos(a), wz


def project_point(view: View, calib: Calib, north_pct: float,
                  de: float, dn: float, du: float
                  ) -> Optional[Tuple[float, float, float]]:
    """Projette un point terrain (Est, Nord, Haut relatifs à la caméra)."""
    dh = math.hypot(de, dn)
    if dh < 1e-9 and abs(du) < 1e-9:
        return None
    az = math.degrees(math.atan2(de, dn))
    elev = math.degrees(math.atan2(du, dh))
    return project(view, calib.pano_yaw(az, north_pct), elev)


def project_segment(view: View, calib: Calib, north_pct: float,
                    p: Sequence[float], q: Sequence[float], steps: int = 32
                    ) -> Optional[Tuple[float, float, float, float]]:
    """Partie visible d'un segment 3D, projetée à l'écran (droite en gnomonique).

    Le segment est échantillonné pour écarter la partie située derrière
    l'observateur ; les extrémités visibles suffisent puisque la projection
    gnomonique conserve les droites.
    """
    first = last = None
    for i in range(steps + 1):
        t = i / steps
        pr = project_point(view, calib, north_pct,
                           p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t,
                           p[2] + (q[2] - p[2]) * t)
        if pr is None or pr[2] < 0.05:        # derrière ou trop rasant
            if last is not None:
                break
            continue
        if first is None:
            first = pr
        last = pr
    if first is None or last is None or first is last:
        return None
    return first[0], first[1], last[0], last[1]


AXES: Dict[str, Tuple[float, float, float]] = {
    'x': (1.0, 0.0, 0.0), 'y': (0.0, 1.0, 0.0), 'z': (0.0, 0.0, 1.0)}


def axis_param(ray: Sequence[float], p0: Sequence[float], axis: Sequence[float],
               max_abs: float = 60.0) -> Optional[float]:
    """Abscisse, sur la droite p0 + t·axe, du point le plus proche du rayon.

    Le rayon part de l'observateur (origine). Retourne None si le rayon est
    presque parallèle à l'axe, s'il regarde à l'opposé ou si le point est
    déraisonnablement loin : le geste est alors simplement ignoré.
    """
    b = sum(r * a for r, a in zip(ray, axis))
    den = 1.0 - b * b
    if den < 1e-4:
        return None
    dr = sum(r * c for r, c in zip(ray, p0))
    ar = sum(a * c for a, c in zip(axis, p0))
    t = (b * dr - ar) / den
    if dr + b * t <= 0 or abs(t) > max_abs:
        return None
    return t


def alt_down(state: int) -> bool:
    """Touche Alt (Option sous macOS) enfoncée, selon la plateforme de Tk."""
    if sys.platform == 'win32':
        return bool(state & 0x20000)       # 0x0008 y signale le verrouillage num.
    if sys.platform == 'darwin':
        return bool(state & 0x0010)
    return bool(state & 0x0008)


MIRE_RING_M = 0.50       # rayon de l'empreinte au sol de la mire (m)
MIRE_TICK_M = 0.10       # graduation de la mire (m)


def shadow_sprite(rx: int, ry: int, ss: int = 2):
    """Ombre douce au sol (ellipse floue, noire translucide) — image RGBA PIL."""
    import numpy as np
    from PIL import Image, ImageFilter
    rx, ry = max(2, int(rx)) * ss, max(1, int(ry)) * ss
    pad = int(0.35 * rx) + 2 * ss
    W, H = 2 * (rx + pad), 2 * (ry + pad)
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    d = np.sqrt(((xx - W / 2) / rx) ** 2 + ((yy - H / 2) / ry) ** 2)
    a = np.clip(1.0 - d, 0, 1) ** 0.7 * 0.55
    alpha = Image.fromarray((a * 255).astype(np.uint8), 'L').filter(
        ImageFilter.GaussianBlur(max(1.0, 0.18 * min(rx, ry * 3))))
    img = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    img.putalpha(alpha)
    if ss > 1:
        img = img.resize((W // ss, H // ss), Image.LANCZOS)
    return img


@lru_cache(maxsize=4096)
def local_color(name: str) -> str:
    """Couleur stable d'un local : même local, même couleur, d'une session à l'autre.

    Teinte tirée du nom (répartition « nombre d'or »), en évitant l'orange
    réservé aux bulles corrigées ; saturation et luminosité lisibles sur une
    image sombre comme claire.
    """
    import colorsys
    import zlib
    h = (zlib.crc32(name.encode('utf-8')) * 0.6180339887) % 1.0
    hue = (50.0 + h * 325.0) % 360.0 / 360.0          # hors 15°–50° (orange d'édition)
    k = zlib.crc32(name[::-1].encode('utf-8'))
    sat = 0.55 + (k % 30) / 100.0
    val = 0.85 + (k // 30 % 15) / 100.0
    r, g, b = colorsys.hsv_to_rgb(hue, sat, val)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def aim_at(view: View, calib: Calib, frm: Station, to: Station,
           eye: float = EYE_HEIGHT_DEFAULT, anchor: str = 'sol') -> None:
    """Oriente la vue de `frm` vers la pastille de `to` (cap et site).

    Sert à regarder d'où l'on vient : la pastille de la bulle quittée doit
    tomber exactement sur le point de prise de vue visible dans l'image.
    """
    dx, dy = to.x - frm.x, to.y - frm.y
    dh = math.hypot(dx, dy)
    # bulle au point de vue : on vise le milieu du mât, pour voir à la fois la
    # sphère (l'appareil) et son empreinte au sol
    cible = (to.z + to.ground(eye)) / 2.0 if anchor == 'vue' else to.ground(eye)
    dz = cible - frm.z
    if dh < 0.05:                            # à l'aplomb : on regarde en haut ou en bas
        view.pitch = PITCH_MAX if dz > 0 else PITCH_MIN
        return
    view.yaw = wrap180(calib.pano_yaw(math.degrees(math.atan2(dx, dy)), frm.north_pct))
    view.pitch = clamp(math.degrees(math.atan2(dz, dh)), PITCH_MIN, PITCH_MAX)


def plan_skeleton(stations: Sequence[Station], links: Sequence[Sequence["Link"]]
                  ) -> List[Tuple[int, int]]:
    """Squelette du réseau pour le plan (voisinage relatif sur le réseau).

    Parmi les liens de navigation d'un même plancher, le lien i–j est omis
    dès qu'une bulle k, reliée à la fois à i et à j, est plus proche de
    chacune des deux qu'elles ne le sont entre elles : le trajet i–k–j le
    remplace. Les couloirs restent lisibles, les longues diagonales qui
    traversent les murs disparaissent, et le squelette reste connexe
    exactement là où le réseau l'est (un lien n'est retiré que s'il existe
    un chemin de liens plus courts).
    """
    nbr: Dict[int, Dict[int, float]] = defaultdict(dict)
    for st in stations:
        if st.idx >= len(links):
            continue
        for lk in links[st.idx]:
            if lk.kind == 'same' and lk.target != st.idx:
                d2 = lk.dist_h * lk.dist_h
                nbr[st.idx][lk.target] = d2
                nbr[lk.target][st.idx] = d2
    keep: List[Tuple[int, int]] = []
    for i, ni in nbr.items():
        for j, dij in ni.items():
            if j <= i:
                continue
            nj = nbr[j]
            lim = dij * (1.0 - 1e-9)
            small, other = (ni, nj) if len(ni) <= len(nj) else (nj, ni)
            if not any(k != i and k != j and dk < lim and other.get(k, math.inf) < lim
                       for k, dk in small.items()):
                keep.append((i, j))
    return keep


def ground_from_screen(view: View, col: float, row: float, calib: Calib,
                       north_pct: float, dz: float,
                       max_dist: float = 80.0) -> Optional[Tuple[float, float]]:
    """Point du sol visé à l'écran : (azimut deg, distance horizontale m).

    `dz` est l'altitude du plan visé par rapport à la caméra (négatif = sous
    l'observateur). Calcul exact : intersection du rayon caméra avec ce plan.
    Retourne None si le rayon ne rencontre pas le plan (regard trop horizontal).
    """
    wx, wy, wz = _pano_ray(view, col, row)
    dh_unit = math.hypot(wx, wy)
    if dh_unit < 1e-9:
        return None
    tan_elev = wz / dh_unit
    if dz < 0:
        if tan_elev > -1e-3:
            return None
    elif dz > 0:
        if tan_elev < 1e-3:
            return None
    else:
        return None
    dist = dz / tan_elev
    if not (0.05 <= dist <= max_dist):
        return None
    psi = math.degrees(math.atan2(wy, wx))
    return calib.azimuth(psi, north_pct), dist


@dataclass(frozen=True)
class Bilan:
    """Décompte des bulles corrigées, par nature."""
    xy: int = 0
    h: int = 0
    delta: int = 0
    nord: int = 0

    def position(self) -> int:
        return self.xy + self.h + self.delta

    def any(self) -> bool:
        return bool(self.xy or self.h or self.delta or self.nord)

    def texte(self) -> str:
        parts = []
        if self.xy:
            parts.append(f"{self.xy} XY")
        if self.h:
            parts.append(f"{self.h} hauteur")
        if self.delta:
            parts.append(f"{self.delta} delta")
        if self.nord:
            parts.append(f"{self.nord} nord")
        return ' · '.join(parts) if parts else "aucune"


class Corrections:
    """Journal des corrections, stocké dans un FICHIER DE CORRECTIONS distinct.

    Le relevé chargé n'est jamais réécrit : il reste la source. Les corrections
    (position et orientation) vivent dans leur propre CSV, écrit en continu,
    relu automatiquement à la réouverture et appliqué par-dessus le relevé.
    Les images ne sont touchées qu'au moment de l'application par lot.
    """

    SUFFIX = '_corrections.csv'
    DELIM = ';'
    # Un enregistrement = un patch : deltas separes par nature physique, et
    # valeurs absolues corrigees, joignables dans QGIS par la colonne « Cle ».
    HEADER = ('Cle', 'Fichier photo', 'Nom du Locator',
              'X', 'Y', 'Z', 'dX', 'dY',
              'dH station', 'dDelta plancher', 'dZ',
              YAW_COLUMN, 'H appareil', 'Delta plancher',
              'Orientation appliquee', 'Date')

    def __init__(self, csv_path: str = '', path: str = '',
                 eye: float = EYE_HEIGHT_DEFAULT):
        self.csv_path = csv_path
        self.path = path or self.default_path(csv_path)
        self.eye = eye                       # hauteur appareil par defaut
        self.applied: Dict[str, str] = {}   # photo -> date de rotation des images
        self.dirty = False
        self._undo: List[Tuple[str, dict]] = []
        self.on_record = None     # appelé à chaque étape annulable (journal commun)
        self._lock = threading.RLock()

    @classmethod
    def default_path(cls, csv_path: str) -> str:
        if not csv_path:
            return ''
        return os.path.splitext(csv_path)[0] + cls.SUFFIX

    # ── etat ─────────────────────────────────────────────────────────
    @staticmethod
    def snapshot(st: Station) -> dict:
        return {'x': st.x, 'y': st.y, 'dh': st.dh, 'ddelta': st.ddelta,
                'yaw_fix': st.yaw_fix}

    @staticmethod
    def restore(st: Station, snap: dict) -> None:
        st.x = float(snap.get('x', st.x))
        st.y = float(snap.get('y', st.y))
        st.dh = float(snap.get('dh', st.dh))
        st.ddelta = float(snap.get('ddelta', st.ddelta))
        st.z = st.oz + st.dh + st.ddelta
        st.yaw_fix = float(snap.get('yaw_fix', st.yaw_fix))

    # ── modifications ────────────────────────────────────────────────
    def apply(self, st: Station, *, x: float = None, y: float = None,
              dh: float = None, ddelta: float = None, yaw_fix: float = None,
              record: bool = True) -> None:
        """Applique une correction, en empilant l'état précédent (annulation).

        Les deux composantes en Z sont distinctes : `dh` (hauteur station) ne
        déplace que la caméra, `ddelta` (delta plancher) déplace caméra et sol.
        L'altitude caméra `z` est toujours recalculée : oz + dh + ddelta.
        """
        if record:
            with self._lock:
                self._undo.append((st.photo, self.snapshot(st)))
                del self._undo[:-500]
            if self.on_record is not None:
                self.on_record()
        if x is not None:
            st.x = float(x)
        if y is not None:
            st.y = float(y)
        if dh is not None:
            st.dh = float(dh)
        if ddelta is not None:
            st.ddelta = float(ddelta)
        st.z = st.oz + st.dh + st.ddelta
        if yaw_fix is not None:
            st.yaw_fix = wrap180(float(yaw_fix))
        self.dirty = True

    def undo(self, by_photo: Dict[str, Station]) -> Optional[Station]:
        with self._lock:
            if not self._undo:
                return None
            photo, snap = self._undo.pop()
        st = by_photo.get(photo)
        if st is not None:
            self.restore(st, snap)
            self.dirty = True
        return st

    def can_undo(self) -> bool:
        return bool(self._undo)

    def drop_if_unchanged(self, st: Station) -> bool:
        """Retire la dernière étape si le geste n'a rien changé (clic sans glisser)."""
        with self._lock:
            if self._undo and self._undo[-1][0] == st.photo \
                    and self._undo[-1][1] == self.snapshot(st):
                self._undo.pop()
                return True
        return False

    def revert(self, st: Station) -> None:
        """Retour aux valeurs du relevé d'origine."""
        self.apply(st, x=st.ox, y=st.oy, dh=0.0, ddelta=0.0, yaw_fix=st.oyaw)
        self.applied.pop(st.key, None)

    def revert_all(self, stations: Sequence[Station]) -> int:
        n = 0
        for st in stations:
            if st.modified():
                self.revert(st)
                n += 1
        return n

    @staticmethod
    def counts(stations: Sequence[Station]) -> "Bilan":
        """Nombre de bulles corrigées, par nature de correction."""
        return Bilan(xy=sum(1 for s in stations if s.moved()),
                     h=sum(1 for s in stations if s.raised()),
                     delta=sum(1 for s in stations if s.shifted()),
                     nord=sum(1 for s in stations if s.turned()))

    @staticmethod
    def pending_images(stations: Sequence[Station]) -> List[Station]:
        """Bulles dont l'image reste à tourner (Δ nord non nul)."""
        return [s for s in stations if s.has_yaw()]

    def mark_applied(self, keys: Iterable[str]) -> None:
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        for key in keys:
            self.applied[key] = stamp
        self.dirty = True

    # ── fichier de corrections ───────────────────────────────────────
    def save(self, stations: Sequence[Station]) -> str:
        """Écrit le fichier de corrections (uniquement les bulles corrigées).

        Écriture atomique : le fichier reste exploitable même si l'outil est
        interrompu en cours d'enregistrement.
        """
        if not self.path:
            return ''
        rows = [st for st in stations if st.modified() or st.key in self.applied]
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        lines = [self.DELIM.join(self.HEADER)]
        for st in rows:
            # précision du micromètre : un relevé à 6 décimales doit revenir
            # intact, sinon une bulle seulement réorientée paraîtrait déplacée
            lines.append(self.DELIM.join((
                st.key, st.photo, st.locator,
                f"{st.x:.6f}", f"{st.y:.6f}", f"{st.z:.6f}",
                f"{st.x - st.ox:+.6f}", f"{st.y - st.oy:+.6f}",
                f"{st.dh:+.6f}", f"{st.ddelta:+.6f}", f"{st.z - st.oz:+.6f}",
                f"{st.yaw_fix:.4f}",
                f"{st.height(self.eye):.6f}", f"{st.delta(self.eye):+.6f}",
                self.applied.get(st.key, ''), stamp)))
        tmp = self.path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8-sig', newline='') as fh:
                fh.write('\r\n'.join(lines) + '\r\n')
            os.replace(tmp, self.path)
        except Exception:
            return ''
        self.dirty = False
        return self.path

    def load(self, by_photo: Dict[str, Station], path: str = '',
             by_key: Optional[Dict[str, Station]] = None) -> Tuple[int, int]:
        """Relit un fichier de corrections et l'applique au relevé en mémoire.

        La correspondance se fait d'abord par la clé immuable (numéro de scan),
        puis par le nom de photo : les corrections survivent au renommage.
        Retourne (corrections appliquées, lignes sans correspondance).
        """
        path = path or self.path
        if not path or not os.path.isfile(path):
            return 0, 0
        text = _read_text(path)
        rows = [r for r in csv.reader(text.splitlines(),
                                      delimiter=_sniff_delimiter(text))
                if any((c or '').strip() for c in r)]
        if len(rows) < 2:
            return 0, 0
        header = [norm_key(c) for c in rows[0]]
        col: Dict[str, int] = {}
        for field_name in ('photo', 'x', 'y', 'z', 'dnord', 'key', 'dh', 'ddelta'):
            for alias in COL_ALIASES[field_name]:
                if alias in header:
                    col[field_name] = header.index(alias)
                    break
        applied_col = header.index('orientationappliquee') if 'orientationappliquee' in header else -1
        if 'photo' not in col and 'key' not in col:
            raise ValueError("Fichier de corrections sans colonne « Cle » ni « Fichier photo ».")
        by_key = by_key or {}
        by_photo_l = {k.lower(): v for k, v in by_photo.items()}

        n_ok = n_miss = 0
        for row in rows[1:]:
            def cell(key: str) -> str:
                i = col.get(key, -1)
                return row[i].strip() if 0 <= i < len(row) else ''
            st = None
            if cell('key'):
                st = by_key.get(cell('key').lower())
            if st is None and cell('photo'):
                st = by_photo_l.get(base_name(cell('photo')).lower())
            if st is None:
                n_miss += 1
                continue
            values = {}
            for axis in ('x', 'y'):
                v = parse_float(cell(axis))
                if v is not None:
                    origine = st.ox if axis == 'x' else st.oy
                    # fichiers écrits au mm près : l'arrondi n'est pas une correction
                    values[axis] = origine if abs(v - origine) < ROUNDING_TOL else v
            dh = parse_float(cell('dh'))
            dd = parse_float(cell('ddelta'))
            if dh is None and dd is None:
                # ancien format : seule l'altitude camera etait ecrite ; on la
                # range en hauteur de station, la lecture la plus courante
                z = parse_float(cell('z'))
                if z is not None:
                    dh = z - st.oz
            if dh is not None and abs(dh) < ROUNDING_TOL:
                dh = 0.0
            if dd is not None and abs(dd) < ROUNDING_TOL:
                dd = 0.0
            if dh is not None:
                values['dh'] = dh
            if dd is not None:
                values['ddelta'] = dd
            dn = parse_float(cell('dnord'))
            if dn is not None:
                values['yaw_fix'] = dn
            if values:
                self.apply(st, record=False, **values)
                n_ok += 1
            if 0 <= applied_col < len(row) and row[applied_col].strip():
                self.applied[st.key] = row[applied_col].strip()
        self.dirty = False
        return n_ok, n_miss


def _format_like(sample: str, value: float, default_decimals: int = 3) -> str:
    """Formate un nombre comme la cellule d'origine (décimales, séparateur)."""
    sample = (sample or '').strip()
    sep = ',' if (',' in sample and '.' not in sample) else '.'
    frac = 0
    for ch in ('.', ','):
        if ch in sample:
            frac = len(sample.rsplit(ch, 1)[1])
            break
    else:
        frac = default_decimals
    frac = min(max(frac, 1), 6)
    out = f"{value:.{frac}f}"
    return out.replace('.', sep) if sep == ',' else out


def write_corrected_csv(src_csv: str, dst_csv: str, stations: Sequence[Station],
                        write_yaw: Optional[bool] = None,
                        eye: float = EYE_HEIGHT_DEFAULT,
                        mapping: Optional[Dict[str, str]] = None) -> Tuple[int, int, bool]:
    """Écrit le CSV corrigé : chaque bulle avec ses bonnes valeurs.

    X / Y corrigés, Z = plancher + delta + hauteur instrument (corrections
    comprises), delta et hauteur dans leurs colonnes (ajoutées si une
    correction les demande et qu'elles manquent), Δ nord.

    Rien n'est destructif : le fichier source n'est pas touché, les images non
    plus. La correction d'orientation est rangée dans une colonne dédiée
    (« Delta Nord (deg) », créée si elle manque) ; la colonne « % NORD » garde
    sa valeur d'origine. Tout le reste est recopié à l'identique : colonnes,
    ordre, séparateur, encodage, fins de ligne, décimales, lignes intactes.

    `write_yaw` : None = colonne écrite dès qu'une bulle porte un Δ nord.

    Retourne (lignes modifiées, lignes recopiées, colonne Δ nord ajoutée).
    """
    text, enc = _read_text_enc(src_csv)
    lines = text.splitlines(keepends=True)
    if not lines:
        raise ValueError("CSV source vide.")

    delim = _sniff_delimiter(text)
    head_body = lines[0].rstrip('\r\n')
    head_eol = lines[0][len(head_body):]
    head_cells = next(csv.reader([head_body], delimiter=delim))
    n_num = sum(1 for c in head_cells if parse_float(c) is not None)
    headerless = n_num >= 2 and n_num >= len([c for c in head_cells if c.strip()]) - 1
    header = [norm_key(c) for c in head_cells]
    col: Dict[str, int] = {}
    if mapping:
        for field_name, ref in mapping.items():
            if not ref:
                continue
            if ref.startswith('#') and ref[1:].isdigit():
                col[field_name] = int(ref[1:])
            elif not headerless and norm_key(ref) in header:
                col[field_name] = header.index(norm_key(ref))
    elif not headerless:
        col = {k: v for k, v in auto_columns(header).items()
               if k in ('photo', 'x', 'y', 'z', 'dnord', 'key', 'hcam', 'delta')}
    # unités d'origine (« Hauteur/cm ») : on réécrit dans la même unité
    scale = {f: (unit_scale(head_cells[col[f]]) if f in col and not headerless
                 and col[f] < len(head_cells) else 1.0) for f in ('hcam', 'delta')}
    if 'photo' not in col and 'key' in col:
        col['photo'] = col['key']
    if 'photo' not in col:
        raise ValueError("Identifiant (« Fichier photo » ou « N° scan ») introuvable "
                         "dans le CSV source.")
    if headerless:
        # pas d'en-tete : la 1re ligne est une donnee, on la traite comme les autres
        lines = [''] + lines
        head_body, head_eol = '', ''
    by_key = {st.key.lower(): st for st in stations}
    by_photo = {st.photo.lower(): st for st in stations}

    def valeurs(st: Station):
        """Bonnes valeurs d'une bulle : position, altitude calculée et ses composantes."""
        out = [('x', st.x), ('y', st.y), ('z', st.z)]
        if 'hcam' in col:
            out.append(('hcam', st.height(eye) / scale['hcam']))
        if 'delta' in col:
            out.append(('delta', st.delta(eye) / scale['delta']))
        return out

    # Colonnes ajoutées au besoin : Δ nord, et les composantes d'altitude
    # corrigées que le relevé ne porte pas encore.
    need_yaw = (any(st.has_yaw() or st.turned() for st in stations)
                if write_yaw is None else bool(write_yaw))
    extras: List[Tuple[str, str, object]] = []
    if need_yaw and 'dnord' not in col:
        extras.append(('dnord', YAW_COLUMN, lambda st: st.yaw_fix))
    if 'delta' not in col and any(st.shifted() for st in stations):
        extras.append(('delta', 'Delta', lambda st: st.delta(eye)))
    if 'hcam' not in col and any(st.raised() for st in stations):
        extras.append(('hcam', 'Hauteur instrument', lambda st: st.height(eye)))

    out: List[str] = ([] if headerless else
                      [head_body + ''.join(delim + h for _, h, _ in extras) + head_eol])
    n_mod = n_keep = 0
    for raw in lines[1:]:
        body = raw.rstrip('\r\n')
        eol = raw[len(body):]
        if not body.strip():
            out.append(raw)
            continue
        quoted = '"' in body
        try:
            fields = (next(csv.reader([body], delimiter=delim)) if quoted
                      else body.split(delim))
        except Exception:
            out.append(body + delim * len(extras) + eol)
            n_keep += 1
            continue
        st = None
        if 'key' in col and col['key'] < len(fields) and fields[col['key']].strip():
            st = by_key.get(fields[col['key']].strip().lower())
        if st is None and col['photo'] < len(fields):
            st = by_photo.get(base_name(fields[col['photo']]).lower())

        changed = False
        if st is not None:
            # chaque cellule reçoit sa bonne valeur, au format d'origine
            for name, value in valeurs(st):
                i = col.get(name, -1)
                if 0 <= i < len(fields):
                    if not fields[i].strip() and (
                            (name == 'delta' and abs(value) < 5e-4)
                            or (name == 'hcam' and st.h0 is None and not st.raised())):
                        continue              # cellule vide = valeur par défaut : on la laisse
                    txt = _format_like(fields[i], value)
                    if txt != fields[i].strip():
                        fields[i] = txt
                        changed = True
            if need_yaw and 0 <= col.get('dnord', -1) < len(fields):
                txt = _format_like(fields[col['dnord']], st.yaw_fix, default_decimals=4)
                if txt != fields[col['dnord']].strip():
                    fields[col['dnord']] = txt
                    changed = True
        if not changed and not extras:
            out.append(raw)
            n_keep += 1
            continue
        for _name, _head, fn in extras:
            fields.append(_format_like('', fn(st), default_decimals=4 if _name == 'dnord'
                                       else 3) if st is not None else '')
        if quoted:                             # ligne avec guillemets : réécriture csv
            import io
            buf = io.StringIO()
            csv.writer(buf, delimiter=delim, lineterminator='').writerow(fields)
            out.append(buf.getvalue() + eol)
        else:                                  # cas courant : substitution en place
            out.append(delim.join(fields) + eol)
        if changed or (st is not None and st.modified()):
            n_mod += 1
        else:
            n_keep += 1

    tmp = dst_csv + '.tmp'
    with open(tmp, 'w', encoding=enc, newline='') as fh:
        fh.write(''.join(out))
    os.replace(tmp, dst_csv)
    return n_mod, n_keep, bool(extras)


def rotate_pano_file(src_path: str, dst_path: str, delta_deg: float) -> Tuple[int, int]:
    """Écrit l'image tournée en lacet de `delta_deg` (rotation cyclique).

    Le décalage est arrondi au pixel : sur un panorama 16000 px de large, le
    pas vaut 0,0225° — aucune interpolation, donc aucun flou introduit. Les
    tables de quantification JPEG et l'EXIF de la source sont conservés pour
    limiter la perte au seul ré-encodage.

    Retourne (largeur, décalage appliqué en pixels).
    """
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    with Image.open(src_path) as im:
        im.load()
        w, h = im.size
        shift = int(round(delta_deg / 360.0 * w)) % w
        if shift == 0:
            out = im.copy()
        else:
            out = Image.new(im.mode, (w, h))
            out.paste(im.crop((w - shift, 0, w, h)), (0, 0))
            out.paste(im.crop((0, 0, w - shift, h)), (shift, 0))
        params = {}
        ext = os.path.splitext(dst_path)[1].lower()
        fmt = 'JPEG' if ext in ('.jpg', '.jpeg') else (im.format or 'PNG')
        if ext in ('.jpg', '.jpeg'):
            params['quality'] = JPEG_QUALITY_FALLBACK
            qt = getattr(im, 'quantization', None)
            if qt:
                params['qtables'] = qt
                params.pop('quality', None)
            try:
                from PIL import JpegImagePlugin
                sub = JpegImagePlugin.get_sampling(im)
                if sub in (0, 1, 2):
                    params['subsampling'] = sub
            except Exception:
                pass
            if im.info.get('progressive'):
                params['progressive'] = True
            params['optimize'] = False
        exif = im.info.get('exif')
        if exif:
            params['exif'] = exif
        icc = im.info.get('icc_profile')
        if icc:
            params['icc_profile'] = icc
        tmp = dst_path + '.tmp'
        out.save(tmp, format=fmt, **params)
        out.close()
    os.replace(tmp, dst_path)
    return w, shift


def export_rotated_images(stations: Sequence[Station], paths: Dict[str, str],
                          out_dir: str, workers: int = 2,
                          progress=None, cancel: "threading.Event" = None
                          ) -> Tuple[int, int, List[str]]:
    """Exporte les images dont l'orientation a été corrigée.

    `workers` reste bas par défaut : un panorama 16000×8000 mobilise environ
    800 Mo par tâche (source + destination décompressées).

    Retourne (exportées, ignorées, erreurs).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    todo = [s for s in stations if s.has_yaw() and s.photo.lower() in paths]
    os.makedirs(out_dir, exist_ok=True)
    errors: List[str] = []
    done = 0

    def one(st: Station) -> None:
        src = paths[st.photo.lower()]
        dst = os.path.join(out_dir, os.path.basename(src))
        if os.path.abspath(dst) == os.path.abspath(src):
            raise ValueError("destination identique à la source")
        rotate_pano_file(src, dst, st.yaw_fix)

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = {pool.submit(one, st): st for st in todo}
        for fut in as_completed(futures):
            st = futures[fut]
            done += 1
            try:
                fut.result()
            except Exception as exc:
                errors.append(f"{st.photo} : {exc}")
            if progress:
                progress(done, len(todo), st.photo)
            if cancel is not None and cancel.is_set():
                for f in futures:
                    f.cancel()
                break
    return done - len(errors), len(todo) - done, errors


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

    def frame_mb(self) -> float:
        """Mémoire occupée par une bulle décodée (équirectangulaire 2:1)."""
        w = self.src_width
        return w * (w / 2.0) * 3 / (1024.0 * 1024.0)

    def effective_cache(self) -> int:
        """Nombre de bulles réellement gardées : le réglage, plafonné par
        l'enveloppe mémoire (une source 8192 pèse 96 Mo, une 16384 en pèserait
        384 — le plafond évite de saturer la machine)."""
        budget = max(1, int(MEMORY_BUDGET_MB / max(1.0, self.frame_mb())))
        return max(2, min(self.cache_size, budget))

    def bind_stations(self, stations: Sequence[Station]) -> int:
        """Rattache chaque bulle à son fichier, même si le nom sur disque est
        le numéro de scan (0347.jpg) ou le nom projeté plutôt que la colonne
        « Fichier photo ». Retourne le nombre de rattachements par alias."""
        n = 0
        with self._lock:
            for st in stations:
                photo = st.photo.lower()
                if photo in self._paths:
                    continue
                for cand in st.name_candidates():
                    path = self._paths.get(cand.lower())
                    if path:
                        self._paths[photo] = path
                        n += 1
                        break
        return n

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
        limit = self.effective_cache()
        while len(self._cache) > limit:
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
        f = lens_focal(fov, w)
        d = lens_mix(fov)
        xs = np.linspace(-w / 2.0, w / 2.0, w, dtype=np.float32)
        ys = np.linspace(-h / 2.0, h / 2.0, h, dtype=np.float32)
        gx, gy = np.meshgrid(xs, ys)
        rays = np.empty((3, w * h), dtype=np.float32)
        if d == 0.0:
            norm = np.sqrt(f * f + gx * gx + gy * gy, dtype=np.float32)
            rays[0] = (f / norm).reshape(-1)
            rays[1] = (gx / norm).reshape(-1)
            rays[2] = (-gy / norm).reshape(-1)
        else:                             # grand angle : inverse exact de lens_g
            rpx = np.sqrt(gx * gx + gy * gy, dtype=np.float32)
            rho = rpx / np.float32(f)
            r = np.sqrt((d + 1.0) ** 2 + rho * rho, dtype=np.float32)
            th = np.arctan2(rho, np.float32(d + 1.0)) + np.arcsin(
                np.clip(rho * np.float32(d) / r, -1.0, 1.0))
            sth = np.sin(th)
            safe = np.where(rpx < 1e-6, np.float32(1.0), rpx)
            rays[0] = np.cos(th).reshape(-1)
            rays[1] = (sth * gx / safe).reshape(-1)
            rays[2] = (-sth * gy / safe).reshape(-1)
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


def compute_hotspots(stations: Sequence[Station], links: Sequence["Link"],
                     idx: int, view: View, calib: Calib, filters: HotspotFilter,
                     has_image, eye: float = EYE_HEIGHT_DEFAULT,
                     disc: float = DISC_RADIUS_M, r_min: float = DISC_PX_MIN,
                     r_max: float = DISC_PX_MAX, anchor: str = 'sol'
                     ) -> Tuple[List["Hotspot"], int]:
    """Pastilles projetées dans une vue, filtres compris.

    Fonction partagée par la vue principale et la vue de comparaison : les deux
    obtiennent exactement la même géométrie.

    `anchor` : 'sol' pose la pastille au sol de la cible (plancher + delta) ;
    'vue' la place au point de vue (sol + hauteur appareil), le pied du mât
    restant au sol.

    Retourne (pastilles du plus loin au plus près, nombre de pastilles masquées).
    """
    if not (0 <= idx < len(stations)) or idx >= len(links):
        return [], 0
    st = stations[idx]
    retenus = [lk for lk in links[idx]
               if filters.accepts(st, stations[lk.target], lk,
                                  has_image(stations[lk.target].photo))]
    masques = len(links[idx]) - len(retenus)
    f = view.focal()
    out: List[Hotspot] = []
    for lk in retenus:
        tgt = stations[lk.target]
        dh = lk.dist_h
        psi = calib.pano_yaw(lk.azimuth, st.north_pct)

        def at(dz: float):
            elev = (math.degrees(math.atan2(dz, dh)) if dh > 1e-6
                    else (90.0 if dz > 0 else -90.0))
            return project(view, psi, elev)

        dz_sol = tgt.ground(eye) - st.z
        sol = at(dz_sol)                          # sol de la cible : plancher + delta
        foot = None
        foot_r = (0.0, 0.0)
        if anchor == 'vue':
            pr = at(tgt.z - st.z)                 # point de vue : sol + hauteur appareil
            if sol is not None:
                foot = (sol[0], sol[1])
                # ombre : disque au sol, du rayon de la sphère, vu en perspective
                d_sol = math.hypot(dh, dz_sol)
                rx = f * SPHERE_RADIUS * disc / max(d_sol, 0.35)
                squash = abs(dz_sol) / max(d_sol, 1e-6)      # sinus de la plongée
                foot_r = (rx, rx * clamp(squash, 0.06, 1.0))
        else:
            pr = sol
        if pr is None:
            continue
        col, row, _ = pr
        if not (-80 <= col <= view.width + 80 and -80 <= row <= view.height + 80):
            continue
        # rayon a l'ecran = focale x rayon physique / distance, borne des deux cotes
        radius = clamp(f * disc / max(lk.dist, 0.35), r_min, r_max)
        out.append(Hotspot(lk, col, row, radius, tgt.locator, foot, foot_r))
    out.sort(key=lambda h: -h.link.dist)     # les plus lointaines dessinees d'abord
    return out, masques


SPHERE_RADIUS = 0.74     # rayon de la sphere, en fraction du rayon de pastille
SHADOW_RX, SHADOW_RY = 1.00, 0.36   # demi-axes de l'ombre au sol (fractions)
SPHERE_LIFT = 0.86       # centre de la sphere au-dessus du sol (fraction de rs)
HALO_RADIUS = 1.75       # rayon du halo de survol (fraction de rs)
HALO_COLOR = (255, 255, 235)

# Repère XYZ du mode édition (centré sur la position d'origine du CSV)
AXIS_COLORS = {'x': '#ff5c5c', 'y': '#4cd964', 'z': '#4da3ff'}
AXIS_NAMES = {'x': 'X Est', 'y': 'Y Nord', 'z': 'Z'}
AXIS_PX = 90.0                 # longueur visée d'un demi-axe à l'écran (px)
AXIS_LEN_LIMITS = (0.3, 5.0)   # bornes de cette longueur en mètres
AXIS_AUTO_PX = 6               # déplacement souris qui choisit l'axe (mode auto)
AXIS_HIT_PX = 6                # tolérance de saisie d'un axe
AXIS_Z_PX_M = 0.002            # m par pixel pour Z sur la bulle active (verticale vue de dessus)
DRAG_AXIS_MODES = ('auto', 'x', 'y', 'z')


def sphere_sprite(color: str, r: int, hover: bool = False, ss: int = 2,
                  floating: bool = False):
    """Pastille en relief : sphère éclairée reposant sur son ombre portée.

    Retourne (image RGBA PIL, (ax, ay)) où (ax, ay) est le point du sol dans
    l'image — c'est lui qui se place sur la position projetée de la bulle.
    `floating` : sphère suspendue au point de vue, sans ombre attachée (son
    ombre est dessinée au sol, en perspective) ; l'ancre est alors son centre.
    Sur-échantillonnage `ss` pour des bords lisses ; ~4 ms par sprite, mis en
    cache par l'application.
    """
    import numpy as np
    from PIL import Image, ImageFilter
    R = max(3, int(r)) * ss
    rs = SPHERE_RADIUS * R
    sx, sy = SHADOW_RX * R, SHADOW_RY * R
    blur = max(1.0, 0.10 * R)
    pad = int(2 * blur + 2 * ss)
    rh = HALO_RADIUS * rs if hover else 0.0        # halo de surbrillance
    W = int(2 * max(sx, rh)) + 2 * pad
    gy = int(max(2 * rs * 0.95, rh + rs * SPHERE_LIFT) + pad)
    H = int(gy + sy + 2 * pad)
    cx = W / 2.0
    cy = gy - rs * SPHERE_LIFT
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)

    # ombre portée : ellipse douce, un peu decalee (lumiere en haut a gauche)
    ox, oy = cx + 0.06 * R, gy + 0.10 * sy
    d = np.sqrt(((xx - ox) / sx) ** 2 + ((yy - oy) / sy) ** 2)
    shadow_a = np.clip((1.0 - d) / 0.35, 0, 1) * (0.0 if floating else 0.55)
    shadow = Image.fromarray((shadow_a * 255).astype(np.uint8), 'L').filter(
        ImageFilter.GaussianBlur(blur))
    out = np.zeros((H, W, 4), np.float32)
    out[..., 3] = np.asarray(shadow, np.float32) / 255.0

    if hover:
        # halo : lueur douce autour de la sphere, bien visible sur toute photo
        dh_ = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max(rh, 1.0)
        halo_a = np.clip(1.0 - dh_, 0, 1) ** 1.6 * 0.75
        halo_rgb = np.array(HALO_COLOR, np.float32) / 255.0
        a_old = out[..., 3]
        a_new = halo_a + a_old * (1 - halo_a)
        out[..., :3] = ((halo_rgb[None, None, :] * halo_a[..., None]
                         + out[..., :3] * (a_old * (1 - halo_a))[..., None])
                        / np.maximum(a_new, 1e-6)[..., None])
        out[..., 3] = a_new

    # sphere : lambert + speculaire + assombrissement du bord
    h = color.lstrip('#')
    base = np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255.0
    if hover:
        base = np.clip(base * 1.12 + 0.05, 0, 1)
    nx = (xx - cx) / rs
    ny = (yy - cy) / rs
    d2 = nx * nx + ny * ny
    nz = np.sqrt(np.clip(1.0 - d2, 0, 1))
    lx, ly, lz = -0.45, -0.60, 0.66
    nl = math.sqrt(lx * lx + ly * ly + lz * lz)
    ndotl = np.clip((nx * lx + ny * ly + nz * lz) / nl, 0, 1)
    light = (0.30 + 0.70 * ndotl) * (1.0 - np.clip((d2 - 0.55) / 0.45, 0, 1) * 0.35)
    spec = ndotl ** 40 * 0.55
    rgb = np.clip(base[None, None, :] * light[..., None] + spec[..., None], 0, 1)
    edge = np.clip((1.0 - np.sqrt(d2)) * rs / ss, 0, 1)
    a_s = np.where(d2 <= 1.0, edge, 0.0)
    a_old = out[..., 3]
    a_new = a_s + a_old * (1 - a_s)
    out[..., :3] = ((rgb * a_s[..., None] + out[..., :3] * (a_old * (1 - a_s))[..., None])
                    / np.maximum(a_new, 1e-6)[..., None])
    out[..., 3] = a_new
    img = Image.fromarray((out * 255).astype(np.uint8), 'RGBA')
    if ss > 1:
        img = img.resize((W // ss, H // ss), Image.LANCZOS)
    return img, (cx / ss, (cy if floating else gy) / ss)


def hotspot_hit(hotspots: Sequence["Hotspot"], x: float, y: float,
                relief: bool = True) -> Optional[int]:
    """Pastille sous le curseur : celle du dessus, comme à l'écran.

    Les pastilles sont dessinées de la plus lointaine à la plus proche : on les
    parcourt à l'envers et la première touchée l'emporte. Indispensable quand
    les bulles sont au point de vue : toutes celles du plancher s'alignent sur
    l'horizon et se recouvrent.
    """
    for i in range(len(hotspots) - 1, -1, -1):
        hs = hotspots[i]
        r = hs.radius
        if hs.foot is not None:             # sphère au point de vue : centrée sur lui
            rs = (SPHERE_RADIUS * r if relief else r) + HIT_SLACK_PX
            if (x - hs.col) ** 2 + (y - hs.row) ** 2 <= rs * rs:
                return i
            continue
        rx, ry = r + HIT_SLACK_PX, r * 0.55 + HIT_SLACK_PX
        d = ((x - hs.col) / rx) ** 2 + ((y - hs.row) / ry) ** 2
        if relief:
            rs = SPHERE_RADIUS * r
            cy = hs.row - rs * SPHERE_LIFT
            ds = ((x - hs.col) ** 2 + (y - cy) ** 2) / (rs + HIT_SLACK_PX) ** 2
            d = min(d, ds)
        if d <= 1.0:
            return i
    return None


class Tooltip:
    """Infobulle d'aide : apparaît après un court survol, disparaît au départ.

    Une seule fenêtre par widget, créée à la demande ; aucun effet sur les
    liaisons existantes (ajout avec add='+').
    """
    DELAY_MS = 450

    def __init__(self, widget, text: str):
        self.widget, self.text = widget, text
        self._job = None
        self._win = None
        widget._bn_tip = text
        widget.bind('<Enter>', self._schedule, add='+')
        widget.bind('<Leave>', self._hide, add='+')
        widget.bind('<ButtonPress>', self._hide, add='+')

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                self.widget.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _show(self) -> None:
        self._job = None
        if self._win is not None or not self.text:
            return
        try:
            x, y = self.widget.winfo_pointerxy()
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.attributes('-topmost', True)
            tk.Label(win, text=self.text, justify='left', font=F_UI, wraplength=380,
                     bg='#fff8dc', fg='#202020', relief='solid', bd=1, padx=6, pady=3
                     ).pack()
            win.update_idletasks()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            win.geometry(f"+{max(0, min(x + 14, sw - w - 4))}+{max(0, min(y + 18, sh - h - 4))}")
            self._win = win
        except Exception:
            self._win = None

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:
                pass
            self._win = None


# Aide générale (F1 ou ?) : tous les raccourcis
HELP_TEXT = """\
NAVIGATION  (vue A ou B)
  Clic sur une pastille ...... y aller (sens de navigation conservé)
  Ctrl+clic ou clic droit .... SONDER : la bulle s'ouvre dans l'autre vue et
                               les deux se font face (chacune voit l'autre)
  Glisser .................... tourner la vue
  Double-clic ................ recentrer la vue sur ce point
  Molette, + / - ............. champ de vision, 30° à 200° (grand angle > 110°),
                               cran à 120° ; bouton « 120° » : les deux vues
  Flèches (Maj = pas large) .. tourner
  Origine (Home) ............. redresser la vue
  Entrée, ou Espace bref ..... avancer vers la pastille la plus centrale
  Espace + glisser ........... DÉPLACER EN PLAN la station active (voir plus bas)
  Retour arrière ............. revenir à la bulle précédente
  En-tête de chaque vue ...... Retour, Origine, H et Δ lus de la bulle de la vue ;
                               celui de B lie les deux vues (face à face,
                               inverser, A → B, B → A)
  O .......................... regarder d'où l'on vient
  G .......................... face à face : A regarde B, B regarde A
  I .......................... inverser A et B (bulles et regards)
  Ctrl+Z ..................... annuler la dernière opération (navigation,
                               correction, bulle ouverte en B)

CORRIGER SANS LE MODE ÉDITION — seule la STATION ACTIVE (vue A) est modifiée
  Alt + molette .............. Δ (delta plancher) de la station active
  Maj + molette .............. H (hauteur station) de la station active
  Espace + glisser dans A .... position en plan, libre dans toutes les directions :
                               on « attrape » le sol ; les pastilles d'avant
                               restent visibles en transparence
  Espace + glisser dans B .... la pastille de la station active suit le curseur ;
                               sa position CSV d'origine reste en transparence
  X / Y (pendant Espace) ..... verrouiller un axe (2e appui : libre)
  Corriger une voisine ....... Ctrl+clic dessus (elle s'ouvre en B), puis I
                               pour l'échanger avec A : elle devient active
  Pas de la molette .......... 5 cm (1 cm possible, dans Réglages)
  Mire ....................... empreinte au sol (50 cm) et mire graduée du sol
                               à la caméra, sur la bulle de l'autre vue et la
                               pastille survolée : elle doit se poser à plat
                               sur le sol de la photo

AFFICHAGE
  Liste « Voir » ............. pastilles montrées : Local, Locaux voisins
                               (défaut : le local et ceux à moins de 6 m),
                               Distance, Plancher entier
  L / T ...................... Voir : Local / Plancher entier (2e appui : retour)
  M .......................... module (fichiers, état)
  F .......................... activer / couper les filtres
  Filtres › Local ............ choisir un local dans la liste
  Menu « Affichage » ......... étiquettes (nom, distance, H / Δ / Z), couleur
                               par local ou par lien, pastille au sol ou au
                               point de vue, regarder d'où l'on vient,
                               infobulle au survol (courte ; masquée pendant
                               une modification)
  F11 / Échap ................ plein écran
  V .......................... afficher le visualiseur
  F1 ou ? .................... cette aide

COMPARAISON  (touche C)
  C .......................... ouvrir / fermer la vue B
  « Vue liée » ............... A et B regardent la même direction terrain
  « Suivi de A » ............. B suit A : même local à un autre plancher, ou
                               la bulle la plus proche
  « A → B » / « B → A » ...... recopier une vue dans l'autre (bulle et regard)
  Ctrl+clic dans B ........... la bulle s'ouvre dans A, face à face

PLAN
  Clic gauche ................ aller sur la bulle la plus proche
  Clic droit ................. ouvrir la bulle dans la vue B
  Clic droit glissé .......... déplacer le plan
  Molette .................... zoom
  Survol ..................... nom de la bulle
  Liste « Plancher » ......... changer de niveau

ÉDITION  (touche E) — rien n'est écrit sur le disque en direct
  Clic sur une pastille ...... la prendre pour cible
  Glisser une pastille ....... la déplacer le long d'un axe (auto X ou Y)
  Glisser un axe du repère ... suivre cet axe
  X / Y / Z .................. verrouiller l'axe (2e appui : auto)
  Ctrl + glisser ............. déplacer la bulle active (sur un axe)
  Ctrl + clic (sans glisser) . sonder, comme hors édition
  Maj + glisser .............. tourner l'image (Δ nord)
  Page haut / bas ............ hauteur station ± pas
  Maj + Page haut / bas ...... delta plancher ± pas
  Glisser un point du plan ... le déplacer en X ou en Y
  Ctrl+Z ..................... annuler

FICHIERS
  Ctrl+S ..................... « Appliquer / enregistrer » : CSV corrigé (bonnes
                               valeurs), images orientées dans un autre dossier
  Les corrections s'enregistrent en continu dans leur propre fichier ;
  le CSV chargé et les images d'origine ne sont jamais modifiés.
"""


# Aide des boutons, par libellé (un bouton peut aussi recevoir la sienne à la création)
BUTTON_TIPS: Dict[str, str] = {
    "Relevé CSV…": "Choisir le relevé CSV (positions, orientation, plancher).",
    "Dossier images…": "Choisir le dossier des images bulles (JPEG équirectangulaires).",
    "Corrections…": "Choisir le fichier de corrections (séparé du relevé, jamais écrasé).",
    "Ouvrir le visualiseur  (V)": "Afficher la fenêtre des vues bulle, du plan et des outils.",
    "Réglages…": "Calibration du nord, hauteur instrument, taille des pastilles, "
                 "réseau (portée, nombre, séparation), mémoire.",
    "Appliquer / enregistrer…": "Bilan des corrections : enregistrer, tourner les images "
                                "par lot, écrire un relevé complet corrigé.",
    "Appliquer / enregistrer…  (Ctrl+S)": "Bilan des corrections : enregistrer, tourner les "
                                          "images par lot, écrire un relevé complet corrigé.",
    "Aide": "Raccourcis clavier et gestes (F1).",
    "?": "Aide : tous les raccourcis clavier et gestes (F1).",
    "Quitter": "Fermer le programme (les corrections sont enregistrées).",
    "Module": "Revenir au module principal (fichiers, état, réglages).",
    "Comparer  (C)": "Ouvrir / fermer la seconde vue bulle (B), sous la première.",
    "Édition  (E)": "Mode édition : orientation, position XY, hauteur station, delta "
                    "plancher. Rien n'est écrit dans les images.",
    "✕": "Masquer le visualiseur (touche V pour le rouvrir).",
    "⛶": "Plein écran (F11), Échap pour en sortir.",
    "Recadrer": "Recadrer le plan sur tout le plancher affiché.",
    "◀ Retour": "Revenir à la bulle précédente (Retour arrière, ou Ctrl+Z).",
    "▸": "Déplier / replier les réglages de filtre.",
    "Réinitialiser les filtres": "Remettre tous les filtres à zéro.",
    "Bulle active": "Prendre la bulle affichée comme cible d'édition.",
    "−": "Diminuer d'un pas (liste « pas ») la valeur de cette ligne.",
    "+": "Augmenter d'un pas (liste « pas ») la valeur de cette ligne.",
    "Appliquer les valeurs saisies": "Appliquer X, Y, hauteur station et delta saisis "
                                     "(Entrée dans un champ fait de même).",
    "−0,5°": "Tourner l'image de −0,5° (correction Δ nord).",
    "−0,05°": "Tourner l'image de −0,05° (correction Δ nord).",
    "+0,05°": "Tourner l'image de +0,05° (correction Δ nord).",
    "+0,5°": "Tourner l'image de +0,5° (correction Δ nord).",
    "ce plancher": "Appliquer le Δ nord de cette bulle à tout son plancher.",
    "tout le relevé": "Appliquer le Δ nord de cette bulle à tout le relevé.",
    "Fichier…": "Choisir un autre fichier de corrections.",
    "Annuler (Ctrl+Z)": "Annuler la dernière opération : correction ou déplacement "
                        "d'une bulle à l'autre.",
    "Réinit. cible": "Rendre à la cible ses valeurs du relevé (annulable).",
    "Réinit. tout": "Annuler toutes les corrections (confirmation demandée).",
    "⇄ Échanger": "Échanger les bulles des vues A et B.",
    "A → B": "Afficher dans B la bulle de la vue A.",
    "Parcourir…": "Choisir le dossier de sortie.",
    "Enregistrer les corrections maintenant": "Écrire tout de suite le fichier de "
                                              "corrections (sinon automatique).",
}


@dataclass
class Hotspot:
    """Pastille projetee dans la vue."""
    link: Link
    col: float
    row: float
    radius: float
    label: str
    foot: Optional[Tuple[float, float]] = None   # pied du mât (pastille au point de vue)
    foot_r: Tuple[float, float] = (0.0, 0.0)     # ombre au sol : demi-axes à l'écran (px)


class BubbleNavApp(_TkBase):
    """Fenetre principale : vue bulle + pastilles cliquables + plan."""

    def __init__(self, cfg: dict, csv_path: str = '', images_dir: str = ''):
        super().__init__()
        self.cfg = cfg
        self.stations: List[Station] = []
        self.links: List[List[Link]] = []
        self.plan_edges: List[Tuple[int, int]] = []    # squelette du plan
        self.floors: List[str] = []
        self.current: int = -1
        self.history: List[int] = []
        # Journal commun des opérations annulables par Ctrl+Z, de la plus
        # ancienne à la plus récente : ('edit',) une correction ;
        # ('nav', idx, yaw, pitch, fov) un passage d'une bulle à l'autre dans A ;
        # ('nav_b', idx, yaw, pitch, fov) la même chose dans la vue B.
        self.journal: List[tuple] = []
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
        self.filters = HotspotFilter(
            active=bool(cfg.get('filter_active', False)),
            floor_mode=str(cfg.get('filter_floor', 'tous')),
            max_dist=float(cfg.get('filter_dist', 0.0)),
            local=str(cfg.get('filter_local', '')),
            inter_floor=bool(cfg.get('filter_inter', True)),
            hide_missing=bool(cfg.get('filter_hide_missing', False)),
            same_local=bool(cfg.get('filter_same_local', False)),
            scope=str(cfg.get('view_scope', 'voisins')),
            scope_dist=float(cfg.get('scope_dist', 6.0)))
        self.filters.near_locals = self._near_locals
        self._near_cache: Dict[Tuple[int, float], frozenset] = {}
        self.hidden_count = 0
        self.focus_idx: Optional[int] = None      # bulle décrite dans le panneau
        self.came_from: Optional[int] = None      # bulle quittée (A), pour s'y retourner
        self._hover: Optional[int] = None
        # Edition
        self.corrections = Corrections()
        self.corrections.on_record = self._journal_edit
        self.by_photo: Dict[str, Station] = {}
        self.by_key: Dict[str, Station] = {}
        self.csv_mapping: Optional[Dict[str, str]] = None   # correspondance imposee
        self.incoherences: Dict[int, str] = {}     # étage déduit contredit par Z
        self.selected: Optional[int] = None      # bulle en cours de modification
        self._hs_drag = None                     # geste d'édition en cours
        self._axis_hits: List[Tuple[str, float, float, float, float]] = []
        self._axis_labels: List[Tuple[float, float, str, str]] = []
        self._axis_origin_px: Optional[Tuple[float, float]] = None
        self._sync_ui = False                    # garde anti-boucle des widgets
        self._hover_xy = None
        self._cone_sig = None                    # état du camembert du plan
        self._sprites: "OrderedDict[tuple, tuple]" = OrderedDict()   # sphères
        self._cmp_sig = None                     # état de synchro de la vue B
        self._last_current = -1
        self._plan_hit = None                    # deplacement sur le plan
        self._plan_hover: Optional[int] = None   # station survolée sur le plan
        self._plan_axis: Optional[str] = None    # axe du glisser sur le plan
        self._plan_start = (0.0, 0.0)
        self._autosave_job = None
        self._graph_job = None
        self._pump_job = None
        self._frame_view: Optional[View] = None
        self._tk_img = None
        self._drag: Optional[Tuple[int, int, float, float]] = None
        self._interactive = False
        self._idle_job = None
        self._plan_view = {'scale': 1.0, 'ox': 0.0, 'oy': 0.0, 'fitted': False}
        self._plan_drag = None
        self._plan_floor = ''

        # Rendu asynchrone : 1 thread, derniere demande gagnante par vue
        # (« A » = vue principale, « B » = vue de comparaison)
        self._reqs: "OrderedDict[str, tuple]" = OrderedDict()
        self.compare: Optional["CompareView"] = None
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
        self._pump_job = self.after(UI_PUMP_MS, self._pump_ui)

        if csv_path:
            self.after(60, lambda: self.load_csv(csv_path, images_dir))
        elif images_dir:
            self.after(60, lambda: self.set_images_dir(images_dir))

    # ═════════════════════════════════════════════════════════════════
    # CONSTRUCTION DE L'INTERFACE
    # ═════════════════════════════════════════════════════════════════
    def _build_ui(self) -> None:
        """Deux fenêtres : le MODULE (fichiers, réglages, état) et le
        VISUALISEUR (vues empilées + panneau latéral). Le module est la racine ;
        fermer le visualiseur ne fait que le masquer."""
        self.title(f"{APP_NAME} v{__version__} — module")
        self.configure(bg=COLORS['bg_dark'])
        self.geometry("580x460+30+30")
        self.minsize(520, 400)
        self.resizable(True, False)

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

        self._build_module()
        self._build_viewer()
        self._attach_tips(self)
        self._bind_keys()

    # ── fenêtre-module ───────────────────────────────────────────────
    def _build_module(self) -> None:
        tk.Label(self, text="BubbleNav — module principal", font=F_TITLE,
                 bg=COLORS['bg_dark'], fg=COLORS['accent']).pack(anchor='w', padx=14,
                                                                 pady=(10, 4))

        def section(title: str) -> tk.Frame:
            tk.Label(self, text=title, font=F_UI_B, bg=COLORS['bg_dark'],
                     fg=COLORS['text']).pack(anchor='w', padx=14, pady=(8, 2))
            fr = tk.Frame(self, bg=COLORS['card'], padx=8, pady=6)
            fr.pack(fill='x', padx=14)
            return fr

        files = section("Fichiers")
        self.module_paths: Dict[str, tk.Label] = {}
        for key, lib, cmd in (('csv', "Relevé CSV", self._open_csv),
                              ('images', "Dossier images", self._open_images),
                              ('corr', "Corrections", self._choose_corrections_file)):
            row = tk.Frame(files, bg=COLORS['card'])
            row.pack(fill='x', pady=1)
            self._mk_button(row, lib + "…", cmd, width=16).pack(side='left')
            lbl = tk.Label(row, text="—", anchor='w', font=F_UI, bg=COLORS['card'],
                           fg=COLORS['text_muted'])
            lbl.pack(side='left', fill='x', expand=True, padx=8)
            self.module_paths[key] = lbl

        etat = section("État")
        self.module_state = tk.Label(etat, text="Aucun relevé chargé.", justify='left',
                                     anchor='w', font=F_MONO, bg=COLORS['card'],
                                     fg=COLORS['text'])
        self.module_state.pack(fill='x')

        actions = section("Actions")
        self._mk_button(actions, "Ouvrir le visualiseur  (V)", self._show_viewer,
                        bg=COLORS['accent']).pack(side='left')
        self._mk_button(actions, "Réglages…", self._dlg_settings).pack(side='left', padx=6)
        self._mk_button(actions, "Appliquer / enregistrer…", self._dlg_apply
                        ).pack(side='left')
        self._mk_button(actions, "Aide", self._dlg_help).pack(side='left', padx=6)
        self._mk_button(actions, "Quitter", self._on_close).pack(side='right')

        self.module_status = tk.Label(self, text="Chargez un relevé, puis le dossier des images.",
                                      anchor='w', bg=COLORS['bg_medium'],
                                      fg=COLORS['text_muted'], font=F_UI, padx=10, pady=4)
        self.module_status.pack(fill='x', side='bottom')

    def _refresh_module(self) -> None:
        """Chemins et état affichés dans la fenêtre-module."""
        if not hasattr(self, 'module_state'):
            return

        def court(path: str) -> str:
            if not path:
                return "—"
            return f"{os.path.basename(path)}   ({os.path.dirname(path)})"

        self.module_paths['csv'].config(text=court(self.csv_path))
        self.module_paths['images'].config(text=self.images_dir or "—")
        corr = self.corrections.path if self.corrections else ''
        self.module_paths['corr'].config(text=court(corr) if self.stations else "—")
        if not self.stations:
            self.module_state.config(text="Aucun relevé chargé.")
            return
        found = sum(1 for st in self.stations if self.store.has(st.photo))
        bilan = Corrections.counts(self.stations)
        pending = len(Corrections.pending_images(self.stations))
        etat = ("modifications non enregistrées" if self.corrections.dirty
                else "enregistrées")
        self.module_state.config(text=(
            f"bulles        {len(self.stations)}   ·   planchers {len(self.floors)}\n"
            f"images        {found}/{len(self.stations)} trouvées\n"
            f"corrections   {bilan.texte()}  ({etat})\n"
            f"images à tourner   {pending}\n"
            f"visualiseur   {'ouvert' if self._viewer_visible() else 'fermé'}"))

    # ── fenêtre de visualisation ─────────────────────────────────────
    def _build_viewer(self) -> None:
        self.viewer = tk.Toplevel(self)
        self.viewer.title(f"{APP_NAME} — visualiseur")
        self.viewer.configure(bg=COLORS['bg_dark'])
        self.viewer.geometry(str(self.cfg.get('viewer_geometry') or "1500x900"))
        self.viewer.minsize(900, 560)
        self.viewer.protocol('WM_DELETE_WINDOW', self._hide_viewer)
        self.viewer.withdraw()

        self._build_toolbar(self.viewer)

        body = tk.Frame(self.viewer, bg=COLORS['bg_dark'])
        body.pack(fill='both', expand=True)

        # Panneau lateral d'abord : il garde sa largeur quoi qu'il arrive,
        # les vues se partagent le reste.
        self._build_side_panel(body)

        # Vues empilées : A en haut, B (comparaison) en dessous quand elle est ouverte
        self.views = tk.PanedWindow(body, orient='vertical', bg=COLORS['bg_dark'],
                                    sashwidth=6, sashrelief='flat', bd=0,
                                    opaqueresize=True)
        self.views.pack(side='left', fill='both', expand=True)
        self.pane_a = tk.Frame(self.views, bg=COLORS['bg_dark'])
        self.views.add(self.pane_a, minsize=160, stretch='always')
        head = tk.Frame(self.pane_a, bg=COLORS['bg_medium'])
        head.pack(fill='x', side='top')
        tk.Label(head, text="Vue A", font=F_TITLE, bg=COLORS['bg_medium'],
                 fg=COLORS['hot']).pack(side='left', padx=(10, 8), pady=3)
        self.ctrl_vals: Dict[Tuple[str, str], tk.Label] = {}
        self.build_view_header(head, 'A')
        self.canvas = tk.Canvas(self.pane_a, bg='#101010', highlightthickness=0,
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
        for seq in RIGHT_CLICK:
            self.canvas.bind(seq, self._on_right_click)
        self._build_status(self.viewer)

    def _dialog_parent(self):
        """Fenêtre devant laquelle ouvrir un dialogue : le visualiseur s'il est affiché."""
        return self.viewer if self._viewer_visible() else self

    def _attach_dialog(self, win) -> None:
        """Un dialogue s'ouvre devant la fenêtre utilisée, même en plein écran."""
        parent = self._dialog_parent()
        try:
            win.transient(parent)
            win.lift(parent)
            win.after_idle(win.focus_force)
        except Exception:
            pass

    def _viewer_visible(self) -> bool:
        try:
            return self.viewer.state() != 'withdrawn'
        except Exception:
            return False

    def _show_viewer(self) -> None:
        premiere = not getattr(self, '_viewer_shown', False)
        self._viewer_shown = True
        self.viewer.deiconify()
        if premiere:
            self._apply_viewer_start()
        self.viewer.lift()
        try:
            self.viewer.focus_force()
        except Exception:
            pass
        self._refresh_module()

    def _apply_viewer_start(self) -> None:
        """Taille d'ouverture du visualiseur : plein écran (défaut), maximisé,
        ou dernière taille mémorisée."""
        mode = str(self.cfg.get('viewer_start', VIEWER_START_MODES[0]))
        v = self.viewer
        try:
            if mode == 'plein écran':
                self._fs_wanted = True
                v.attributes('-fullscreen', True)
            elif mode == 'maximisé':
                try:
                    v.state('zoomed')                     # Windows
                except Exception:
                    try:
                        v.attributes('-zoomed', True)     # Linux
                    except Exception:
                        v.geometry(f"{v.winfo_screenwidth()}x{v.winfo_screenheight() - 60}+0+0")
            else:
                geo = str(self.cfg.get('viewer_geometry') or '')
                if geo:
                    v.geometry(geo)
        except Exception:
            pass

    def _viewer_fullscreen(self) -> bool:
        """État plein écran : celui demandé par l'outil, ou celui confirmé par
        le gestionnaire de fenêtres (certains ne le relisent pas tout de suite)."""
        if getattr(self, '_fs_wanted', False):
            return True
        try:
            return bool(self.viewer.attributes('-fullscreen'))
        except Exception:
            return False

    def _leave_fullscreen(self) -> None:
        """Échap : sortie du plein écran, retour à la dernière taille connue."""
        if not self._viewer_fullscreen():
            return
        self._fs_wanted = False
        try:
            self.viewer.attributes('-fullscreen', False)
            geo = str(self.cfg.get('viewer_geometry') or '')
            if geo:
                self.viewer.geometry(geo)
        except Exception:
            pass

    def _hide_viewer(self) -> None:
        """Fermer la fenêtre de visualisation ne fait que la masquer."""
        try:
            if not self._viewer_fullscreen():        # une geometrie plein ecran n'est pas une taille
                self.cfg['viewer_geometry'] = self.viewer.geometry()
        except Exception:
            pass
        self.viewer.withdraw()
        self._refresh_module()
        self.lift()

    def _mk_button(self, parent, text, cmd, bg=None, width=None, tip=None):
        b = tk.Button(parent, text=text, command=cmd,
                      bg=bg or COLORS['bg_light'], fg=COLORS['text'],
                      activebackground=COLORS['accent'], activeforeground='white',
                      relief='flat', bd=0, padx=10, pady=4, font=F_UI,
                      cursor='hand2', highlightthickness=0)
        if width:
            b.config(width=width)
        tip = tip or BUTTON_TIPS.get(text)
        if tip:
            Tooltip(b, tip)
        return b

    def tip(self, widget, text: str):
        """Infobulle d'aide sur un contrôle (renvoie le contrôle)."""
        if widget is not None and text and not hasattr(widget, '_bn_tip'):
            Tooltip(widget, text)
        return widget

    def _attach_tips(self, root) -> int:
        """Infobulles des contrôles sans libellé explicite, d'après leur variable
        ou leur texte. Retourne le nombre d'infobulles posées."""
        by_var = {}
        pairs = (
            ('floor_var', "Plancher affiché sur le plan ; en changer rejoint la bulle "
                          "la plus proche à l'aplomb."),
            ('fov_var', "Champ de vision (zoom), aussi à la molette ou + / −."),
            ('qual_var', "Largeur de décodage des images : plus grand = plus net, "
                         "plus lent et plus gourmand en mémoire."),
            ('plan_links_var', "Réseau dessiné sur le plan : squelette (lisible), "
                               "complet (tous les liens) ou aucun."),
            ('filter_var', "Activer / désactiver les filtres de pastilles (F), "
                           "sans perdre les réglages."),
            ('f_floor', "Pastilles d'un plancher seulement (tous, courant, ou un plancher)."),
            ('f_dist', "Distance maximale des pastilles affichées (0 = sans limite)."),
            ('f_local', "Choisir un local dans la liste, ou saisir des motifs séparés "
                        "par des virgules (préfixe ou *) : ex. K256, W25*"),
            ('f_inter', "Garder les pastilles ▲▼ vers les planchers voisins."),
            ('f_missing', "Masquer les pastilles dont l'image est absente du dossier."),
            ('f_same_local', "Seulement les pastilles du local de la bulle courante ; le "
                             "filtre suit la bulle quand on navigue (touche L)."),
            ('step_var', "Pas des boutons + / − (m)."),
            ('drag_axis_var', "Axe suivi par le glisser : auto (X ou Y selon le geste), "
                              "X Est, Y Nord ou Z. Touches X / Y / Z."),
            ('drag_z_var', "L'axe Z corrige le delta plancher (sol et caméra bougent) "
                           "ou la hauteur station (caméra seule)."),
            ('yaw_var', "Δ nord de la bulle active (°) : l'image tourne sous les "
                        "pastilles, rien n'est écrit dans l'image."),
        )
        for attr, text in pairs:
            var = getattr(self, attr, None)
            if var is not None:
                by_var[str(var)] = text
        for axis, text in (('x', "X (Est) de la cible, en m."), ('y', "Y (Nord) de la cible, en m."),
                           ('dh', "Hauteur station : la caméra bouge, le sol reste."),
                           ('ddelta', "Delta plancher : caméra et sol bougent "
                                      "(marche, faux plancher).")):
            var = getattr(self, 'pos_vars', {}).get(axis)
            if var is not None:
                by_var[str(var)] = text
        n = 0
        stack = [root]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if hasattr(w, '_bn_tip'):
                continue
            text = None
            for opt in ('textvariable', 'variable'):
                try:
                    v = str(w.cget(opt))
                except Exception:
                    continue
                if v and v in by_var:
                    text = by_var[v]
                    break
            if text is None:
                try:
                    text = BUTTON_TIPS.get(str(w.cget('text')))
                except Exception:
                    text = None
            if text:
                Tooltip(w, text)
                n += 1
        return n

    def build_view_header(self, bar, which: str) -> None:
        """En-tête d'une vue (A ou B) : les mêmes commandes, pour la bulle de cette vue.

        Retour et Origine, puis la hauteur H et le delta Δ de la bulle affichée,
        avec − / +. L'en-tête de B porte en plus les commandes qui lient A et B.
        """
        mk = self._mk_button
        if which == 'A':
            back, origin = self.go_back, self.look_back
        else:
            back = lambda: self.compare and self.compare.go_back()
            origin = lambda: self.compare and self.compare.look_back()
        for txt, cmd, tip in (
                ("◀ Retour", back, f"Vue {which} : revenir à la bulle précédente."),
                ("↩ Origine", origin, f"Vue {which} : regarder d'où l'on vient (O dans A).")):
            mk(bar, txt, cmd, tip=tip).pack(side='left', padx=2, pady=3)
        if which == 'B':
            for txt, cmd, tip in (
                    ("⇆ Face à face", self.face_a_face,
                     "A regarde B et B regarde A : chaque vue montre la pastille de l'autre (G)."),
                    ("⇄ Inverser", self.swap_ab, "Échanger A et B, bulles et regards compris (I)."),
                    ("A → B", self.a_to_b, "Mettre dans B la bulle et le regard de A."),
                    ("B → A", self.b_to_a, "Mettre dans A la bulle et le regard de B.")):
                mk(bar, txt, cmd, tip=tip).pack(side='left', padx=2, pady=3)
        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y', padx=6, pady=5)
        name = tk.Label(bar, text=which, font=F_UI_B, bg=COLORS['bg_medium'],
                        fg=COLORS['hot'] if which == 'A' else COLORS['sel'], anchor='w')
        name.pack(side='left', padx=(2, 6))
        self.ctrl_vals[(which, 'name')] = name
        for comp, lib in (('dh', 'H'), ('ddelta', 'Δ')):
            val = tk.Label(bar, text=f"{lib} —", font=F_MONO, bg=COLORS['bg_medium'],
                           fg=COLORS['text'], width=9)
            val.pack(side='left')
            Tooltip(val, "Hauteur station (caméra seule) : Maj + molette." if comp == 'dh'
                    else "Delta plancher (caméra et sol) : Alt + molette.")
            self.ctrl_vals[(which, comp)] = val
        self._refresh_ctrlbar()

    def _refresh_ctrlbar(self) -> None:
        """Valeurs de la barre de contrôle, relues à chaque changement."""
        if not hasattr(self, 'ctrl_vals'):
            return
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        b = self.compare.station() if self.compare is not None else None
        for which, st in (('A', self.station()), ('B', b)):
            lbl = self.ctrl_vals.get((which, 'name'))
            try:
                if lbl is None or not lbl.winfo_exists():
                    continue                    # en-tête de B fermé
            except Exception:
                continue
            if st is None:
                self.ctrl_vals[(which, 'name')].config(text=f"{which} —")
                self.ctrl_vals[(which, 'dh')].config(text="H —", fg=COLORS['text_muted'])
                self.ctrl_vals[(which, 'ddelta')].config(text="Δ —", fg=COLORS['text_muted'])
                continue
            nom = st.key if st.key_explicit and st.key and st.key != st.locator else st.locator
            self.ctrl_vals[(which, 'name')].config(text=f"{which} {nom}")
            self.ctrl_vals[(which, 'dh')].config(
                text=f"H {st.height(eye):.3f}",
                fg=COLORS['edit'] if st.raised() else COLORS['text'])
            self.ctrl_vals[(which, 'ddelta')].config(
                text=f"Δ {st.delta(eye):+.3f}",
                fg=COLORS['edit'] if st.shifted() else COLORS['text'])

    # ── sonder, face à face, inverser ────────────────────────────────
    def _aim_a(self, idx: int) -> None:
        cur = self.station()
        if cur is not None and 0 <= idx < len(self.stations) and idx != cur.idx:
            aim_at(self.view, self.calib, cur, self.stations[idx],
                   float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)), self.anchor())
            self._request_render(force=True)

    def sonde(self, idx: int, src: str = 'A') -> None:
        """Ctrl+clic : la bulle visée s'ouvre dans l'autre vue, et les deux se font face.

        Depuis A : B s'ouvre sur la bulle visée, tournée vers A, et A se tourne
        vers elle. Depuis B : c'est A qui s'ouvre sur la bulle visée. Chaque
        vue montre alors la pastille de l'autre : c'est là que se jugent la
        hauteur et le delta (mire graduée du sol à la caméra).
        """
        if not (0 <= idx < len(self.stations)):
            return
        if src == 'B' and self.compare is not None:
            b = self.compare.station()
            if b is None or idx == b.idx:
                return
            self.goto(idx, keep_heading=False)
            self._last_current = self.current      # le suivi de A ne bouge pas B
            self._aim_a(b.idx)
            self.compare.aim_at_station(idx)
            other = b
        else:
            cur = self.station()
            if cur is None or idx == cur.idx:
                return
            self.open_in_b(idx)
            self._aim_a(idx)
            other = cur
        self._cone_sig = None
        self._refresh_ctrlbar()
        self._set_status(f"Sonde : {self._nom(idx)} et {self._nom(other.idx)} face à face — "
                         "Alt+molette : Δ, Maj+molette : H, sur une pastille",
                         COLORS['sel'])

    def _nom(self, idx: int) -> str:
        st = self.stations[idx]
        return st.key if st.key_explicit and st.key and st.key != st.locator else st.locator

    def face_a_face(self) -> None:
        """Touche G : A regarde B, B regarde A."""
        cv = self.compare
        b = cv.station() if cv is not None else None
        if b is None or b.idx == self.current:
            self._set_status("Face à face : ouvrez d'abord une autre bulle dans B "
                             "(Ctrl+clic ou clic droit sur une pastille)")
            return
        self._aim_a(b.idx)
        cv.aim_at_station(self.current)
        self._cone_sig = None
        self._set_status(f"Face à face : {self._nom(self.current)} ↔ {self._nom(b.idx)}",
                         COLORS['sel'])

    def swap_ab(self) -> None:
        """Touche I : A et B échangent bulles et regards."""
        cv = self.compare
        if cv is None or cv.station() is None or cv.idx == self.current:
            self._set_status("Inverser : il faut deux bulles différentes dans A et B")
            return
        a_idx, b_idx = self.current, cv.idx
        a_v = (self.view.yaw, self.view.pitch, self.view.fov)
        b_v = (cv.view.yaw, cv.view.pitch, cv.view.fov)
        if cv.linked.get():
            cv.linked.set(False)
            cv._on_linked()
        self.goto(b_idx, keep_heading=False)
        self._last_current = self.current
        self.view.yaw, self.view.pitch, self.view.fov = b_v
        cv.goto(a_idx, keep_heading=False)
        cv.view.yaw, cv.view.pitch, cv.view.fov = a_v
        self._sync_fov_widgets()
        self._request_render(force=True)
        cv.request_render(force=True)
        self._cone_sig = None
        self._refresh_ctrlbar()
        self._set_status(f"Inversé : A = {self._nom(b_idx)}, B = {self._nom(a_idx)}",
                         COLORS['sel'])

    def a_to_b(self) -> None:
        """Bouton A → B : B reprend la bulle et le regard de A."""
        if self.current < 0:
            return
        if self.compare is None:
            self._open_compare(self.current)
            self._last_current = self.current
        cv = self.compare
        cv.goto(self.current, keep_heading=False, record=True)
        if not cv.linked.get():
            cv.view.yaw, cv.view.pitch, cv.view.fov = (self.view.yaw, self.view.pitch,
                                                       self.view.fov)
        cv.request_render(force=True)
        self._refresh_ctrlbar()

    def b_to_a(self) -> None:
        """Bouton B → A : A reprend la bulle et le regard de B."""
        cv = self.compare
        if cv is None or cv.station() is None:
            self._set_status("B → A : la vue B n'est pas ouverte")
            return
        yaw, pitch, fov = cv.view.yaw, cv.view.pitch, cv.view.fov
        self.goto(cv.idx, keep_heading=False)
        self._last_current = self.current
        self.view.yaw, self.view.pitch, self.view.fov = yaw, pitch, fov
        self._sync_fov_widgets()
        self._request_render(force=True)

    # ── réglage fin des altitudes ────────────────────────────────────
    def adjust_alt(self, which, comp: str, sign: int) -> None:
        """Hauteur (dh) ou delta (ddelta) d'une bulle, ± un pas.

        `which` : 'A', 'B' ou l'indice d'une bulle. Une rafale (molette,
        clics rapprochés) sur la même bulle et la même composante ne compte
        que pour une étape d'annulation.
        """
        if which == 'A':
            st = self.station()
        elif which == 'B':
            st = self.compare.station() if self.compare is not None else None
        else:
            st = self.stations[which] if 0 <= which < len(self.stations) else None
        if st is None:
            return
        step = self.wheel_step()
        now = time.monotonic()
        last = getattr(self, '_last_adj', None)
        burst = last is not None and last[0] == st.idx and last[1] == comp and now - last[2] < 1.2
        self._last_adj = (st.idx, comp, now)
        self.corrections.apply(st, **{comp: round(getattr(st, comp) + sign * step, 6)},
                               record=not burst)
        self._after_edit(moved=True)
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        self._set_status(f"Station active {self._nom(st.idx)} : " + (
            f"hauteur station {st.height(eye):.3f} (caméra seule)" if comp == 'dh'
            else f"delta plancher {st.delta(eye):+.3f} (caméra et sol)")
            + f" · Z {st.z:.3f}", COLORS['edit'])

    # ── déplacement en plan de la station active (Espace + glisser) ────
    def _ground_rel(self, view: View, cam: Station, x: float, y: float,
                    dz: float) -> Optional[Tuple[float, float]]:
        """Point du plan d'altitude relative `dz` visé à l'écran (Est, Nord) / caméra."""
        res = ground_from_screen(view, x, y, self.calib, cam.north_pct, dz, max_dist=60.0)
        if res is None:
            return None
        az, dist = res
        a = math.radians(az)
        return dist * math.sin(a), dist * math.cos(a)

    def start_plan_drag(self, event, view: View, cam_idx: int, mode: str) -> bool:
        """Début d'un déplacement en plan de la STATION ACTIVE.

        mode 'monde' (vue A) : on tire le terrain, la station part en sens
        inverse ; le point du sol saisi reste sous le curseur.
        mode 'pastille' (vue B) : la pastille de la station active suit le
        curseur sur son sol.
        Libre dans toutes les directions ; X / Y verrouillés le contraignent.
        """
        st = self.station()
        if st is None or not (0 <= cam_idx < len(self.stations)):
            return False
        cam = self.stations[cam_idx]
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        dz = -st.height(eye) if mode == 'monde' else st.ground(eye) - cam.z
        v = dc_replace(view)
        p0 = self._ground_rel(v, cam, event.x, event.y, dz)
        if p0 is None:
            self._set_status("Espace + glisser : visez le sol (vers le bas de l'image)",
                             COLORS['warning'])
            return False
        self.corrections.apply(st)                    # état avant le geste
        self._hs_drag = ('plan', {'idx': st.idx, 'start': (st.x, st.y), 'p0': p0,
                                  'dz': dz, 'cam': cam_idx, 'view': v, 'mode': mode})
        # fantômes : où étaient les pastilles avant le geste (vue A)
        self._ghosts = ([(h.col, h.row, h.radius, self.hotspot_color(h.link,
                          self.stations[h.link.target]), h.foot is not None)
                         for h in self.hotspots] if mode == 'monde' else [])
        self._draw_overlay()
        return True

    def _drag_plan(self, event) -> None:
        info = self._hs_drag[1]
        st = self.stations[info['idx']]
        cam = self.stations[info['cam']]
        p = self._ground_rel(info['view'], cam, event.x, event.y, info['dz'])
        if p is None:
            return
        dx, dy = p[0] - info['p0'][0], p[1] - info['p0'][1]
        if info['mode'] == 'monde':                  # on tire le terrain
            dx, dy = -dx, -dy
        axis = self._locked_axis()
        if axis == 'x':
            dy = 0.0
        elif axis == 'y':
            dx = 0.0
        x0, y0 = info['start']
        self.corrections.apply(st, x=round(x0 + dx, 3), y=round(y0 + dy, 3), record=False)
        self._draw_overlay()
        self._redraw_compare()
        self._draw_plan()
        self._refresh_ctrlbar()
        self._set_status(f"Station active {self._nom(st.idx)} : ΔX {st.x - st.ox:+.3f}  "
                         f"ΔY {st.y - st.oy:+.3f} m depuis le CSV", COLORS['edit'])

    def _end_plan_drag(self) -> None:
        info = self._hs_drag[1]
        self._hs_drag = None
        self._ghosts = []
        st = self.stations[info['idx']]
        if self.corrections.drop_if_unchanged(st):
            if self.journal and self.journal[-1] == ('edit',):
                self.journal.pop()
            self._draw_overlay()
            return
        self._after_edit(moved=True)

    def ghost_of_active(self, canvas, view: View, cam: Station) -> None:
        """Vue B : position d'origine (CSV) de la station active, en transparence,
        reliée à sa position corrigée."""
        st = self.station()
        if st is None or st.idx == cam.idx or not (st.moved() or st.z_changed()):
            return
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        h0 = st.h0 if st.h0 is not None else eye
        vue = self.anchor() == 'vue'
        z0 = st.oz if vue else st.oz - h0
        z1 = st.z if vue else st.ground(eye)
        a = project_point(view, self.calib, cam.north_pct, st.ox - cam.x, st.oy - cam.y,
                          z0 - cam.z)
        b = project_point(view, self.calib, cam.north_pct, st.x - cam.x, st.y - cam.y,
                          z1 - cam.z)
        if a is None or a[2] < 0.05:
            return
        d = max(0.35, math.hypot(st.ox - cam.x, st.oy - cam.y))
        r_min, r_max = self.disc_bounds()
        r = clamp(view.focal() * float(self.cfg.get('disc_radius', DISC_RADIUS_M)) / d,
                  r_min, r_max)
        if b is not None and b[2] > 0.05:
            canvas.create_line(a[0], a[1], b[0], b[1], fill=COLORS['edit'], width=2,
                               dash=(4, 3), tags='hs')
        if self.relief():
            photo, ax, ay = self._sprite(local_color(st.parts().local or st.floor), r,
                                         False, alpha=0.35, floating=vue)
            canvas.create_image(a[0] - ax, a[1] - ay, anchor='nw', image=photo, tags='hs')
        canvas.create_text(a[0], a[1] - SPHERE_RADIUS * r - 8, text="origine CSV",
                           fill=MARK_TEXT, font=F_TINY, tags='hs')

    def wheel_step(self) -> float:
        """Pas de la molette pour H et Δ (Réglages : 1 ou 5 cm, 5 par défaut)."""
        v = parse_float(str(self.cfg.get('wheel_step', 0.05)))
        return v if v and v > 0 else 0.05

    def wheel_alt(self, event, hotspots, station_idx: int, direction: int) -> bool:
        """Alt+molette : Δ, Maj+molette : H — toujours de la STATION ACTIVE (vue A).

        Une seule station est modifiable à la fois, la bulle active : pas de
        correction accidentelle d'une voisine survolée. Depuis la vue B, on
        voit la pastille de la station active monter ou descendre.
        Retourne True si la molette a servi à cela.
        """
        alt = alt_down(event.state)
        shift = bool(event.state & 0x0001)
        if not (alt or shift):
            return False
        if self.current >= 0:
            self.adjust_alt(self.current, 'ddelta' if alt else 'dh', direction)
        return True

    # ── mire de hauteur ──────────────────────────────────────────────
    def draw_mire(self, canvas, view: View, cam: Station, tgt: Station, color: str,
                  hs: Optional["Hotspot"] = None, hovered: bool = False) -> None:
        """Empreinte au sol et mire graduée de la bulle `tgt`, vue depuis `cam`.

        L'empreinte (cercle de 50 cm posé sur le sol de la bulle, en
        perspective) doit s'inscrire à plat sur le sol de la photo ; la mire
        monte du sol à la caméra, graduée tous les 10 cm. Hauteur et delta se
        jugent ainsi dans l'image, et se corrigent à la molette.
        """
        if tgt.idx == cam.idx:
            return
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        north = cam.north_pct
        dx, dy = tgt.x - cam.x, tgt.y - cam.y
        g = tgt.ground(eye) - cam.z
        top = tgt.z - cam.z
        pts = []
        for k in range(33):
            a = 2 * math.pi * k / 32
            pr = project_point(view, self.calib, north, dx + MIRE_RING_M * math.sin(a),
                               dy + MIRE_RING_M * math.cos(a), g)
            if pr is None or pr[2] < 0.05:
                pts = []
                break
            pts += [pr[0], pr[1]]
        if pts:
            canvas.create_line(*pts, fill='#000000', width=4, tags='hs')
            canvas.create_line(*pts, fill=color, width=2, tags='hs')
        sg = project_segment(view, self.calib, north, (dx, dy, g), (dx, dy, top))
        if sg is None:
            return
        pole = None
        if hs is not None and hs.foot is not None:
            # la mire EST le mât : même verticale, du sol au pôle sud de la sphère
            px, py, rs = self.sphere_pole(hs, hovered)
            pole = (px, py)
            sg = (sg[0], sg[1], px, py)
        canvas.create_line(*sg, fill='#000000', width=5, tags='hs')
        canvas.create_line(*sg, fill=color, width=3, tags='hs')
        h = tgt.height(eye)
        nd = 3 if self.edit_mode else 2          # au millimètre en édition
        n = int(h / MIRE_TICK_M + 1e-9)
        lim = math.hypot(sg[2] - sg[0], sg[3] - sg[1])
        for k in range(1, n + 1):
            pr = project_point(view, self.calib, north, dx, dy, g + k * MIRE_TICK_M)
            if pr is None or pr[2] < 0.05:
                continue
            if pole is not None and math.hypot(pr[0] - sg[0], pr[1] - sg[1]) > lim:
                continue                          # graduations cachées par la sphère
            w = 7 if k % 5 == 0 else 4
            canvas.create_line(pr[0] - w, pr[1], pr[0] + w, pr[1], fill=color, width=1,
                               tags='hs')
        foot = project_point(view, self.calib, north, dx, dy, g)
        head = project_point(view, self.calib, north, dx, dy, top)
        if foot is not None and foot[2] > 0.05:
            canvas.create_line(foot[0] - 9, foot[1], foot[0] + 9, foot[1], fill=color,
                               width=2, tags='hs')
            canvas.create_text(foot[0] + 12, foot[1] + 1, anchor='w', font=F_TINY_B,
                               fill=color, text=f"sol {tgt.ground(eye):.{nd}f}  "
                                                f"Δ {tgt.delta(eye):+.{nd}f}",
                               tags='hs')
        if pole is not None:                      # à côté de la sphère (l'appareil)
            canvas.create_text(hs.col + rs + 6, hs.row, anchor='w', font=F_TINY_B,
                               fill=color, text=f"H {h:.{nd}f}  Z {tgt.z:.{nd}f}", tags='hs')
        elif head is not None and head[2] > 0.05:
            canvas.create_oval(head[0] - 4, head[1] - 4, head[0] + 4, head[1] + 4,
                               outline=color, width=2, tags='hs')
            canvas.create_text(head[0] + 12, head[1], anchor='w', font=F_TINY_B, fill=color,
                               text=f"H {h:.{nd}f}  Z {tgt.z:.{nd}f}", tags='hs')

    def mire_targets(self, current_idx: int, partner_idx: Optional[int],
                     hotspots, hover: Optional[int]) -> List[int]:
        """Bulles dotées d'une mire : l'autre vue, la pastille survolée, la cible."""
        if not self.cfg.get('show_mire', True):
            return []
        out = []
        if partner_idx is not None and partner_idx != current_idx:
            out.append(partner_idx)
        if hover is not None and hover < len(hotspots):
            out.append(hotspots[hover].link.target)
        if self.edit_mode and self.selected is not None:
            out.append(self.selected)
        return [i for k, i in enumerate(out) if i not in out[:k] and 0 <= i < len(self.stations)]

    def _build_toolbar(self, parent) -> None:
        bar = tk.Frame(parent, bg=COLORS['bg_medium'])
        bar.pack(fill='x', side='top')

        tk.Label(bar, text="BubbleNav", font=F_TITLE, bg=COLORS['bg_medium'],
                 fg=COLORS['accent']).pack(side='left', padx=(10, 12), pady=5)
        tk.Label(bar, text="Plancher", bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                 font=F_UI).pack(side='left', padx=(2, 4))
        self.floor_var = tk.StringVar()
        self.floor_cb = ttk.Combobox(bar, textvariable=self.floor_var, width=20,
                                     state='readonly', style='BN.TCombobox')
        self.floor_cb.pack(side='left', padx=2, pady=4)
        self.floor_cb.bind('<<ComboboxSelected>>', self._on_floor_selected)

        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y',
                                                         padx=8, pady=6)

        tk.Label(bar, text="Champ", bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                 font=F_UI).pack(side='left', padx=(2, 2))
        self.fov_var = tk.DoubleVar(value=self.view.fov)
        self.fov_scale = tk.Scale(bar, from_=FOV_MIN, to=FOV_MAX, resolution=1,
                                  orient='horizontal', length=190, showvalue=False,
                                  variable=self.fov_var, command=self._on_fov,
                                  bg=COLORS['text_muted'], fg=COLORS['text'],
                                  troughcolor=COLORS['bg_dark'], highlightthickness=0,
                                  bd=0, sliderrelief='flat',
                                  activebackground=COLORS['accent'])
        self.fov_scale.pack(side='left', padx=2)
        # la molette agit aussi sur le curseur
        self.fov_scale.bind('<MouseWheel>', lambda e: self._zoom(
            -6 if getattr(e, 'delta', 0) > 0 else 6) or 'break')
        self.fov_scale.bind('<Button-4>', lambda e: self._zoom(-6) or 'break')
        self.fov_scale.bind('<Button-5>', lambda e: self._zoom(+6) or 'break')
        self.fov_lbl = tk.Label(bar, text=f"{self.view.fov:.0f}°", width=5,
                                bg=COLORS['bg_medium'], fg=COLORS['text'], font=F_MONO)
        self.fov_lbl.pack(side='left')
        b120 = self._mk_button(bar, "120°", self.fov_reset,
                               tip="Champ de vision à 120° pour les deux vues ; le curseur "
                                   "et la molette marquent un cran à 120°.")
        b120.config(padx=6)
        b120.pack(side='left', padx=(2, 0), pady=4)

        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y',
                                                         padx=8, pady=6)

        self.qual_var = tk.StringVar(value=str(self.store.src_width))   # (Réglages)
        # Ce que l'on voit : un seul choix, du plus serré au plus large
        tk.Label(bar, text="Voir", bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                 font=F_UI).pack(side='left', padx=(2, 4))
        sc = self.cfg.get('view_scope', 'voisins')
        self.scope_var = tk.StringVar(value=SCOPE_LABELS.get(sc, SCOPE_LABELS['voisins']))
        scb = ttk.Combobox(bar, textvariable=self.scope_var, width=15, state='readonly',
                           style='BN.TCombobox', values=list(SCOPE_LABELS.values()))
        scb.pack(side='left', padx=2)
        scb.bind('<<ComboboxSelected>>', lambda e: self.set_scope(
            next(k for k, v in SCOPE_LABELS.items() if v == self.scope_var.get())))
        Tooltip(scb, "Stations montrées en pastilles : le local de la bulle (L), les "
                     "locaux voisins (défaut : le local et ceux qui ont une station à "
                     "moins de la distance de voisinage), une distance, ou tout le "
                     "plancher (T). Distance réglable dans Réglages.")

        self.labels_var = tk.BooleanVar(value=bool(self.cfg.get('show_labels', True)))
        self.names_var = tk.BooleanVar(value=bool(self.cfg.get('show_names', True)))
        self.heights_var = tk.BooleanVar(value=bool(self.cfg.get('show_heights', True)))
        # Un seul bouton pour les étiquettes : la barre reste courte.
        mb = tk.Menubutton(bar, text="Affichage ▾", font=F_UI, relief='flat',
                           bg=COLORS['bg_light'], fg=COLORS['text'],
                           activebackground=COLORS['accent'],
                           activeforeground='white', padx=8, pady=3)
        menu = tk.Menu(mb, tearoff=False, bg=COLORS['bg_light'], fg=COLORS['text'],
                       activebackground=COLORS['accent'], activeforeground='white',
                       selectcolor=COLORS['text'], font=F_UI)
        for txt, var in (("Distance sous la pastille", self.labels_var),
                         ("Nom de la station au-dessus", self.names_var),
                         ("Hauteur appareil, delta, altitude (H / Δ / Z)",
                          self.heights_var)):
            menu.add_checkbutton(label=txt, variable=var, command=self._on_marks)
        menu.add_separator()
        self.anchor_var = tk.StringVar(value=self.anchor())
        menu.add_radiobutton(label="Bulle au point de vue (appareil), mât et ombre au sol",
                             value='vue', variable=self.anchor_var, command=self._set_anchor)
        menu.add_radiobutton(label="Pastille posée au sol (plancher + Δ)", value='sol',
                             variable=self.anchor_var, command=self._set_anchor)
        menu.add_separator()
        self.all_var = tk.BooleanVar(value=self.whole_floor())
        menu.add_checkbutton(label="Toutes les bulles du plancher  (T)",
                             variable=self.all_var,
                             command=lambda: self._toggle_all(bool(self.all_var.get())))
        menu.add_separator()
        self.color_var = tk.StringVar(value=self.cfg.get('color_mode', 'local'))
        menu.add_radiobutton(label="Couleur par local", value='local',
                             variable=self.color_var, command=self._set_color_mode)
        menu.add_radiobutton(label="Couleur par type de lien (▲ ▼)", value='lien',
                             variable=self.color_var, command=self._set_color_mode)
        menu.add_separator()
        self.focus_var = tk.BooleanVar(value=bool(self.cfg.get('focus_origin')))
        menu.add_checkbutton(label="À l'arrivée, regarder d'où l'on vient",
                             variable=self.focus_var, command=self._set_focus_origin)
        menu.add_command(label="Regarder d'où l'on vient  (O)", command=self.look_back)
        menu.add_separator()
        self.tip_var = tk.BooleanVar(value=bool(self.cfg.get('show_tooltip', True)))
        menu.add_checkbutton(label="Infobulle au survol des pastilles", variable=self.tip_var,
                             command=lambda: (self.cfg.__setitem__('show_tooltip',
                                                                   bool(self.tip_var.get())),
                                              self.canvas.delete('tip'),
                                              self.compare and self.compare.canvas.delete('tip')))
        self.mire_var = tk.BooleanVar(value=bool(self.cfg.get('show_mire', True)))
        menu.add_checkbutton(label="Mire de hauteur (empreinte au sol, sol → caméra)",
                             variable=self.mire_var,
                             command=lambda: (self.cfg.__setitem__('show_mire',
                                                                   bool(self.mire_var.get())),
                                              self._draw_overlay(), self._redraw_compare()))
        mb.config(menu=menu)
        self.display_btn = mb
        Tooltip(mb, "Étiquettes des pastilles (distance, nom, H / Δ / Z), "
                    "hauteur des pastilles (sol ou point de vue), toutes les pastilles (T), "
                    "regarder d'où l'on vient (O).")
        mb.pack(side='left', padx=8, pady=4)

        tk.Frame(bar, bg=COLORS['border'], width=1).pack(side='left', fill='y',
                                                         padx=8, pady=6)
        self.cmp_btn = self._mk_button(bar, "Comparer  (C)", self._toggle_compare)
        self.cmp_btn.pack(side='left', padx=3, pady=4)
        self.edit_var = tk.BooleanVar(value=False)
        self.edit_btn = self._mk_button(bar, "Édition  (E)", self._toggle_edit)
        self.edit_btn.pack(side='left', padx=3, pady=4)
        self.edit_lbl = tk.Label(bar, text="", bg=COLORS['bg_medium'],
                                 fg=COLORS['edit'], font=F_UI_B)
        self.edit_lbl.pack(side='left', padx=6)

        # En plein ecran la barre de titre (et sa croix) disparait : on la remplace.
        self._mk_button(bar, "✕", self._hide_viewer, bg=COLORS['bg_light']
                        ).pack(side='right', padx=(3, 8), pady=4)
        self.fs_btn = self._mk_button(bar, "⛶", self._toggle_fullscreen)
        self.fs_btn.pack(side='right', padx=3, pady=4)
        self._mk_button(bar, "?", self._dlg_help).pack(side='right', padx=3, pady=4)
        self._mk_button(bar, "Réglages…", self._dlg_settings).pack(side='right', padx=3, pady=4)

    def _build_side_panel(self, parent) -> None:
        side = tk.Frame(parent, bg=COLORS['bg_medium'], width=360)
        side.pack(side='right', fill='y')
        side.pack_propagate(False)

        tk.Label(side, text="Plan du plancher", font=F_UI_B, bg=COLORS['bg_medium'],
                 fg=COLORS['text']).pack(anchor='w', padx=10, pady=(8, 2))
        self.plan = tk.Canvas(side, bg='#161616', height=PLAN_H, highlightthickness=1,
                              highlightbackground=COLORS['border'])
        self.plan.pack(fill='x', padx=10)
        self.plan.bind('<Configure>', lambda e: self._draw_plan())
        self.plan.bind('<ButtonPress-1>', self._on_plan_press_left)
        self.plan.bind('<B1-Motion>', self._on_plan_drag_left)
        self.plan.bind('<ButtonRelease-1>', self._on_plan_release_left)
        self.plan.bind('<Double-Button-1>', self._on_plan_double)
        self.plan.bind('<Motion>', self._on_plan_motion)
        self.plan.bind('<Leave>', lambda e: self._set_plan_hover(None))
        self.plan.bind('<ButtonPress-3>', self._on_plan_press)
        self.plan.bind('<B3-Motion>', self._on_plan_drag)
        self.plan.bind('<ButtonRelease-3>', self._on_plan_release_right)
        self.plan.bind('<MouseWheel>', self._on_plan_wheel)
        self.plan.bind('<Button-4>', lambda e: self._on_plan_wheel(e, +1))
        self.plan.bind('<Button-5>', lambda e: self._on_plan_wheel(e, -1))

        btns = tk.Frame(side, bg=COLORS['bg_medium'])
        btns.pack(fill='x', padx=10, pady=6)
        self._mk_button(btns, "Recadrer", self._plan_fit).pack(side='left')
        self._mk_button(btns, "◀ Retour", self.go_back).pack(side='left', padx=6)
        mode = self.cfg.get('plan_links', 'squelette')
        self.plan_links_var = tk.StringVar(
            value=mode if mode in PLAN_LINK_MODES else 'squelette')
        cb = ttk.Combobox(btns, textvariable=self.plan_links_var, width=9,
                          state='readonly', style='BN.TCombobox', values=PLAN_LINK_MODES)
        cb.pack(side='right')
        cb.bind('<<ComboboxSelected>>', lambda e: self._set_plan_links())
        tk.Label(btns, text="réseau", font=F_UI, bg=COLORS['bg_medium'],
                 fg=COLORS['text_muted']).pack(side='right', padx=(0, 4))

        self._build_filter_panel(side)

        self.info_title = tk.Label(side, text="Bulle courante", font=F_UI_B,
                                   bg=COLORS['bg_medium'], fg=COLORS['text'])
        self.info_title.pack(anchor='w', padx=10, pady=(6, 2))
        self.info = tk.Label(side, text="—", justify='left', anchor='nw', font=F_MONO,
                             bg=COLORS['card'], fg=COLORS['text'], padx=8, pady=6,
                             wraplength=330)
        self.info.pack(fill='x', padx=10)

        self._build_edit_panel(side)

        self.nb_title = tk.Label(side, text="Voisins (double-clic pour y aller)",
                                 font=F_UI_B, bg=COLORS['bg_medium'], fg=COLORS['text'])
        self.nb_title.pack(anchor='w', padx=10, pady=(10, 2))
        wrap = tk.Frame(side, bg=COLORS['bg_medium'])
        wrap.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.nb_wrap = wrap
        sb = tk.Scrollbar(wrap, orient='vertical')
        sb.pack(side='right', fill='y')
        self.nb_list = tk.Listbox(wrap, bg=COLORS['card'], fg=COLORS['text'],
                                  font=F_MONO, activestyle='none', bd=0,
                                  highlightthickness=0, selectbackground=COLORS['accent'],
                                  yscrollcommand=sb.set)
        self.nb_list.pack(side='left', fill='both', expand=True)
        sb.config(command=self.nb_list.yview)
        self.nb_list.bind('<<ListboxSelect>>', self._on_nb_select)
        self.nb_list.bind('<Double-Button-1>', self._on_nb_activate)
        self.nb_list.bind('<Return>', self._on_nb_activate)

    def _show_module(self) -> None:
        self.deiconify()
        self.lift()

    def _build_status(self, parent) -> None:
        bar = tk.Frame(parent, bg=COLORS['bg_medium'])
        bar.pack(fill='x', side='bottom')
        self.status = tk.Label(bar, text="Chargez un CSV puis le dossier des images.",
                               anchor='w', bg=COLORS['bg_medium'], fg=COLORS['text_muted'],
                               font=F_UI, padx=10, pady=4)
        self.status.pack(side='left', fill='x', expand=True)
        self.heading_lbl = tk.Label(bar, text="", bg=COLORS['bg_medium'],
                                    fg=COLORS['text'], font=F_MONO, padx=10)
        self.heading_lbl.pack(side='right')

    def _bind_keys(self) -> None:
        """Raccourcis actifs dans le module comme dans le visualiseur, sauf quand
        un champ de saisie a le clavier."""
        def key(fn):
            def handler(event):
                w = event.widget
                if isinstance(w, (tk.Entry, tk.Text, ttk.Combobox, tk.Spinbox)):
                    return None
                fn()
                return 'break'
            return handler

        raccourcis = {
            '<Left>': lambda: self._nudge(yaw=-6), '<Right>': lambda: self._nudge(yaw=+6),
            '<Up>': lambda: self._nudge(pitch=+5), '<Down>': lambda: self._nudge(pitch=-5),
            '<Shift-Left>': lambda: self._nudge(yaw=-25),
            '<Shift-Right>': lambda: self._nudge(yaw=+25),
            '<plus>': lambda: self._zoom(-6), '<KP_Add>': lambda: self._zoom(-6),
            '<minus>': lambda: self._zoom(+6), '<KP_Subtract>': lambda: self._zoom(+6),
            '<Return>': self._go_forward,
            '<BackSpace>': self.go_back, '<Home>': self._reset_view,
            '<F11>': self._toggle_fullscreen,
            '<c>': self._toggle_compare, '<C>': self._toggle_compare,
            '<f>': self._toggle_filters, '<F>': self._toggle_filters,
            '<e>': self._toggle_edit, '<E>': self._toggle_edit,
            '<v>': self._show_viewer, '<V>': self._show_viewer,
            '<t>': lambda: self._toggle_scope('plancher'),
            '<T>': lambda: self._toggle_scope('plancher'),
            '<o>': self.look_back, '<O>': self.look_back,
            '<l>': lambda: self._toggle_scope('local'),
            '<L>': lambda: self._toggle_scope('local'),
            '<m>': self._show_module, '<M>': self._show_module,
            '<g>': self.face_a_face, '<G>': self.face_a_face,
            '<i>': self.swap_ab, '<I>': self.swap_ab,
            '<F1>': self._dlg_help, '<question>': self._dlg_help,
            '<x>': lambda: self._lock_axis('x'), '<X>': lambda: self._lock_axis('x'),
            '<y>': lambda: self._lock_axis('y'), '<Y>': lambda: self._lock_axis('y'),
            '<z>': lambda: self._lock_axis('z'), '<Z>': lambda: self._lock_axis('z'),
            '<Control-z>': self.undo, '<Control-Z>': self.undo,
            '<Control-s>': self._dlg_apply,
            '<Prior>': lambda: self._bump('dh', +1), '<Next>': lambda: self._bump('dh', -1),
            '<Shift-Prior>': lambda: self._bump('ddelta', +1),
            '<Shift-Next>': lambda: self._bump('ddelta', -1),
        }
        for seq, fn in raccourcis.items():
            self.bind_all(seq, key(fn))
        self.bind_all('<Escape>', key(self._leave_fullscreen))
        # Espace : tenu, il transforme le glisser en déplacement en plan ;
        # appui bref sans glisser, il avance comme avant.
        self._space_down = False
        self._space_used = False
        self._space_t = 0.0
        self._space_job = None

        def sp_press(event):
            if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Combobox, tk.Spinbox)):
                return None
            if self._space_job is not None:        # répétition automatique (X11)
                self.after_cancel(self._space_job)
                self._space_job = None
                return 'break'
            if not self._space_down:
                self._space_down, self._space_used = True, False
                self._space_t = time.monotonic()
                try:
                    self.canvas.config(cursor='crosshair')
                except Exception:
                    pass
            return 'break'

        def sp_release(event):
            if isinstance(event.widget, (tk.Entry, tk.Text, ttk.Combobox, tk.Spinbox)):
                return None
            if self._space_job is not None:
                self.after_cancel(self._space_job)
            self._space_job = self.after(40, sp_done)
            return 'break'

        def sp_done():
            self._space_job = None
            bref = time.monotonic() - self._space_t < 0.35
            used = self._space_used
            self._space_down = False
            try:
                self.canvas.config(cursor='fleur')
            except Exception:
                pass
            if bref and not used:
                self._go_forward()
        self.bind_all('<KeyPress-space>', sp_press)
        self.bind_all('<KeyRelease-space>', sp_release)

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
        self._pump_job = None
        if not self._stop.is_set():
            try:
                self._sync_compare()
                self._sync_plan_cone()
            except Exception:
                pass
            try:
                self._pump_job = self.after(UI_PUMP_MS, self._pump_ui)
            except Exception:
                pass

    def _set_status(self, text: str, color: str = None) -> None:
        for lbl in (getattr(self, 'status', None), getattr(self, 'module_status', None)):
            try:
                if lbl is not None:
                    lbl.config(text=text, fg=color or COLORS['text_muted'])
            except Exception:
                pass

    def load_csv(self, path: str, images_dir: str = '') -> bool:
        mapping = None
        try:
            stations, warns = read_survey_csv(path)
        except ColumnMappingNeeded as need:
            # Format deja rencontre ? On reprend la correspondance choisie alors.
            mapping = self.cfg.get('csv_mappings', {}).get(need.signature)
            try:
                if not mapping:
                    raise need
                stations, warns = read_survey_csv(path, mapping)
            except ColumnMappingNeeded:
                mapping = self._dlg_mapping(path, need)
                if not mapping:
                    return False
                try:
                    stations, warns = read_survey_csv(path, mapping)
                except Exception as exc:
                    messagebox.showerror("Lecture du CSV", f"{path}\n\n{exc}")
                    return False
                maps = dict(self.cfg.get('csv_mappings', {}))
                maps[need.signature] = mapping
                self.cfg['csv_mappings'] = dict(list(maps.items())[-20:])
                save_config(self.cfg)
            except Exception as exc:
                messagebox.showerror("Lecture du CSV", f"{path}\n\n{exc}")
                return False
        except Exception as exc:
            messagebox.showerror("Lecture du CSV", f"{path}\n\n{exc}")
            return False
        self.csv_mapping = mapping

        self.stations = stations
        self.warnings = warns
        self.csv_path = path
        self.cfg['csv_path'] = path
        self.history.clear()
        self.current = -1

        self.floors = sorted({s.floor for s in stations})
        self.floor_cb.config(values=self.floors)
        self.f_floor_cb.config(values=['tous', 'courant'] + self.floors)
        self.f_local_cb.config(values=sorted({s.parts().local for s in stations
                                              if s.parts().local}))
        self.by_photo = {s.photo: s for s in stations}
        # altitude du point de vue : plancher + delta + hauteur (avant corrections)
        apply_altimetry(stations, float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)))
        self.by_key = {s.key.lower(): s for s in stations}
        custom = self.cfg.get('corr_paths', {}).get(path, '')
        self.corrections = Corrections(path, custom if isinstance(custom, str) else '',
                                       eye=float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)))
        self.selected = None
        corr_msg = ''
        if os.path.isfile(self.corrections.path):
            try:
                n_ok, n_miss = self.corrections.load(self.by_photo, by_key=self.by_key)
                corr_msg = (f" · {n_ok} correction(s) reprises de "
                            f"{os.path.basename(self.corrections.path)}")
                if n_miss:
                    corr_msg += f" ({n_miss} ligne(s) sans correspondance)"
            except Exception as exc:
                messagebox.showwarning("Fichier de corrections",
                                       f"{self.corrections.path}\n\n{exc}")
        self.journal.clear()
        self.corrections.on_record = self._journal_edit
        self.rebuild_graph()

        msg = f"{len(stations)} bulles · {len(self.floors)} planchers · {os.path.basename(path)}"
        if warns:
            msg += f" · {len(warns)} ligne(s) ignorée(s)"
        self.incoherences = check_floor_coherence(stations)
        deduits = sum(1 for st in stations if st.parts().etage_deduit)
        if deduits:
            msg += f" · {deduits} étage(s) déduit(s) du n° de local"
        if self.incoherences:
            msg += f" · ⚠ {len(self.incoherences)} incohérence(s) étage / Z"
            locs = sorted({stations[i].parts().local for i in self.incoherences})
            warns.append(f"{len(self.incoherences)} bulle(s) à l'étage déduit du local "
                         f"incohérent avec leur altitude : {', '.join(locs)}")
        anomalies = sum(1 for st in stations
                        if [a for a in st.parts().anomalies() if a != 'date'])
        if anomalies:
            msg += f" · {anomalies} nom(s) incomplet(s)"
        msg += corr_msg
        self._set_status(msg, COLORS['ok'] if not warns else COLORS['warning'])

        target = images_dir or self.cfg.get('images_dir', '')
        if target and os.path.isdir(target):
            self.set_images_dir(target)
        else:
            self._plan_view['fitted'] = False
            self.goto(0, keep_heading=False)
        save_config(self.cfg)
        self._show_viewer()
        self._refresh_module()
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
        alias = self.store.bind_stations(self.stations)   # num scan / nom projeté
        found = sum(1 for s in self.stations if self.store.has(s.photo))
        total = len(self.stations)
        color = COLORS['ok'] if found == total else COLORS['warning']
        self._set_status(f"{found}/{total} images trouvées dans « {self.images_dir} » "
                         f"({len(paths)} fichiers indexés"
                         + (f", {alias} rattachée(s) par numéro de scan ou nom projeté"
                            if alias else '') + ")", color)
        save_config(self.cfg)
        self._refresh_module()
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
        self.links = build_graph(self.stations, self.graph_params())
        if hasattr(self, '_near_cache'):
            self._near_cache.clear()
        self.plan_edges = plan_skeleton(self.stations, self.links)
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
    def goto(self, idx: int, keep_heading: bool = True, push: bool = True,
             focus: Optional[bool] = None) -> None:
        if not (0 <= idx < len(self.stations)) or idx == self.current:
            return
        prev = self.station()
        if push and self.current >= 0:     # regard d'avant le départ, pour Ctrl+Z
            self.history.append(self.current)
            del self.history[:-200]
            self._journal_push(('nav', self.current, self.view.yaw, self.view.pitch,
                                self.view.fov))
        if keep_heading and prev is not None and self.cfg.get('keep_heading', True):
            # conserve le cap terrain : azimut vise avant -> apres
            az = self.calib.azimuth(self.view.yaw, prev.north_pct)
            self.view.yaw = self.calib.pano_yaw(az, self.stations[idx].north_pct)
        if prev is not None:
            self.came_from = prev.idx
            if focus if focus is not None else bool(self.cfg.get('focus_origin')):
                aim_at(self.view, self.calib, self.stations[idx], prev,
                       float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)), self.anchor())
        self.current = idx
        self.selected = None
        self.focus_idx = None
        st = self.stations[idx]
        if self.floor_var.get() != st.floor:
            self.floor_var.set(st.floor)
            self._plan_view['fitted'] = False
        self._request_render(force=True)
        self._refresh_side()
        self._draw_plan()
        if idx < len(self.links):      # prechargement des voisins immediats
            self.store.prefetch([self.stations[lk.target].photo for lk in self.links[idx]])

    def look_back(self) -> None:
        """Touche O : tourne la vue vers la bulle d'où l'on vient."""
        cur = self.station()
        src = self.came_from
        if cur is None or src is None or not (0 <= src < len(self.stations)) \
                or src == cur.idx:
            self._set_status("Pas de bulle d'origine : naviguez d'abord d'une bulle à l'autre")
            return
        aim_at(self.view, self.calib, cur, self.stations[src],
               float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)), self.anchor())
        self._request_render(force=True)
        self._set_status(f"Vue tournée vers {self.stations[src].locator}, d'où l'on vient : "
                         "sa pastille doit tomber sur le point de prise de vue",
                         COLORS['sel'])

    def _set_focus_origin(self) -> None:
        self.cfg['focus_origin'] = bool(self.focus_var.get())
        save_config(self.cfg)
        self._set_status("À l'arrivée : " + ("regard tourné vers la bulle quittée"
                                              if self.cfg['focus_origin']
                                              else "cap conservé"))

    def go_back(self) -> None:
        if self.history:
            idx = self.history.pop()
            for k in range(len(self.journal) - 1, -1, -1):   # retour = annulation
                if self.journal[k][0] == 'nav':
                    del self.journal[k]
                    break
            self.goto(idx, keep_heading=True, push=False)

    # ── annulation universelle (Ctrl+Z) ──────────────────────────────
    def _journal_push(self, entry: tuple) -> None:
        self.journal.append(entry)
        del self.journal[:-600]

    def _journal_edit(self) -> None:
        self._journal_push(('edit',))

    def undo(self) -> None:
        """Ctrl+Z : annule la dernière opération, quelle qu'elle soit.

        Correction (position, altitude, orientation) ou passage d'une bulle à
        l'autre, dans la vue A comme dans la vue B : on revient à la bulle
        quittée, avec le cap, le site et le champ qu'elle avait.
        """
        while self.journal:
            entry = self.journal.pop()
            kind = entry[0]
            if kind == 'edit':
                st = self.corrections.undo(self.by_photo)
                if st is None:
                    continue
                self._after_edit(moved=True, turned=True)
                self._set_status(f"Annulé : correction sur {st.locator}", COLORS['edit'])
                return
            _, idx, yaw, pitch, fov = entry
            if not (0 <= idx < len(self.stations)):
                continue
            if kind == 'nav':
                if self.history and self.history[-1] == idx:
                    self.history.pop()
                self.goto(idx, keep_heading=False, push=False)
                self.view.yaw, self.view.pitch, self.view.fov = yaw, pitch, fov
                self._sync_fov_widgets()
                self._request_render(force=True)
                self._set_status(f"Annulé : retour à {self.stations[idx].locator}",
                                 COLORS['sel'])
                return
            if kind == 'nav_b' and self.compare is not None:
                cv = self.compare
                cv.goto(idx, keep_heading=False)
                cv.view.yaw, cv.view.pitch, cv.view.fov = yaw, pitch, fov
                cv.request_render(force=True)
                self._cone_sig = None
                self._set_status(f"Annulé : vue B revenue à {self.stations[idx].locator}",
                                 COLORS['sel'])
                return
        # plus rien au journal : dernier recours, la pile propre aux corrections
        st = self.corrections.undo(self.by_photo)
        if st is not None:
            self._after_edit(moved=True, turned=True)
            self._set_status(f"Annulé : correction sur {st.locator}", COLORS['edit'])
        else:
            self._set_status("Rien à annuler")

    def _sync_fov_widgets(self) -> None:
        try:
            self.fov_var.set(self.view.fov)
            self.fov_lbl.config(text=f"{self.view.fov:.0f}°")
        except Exception:
            pass

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
        if self._viewer_fullscreen():
            self._leave_fullscreen()
            return
        try:
            self.cfg['viewer_geometry'] = self.viewer.geometry()
            self._fs_wanted = True
            self.viewer.attributes('-fullscreen', True)
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
        # La correction d'orientation fait tourner l'image sous les pastilles :
        # on echantillonne la source a (cap - correction), les pastilles
        # (georeferencees, donc de reference) restant a leur place.
        fix = self.stations[self.current].yaw_fix if self.current < len(self.stations) else 0.0
        rv = View(wrap180(self.view.yaw - fix), self.view.pitch, self.view.fov, w, h)
        dv = View(self.view.yaw, self.view.pitch, self.view.fov,
                  self.view.width, self.view.height)
        self.submit_render('A', self.current, rv, dv, scale, self)
        if interactive:
            if self._idle_job:
                self.after_cancel(self._idle_job)
            self._idle_job = self.after(IDLE_FULL_MS, self._render_full)

    def submit_render(self, key: str, idx: int, rv: View, dv: View,
                      scale: float, pane) -> None:
        """Dépose une demande de rendu pour une vue.

        Une seule demande en attente par vue : la plus récente remplace la
        précédente, et la vue manipulée en dernier est servie en premier.
        """
        with self._cv:
            self._req_seq += 1
            self._reqs.pop(key, None)
            self._reqs[key] = (self._req_seq, idx, rv, dv, scale, pane)
            self._cv.notify()

    def _render_full(self) -> None:
        self._idle_job = None
        self._interactive = False
        self._request_render(force=True, interactive=False)

    def _render_worker(self) -> None:
        """Thread de rendu : toujours la demande la plus recente."""
        from PIL import Image
        while not self._stop.is_set():
            with self._cv:
                while not self._reqs and not self._stop.is_set():
                    self._cv.wait(0.3)
                if self._stop.is_set():
                    break
                key = next(reversed(self._reqs))      # la vue la plus sollicitée
                req = self._reqs.pop(key)
            seq, idx, rv, dv, scale, pane = req
            try:
                st = self.stations[idx]
            except Exception:
                continue
            src = self.store.peek(st.photo)
            if src is None:
                if not self.store.has(st.photo):
                    self._post(pane.publish_missing, seq, idx)
                    continue
                self._post(self._set_status, f"Chargement de {st.photo} …")
                src = self.store.load(st.photo)
                with self._cv:
                    superseded = key in self._reqs
                if superseded:
                    continue                    # une demande plus recente existe
                if src is None:
                    self._post(pane.publish_missing, seq, idx)
                    continue
            try:
                out = self.renderer.render(src, rv)
                img = Image.fromarray(out)
            except Exception as exc:
                self._post(self._set_status, f"Erreur de rendu : {exc}", COLORS['error'])
                continue
            self._post(pane.publish, img, seq, rv, dv, idx, scale)

    def publish(self, img, seq: int, rv: View, dv: View, idx: int, scale: float) -> None:
        """Affiche une image rendue (thread principal uniquement)."""
        if self._stop.is_set() or seq <= self._shown_seq or idx != self.current:
            return
        try:
            from PIL import Image, ImageTk
            if scale != 1.0 and (rv.width != self.view.width or rv.height != self.view.height):
                img = img.resize((max(1, self.view.width), max(1, self.view.height)),
                                 Image.BILINEAR)
            self._shown_seq = seq
            self._frame_view = View(dv.yaw, dv.pitch, dv.fov,
                                    self.view.width, self.view.height)
            self._tk_img = ImageTk.PhotoImage(img)
            self.canvas.delete('frame')
            self.canvas.create_image(0, 0, anchor='nw', image=self._tk_img, tags='frame')
            self.canvas.tag_lower('frame')
            self._draw_overlay()
            self._sync_plan_cone()
            if scale == 1.0:
                st = self.stations[idx]
                n = len(self.links[idx]) if idx < len(self.links) else 0
                fix = f" · Δnord {st.yaw_fix:+.2f}°" if st.turned() else ""
                self._set_status(f"{st.locator} · {n} voisin(s) · "
                                 f"{len(self.hotspots)} pastille(s) en vue · "
                                 f"{rv.width}×{rv.height}{fix}")
        except Exception as exc:
            self._set_status(f"Affichage impossible : {exc}", COLORS['error'])

    def publish_missing(self, seq: int, idx: int) -> None:
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
        self._sync_plan_cone()

    # ═════════════════════════════════════════════════════════════════
    # PASTILLES
    # ═════════════════════════════════════════════════════════════════
    def _compute_hotspots(self, view: View) -> List[Hotspot]:
        out, masques = compute_hotspots(
            self.stations, self.links, self.current, view, self.calib, self.filters,
            self.store.has, float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)),
            float(self.cfg.get('disc_radius', DISC_RADIUS_M)), *self.disc_bounds(),
            anchor=self.anchor())
        self.hidden_count = masques
        return out

    def _sprite(self, color: str, r: float, hover: bool = False, alpha: float = 1.0,
                floating: bool = False):
        """Sphère ombrée prête à afficher, mise en cache par couleur, taille, opacité."""
        rq = max(3, int(round(r / 2.0) * 2))          # pas de 2 px : cache compact
        alpha = round(clamp(alpha, 0.05, 1.0), 2)
        key = (color, rq, hover, alpha, floating)
        hit = self._sprites.get(key)
        if hit is not None:
            return hit
        from PIL import ImageTk
        img, (ax, ay) = sphere_sprite(color, rq, hover, floating=floating)
        if alpha < 1.0:                               # fantôme : opacité réduite
            a = img.getchannel('A').point(lambda v: int(v * alpha))
            img.putalpha(a)
        entry = (ImageTk.PhotoImage(img), ax, ay)
        if len(self._sprites) >= 400:
            self._sprites.pop(next(iter(self._sprites)))
        self._sprites[key] = entry
        return entry

    def relief(self) -> bool:
        return bool(self.cfg.get('disc_3d', True))

    def _shadow(self, rx: float, ry: float):
        """Ombre douce au sol, mise en cache par taille (pas de 2 px)."""
        qx, qy = max(2, int(round(rx / 2.0) * 2)), max(1, int(round(ry / 2.0) * 2) or 1)
        key = ('ombre', qx, qy)
        hit = self._sprites.get(key)
        if hit is not None:
            return hit
        from PIL import ImageTk
        img = shadow_sprite(qx, qy)
        entry = (ImageTk.PhotoImage(img), img.width / 2.0, img.height / 2.0)
        if len(self._sprites) >= 400:
            self._sprites.pop(next(iter(self._sprites)))
        self._sprites[key] = entry
        return entry

    def refresh_altimetry(self, delay_ms: int = 0) -> None:
        """Recalcule les altitudes (hauteur instrument changée)."""
        job = getattr(self, '_alti_job', None)
        if job:
            self.after_cancel(job)
            self._alti_job = None
        if delay_ms:
            self._alti_job = self.after(delay_ms, self.refresh_altimetry)
            return
        if not self.stations:
            return
        apply_altimetry(self.stations, float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)))
        self.rebuild_graph()          # les liens entre planchers dépendent des Z
        self._redraw_compare()
        self._refresh_edit_panel()

    def anchor(self) -> str:
        """Hauteur des pastilles : 'sol' (plancher + delta) ou 'vue' (+ hauteur)."""
        return 'sol' if self.cfg.get('bubble_anchor', 'vue') == 'sol' else 'vue'

    def _set_anchor(self) -> None:
        self.cfg['bubble_anchor'] = self.anchor_var.get()
        save_config(self.cfg)
        self._draw_overlay()
        self._redraw_compare()
        self._set_status("Pastilles " + ("au point de vue (sol + hauteur appareil), "
                                         "mât jusqu'au sol" if self.anchor() == 'vue'
                                         else "posées au sol (plancher + delta)"))

    def _near_locals(self, idx: int) -> frozenset:
        """Locaux voisins d'une bulle : le sien et ceux ayant une station à moins de
        la distance de voisinage, sur le même plancher (mis en cache)."""
        key = (idx, self.filters.scope_dist)
        hit = self._near_cache.get(key)
        if hit is not None:
            return hit
        st = self.stations[idx]
        out = {st.parts().local or st.locator}
        if idx < len(self.links):
            for lk in self.links[idx]:
                if lk.kind == 'same' and lk.dist_h <= self.filters.scope_dist:
                    t = self.stations[lk.target]
                    out.add(t.parts().local or t.locator)
        res = frozenset(out)
        if len(self._near_cache) > 4000:
            self._near_cache.clear()
        self._near_cache[key] = res
        return res

    def set_scope(self, scope: str) -> None:
        """Ce que l'on voit : local, locaux voisins, distance ou plancher entier."""
        if scope not in SCOPE_LABELS:
            return
        if self.filters.scope != scope:
            self._prev_scope = self.filters.scope
        self.filters.scope = scope
        self.cfg['view_scope'] = scope
        if hasattr(self, 'scope_var'):
            self.scope_var.set(SCOPE_LABELS[scope])
        save_config(self.cfg)
        self._draw_overlay()
        self._redraw_compare()
        self._refresh_side()
        self._draw_plan()
        n = len(self._visible_links(self.current)) if self.current >= 0 else 0
        self._set_status(f"Voir : {self.filters.scope_text()} — {n} pastille(s)",
                         COLORS['sel'])

    def _toggle_scope(self, scope: str) -> None:
        """L et T : bascule vers « local » / « plancher entier », puis retour."""
        if self.filters.scope == scope:
            prev = getattr(self, '_prev_scope', 'voisins')
            self.set_scope(prev if prev != scope else 'voisins')
        else:
            self.set_scope(scope)

    def whole_floor(self) -> bool:
        """Pastilles de toutes les bulles du plancher (défaut), ou réseau élagué."""
        return self.cfg.get('hotspots_mode', 'plancher') != 'reseau'

    def graph_params(self) -> GraphParams:
        """Paramètres du réseau : tout le plancher, ou élagage (portée, nombre, direction)."""
        if self.whole_floor():
            return dc_replace(self.params, kmax=10 ** 6, ang_min=0.0, whole_floor=True)
        return self.params

    def _toggle_all(self, on: Optional[bool] = None) -> None:
        """Touche T : toutes les bulles du plancher ↔ réseau élagué."""
        if on is None:
            on = not self.whole_floor()
        self.cfg['hotspots_mode'] = 'plancher' if on else 'reseau'
        if hasattr(self, 'all_var'):
            self.all_var.set(on)
        save_config(self.cfg)
        self.rebuild_graph()
        n = len(self.links[self.current]) if 0 <= self.current < len(self.links) else 0
        self._set_status(
            (f"Toutes les bulles du plancher : {n} pastille(s) autour de cette bulle "
             "(T : réseau élagué)") if on else
            (f"Réseau élagué : {self.params.kmax} pastilles max à moins de "
             f"{self.params.radius:g} m, une par direction ({self.params.ang_min:g}°) — "
             f"{n} ici (T : tout le plancher)"), COLORS['sel'] if on else None)

    def hotspot_color(self, lk: "Link", tgt: Station) -> str:
        """Couleur d'une pastille : par local (défaut) ou par type de lien.

        Une bulle corrigée garde sa couleur (c'est son texte qui passe en
        orange) ; une image absente reste en rouge sombre.
        """
        if not self.store.has(tgt.photo):
            return COLORS['plan_missing']
        if self.cfg.get('color_mode', 'local') == 'local':
            return local_color(tgt.parts().local or tgt.floor)
        return {'same': COLORS['hot'], 'up': COLORS['hot_up'],
                'down': COLORS['hot_down']}[lk.kind]

    def _set_color_mode(self) -> None:
        self.cfg['color_mode'] = self.color_var.get()
        save_config(self.cfg)
        self._draw_overlay()
        self._redraw_compare()
        self._draw_plan()

    def sphere_pole(self, hs: "Hotspot", hovered: bool) -> Tuple[float, float, float]:
        """Pôle sud de la sphère (point où la verticale du sol entre dans la
        sphère, à l'écran) et rayon de la sphère."""
        r = hs.radius * (1.25 if hovered else 1.0)
        rs = SPHERE_RADIUS * r if self.relief() else r
        fx, fy = hs.foot
        vx, vy = fx - hs.col, fy - hs.row
        n = math.hypot(vx, vy)
        if n < 1e-6:
            return hs.col, hs.row + rs, rs
        return hs.col + vx / n * rs, hs.row + vy / n * rs, rs

    def draw_hotspot(self, canvas, hs: "Hotspot", color: str, hovered: bool,
                     selected: bool = False, mast: bool = True) -> None:
        """Dessine une pastille (relief ou plate) sur un canevas."""
        r = hs.radius * (1.25 if hovered else 1.0)
        floating = hs.foot is not None
        if floating:
            # Sphère au point de vue (là où était l'appareil), ombre posée au sol
            # en perspective, mât entre les deux : la profondeur se lit d'un coup
            # d'œil, et la hauteur de l'appareil aussi.
            fx, fy = hs.foot
            sx, sy = hs.foot_r
            if sx >= 1.0:
                photo, ax, ay = self._shadow(sx, sy)       # ombre douce, à plat au sol
                canvas.create_image(fx - ax, fy - ay, anchor='nw', image=photo, tags='hs')
            if mast:
                # la verticale du point de vue : du sol au pôle sud de la sphère (en
                # perspective, la verticale 3D se projette sur la droite sol → centre)
                px, py, _ = self.sphere_pole(hs, hovered)
                canvas.create_line(fx, fy, px, py, fill='#000000', width=4, tags='hs')
                canvas.create_line(fx, fy, px, py, fill=color, width=2, tags='hs')
        if self.relief():
            photo, ax, ay = self._sprite(color, r, hovered, floating=floating)
            canvas.create_image(hs.col - ax, hs.row - ay, anchor='nw', image=photo,
                                tags='hs')
            if selected:
                rs = SPHERE_RADIUS * r * 1.35
                cy = hs.row - (0 if floating else SPHERE_RADIUS * r * SPHERE_LIFT)
                canvas.create_oval(hs.col - rs, cy - rs, hs.col + rs, cy + rs,
                                   outline=COLORS['sel'], width=2, tags='hs')
            return
        if selected:
            canvas.create_oval(hs.col - r * 1.6, hs.row - r * 0.95,
                               hs.col + r * 1.6, hs.row + r * 0.95,
                               outline=COLORS['sel'], width=2, tags='hs')
        canvas.create_oval(hs.col - r, hs.row - r * 0.55, hs.col + r, hs.row + r * 0.55,
                           fill=color, outline=COLORS['hot_edge'],
                           width=2 if hovered else 1, tags='hs')
        canvas.create_oval(hs.col - r * 0.22, hs.row - r * 0.12,
                           hs.col + r * 0.22, hs.row + r * 0.12,
                           fill=COLORS['hot_edge'], outline='', tags='hs')

    def _on_marks(self) -> None:
        """Cases Distances / Noms / Hauteurs : mémorisées, appliquées aux deux vues."""
        self.cfg['show_labels'] = bool(self.labels_var.get())
        self.cfg['show_names'] = bool(self.names_var.get())
        self.cfg['show_heights'] = bool(self.heights_var.get())
        save_config(self.cfg)
        self._draw_overlay()
        if self.compare is not None:
            self.compare._draw_overlay()

    def declutter(self, hotspots: Sequence["Hotspot"]) -> set:
        """Pastilles autorisées à porter leurs étiquettes, sans chevauchement.

        Les plus proches (les plus grosses à l'écran) passent d'abord ; une
        étiquette qui mordrait sur une autre est omise, la sphère restant
        affichée (le survol montre tout).
        """
        lo = self.disc_bounds()[0] + 0.5
        n_lines = (int(bool(self.labels_var.get())) * 13
                   + int(bool(self.heights_var.get())) * 33)
        taken: List[Tuple[float, float, float, float]] = []
        ok = set()
        for i in sorted(range(len(hotspots)), key=lambda k: -hotspots[k].radius):
            hs = hotspots[i]
            if hs.radius <= lo:
                continue
            tgt = self.stations[hs.link.target]
            w = max(56.0, 6.2 * (len(tgt.locator) + 8)) / 2.0
            y0 = self.glyph_y(hs, False) - 9
            y1 = self.label_y(hs, False) + n_lines - 4
            box = (hs.col - w, y0, hs.col + w, y1)
            if any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
                   for b in taken):
                continue
            taken.append(box)
            ok.add(i)
        return ok

    def draw_marks(self, canvas, hs: "Hotspot", tgt: Station, color: str,
                   hovered: bool, selected: bool = False, missing: bool = False,
                   tag: str = '', labels: bool = True, mire: bool = False) -> None:
        """Étiquettes d'une pastille, lues à chaque dessin (donc toujours à jour).

        Au-dessus : le nom de la station. Dessous : la distance, puis trois
        lignes discrètes — hauteur de l'appareil, delta plancher et altitude
        finale du point de vue (Z caméra). Une valeur corrigée passe en
        orange. Les lignes d'altitude sont réservées aux pastilles proches
        (non réduites à leur taille minimale), à la pastille survolée et à
        la cible d'édition, pour ne pas encombrer le lointain.
        """
        lk = hs.link
        top = self.glyph_y(hs, hovered)
        if lk.kind != 'same':
            canvas.create_text(hs.col, top, text='▲' if lk.kind == 'up' else '▼',
                               fill=color, font=('Segoe UI', 11, 'bold'), tags='hs')
            top -= 13

        def text(x, y, txt, fill, font):
            canvas.create_text(x + 1, y + 1, text=txt, fill='#000000', font=font, tags='hs')
            canvas.create_text(x, y, text=txt, fill=fill, font=font, tags='hs')

        if tag:                              # bulle quittée, ou bulle de l'autre vue
            r = hs.radius * (1.25 if hovered else 1.0)
            rs = (SPHERE_RADIUS * r if self.relief() else r) * 1.7
            cy = hs.row - (SPHERE_RADIUS * r * SPHERE_LIFT
                           if self.relief() and hs.foot is None else 0)
            canvas.create_oval(hs.col - rs, cy - rs, hs.col + rs, cy + rs,
                               outline='white', width=2, dash=(4, 3), tags='hs')
        # Pastilles lointaines (à leur taille minimale) : la sphère seule, pour
        # que tout le plancher reste lisible ; le survol donne tout.
        near = labels and hs.radius > self.disc_bounds()[0] + 0.5
        # Mode num scan : le n° de scan (nom de l'image) au-dessus de la pastille ;
        # le survol ajoute le locator.
        scan = tgt.key_explicit and tgt.key and tgt.key != tgt.locator
        name = tgt.key if scan else tgt.locator
        if scan and (hovered or selected):
            name += f" · {tgt.locator}"
        if (self.names_var.get() and near) or hovered or tag or selected:
            text(hs.col, top, (tag + '  ' if tag else '') + name,
                 COLORS['edit'] if tgt.modified() else
                 'white' if hovered or tag else MARK_TEXT,
                 F_TINY_B if hovered or selected or tag else F_TINY)
        y = self.label_y(hs, hovered)
        if (self.labels_var.get() and near) or hovered:
            txt = human_dist(lk.dist)
            if hovered and missing:
                txt += " · image absente"
            text(hs.col, y, txt, COLORS['edit'] if tgt.modified() else
                 'white' if hovered else '#e8e8e8', F_UI_B if hovered else F_UI)
            y += 13
        if self.heights_var.get() and (near or hovered or selected) and not mire:
            eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
            nd = 3 if self.edit_mode else 2
            for label, val, fix, signed in (
                    ("H", tgt.height(eye), tgt.raised(), False),
                    ("Δ", tgt.delta(eye), tgt.shifted(), True),
                    ("Z", tgt.z, tgt.z_changed(), False)):
                val_txt = f"{val:+.{nd}f}" if signed else f"{val:.{nd}f}"
                text(hs.col, y, f"{label} {val_txt}",
                     COLORS['edit'] if fix else MARK_TEXT, F_TINY)
                y += 11

    def label_y(self, hs: "Hotspot", hovered: bool) -> float:
        r = hs.radius * (1.25 if hovered else 1.0)
        if hs.foot is not None:
            return hs.row + (SPHERE_RADIUS * r if self.relief() else r) + 10
        return hs.row + (SHADOW_RY * r if self.relief() else 0.55 * r) + 10

    def glyph_y(self, hs: "Hotspot", hovered: bool) -> float:
        r = hs.radius * (1.25 if hovered else 1.0)
        if hs.foot is not None:
            return hs.row - (SPHERE_RADIUS * r if self.relief() else r) - 8
        if self.relief():
            return hs.row - SPHERE_RADIUS * r * (SPHERE_LIFT + 1.0) - 8
        return hs.row - r * 0.9

    def disc_bounds(self) -> Tuple[float, float]:
        """Bornes d'affichage des pastilles (px), toujours cohérentes."""
        lo, hi = DISC_PX_LIMITS
        r_min = clamp(float(self.cfg.get('disc_min_px', DISC_PX_MIN)), lo, hi)
        r_max = clamp(float(self.cfg.get('disc_max_px', DISC_PX_MAX)), lo, hi)
        if r_max < r_min + 2.0:      # un réglage incohérent ne casse pas le rendu
            r_max = r_min + 2.0
        return r_min, r_max

    def _draw_overlay(self) -> None:
        view = self._frame_view
        self.canvas.delete('hs')
        self.canvas.delete('tip')
        if view is None or self.current < 0:
            return
        self.hotspots = self._compute_hotspots(view)
        espace = bool(self._hs_drag and self._hs_drag[0] == 'plan')
        if self.edit_mode:
            self._draw_edit_refs(view)
            self._draw_axes(view)
        else:
            self._axis_hits = []
            self._axis_labels = []
        if espace:                               # avant le geste, en transparence
            for col, row, rad, color, flot in getattr(self, '_ghosts', []):
                if self.relief():
                    photo, ax, ay = self._sprite(color, rad, False, alpha=0.30,
                                                 floating=flot)
                    self.canvas.create_image(col - ax, row - ay, anchor='nw', image=photo,
                                             tags='hs')
        libres = self.declutter(self.hotspots)
        mires = set(self.mire_targets(self.current,
                                      self.compare.idx if self.compare is not None else None,
                                      self.hotspots, self._hover))
        for i, hs in enumerate(self.hotspots):
            lk = hs.link
            tgt = self.stations[lk.target]
            missing = not self.store.has(tgt.photo)
            color = self.hotspot_color(lk, tgt)
            hovered = (i == self._hover)
            selected = self.edit_mode and self.selected == tgt.idx
            self.draw_hotspot(self.canvas, hs, color, hovered, selected=selected,
                              mast=tgt.idx not in mires)
            tag = ("↩ origine" if tgt.idx == self.came_from else
                   "B" if self.compare is not None and tgt.idx == self.compare.idx else '')
            self.draw_marks(self.canvas, hs, tgt, color, hovered, selected, missing, tag,
                            labels=i in libres, mire=tgt.idx in mires)
        if self.edit_mode or espace:
            for x, y, txt, col in self._axis_labels:
                self.canvas.create_text(x + 1, y + 1, text=txt, fill='#000000',
                                        font=F_UI_B, tags='hs')
                self.canvas.create_text(x, y, text=txt, fill=col, font=F_UI_B, tags='hs')
        cur = self.station()
        par_bulle = {h.link.target: (k, h) for k, h in enumerate(self.hotspots)}
        for idx in self.mire_targets(self.current,
                                     self.compare.idx if self.compare is not None else None,
                                     self.hotspots, self._hover):
            k, h = par_bulle.get(idx, (None, None))
            self.draw_mire(self.canvas, view, cur, self.stations[idx],
                           COLORS['sel'] if self.compare is not None
                           and idx == self.compare.idx else 'white',
                           hs=h, hovered=k is not None and k == self._hover)
        self._draw_hud(view)
        if self._hover is not None and getattr(self, '_hover_xy', None):
            self._draw_tooltip(self._hover_xy[0], self._hover_xy[1], self._hover)

    def _draw_hud(self, view: View) -> None:
        st = self.station()
        if st is None:
            return
        az = self.calib.azimuth(view.yaw, st.north_pct)
        self.heading_lbl.config(
            text=f"cap {az:+07.1f}°  |  site {view.pitch:+05.1f}°  |  champ {view.fov:.0f}°")
        title = f"{st.locator}   ({st.floor})"
        if st.key_explicit and st.key and st.key != st.locator:
            title = f"{st.key} · {st.locator}   ({st.floor})"
        if st.turned():
            title += f"   Δnord {st.yaw_fix:+.2f}°"
        self.canvas.create_text(15, 13, text=title, anchor='nw', fill='#000000',
                                font=('Segoe UI', 12, 'bold'), tags='hs')
        self.canvas.create_text(14, 12, text=title, anchor='nw',
                                fill=COLORS['edit'] if st.modified() else COLORS['hot'],
                                font=('Segoe UI', 12, 'bold'), tags='hs')
        if not self.filters.active and self.filters.scope != 'plancher':
            self.canvas.create_text(
                view.width - 14, 12, anchor='ne', tags='hs', fill=COLORS['text_muted'],
                font=F_UI, text=f"voir : {self.filters.scope_text()} · "
                                f"{len(self.hotspots)} pastille(s)")
        if self.filters.active:
            self.canvas.create_text(
                view.width - 14, 12, anchor='ne', tags='hs', fill=COLORS['sel'],
                font=F_UI_B,
                text=f"FILTRES : {self.filters.resume()}\n"
                     f"{len(self.hotspots)} pastille(s) affichée(s), "
                     f"{self.hidden_count} masquée(s)", justify='right')
        if self.edit_mode:
            self.canvas.create_text(
                view.width / 2, view.height - 10, anchor='s', tags='hs',
                fill=COLORS['edit'], font=('Segoe UI', 10, 'bold'),
                text="ÉDITION — glisser pastille ou axe : déplacement sur un axe "
                     "(X/Y/Z verrouille) · Ctrl : bulle active · Maj : tourner l'image")
            self._axis_readout(view)
        elif self._hs_drag and self._hs_drag[0] == 'plan':
            self._axis_readout(view)
        # rose des vents : direction du nord dans la vue
        pr = project(view, self.calib.pano_yaw(0.0, st.north_pct), 0.0)
        if pr is not None:
            col, row, _ = pr
            if 0 <= col <= view.width:
                self.canvas.create_text(col, 34, text="N", fill='#ff6b6b',
                                        font=('Segoe UI', 12, 'bold'), tags='hs')
                self.canvas.create_line(col, 44, col, 56, fill='#ff6b6b', width=2, tags='hs')

    def _draw_edit_refs(self, view: View) -> None:
        """Repères d'alignement : toutes les bulles proches, même non liées.

        Elles servent à juger la cohérence de l'orientation de l'image avec
        l'ensemble du réseau, et pas seulement avec les 8 pastilles retenues.
        """
        st = self.station()
        if st is None:
            return
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        linked = {lk.target for lk in self.links[self.current]} if self.current < len(self.links) else set()
        radius = self.params.radius
        for other in self.stations:
            if other.idx == st.idx or other.idx in linked or other.floor != st.floor:
                continue
            dx, dy = other.x - st.x, other.y - st.y
            if abs(dx) > radius or abs(dy) > radius:
                continue
            dh = math.hypot(dx, dy)
            if dh > radius or dh < 1e-6:
                continue
            dz = other.ground(eye) - st.z
            az = math.degrees(math.atan2(dx, dy))
            elev = math.degrees(math.atan2(dz, dh))
            pr = project(view, self.calib.pano_yaw(az, st.north_pct), elev)
            if pr is None:
                continue
            col, row, _ = pr
            if not (0 <= col <= view.width and 0 <= row <= view.height):
                continue
            self.canvas.create_line(col - 6, row, col + 6, row,
                                    fill=COLORS['sel'], tags='hs')
            self.canvas.create_line(col, row - 4, col, row + 4,
                                    fill=COLORS['sel'], tags='hs')
            self.canvas.create_text(col + 8, row - 7, anchor='w', text=other.locator,
                                    fill=COLORS['sel'], font=F_UI, tags='hs')

    def tooltip_lines(self, hs: "Hotspot", origin: Optional[Station]) -> Tuple[List[str], bool]:
        """Infobulle courte : nom, distance, altitude ; le détail est dans le panneau."""
        lk = hs.link
        tgt = self.stations[lk.target]
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        scan = tgt.key_explicit and tgt.key and tgt.key != tgt.locator
        sens = {'same': '', 'up': '  ▲ niveau au-dessus', 'down': '  ▼ niveau en dessous'}
        lines = [f"{tgt.key} · {tgt.locator}" if scan else tgt.locator,
                 f"{lk.dist:.2f} m   Δz {lk.dz:+.2f}{sens[lk.kind]}",
                 f"H {tgt.height(eye):.2f}   Δ {tgt.delta(eye):+.2f}   Z {tgt.z:.2f}"]
        if not self.store.has(tgt.photo):
            lines.append("image absente")
        if tgt.modified():
            marks = []
            if tgt.moved():
                marks.append(f"XY {math.hypot(tgt.x - tgt.ox, tgt.y - tgt.oy):.2f} m")
            if tgt.raised():
                marks.append(f"H {tgt.dh:+.2f}")
            if tgt.shifted():
                marks.append(f"Δ {tgt.ddelta:+.2f}")
            if tgt.turned():
                marks.append(f"nord {tgt.yaw_fix:+.1f}°")
            lines.append("corrigée : " + ' · '.join(marks))
        return lines, tgt.modified()

    def tooltip_allowed(self) -> bool:
        """Infobulle au survol : affichable ou non, et jamais pendant une correction."""
        if not self.cfg.get('show_tooltip', True):
            return False
        if self._hs_drag is not None:
            return False
        last = getattr(self, '_last_adj', None)
        return last is None or time.monotonic() - last[2] > 1.5

    @staticmethod
    def draw_tooltip(canvas, x: int, y: int, lines: List[str], width: int, height: int,
                     modified: bool = False) -> None:
        """Dessine une infobulle (étiquette « tip ») en la gardant dans la vue."""
        canvas.delete('tip')
        item = canvas.create_text(x + 18, y + 18, anchor='nw', text='\n'.join(lines),
                                  fill=COLORS['text'], font=F_MONO, tags='tip')
        bbox = canvas.bbox(item)
        if not bbox:
            return
        x1, y1, x2, y2 = bbox
        dx = dy = 0
        if x2 + 8 > width:
            dx = -(x2 - x1) - 36
        if y2 + 8 > height:
            dy = -(y2 - y1) - 36
        if dx or dy:
            canvas.move(item, dx, dy)
            x1, y1, x2, y2 = canvas.bbox(item)
        rect = canvas.create_rectangle(x1 - 8, y1 - 6, x2 + 8, y2 + 6, fill=COLORS['tip_bg'],
                                       outline=COLORS['edit'] if modified else COLORS['sel'],
                                       width=1, tags='tip')
        canvas.tag_lower(rect, item)

    def _draw_tooltip(self, x: int, y: int, hit: Optional[int]) -> None:
        """Infobulle au survol : nom et attributs de la bulle visée."""
        self.canvas.delete('tip')
        if hit is None or hit >= len(self.hotspots):
            return
        if not self.tooltip_allowed():
            return
        lines, modified = self.tooltip_lines(self.hotspots[hit], self.station())
        self.draw_tooltip(self.canvas, x, y, lines, self.view.width, self.view.height,
                          modified)

    def _hotspot_at(self, x: float, y: float) -> Optional[int]:
        return hotspot_hit(self.hotspots, x, y, self.relief())

    # ═════════════════════════════════════════════════════════════════
    # EVENEMENTS SOURIS / CLAVIER
    # ═════════════════════════════════════════════════════════════════
    def _on_press(self, event) -> None:
        self.focus_set()
        self._press_xy = (event.x, event.y)
        self._hs_drag = None
        if getattr(self, '_space_down', False) and self.current >= 0:
            # Espace + glisser : la station active se déplace en plan, librement ;
            # on tire le terrain, les pastilles suivent le curseur
            self._space_used = True
            if self.start_plan_drag(event, self._frame_view or self.view, self.current,
                                    'monde'):
                return
        if self.edit_mode and self.current >= 0:
            st = self.station()
            ctrl = bool(event.state & 0x0004)
            shift = bool(event.state & 0x0001)
            if shift:                                    # tourner l'image
                self.corrections.apply(st)               # état avant le geste
                self._hs_drag = ('yaw', self.current, st.yaw_fix, event.x)
                return
            axis = self._axis_at(event.x, event.y)
            if axis is not None:                         # saisie d'un axe du repère
                if self._start_axis_drag(self._edit_target(), event, axis, on_origin=True):
                    return
            if ctrl:                                     # deplacer la bulle active
                probe = self._hotspot_at(event.x, event.y)
                self._set_target(None)
                if self._start_axis_drag(st, event, self._locked_axis()):
                    if probe is not None:          # simple clic : ce sera une sonde
                        self._hs_drag[1]['probe'] = self.hotspots[probe].link.target
                    return
            hit = self._hotspot_at(event.x, event.y)
            if hit is not None:
                tgt = self.stations[self.hotspots[hit].link.target]
                self._set_target(tgt.idx)
                self._start_axis_drag(tgt, event, self._locked_axis())
                return
        self._drag = (event.x, event.y, self.view.yaw, self.view.pitch)

    def _on_drag(self, event) -> None:
        if self._hs_drag is not None:
            self._drag_edit(event)
            return
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
        if self._hs_drag is not None:
            self._end_edit_drag()
            return
        self._drag = None
        if moved <= 4 and not self.edit_mode:
            hit = self._hotspot_at(event.x, event.y)
            if hit is not None:
                if event.state & 0x0004:            # Ctrl+clic : sonder
                    self.sonde(self.hotspots[hit].link.target)
                else:                              # clic : y aller, cap conservé
                    self.goto(self.hotspots[hit].link.target)
                return
        if self._interactive:
            self._render_full()

    def _on_motion(self, event) -> None:
        self._hover_xy = (event.x, event.y)
        hit = self._hotspot_at(event.x, event.y)
        if hit != self._hover:
            self._hover = hit
            self.canvas.config(cursor='hand2' if hit is not None else 'fleur')
            if hit is not None and not self.edit_mode:
                self.focus_idx = self.hotspots[hit].link.target
                self._refresh_side()
            self._draw_overlay()
        self._draw_tooltip(event.x, event.y, hit)

    def _on_wheel(self, event, direction: int = 0) -> None:
        step = direction if direction else (1 if getattr(event, 'delta', 0) > 0 else -1)
        if self.wheel_alt(event, self.hotspots, self.current, step):
            return                          # Alt / Maj + molette : Δ / H
        self._zoom(-6 * step)

    def _on_double(self, event) -> None:
        """Double-clic : rejoint la pastille visée, sinon recentre la vue."""
        hit = self._hotspot_at(event.x, event.y)
        if hit is not None:
            if self.edit_mode:
                self.goto(self.hotspots[hit].link.target)
            return
        view = self._frame_view or self.view
        wx, wy, wz = _pano_ray(view, event.x, event.y)
        self.view.yaw = wrap180(math.degrees(math.atan2(wy, wx)))
        self.view.pitch = clamp(math.degrees(math.asin(clamp(wz, -1.0, 1.0))),
                                PITCH_MIN, PITCH_MAX)
        self._request_render(force=True)

    def _nudge(self, yaw: float = 0.0, pitch: float = 0.0) -> None:
        if yaw:
            self.view.yaw = wrap180(self.view.yaw + yaw)
        if pitch:
            self.view.pitch = clamp(self.view.pitch + pitch, PITCH_MIN, PITCH_MAX)
        self._request_render(force=True)

    def fov_reset(self) -> None:
        """Bouton « 120° » : les deux vues au même champ."""
        self.view.fov = FOV_SNAP
        self.fov_var.set(FOV_SNAP)
        self.fov_lbl.config(text=f"{FOV_SNAP:.0f}°")
        self.cfg['fov'] = FOV_SNAP
        self._request_render(force=True)
        if self.compare is not None:
            self.compare.view.fov = FOV_SNAP
            self.compare.request_render(force=True)

    def _zoom(self, delta: float) -> None:
        old = self.view.fov
        new = clamp(old + delta, FOV_MIN, FOV_MAX)
        if (old - FOV_SNAP) * (new - FOV_SNAP) < 0 or (
                old != FOV_SNAP and abs(new - FOV_SNAP) <= FOV_SNAP_TOL):
            new = FOV_SNAP                                 # cran : on s'arrête à 120°
        self.view.fov = new
        self.fov_var.set(self.view.fov)
        self.fov_lbl.config(text=f"{self.view.fov:.0f}°")
        self.cfg['fov'] = self.view.fov
        self._request_render(interactive=True)

    def _on_fov(self, _val=None) -> None:
        v = float(self.fov_var.get())
        if abs(v - FOV_SNAP) <= FOV_SNAP_TOL and v != FOV_SNAP:   # cran aimanté
            v = FOV_SNAP
            self.fov_var.set(v)
        self.view.fov = v
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

    def _on_nb_select(self, _evt=None) -> None:
        """Clic simple : décrit la bulle (distance comprise) sans y aller."""
        sel = self.nb_list.curselection()
        links = self._visible_links(self.current)
        if sel and 0 <= sel[0] < len(links):
            self.focus_idx = links[sel[0]].target
            self._refresh_side()

    def _on_nb_activate(self, _evt=None) -> None:
        sel = self.nb_list.curselection()
        if not sel or self.current < 0:
            return
        links = self._visible_links(self.current)
        i = sel[0]
        if 0 <= i < len(links):
            self.goto(links[i].target)

    # ═════════════════════════════════════════════════════════════════
    # FILTRES DES PASTILLES
    # ═════════════════════════════════════════════════════════════════
    def _build_filter_panel(self, side) -> None:
        head = tk.Frame(side, bg=COLORS['bg_medium'])
        head.pack(fill='x', padx=10, pady=(8, 0))
        self.filter_var = tk.BooleanVar(value=self.filters.active)
        tk.Checkbutton(head, text="Filtres des pastilles  (F)", variable=self.filter_var,
                       command=self._on_filter_change, font=F_UI_B, bg=COLORS['bg_medium'],
                       fg=COLORS['text'], selectcolor=COLORS['bg_light'], bd=0,
                       highlightthickness=0, activebackground=COLORS['bg_medium'],
                       activeforeground=COLORS['text']).pack(side='left')
        self.filter_toggle = self._mk_button(head, "▸", self._toggle_filter_panel)
        self.filter_toggle.pack(side='right')
        self.filter_lbl = tk.Label(side, text="", font=F_UI, bg=COLORS['bg_medium'],
                                   fg=COLORS['text_muted'], anchor='w')
        self.filter_lbl.pack(fill='x', padx=12)

        body = tk.Frame(side, bg=COLORS['card'], padx=8, pady=6)
        self.filter_body = body      # replié au départ : ouvert par le bouton ▸

        row = tk.Frame(body, bg=COLORS['card'])
        row.pack(fill='x', pady=1)
        tk.Label(row, text="Plancher", width=9, anchor='w', font=F_UI,
                 bg=COLORS['card'], fg=COLORS['text']).pack(side='left')
        self.f_floor = tk.StringVar(value=self.filters.floor_mode)
        self.f_floor_cb = ttk.Combobox(row, textvariable=self.f_floor, state='readonly',
                                       style='BN.TCombobox', width=22,
                                       values=('tous', 'courant'))
        self.f_floor_cb.pack(side='left')
        self.f_floor_cb.bind('<<ComboboxSelected>>', self._on_filter_change)

        row = tk.Frame(body, bg=COLORS['card'])
        row.pack(fill='x', pady=1)
        tk.Label(row, text="Distance", width=9, anchor='w', font=F_UI,
                 bg=COLORS['card'], fg=COLORS['text']).pack(side='left')
        self.f_dist = tk.DoubleVar(value=self.filters.max_dist)
        tk.Scale(row, from_=0, to=40, resolution=0.5, orient='horizontal', length=170,
                 variable=self.f_dist, command=self._on_filter_change, showvalue=False,
                 bg=COLORS['text_muted'], fg=COLORS['text'], troughcolor=COLORS['bg_dark'],
                 highlightthickness=0, bd=0, sliderrelief='flat',
                 activebackground=COLORS['accent']).pack(side='left')
        self.f_dist_lbl = tk.Label(row, text="", width=7, font=F_MONO,
                                   bg=COLORS['card'], fg=COLORS['text'])
        self.f_dist_lbl.pack(side='left')

        row = tk.Frame(body, bg=COLORS['card'])
        row.pack(fill='x', pady=1)
        tk.Label(row, text="Local", width=9, anchor='w', font=F_UI,
                 bg=COLORS['card'], fg=COLORS['text']).pack(side='left')
        self.f_local = tk.StringVar(value=self.filters.local)
        # liste des locaux du relevé, et saisie libre (motifs, virgules)
        self.f_local_cb = ttk.Combobox(row, textvariable=self.f_local, width=20,
                                       font=F_MONO, values=())
        self.f_local_cb.pack(side='left')
        self.f_local_cb.bind('<KeyRelease>', self._on_filter_change)
        self.f_local_cb.bind('<<ComboboxSelected>>', self._on_local_chosen)
        tk.Label(body, text="ex. K256, W25*  — préfixe suffisant", font=F_UI,
                 bg=COLORS['card'], fg=COLORS['text_muted'], anchor='w'
                 ).pack(fill='x', padx=(70, 0))

        row = tk.Frame(body, bg=COLORS['card'])
        row.pack(fill='x', pady=(2, 0))
        self.f_inter = tk.BooleanVar(value=self.filters.inter_floor)
        self.f_missing = tk.BooleanVar(value=self.filters.hide_missing)
        self.f_same_local = tk.BooleanVar(value=self.filters.same_local)
        for text, var in (("liens ▲▼", self.f_inter),
                          ("masquer images absentes", self.f_missing)):
            tk.Checkbutton(row, text=text, variable=var, command=self._on_filter_change,
                           font=F_UI, bg=COLORS['card'], fg=COLORS['text'],
                           selectcolor=COLORS['bg_light'], bd=0, highlightthickness=0,
                           activebackground=COLORS['card'], activeforeground=COLORS['text']
                           ).pack(side='left', padx=(0, 8))
        self._mk_button(body, "Réinitialiser les filtres",
                        self._reset_filters).pack(anchor='w', pady=(4, 0))

    def _toggle_filter_panel(self) -> None:
        """Ouvre/replie les réglages ; le plan cède la place quand ils sont ouverts."""
        if self.filter_body.winfo_ismapped():
            self.filter_body.pack_forget()
            self.filter_toggle.config(text="▸")
            if not self.edit_mode:
                self.plan.config(height=PLAN_H)
        else:
            self.filter_body.pack(fill='x', padx=10, pady=(2, 4),
                                  before=self.info_title)
            self.filter_toggle.config(text="▾")
            self.plan.config(height=PLAN_H_EDIT)

    def _toggle_compare(self) -> None:
        """Ouvre ou ferme la seconde vue bulle."""
        if self.compare is not None:
            self.compare.close()
            self.cmp_btn.config(bg=COLORS['bg_light'], fg=COLORS['text'])
            self._set_status("Vue de comparaison fermée")
            return
        if self.current < 0:
            return
        depart = self.current
        voisins = self._visible_links(self.current)
        if voisins:                      # un voisin immédiat : comparaison utile d'emblée
            depart = min(voisins, key=lambda lk: lk.dist).target
        self._open_compare(depart)
        self._set_status("Vue de comparaison ouverte — « Vue liée » fait tourner "
                         "les deux vues ensemble", COLORS['sel'])

    def _open_compare(self, idx: int) -> None:
        self.compare = CompareView(self, idx)
        self.cmp_btn.config(bg=COLORS['sel'], fg='#101010')
        self._cone_sig = None
        self._refresh_ctrlbar()

    def open_in_b(self, idx: int) -> None:
        """Ouvre une bulle dans la vue B, en ouvrant la comparaison au besoin."""
        if not (0 <= idx < len(self.stations)):
            return
        if self.compare is None:
            self._open_compare(idx)
            # le suivi de A ne doit pas remplacer aussitôt la bulle demandée
            self._last_current = self.current
        else:
            self.compare.goto(idx, record=True)
        # B regarde vers A : chaque vue montre la pastille de l'autre, la
        # cohérence de position et d'orientation se juge d'un coup d'œil
        if self.current >= 0 and idx != self.current:
            self.compare.aim_at_station(self.current)
        self._cone_sig = None
        cur = self.station()
        self._set_status(f"{self.stations[idx].locator} ouvert dans la vue B, tourné vers "
                         f"{cur.locator if cur else 'A'} (vue liée suspendue)",
                         COLORS['sel'])

    def _on_right_click(self, event) -> None:
        """Clic droit sur une pastille de A : la sonder (comme Ctrl+clic)."""
        hit = self._hotspot_at(event.x, event.y)
        if hit is not None:
            self.sonde(self.hotspots[hit].link.target)

    def _sync_compare(self) -> None:
        """Tient la seconde vue alignée sur la vue principale."""
        cmp_view = self.compare
        if cmp_view is None:
            return
        if self._last_current != self.current:
            self._last_current = self.current
            cmp_view.follow_a(self.current)
        sig = cmp_view.sync_signature()
        if sig != self._cmp_sig:
            self._cmp_sig = sig
            cmp_view.sync_from_a()

    def _toggle_filters(self) -> None:
        self.filter_var.set(not self.filter_var.get())
        self._on_filter_change()

    def _on_filter_change(self, _evt=None) -> None:
        """Prise en compte immédiate : seules les pastilles sont redessinées."""
        self.filters.active = bool(self.filter_var.get())
        self.filters.floor_mode = self.f_floor.get() or 'tous'
        self.filters.max_dist = float(self.f_dist.get())
        self.filters.local = self.f_local.get()
        self.filters.inter_floor = bool(self.f_inter.get())
        self.filters.hide_missing = bool(self.f_missing.get())
        self.filters.same_local = bool(self.f_same_local.get())
        self.cfg['filter_same_local'] = self.filters.same_local
        self.cfg.update({
            'filter_active': self.filters.active, 'filter_floor': self.filters.floor_mode,
            'filter_dist': self.filters.max_dist, 'filter_local': self.filters.local,
            'filter_inter': self.filters.inter_floor,
            'filter_hide_missing': self.filters.hide_missing})
        self.f_dist_lbl.config(text="illim." if self.filters.max_dist <= 0
                               else f"{self.filters.max_dist:g} m")
        self._draw_overlay()
        self._refresh_side()
        self._draw_plan()
        self._redraw_compare()

    def _reset_filters(self) -> None:
        self.filter_var.set(False)
        self.f_floor.set('tous')
        self.f_dist.set(0.0)
        self.f_local.set('')
        self.f_inter.set(True)
        self.f_missing.set(False)
        self.f_same_local.set(False)
        self._on_filter_change()

    def _on_local_chosen(self, _evt=None) -> None:
        """Un local choisi dans la liste : filtres activés sur ce seul local."""
        self.filter_var.set(True)
        self.f_same_local.set(False)
        self._on_filter_change()
        self._set_status(f"Pastilles du local {self.f_local.get()} seulement "
                         "(Réinitialiser les filtres pour tout revoir)", COLORS['sel'])

    def _toggle_same_local(self) -> None:
        """Touche L : seulement les pastilles du local de la bulle courante."""
        on = not (self.filters.active and self.filters.same_local)
        if on:
            self.filter_var.set(True)
        self.f_same_local.set(on)
        self._on_filter_change()
        cur = self.station()
        loc = (cur.parts().local or cur.locator) if cur else ''
        self._set_status((f"Pastilles du local {loc} seulement — le filtre suit la bulle "
                          "courante (L pour tout revoir)") if on
                         else "Toutes les pastilles (filtre « local courant » retiré)",
                         COLORS['sel'] if on else None)

    def _visible_links(self, idx: int) -> List[Link]:
        """Liens retenus par les filtres pour la bulle `idx`."""
        if idx < 0 or idx >= len(self.links):
            return []
        st = self.stations[idx]
        return [lk for lk in self.links[idx]
                if self.filters.accepts(st, self.stations[lk.target], lk,
                                        self.store.has(self.stations[lk.target].photo))]

    # ═════════════════════════════════════════════════════════════════
    # PANNEAU LATERAL
    # ═════════════════════════════════════════════════════════════════
    def _refresh_side(self) -> None:
        self._refresh_ctrlbar()
        st = self.station()
        if st is None or self.current >= len(self.links):
            return
        focus = st
        if self.edit_mode:
            focus = self._edit_target() or st
        elif self.focus_idx is not None and 0 <= self.focus_idx < len(self.stations):
            focus = self.stations[self.focus_idx]
        self.info_title.config(
            text="Bulle courante" if focus is st else f"Cible : {focus.locator}")
        self.info.config(fg=COLORS['edit'] if focus.modified() else COLORS['text'],
                         text=self._describe(focus, st))

        links = self._visible_links(self.current)
        self.nb_list.delete(0, 'end')
        for lk in links:
            tgt = self.stations[lk.target]
            mark = {'same': ' ', 'up': '▲', 'down': '▼'}[lk.kind]
            flag = '' if self.store.has(tgt.photo) else '  (img?)'
            self.nb_list.insert('end',
                                f"{mark} {tgt.locator:<12} {lk.dist:5.1f} m  "
                                f"az {lk.azimuth:+06.1f}°{flag}")
        hidden = len(self.links[self.current]) - len(links)
        self.nb_title.config(
            text=("Voisins (double-clic pour y aller)" if not hidden else
                  f"Voisins — {hidden} masqué(s) par les filtres"),
            fg=COLORS['sel'] if hidden else COLORS['text'])
        self.filter_lbl.config(
            text=f"{self.filters.resume()} · {len(links)}/{len(self.links[self.current])} "
                 f"pastille(s)",
            fg=COLORS['sel'] if self.filters.active else COLORS['text_muted'])
        self._refresh_edit_panel()

    def _describe(self, st: Station, origin: Optional[Station] = None) -> str:
        """Fiche d'une bulle : nom analysé, position, état, distance à l'origine."""
        p = st.parts()
        lignes = [st.locator, f"photo    {st.photo}"]
        if st.key_explicit or st.key != st.photo:
            lignes.append(f"clé      {st.key}")
        if st.target:
            lignes.append(f"projeté  {st.target}")
        repere = ' · '.join(v for v in (p.campagne, p.site, p.tranche, p.ouvrage) if v)
        if repere:
            lignes.append(f"repère   {repere}")
        detail = ' · '.join(f"{k} {v}" for k, v in (
            ('étage', p.etage + (' (déduit du local)' if p.etage_deduit else '')),
            ('local', p.local), ('index', p.index)) if v and not v.startswith(' '))
        if detail:
            lignes.append(detail)
        if p.niveau_local and not p.etage_deduit and p.etage \
                and p.niveau_local != p.etage.zfill(2):
            lignes.append(f"local du niveau {p.niveau_local}, sur le plancher {p.etage}")
        if st.idx in getattr(self, 'incoherences', {}):
            lignes.append("⚠ " + self.incoherences[st.idx])
        if p.date_lisible():
            lignes.append(f"prise de vue {p.date_lisible()}")
        manque = p.anomalies()
        if manque:
            lignes.append(f"nom      incomplet : {', '.join(manque)} absent(s)")
        lignes += [
            f"plancher {st.floor}",
            f"X/Y/Z    {st.x:.2f} / {st.y:.2f} / {st.z:.2f}",
            f"nord     {st.north_pct:g} %   ·   image "
            f"{'présente' if self.store.has(st.photo) else 'ABSENTE'}",
        ]
        if origin is not None and origin.idx != st.idx:
            _, _, d3 = azimuth_elev(st.x - origin.x, st.y - origin.y, st.z - origin.z)
            dh = math.hypot(st.x - origin.x, st.y - origin.y)
            lignes.append(f"distance {d3:.2f} m (3D) · {dh:.2f} m (plan)")
            lignes.append(f"         Δz {st.z - origin.z:+.2f} m depuis {origin.locator}")
        if st.has_yaw():
            lignes.append(f"Δ nord   {st.yaw_fix:+.3f}°  (à appliquer à l'image)")
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        plancher = (f"plancher {st.floor_alt:+.2f} + " if st.floor_alt is not None
                    else '')
        lignes.append(f"sol      {plancher}Δ {st.delta(eye):+.2f} = {st.ground(eye):.2f}")
        lignes.append(f"caméra   sol + H {st.height(eye):.2f} = {st.z:.2f} (point de vue)")
        if st.moved():
            lignes.append(f"DÉPLACÉE en plan de {math.hypot(st.x - st.ox, st.y - st.oy):.2f} m")
        if st.raised():
            lignes.append(f"HAUTEUR STATION corrigée de {st.dh:+.3f} m")
        if st.shifted():
            lignes.append(f"DELTA PLANCHER corrigé de {st.ddelta:+.3f} m")
        applied = self.corrections.applied.get(st.key) if self.corrections else None
        if applied:
            lignes.append(f"image tournée le {applied}")
        return '\n'.join(lignes)

    # ═════════════════════════════════════════════════════════════════
    # EDITION : POSITION XYZ (CSV) ET ORIENTATION (IMAGE)
    # ═════════════════════════════════════════════════════════════════
    def _build_edit_panel(self, side) -> None:
        # Hôte défilant : le panneau reste utilisable sur un écran peu haut.
        self.edit_host = tk.Frame(side, bg=COLORS['card'])
        vsb = tk.Scrollbar(self.edit_host, orient='vertical')
        vsb.pack(side='right', fill='y')
        holder = tk.Canvas(self.edit_host, bg=COLORS['card'], highlightthickness=0,
                           yscrollcommand=vsb.set)
        holder.pack(side='left', fill='both', expand=True)
        vsb.config(command=holder.yview)
        self.edit_frame = tk.Frame(holder, bg=COLORS['card'], padx=8, pady=6)
        win = holder.create_window((0, 0), window=self.edit_frame, anchor='nw')
        self.edit_frame.bind('<Configure>',
                             lambda e: holder.config(scrollregion=holder.bbox('all')))
        holder.bind('<Configure>', lambda e: holder.itemconfigure(win, width=e.width))

        def wheel(event, direction=0):
            step = direction if direction else (1 if getattr(event, 'delta', 0) > 0 else -1)
            holder.yview_scroll(-step, 'units')
        for widget in (holder, self.edit_frame):
            widget.bind('<MouseWheel>', wheel)
            widget.bind('<Button-4>', lambda e: wheel(e, +1))
            widget.bind('<Button-5>', lambda e: wheel(e, -1))

        def label(parent, text, **kw):
            return tk.Label(parent, text=text, font=F_UI, bg=COLORS['card'],
                            fg=COLORS['text'], **kw)

        # Cible d'edition
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x')
        label(row, "Cible :").pack(side='left')
        self._mk_button(row, "Bulle active", lambda: self._set_target(None),
                        bg=COLORS['bg_light']).pack(side='left', padx=4)
        self.target_lbl = tk.Label(row, text="—", font=F_UI_B, bg=COLORS['card'],
                                   fg=COLORS['sel'])
        self.target_lbl.pack(side='left', padx=4)
        label(self.edit_frame,
              "clic sur une pastille = la prendre pour cible"
              ).pack(anchor='w', pady=(0, 4))

        # Position XYZ
        tk.Label(self.edit_frame, text="Position (CSV corrigé)", font=F_UI_B,
                 bg=COLORS['card'], fg=COLORS['accent']).pack(anchor='w')
        grid = tk.Frame(self.edit_frame, bg=COLORS['card'])
        grid.pack(fill='x', pady=2)
        self.pos_vars = {}
        for i, axis in enumerate(('x', 'y')):
            tk.Label(grid, text=axis.upper(), font=F_MONO, width=2, bg=COLORS['card'],
                     fg=COLORS['text']).grid(row=i, column=0)
            var = tk.StringVar(value='—')
            self.pos_vars[axis] = var
            ent = tk.Entry(grid, textvariable=var, width=11, font=F_MONO,
                           bg=COLORS['bg_light'], fg=COLORS['text'], relief='flat',
                           insertbackground=COLORS['text'])
            ent.grid(row=i, column=1, padx=3, pady=1)
            ent.bind('<Return>', lambda e: self._apply_position_fields())
            self._mk_button(grid, "−", lambda a=axis: self._bump(a, -1)).grid(row=i, column=2)
            self._mk_button(grid, "+", lambda a=axis: self._bump(a, +1)).grid(row=i, column=3, padx=2)
        tk.Label(grid, text="pas", font=F_UI, bg=COLORS['card'],
                 fg=COLORS['text_muted']).grid(row=0, column=4, padx=(8, 2))
        self.step_var = tk.StringVar(value='0.05')
        ttk.Combobox(grid, textvariable=self.step_var, width=5, state='readonly',
                     style='BN.TCombobox', values=('0.01', '0.05', '0.10', '0.50')
                     ).grid(row=1, column=4, padx=(8, 2))

        # Glisser contraint : toujours le long d'un axe
        tk.Label(self.edit_frame, text="Glisser selon l'axe", font=F_UI_B,
                 bg=COLORS['card'], fg=COLORS['accent']).pack(anchor='w', pady=(6, 0))
        ax = self.cfg.get('drag_axis', 'auto')
        self.drag_axis_var = tk.StringVar(value=ax if ax in DRAG_AXIS_MODES else 'auto')
        zk = self.cfg.get('drag_z', 'ddelta')
        self.drag_z_var = tk.StringVar(value=zk if zk in ('dh', 'ddelta') else 'ddelta')
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=2)
        for val, txt in (('auto', "Auto X/Y"), ('x', "X Est"), ('y', "Y Nord"), ('z', "Z")):
            tk.Radiobutton(row, text=txt, value=val, variable=self.drag_axis_var,
                           indicatoron=False, command=self._on_axis_mode, font=F_UI_B,
                           bg=COLORS['bg_light'], fg=AXIS_COLORS.get(val, COLORS['text']),
                           selectcolor=COLORS['bg_dark'], activebackground=COLORS['bg_light'],
                           activeforeground=COLORS['text'], relief='flat', bd=0,
                           padx=8, pady=2).pack(side='left', padx=2)
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=(0, 2))
        label(row, "Z agit sur :").pack(side='left')
        for val, txt in (('ddelta', "Δ plancher"), ('dh', "H station")):
            tk.Radiobutton(row, text=txt, value=val, variable=self.drag_z_var,
                           command=self._on_axis_mode, font=F_UI, bg=COLORS['card'],
                           fg=COLORS['text'], selectcolor=COLORS['bg_dark'],
                           activebackground=COLORS['card'],
                           activeforeground=COLORS['text']).pack(side='left', padx=2)
        label(self.edit_frame,
              "Repère gradué, centré sur la position du CSV.\n"
              "Auto : X ou Y selon le début du geste.\n"
              "Touches X / Y / Z : verrouiller (2e appui : auto).\n"
              "Saisir un axe du repère : le geste le suit.",
              justify='left').pack(anchor='w', pady=(0, 2))

        # Altitude : deux composantes de nature physique differente
        tk.Label(self.edit_frame, text="Altitude (deux composantes)", font=F_UI_B,
                 bg=COLORS['card'], fg=COLORS['accent']).pack(anchor='w', pady=(6, 0))
        zg = tk.Frame(self.edit_frame, bg=COLORS['card'])
        zg.pack(fill='x', pady=2)
        for i, (axis, lib) in enumerate((('dh', "Hauteur station"),
                                         ('ddelta', "Delta plancher"))):
            tk.Label(zg, text=lib, font=F_UI, width=15, anchor='w', bg=COLORS['card'],
                     fg=COLORS['text']).grid(row=i, column=0)
            var = tk.StringVar(value='0.000')
            self.pos_vars[axis] = var
            ent = tk.Entry(zg, textvariable=var, width=8, font=F_MONO,
                           bg=COLORS['bg_light'], fg=COLORS['text'], relief='flat',
                           insertbackground=COLORS['text'])
            ent.grid(row=i, column=1, padx=3, pady=1)
            ent.bind('<Return>', lambda e: self._apply_position_fields())
            self._mk_button(zg, "−", lambda a=axis: self._bump(a, -1)).grid(row=i, column=2)
            self._mk_button(zg, "+", lambda a=axis: self._bump(a, +1)).grid(row=i, column=3, padx=2)
        self.z_lbl = tk.Label(self.edit_frame, text="", font=F_MONO, bg=COLORS['card'],
                              fg=COLORS['text_muted'], anchor='w', justify='left')
        self.z_lbl.pack(fill='x')
        label(self.edit_frame,
              "hauteur station : la caméra bouge, le sol reste (PgUp/PgDn)\n"
              "delta plancher : caméra et sol bougent — marche, faux\n"
              "plancher (Maj+PgUp/PgDn)").pack(anchor='w', pady=(0, 2))
        self._mk_button(self.edit_frame, "Appliquer les valeurs saisies",
                        self._apply_position_fields, bg=COLORS['bg_light']
                        ).pack(anchor='w', pady=(2, 0))

        # Orientation image
        tk.Label(self.edit_frame, text="Orientation — Δ nord (enregistré au CSV)",
                 font=F_UI_B, bg=COLORS['card'], fg=COLORS['accent']
                 ).pack(anchor='w', pady=(8, 0))
        self.yaw_var = tk.DoubleVar(value=0.0)
        self.yaw_scale = tk.Scale(self.edit_frame, from_=-30, to=30, resolution=0.05,
                                  orient='horizontal', variable=self.yaw_var,
                                  command=self._on_yaw_slider, length=300,
                                  showvalue=False, bg=COLORS['text_muted'],
                                  fg=COLORS['text'], troughcolor=COLORS['bg_dark'],
                                  highlightthickness=0, bd=0, sliderrelief='flat',
                                  activebackground=COLORS['edit'])
        self.yaw_scale.pack(fill='x')
        # une manipulation du curseur = une seule étape annulable
        self.yaw_scale.bind('<ButtonPress-1>',
                            lambda e: self.station() and self.corrections.apply(self.station()))
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=2)
        for txt, d in (("−0,5°", -0.5), ("−0,05°", -0.05), ("+0,05°", 0.05), ("+0,5°", 0.5)):
            self._mk_button(row, txt, lambda dd=d: self._nudge_yaw(dd)).pack(side='left', padx=2)
        self.yaw_lbl = tk.Label(row, text="0,00°", font=F_MONO, bg=COLORS['card'],
                                fg=COLORS['edit'])
        self.yaw_lbl.pack(side='right')
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=(0, 2))
        label(row, "appliquer à :").pack(side='left')
        self._mk_button(row, "ce plancher",
                        lambda: self._spread_yaw('plancher')).pack(side='left', padx=3)
        self._mk_button(row, "tout le relevé",
                        lambda: self._spread_yaw('tout')).pack(side='left', padx=3)
        label(self.edit_frame,
              "Maj + glisser dans la vue = tourner l'image ;\n"
              "glisser une pastille = la déplacer le long d'un axe ;\n"
              "Ctrl + glisser = déplacer la bulle active (sur un axe).\n"
              "Rien n'est écrit dans les images : l'angle vit dans le CSV et\n"
              "s'applique à l'affichage. Les images ne sont tournées qu'au\n"
              "moment choisi, par « Appliquer / enregistrer… »."
              ).pack(anchor='w', pady=(2, 6))

        # Fichier de corrections
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=(4, 0))
        label(row, "Corrections :").pack(side='left')
        self.corr_lbl = tk.Label(row, text="—", font=F_UI, bg=COLORS['card'],
                                 fg=COLORS['ok'], anchor='w')
        self.corr_lbl.pack(side='left', padx=4, fill='x', expand=True)
        self._mk_button(row, "Fichier…", self._choose_corrections_file).pack(side='right')

        # Annulation et sorties
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=2)
        self._mk_button(row, "Annuler (Ctrl+Z)", self._undo_edit).pack(side='left')
        self._mk_button(row, "Réinit. cible", self._revert_target).pack(side='left', padx=4)
        self._mk_button(row, "Réinit. tout", self._revert_all).pack(side='left')
        row = tk.Frame(self.edit_frame, bg=COLORS['card'])
        row.pack(fill='x', pady=4)
        self._mk_button(row, "Appliquer / enregistrer…  (Ctrl+S)", self._dlg_apply,
                        bg=COLORS['accent']).pack(side='left')
        self.edit_count = tk.Label(self.edit_frame, text="aucune modification",
                                   font=F_UI, bg=COLORS['card'], fg=COLORS['text_muted'])
        self.edit_count.pack(anchor='w')

    # ── bascule ──────────────────────────────────────────────────────
    @property
    def edit_mode(self) -> bool:
        return bool(getattr(self, 'edit_var', None) and self.edit_var.get())

    def _toggle_edit(self, force: Optional[bool] = None) -> None:
        state = (not self.edit_mode) if force is None else bool(force)
        self.edit_var.set(state)
        if state:
            # Le panneau d'édition a besoin de place : plan réduit et filtres
            # repliés (l'état des filtres est conservé et restitué en sortie).
            self._filters_were_open = self.filter_body.winfo_ismapped()
            if self._filters_were_open:
                self._toggle_filter_panel()
            self.plan.config(height=PLAN_H_EDIT)
            self.nb_title.pack_forget()
            self.nb_wrap.pack_forget()
            self.edit_host.pack(fill='both', expand=True, padx=10, pady=(8, 10))
            self.edit_btn.config(bg=COLORS['edit'], fg='#101010')
            self._set_target(None)
        else:
            self.edit_host.pack_forget()
            self.plan.config(height=PLAN_H)
            if getattr(self, '_filters_were_open', False) \
                    and not self.filter_body.winfo_ismapped():
                self._toggle_filter_panel()
            self.nb_title.pack(anchor='w', padx=10, pady=(10, 2))
            self.nb_wrap.pack(fill='both', expand=True, padx=10, pady=(0, 10))
            self.edit_btn.config(bg=COLORS['bg_light'], fg=COLORS['text'])
            self.selected = None
        self._refresh_edit_panel()
        self._draw_overlay()
        self._draw_plan()

    # ── cible ────────────────────────────────────────────────────────
    def _edit_target(self) -> Optional[Station]:
        if self.selected is not None and 0 <= self.selected < len(self.stations):
            return self.stations[self.selected]
        return self.station()

    def _set_target(self, idx: Optional[int]) -> None:
        self.selected = idx
        self._refresh_edit_panel()
        self._draw_overlay()
        self._draw_plan()

    def _refresh_edit_panel(self) -> None:
        if not hasattr(self, 'target_lbl'):
            return
        st = self._edit_target()
        if st is None:
            return
        who = "bulle active" if self.selected is None else "pastille"
        self.target_lbl.config(text=f"{st.locator}  ({who})")
        for axis in ('x', 'y'):
            self.pos_vars[axis].set(f"{getattr(st, axis):.3f}")
        self.pos_vars['dh'].set(f"{st.dh:+.3f}")
        self.pos_vars['ddelta'].set(f"{st.ddelta:+.3f}")
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        base = f"plancher {st.floor_alt:.3f} + " if st.floor_alt is not None else ''
        self.z_lbl.config(text=(f"{base}Δ {st.delta(eye):+.3f} + H {st.height(eye):.3f} "
                                f"= Z {st.z:.3f}  ·  sol {st.ground(eye):.3f}"))
        self._sync_ui = True
        try:
            self.yaw_var.set(round(self.stations[self.current].yaw_fix, 2)
                             if self.current >= 0 else 0.0)
        finally:
            self._sync_ui = False
        self.yaw_lbl.config(text=f"{self.yaw_var.get():+.2f}°".replace('.', ','))
        bilan = Corrections.counts(self.stations)
        pending = len(Corrections.pending_images(self.stations))
        etat = "modifications non enregistrées" if self.corrections.dirty else "enregistré"
        self.corr_lbl.config(
            text=f"{os.path.basename(self.corrections.path) or '—'} ({etat})",
            fg=COLORS['edit'] if self.corrections.dirty else COLORS['ok'])
        self._refresh_module()
        if bilan.any():
            self.edit_count.config(
                text=(f"corrections : {bilan.texte()}\n"
                      f"images à tourner : {pending}"), fg=COLORS['edit'])
            self.edit_lbl.config(text=f"✎ {bilan.texte()}")
        else:
            self.edit_count.config(
                text=("aucune correction" if not pending else
                      f"{pending} image(s) à tourner"), fg=COLORS['text_muted'])
            self.edit_lbl.config(text="")

    # ── modifications ────────────────────────────────────────────────
    def _after_edit(self, moved: bool = False, turned: bool = False) -> None:
        """Suites d'une correction : rendu, réseau, panneau, sauvegarde."""
        if turned:
            self._request_render(force=True)
        if moved:
            if self._graph_job:
                self.after_cancel(self._graph_job)
            self._graph_job = self.after(300, self._rebuild_after_move)
        self._refresh_side()
        self._draw_overlay()
        self._draw_plan()
        self._redraw_compare()
        if self._autosave_job:
            self.after_cancel(self._autosave_job)
        self._autosave_job = self.after(1200, self._autosave)

    def _redraw_compare(self) -> None:
        """La vue B montre les mêmes stations : ses pastilles suivent les corrections."""
        if self.compare is not None:
            self.compare._draw_overlay()

    def _rebuild_after_move(self) -> None:
        self._graph_job = None
        self.rebuild_graph()

    def _autosave(self) -> None:
        self._autosave_job = None
        if not self.corrections.dirty:
            return
        path = self.corrections.save(self.stations)
        bilan = Corrections.counts(self.stations)
        if path:
            self._set_status(f"corrections enregistrées ({bilan.texte()}) "
                             f"→ {os.path.basename(path)}")
        else:
            self._set_status("Enregistrement des corrections impossible : "
                             f"{self.corrections.path}", COLORS['error'])
        self._refresh_edit_panel()

    def _apply_position_fields(self) -> None:
        st = self._edit_target()
        if st is None:
            return
        vals = {}
        for axis in ('x', 'y', 'dh', 'ddelta'):
            v = parse_float(self.pos_vars[axis].get())
            if v is None:
                messagebox.showwarning("Position", f"Valeur « {axis} » illisible.")
                self._refresh_edit_panel()
                return
            vals[axis] = v
        self.corrections.apply(st, **vals)
        self._after_edit(moved=True)

    def _bump(self, axis: str, sign: int) -> None:
        st = self._edit_target()
        if st is None:
            return
        step = parse_float(self.step_var.get()) or 0.05
        self.corrections.apply(st, **{axis: getattr(st, axis) + sign * step})
        self._after_edit(moved=True)

    def _nudge_yaw(self, delta: float) -> None:
        st = self.station()
        if st is None:
            return
        self.corrections.apply(st, yaw_fix=st.yaw_fix + delta)
        self.yaw_var.set(round(st.yaw_fix, 2))
        self._after_edit(turned=True)

    def _on_yaw_slider(self, _val=None) -> None:
        st = self.station()
        if st is None or getattr(self, '_sync_ui', False):
            return
        value = round(float(self.yaw_var.get()), 2)
        if abs(value - st.yaw_fix) < 5e-3:
            return
        self.corrections.apply(st, yaw_fix=value, record=False)
        self.yaw_lbl.config(text=f"{value:+.2f}°".replace('.', ','))
        self._request_render(interactive=True)
        self._refresh_edit_panel()
        if self._autosave_job:
            self.after_cancel(self._autosave_job)
        self._autosave_job = self.after(1200, self._autosave)

    def _spread_yaw(self, scope: str) -> None:
        st = self.station()
        if st is None:
            return
        value = st.yaw_fix
        targets = [s for s in self.stations
                   if scope == 'tout' or s.floor == st.floor]
        if not messagebox.askyesno(
                "Appliquer la correction d'orientation",
                f"Appliquer Δ nord = {value:+.2f}° à {len(targets)} bulle(s) "
                f"({'tout le relevé' if scope == 'tout' else st.floor}) ?\n\n"
                "Les corrections déjà saisies sur ces bulles seront remplacées."):
            return
        for s in targets:
            self.corrections.apply(s, yaw_fix=value)
        self._after_edit(turned=True)
        self._set_status(f"Δ nord {value:+.2f}° appliqué à {len(targets)} bulle(s)",
                         COLORS['edit'])

    def _undo_edit(self) -> None:
        self.undo()

    def _revert_target(self) -> None:
        st = self._edit_target()
        if st is None or not st.modified():
            return
        self.corrections.revert(st)
        self._after_edit(moved=True, turned=True)

    def _revert_all(self) -> None:
        moved, turned = Corrections.counts(self.stations)
        if not (moved or turned):
            return
        if not messagebox.askyesno("Tout réinitialiser",
                                   f"Annuler les {moved + turned} correction(s) ?"):
            return
        n = self.corrections.revert_all(self.stations)
        self._after_edit(moved=True, turned=True)
        self._set_status(f"{n} bulle(s) réinitialisée(s)", COLORS['edit'])

    # ── deplacements a la souris : toujours le long d'un axe ─────────
    def _locked_axis(self) -> Optional[str]:
        """Axe imposé par l'utilisateur, ou None en mode automatique."""
        v = self.drag_axis_var.get() if hasattr(self, 'drag_axis_var') else 'auto'
        return v if v in AXES else None

    def _lock_axis(self, axis: str) -> None:
        """Touches X / Y / Z : verrouille l'axe (seconde pression : auto)."""
        if not hasattr(self, 'drag_axis_var'):
            return
        self.drag_axis_var.set('auto' if self.drag_axis_var.get() == axis else axis)
        self._on_axis_mode()

    def _on_axis_mode(self) -> None:
        self.cfg['drag_axis'] = self.drag_axis_var.get()
        self.cfg['drag_z'] = self.drag_z_var.get()
        save_config(self.cfg)
        v = self.drag_axis_var.get()
        self._set_status("Glisser : " + (f"axe {AXIS_NAMES[v]} verrouillé" if v in AXES
                                         else "axe choisi par le geste (X ou Y)"))
        self._draw_overlay()
        self._draw_plan()

    def _axis_frame(self, tgt: Station) -> Optional[Dict[str, object]]:
        """Géométrie du repère de `tgt`, relative à la caméra courante (E, N, H).

        origin : position d'origine du CSV (sol) — centre du repère ;
        point  : position actuelle (sol) — là où est la pastille ;
        eye    : caméra de la cible (hauteur station).
        """
        cam = self.station()
        if cam is None:
            return None
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        h0 = tgt.h0 if tgt.h0 is not None else eye
        sol0 = tgt.oz - h0
        origin = (tgt.ox - cam.x, tgt.oy - cam.y, sol0 - cam.z)
        point = (tgt.x - cam.x, tgt.y - cam.y, tgt.ground(eye) - cam.z)
        eye_pt = (tgt.x - cam.x, tgt.y - cam.y, tgt.z - cam.z)
        return {'origin': origin, 'point': point, 'eye': eye_pt,
                'active': tgt.idx == cam.idx, 'north': cam.north_pct}

    def _axis_at(self, x: float, y: float) -> Optional[str]:
        """Axe du repère sous le curseur (hors du voisinage de son centre)."""
        o = self._axis_origin_px
        best, best_d = None, float(AXIS_HIT_PX)
        for axis, x1, y1, x2, y2 in self._axis_hits:
            if o is not None and math.hypot(x - o[0], y - o[1]) < 12:
                return None                     # le centre appartient à la pastille
            dx, dy = x2 - x1, y2 - y1
            l2 = dx * dx + dy * dy
            if l2 < 1:
                continue
            u = clamp(((x - x1) * dx + (y - y1) * dy) / l2, 0.0, 1.0)
            d = math.hypot(x - (x1 + u * dx), y - (y1 + u * dy))
            if d < best_d:
                best, best_d = axis, d
        return best

    def _start_axis_drag(self, tgt: Station, event, axis: Optional[str],
                         on_origin: bool = False) -> bool:
        """Début d'un déplacement contraint : l'état avant le geste est mémorisé."""
        fr = self._axis_frame(tgt)
        if fr is None:
            return False
        self.corrections.apply(tgt)                  # état avant le geste (annulable)
        zkey = self.drag_z_var.get() if hasattr(self, 'drag_z_var') else 'ddelta'
        self._hs_drag = ('axe', {
            'idx': tgt.idx, 'active': fr['active'], 'axis': None, 'want': axis,
            'line': (fr['origin'] if on_origin else
                     fr['eye'] if self.anchor() == 'vue' and not fr['active']
                     else fr['point']),
            'press': (event.x, event.y), 't0': None, 'north': fr['north'],
            'start': (tgt.x, tgt.y, tgt.dh, tgt.ddelta),
            'zkey': zkey if zkey in ('dh', 'ddelta') else 'ddelta', 'd': 0.0})
        if axis is not None:
            self._fix_axis(self._hs_drag[1], axis)
        self._draw_overlay()
        return True

    def _fix_axis(self, info: Dict[str, object], axis: str) -> None:
        info['axis'] = axis
        view = self._frame_view or self.view
        ray = screen_ray(view, *info['press'], self.calib, info['north'])
        info['t0'] = axis_param(ray, info['line'], AXES[axis])

    def _auto_axis(self, info: Dict[str, object], x: float, y: float) -> Optional[str]:
        """Mode auto : l'axe horizontal le plus aligné avec le début du geste."""
        px, py = info['press']
        mx, my = x - px, y - py
        if math.hypot(mx, my) < AXIS_AUTO_PX:
            return None
        view = self._frame_view or self.view
        p = info['line']
        base = project_point(view, self.calib, info['north'], *p)
        best, best_c = 'x', -1.0
        for axis in ('x', 'y'):
            a = AXES[axis]
            tip = project_point(view, self.calib, info['north'],
                                p[0] + 0.5 * a[0], p[1] + 0.5 * a[1], p[2] + 0.5 * a[2])
            if base is None or tip is None:
                continue
            vx, vy = tip[0] - base[0], tip[1] - base[1]
            n = math.hypot(vx, vy)
            if n < 1e-6:
                continue
            c = abs(mx * vx + my * vy) / (n * math.hypot(mx, my))
            if c > best_c:
                best, best_c = axis, c
        return best

    def _drag_edit(self, event) -> None:
        kind = self._hs_drag[0]
        if kind == 'plan':
            self._drag_plan(event)
            return
        if kind == 'yaw':
            _, idx, start, x0 = self._hs_drag
            st = self.stations[idx]
            deg_per_px = self.view.fov / max(1, self.view.width)
            self.corrections.apply(st, yaw_fix=start + (event.x - x0) * deg_per_px,
                                   record=False)
            self.yaw_var.set(round(st.yaw_fix, 2))
            self.yaw_lbl.config(text=f"{st.yaw_fix:+.2f}°".replace('.', ','))
            self._request_render(interactive=True)
            return
        if kind != 'axe':
            return
        info = self._hs_drag[1]
        if info['axis'] is None:
            axis = self._auto_axis(info, event.x, event.y)
            if axis is None:
                return
            self._fix_axis(info, axis)
        axis = info['axis']
        st = self.stations[info['idx']]
        if info['active'] and axis == 'z' and info['t0'] is None:
            # verticale de la bulle active vue d'aplomb : glisser haut/bas
            d = (info['press'][1] - event.y) * AXIS_Z_PX_M
        else:
            if info['t0'] is None:
                self._set_status(f"Axe {AXIS_NAMES[axis]} vu dans l'axe du regard : "
                                 "changer de point de vue ou d'axe", COLORS['error'])
                return
            view = self._frame_view or self.view
            t = axis_param(screen_ray(view, event.x, event.y, self.calib, info['north']),
                           info['line'], AXES[axis])
            if t is None:
                return
            d = t - info['t0']
        d = round(d, 3)                                 # pas du millimètre
        info['d'] = d
        # Bulle active : on tire le terrain, la station part en sens inverse.
        s_ = -d if info['active'] else d
        x0, y0, dh0, dd0 = info['start']
        if axis == 'x':
            self.corrections.apply(st, x=x0 + s_, y=y0, record=False)
        elif axis == 'y':
            self.corrections.apply(st, x=x0, y=y0 + s_, record=False)
        elif info['zkey'] == 'dh':
            self.corrections.apply(st, dh=dh0 + s_, record=False)
        else:
            self.corrections.apply(st, ddelta=dd0 + s_, record=False)
        self._refresh_edit_panel()
        self._draw_overlay()
        self._draw_plan()
        self._redraw_compare()

    def _end_edit_drag(self) -> None:
        kind = self._hs_drag[0] if self._hs_drag else None
        if kind == 'plan':
            self._end_plan_drag()
            return
        if kind == 'axe' and self._hs_drag[1].get('space') and not self.edit_mode:
            self.after_idle(lambda: setattr(self, 'selected', None))
        moved = kind == 'axe' and self._hs_drag[1]['axis'] is not None
        probe = self._hs_drag[1].get('probe') if kind == 'axe' and not moved else None
        idx = (self._hs_drag[1]['idx'] if kind == 'axe' else
               self._hs_drag[1] if kind == 'yaw' else None)
        self._hs_drag = None
        if kind is None:
            return
        if idx is not None and 0 <= idx < len(self.stations) \
                and self.corrections.drop_if_unchanged(self.stations[idx]):
            if self.journal and self.journal[-1] == ('edit',):
                self.journal.pop()          # clic sans effet : aucune étape à annuler
            if probe is not None:
                self.sonde(probe)
                return
            self._draw_overlay()
            return
        if kind == 'axe' and not moved:
            self._draw_overlay()
            return
        self._after_edit(moved=moved, turned=kind == 'yaw')

    # ── repère XYZ ───────────────────────────────────────────────────
    def _draw_axes(self, view: View) -> None:
        """Repère X (Est), Y (Nord), Z centré sur la position d'origine du CSV.

        Les axes sont gradués (pas adapté à la distance) ; un trait relie
        l'origine à la position actuelle et, pendant un geste, le rail de
        l'axe suivi est prolongé.
        """
        self._axis_hits = []
        self._axis_labels = []
        self._axis_origin_px = None
        tgt = self._edit_target()
        if tgt is None:
            return
        fr = self._axis_frame(tgt)
        if fr is None:
            return
        north = fr['north']
        o = fr['origin']
        dist = max(0.3, math.sqrt(sum(c * c for c in o)))
        length = clamp(AXIS_PX * dist / max(1.0, view.focal()), *AXIS_LEN_LIMITS)
        drag = self._hs_drag[1] if self._hs_drag and self._hs_drag[0] == 'axe' else None
        hot = (drag['axis'] or drag['want']) if drag else self._locked_axis()
        c = self.canvas

        def seg(p, q):
            return project_segment(view, self.calib, north, p, q)

        def at(p, a, t):
            return (p[0] + a[0] * t, p[1] + a[1] * t, p[2] + a[2] * t)

        # graduation : le plus petit pas lisible (>= 7 px entre traits)
        px_m = view.focal() / dist
        grad = next((g for g in (0.05, 0.1, 0.25, 0.5, 1.0) if g * px_m >= 7), 1.0)
        for axis in ('x', 'y', 'z'):
            a = AXES[axis]
            lo = -length
            hi = length
            if axis == 'z' and not fr['active']:
                hi = max(length, fr['eye'][2] - o[2] + 0.15)   # jusqu'à la caméra
            sg = seg(at(o, a, lo), at(o, a, hi))
            if sg is None:
                continue
            col = AXIS_COLORS[axis]
            width = 3 if axis == hot else 1.5
            c.create_line(*sg, fill=col, width=width, tags='hs')
            self._axis_hits.append((axis, *sg))
            n = int(length / grad)
            for k in range(-n, n + 1):
                if k == 0:
                    continue
                pr = project_point(view, self.calib, north, *at(o, a, k * grad))
                if pr is not None and pr[2] > 0.05:
                    r = 2.2 if abs(k * grad - round(k * grad)) < 1e-9 else 1.3
                    c.create_oval(pr[0] - r, pr[1] - r, pr[0] + r, pr[1] + r,
                                  fill=col, outline='', tags='hs')
            tip = project_point(view, self.calib, north, *at(o, a, hi))
            if tip is not None and tip[2] > 0.05:
                c.create_text(tip[0] + 1, tip[1] - 9, text=axis.upper(), fill='#000000',
                              font=F_UI_B, tags='hs')
                c.create_text(tip[0], tip[1] - 10, text=axis.upper(), fill=col,
                              font=F_UI_B, tags='hs')
        base = project_point(view, self.calib, north, *o)
        if base is not None and base[2] > 0.05:
            self._axis_origin_px = (base[0], base[1])
            c.create_oval(base[0] - 5, base[1] - 5, base[0] + 5, base[1] + 5,
                          outline='white', width=1.5, tags='hs')
        # déplacement depuis l'origine CSV, décomposé : ΔX puis ΔY puis ΔZ
        p = fr['point']
        if any(abs(p[i] - o[i]) > 1e-4 for i in range(3)):
            self._draw_ghost(view, tgt, fr, o)
            sg = seg(o, p)
            if sg is not None:                 # résultante, fine
                c.create_line(*sg, fill=COLORS['edit'], width=1, dash=(4, 3), tags='hs')
            d = (p[0] - o[0], p[1] - o[1], p[2] - o[2])
            k1 = (o[0] + d[0], o[1], o[2])
            k2 = (o[0] + d[0], o[1] + d[1], o[2])
            for axis, q0, q1, val in (('x', o, k1, d[0]), ('y', k1, k2, d[1]),
                                      ('z', k2, p, d[2])):
                if abs(val) < 1e-4:
                    continue
                sg = seg(q0, q1)
                if sg is None:
                    continue
                col = AXIS_COLORS[axis]
                c.create_line(*sg, fill='#000000', width=5, tags='hs')
                c.create_line(*sg, fill=col, width=3, tags='hs')
                mx, my = (sg[0] + sg[2]) / 2.0, (sg[1] + sg[3]) / 2.0
                # valeurs écrites après les pastilles, pour rester lisibles
                self._axis_labels.append((mx, my - 10, f"Δ{axis.upper()} {val:+.3f}"
                                          .replace('.', ','), col))
        if not fr['active'] and tgt.raised():
            e0 = (o[0], o[1], o[2] + (tgt.h0 if tgt.h0 is not None else
                                      float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))))
            for q, col in ((e0, 'white'), (fr['eye'], COLORS['edit'])):
                pr = project_point(view, self.calib, north, *q)
                if pr is not None and pr[2] > 0.05:
                    c.create_line(pr[0] - 7, pr[1], pr[0] + 7, pr[1], fill=col,
                                  width=2, tags='hs')
        # rail de l'axe suivi pendant le geste
        if drag and drag['axis'] and not (drag['active'] and drag['axis'] == 'z'):
            a = AXES[drag['axis']]
            rail = (fr['point'] if drag['axis'] != 'z' or drag['zkey'] == 'ddelta'
                    else fr['eye'])
            if drag['active']:
                rail = o
            sg = seg(at(rail, a, -3 * length), at(rail, a, 3 * length))
            if sg is not None:
                c.create_line(*sg, fill=AXIS_COLORS[drag['axis']], width=1,
                              dash=(6, 4), tags='hs')

    def _draw_ghost(self, view: View, tgt: Station, fr: Dict[str, object],
                    o: Sequence[float]) -> None:
        """Pastille fantôme, semi-transparente, à la position d'origine du CSV."""
        if fr['active']:
            return                  # la bulle active n'a pas de pastille dans sa vue
        eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
        h0 = tgt.h0 if tgt.h0 is not None else eye
        q = (o[0], o[1], o[2] + h0) if self.anchor() == 'vue' else tuple(o)
        pr = project_point(view, self.calib, fr['north'], *q)
        if pr is None or pr[2] < 0.05:
            return
        dist = max(0.35, math.sqrt(sum(v * v for v in q)))
        r_min, r_max = self.disc_bounds()
        r = clamp(view.focal() * float(self.cfg.get('disc_radius', DISC_RADIUS_M)) / dist,
                  r_min, r_max)
        if self.relief():
            photo, ax, ay = self._sprite(COLORS['hot'], r, False, alpha=0.38,
                                         floating=self.anchor() == 'vue')
            self.canvas.create_image(pr[0] - ax, pr[1] - ay, anchor='nw', image=photo,
                                     tags='hs')
        else:
            self.canvas.create_oval(pr[0] - r, pr[1] - r * 0.55, pr[0] + r, pr[1] + r * 0.55,
                                    outline=COLORS['hot'], dash=(2, 2), tags='hs')
        self.canvas.create_text(pr[0], pr[1] + r * 0.6 + 9, text="origine CSV",
                                fill=MARK_TEXT, font=F_TINY, tags='hs')

    def _axis_readout(self, view: View) -> None:
        """Déplacement de la cible depuis le CSV, axe par axe."""
        tgt = self._edit_target()
        if tgt is None:
            return
        drag = self._hs_drag[1] if self._hs_drag and self._hs_drag[0] == 'axe' else None
        hot = (drag['axis'] or drag['want']) if drag else self._locked_axis()
        items = (('x', f"ΔX {tgt.x - tgt.ox:+.3f}"), ('y', f"ΔY {tgt.y - tgt.oy:+.3f}"),
                 ('z', f"ΔH {tgt.dh:+.3f}  ΔΔ {tgt.ddelta:+.3f}"))
        x = 14
        y = view.height - 34
        head = f"{tgt.locator} / CSV (m)"
        t = self.canvas.create_text(x, y, text=head, anchor='sw', fill=COLORS['sel'],
                                    font=F_UI_B, tags='hs')
        x = self.canvas.bbox(t)[2] + 10
        for axis, txt in items:
            t = self.canvas.create_text(x, y, text=txt.replace('.', ','), anchor='sw',
                                        fill=AXIS_COLORS[axis],
                                        font=F_UI_B if axis == hot else F_UI, tags='hs')
            x = self.canvas.bbox(t)[2] + 14
        mode = f"axe {AXIS_NAMES[hot]}" if hot else "axe auto"
        if drag and drag['axis']:
            mode += f" {drag['d']:+.3f}".replace('.', ',')
        self.canvas.create_text(x, y, text=f"[{mode}]", anchor='sw',
                                fill=COLORS['text'], font=F_UI, tags='hs')

    # ── application par lot ──────────────────────────────────────────
    def _dlg_apply(self) -> None:
        """Bilan des corrections, puis traitements par lot en une passe."""
        if not self.stations:
            return
        self._autosave()
        bilan = Corrections.counts(self.stations)
        pending = Corrections.pending_images(self.stations)

        win = tk.Toplevel(self)
        win.title("Corrections — bilan et application")
        win.configure(bg=COLORS['bg_dark'])
        self._attach_dialog(win)
        win.resizable(False, False)

        tk.Label(win, text="Bilan des corrections", font=F_UI_B, bg=COLORS['bg_dark'],
                 fg=COLORS['accent']).pack(anchor='w', padx=14, pady=(12, 2))
        tk.Label(win, justify='left', anchor='w', font=F_MONO, bg=COLORS['card'],
                 fg=COLORS['text'], padx=10, pady=8, text=(
                     f"positions XY corrigées   {bilan.xy:4d}\n"
                     f"hauteurs de station      {bilan.h:4d}\n"
                     f"deltas plancher          {bilan.delta:4d}\n"
                     f"orientations corrigées   {bilan.nord:4d}\n"
                     f"images à tourner         {len(pending):4d}\n\n"
                     f"relevé chargé (intact)   {os.path.basename(self.csv_path)}\n"
                     f"fichier de corrections   {os.path.basename(self.corrections.path)}")
                 ).pack(fill='x', padx=14)

        img_var = tk.StringVar(value=os.path.join(self.images_dir, '_oriente')
                               if self.images_dir else '')
        base, ext = os.path.splitext(os.path.basename(self.csv_path))
        merged_var = tk.StringVar(value=os.path.join(
            os.path.dirname(self.csv_path),
            f"{base}_corrige_{datetime.now():%Y%m%d_%Hh%M}{ext or '.csv'}"))
        do_img = tk.BooleanVar(value=bool(pending))
        do_merged = tk.BooleanVar(value=True)

        def path_row(var, browse):
            row = tk.Frame(win, bg=COLORS['bg_dark'])
            row.pack(fill='x', padx=30, pady=(0, 6))
            tk.Entry(row, textvariable=var, font=F_UI, bg=COLORS['bg_light'],
                     fg=COLORS['text'], relief='flat', width=52,
                     insertbackground=COLORS['text']).pack(side='left', fill='x', expand=True)
            self._mk_button(row, "Parcourir…", browse).pack(side='left', padx=4)

        def pick_dir():
            path = filedialog.askdirectory(title="Dossier des images orientées",
                                           initialdir=self.images_dir or None)
            if path:
                img_var.set(path)

        def pick_merged():
            path = filedialog.asksaveasfilename(
                title="Relevé complet corrigé", defaultextension=ext or '.csv',
                initialdir=os.path.dirname(merged_var.get()),
                initialfile=os.path.basename(merged_var.get()),
                filetypes=[("Fichiers CSV", "*.csv *.txt"), ("Tous les fichiers", "*.*")])
            if path:
                merged_var.set(path)

        def check(text, var):
            return tk.Checkbutton(win, text=text, variable=var, font=F_UI_B, anchor='w',
                                  bg=COLORS['bg_dark'], fg=COLORS['text'],
                                  selectcolor=COLORS['bg_light'], bd=0, highlightthickness=0,
                                  activebackground=COLORS['bg_dark'],
                                  activeforeground=COLORS['text'])

        workers = max(1, int(self.cfg.get('export_workers', 2)))
        check("Appliquer l'orientation aux images  (copie dans un autre dossier)",
              do_img).pack(fill='x', padx=14, pady=(12, 2))
        tk.Label(win, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'],
                 anchor='w', justify='left', text=(
                     f"{len(pending)} image(s) à écrire, originaux intacts. Panoramas "
                     f"16000×8000 : ~{len(pending) * 7 / workers / 60:.0f} min, "
                     f"~{workers * 0.8:.1f} Go.\n"
                     "Rotation au pixel entier, tables JPEG de la source réutilisées.\n"
                     "Une fois appliqué, le Δ nord repasse à 0 dans le fichier de "
                     "corrections (date d'application conservée).")
                 ).pack(fill='x', padx=30, pady=(0, 4))
        path_row(img_var, pick_dir)

        check("Écrire le CSV corrigé",
              do_merged).pack(fill='x', padx=14, pady=(8, 2))
        tk.Label(win, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'],
                 anchor='w', justify='left', text=(
                     "Même format que le CSV chargé, avec les bonnes valeurs : X, Y, "
                     "Z = plancher + Δ + H,\n"
                     "delta, hauteur instrument et Δ nord. Le CSV chargé n'est pas "
                     "modifié.")
                 ).pack(fill='x', padx=30, pady=(0, 4))
        path_row(merged_var, pick_merged)

        foot = tk.Frame(win, bg=COLORS['bg_dark'])
        foot.pack(fill='x', padx=14, pady=12)

        def run():
            out_dir = img_var.get().strip()
            merged = merged_var.get().strip()
            if do_img.get():
                if not out_dir:
                    messagebox.showwarning("Appliquer", "Indiquez le dossier de destination.")
                    return
                if self.images_dir and os.path.abspath(out_dir) == os.path.abspath(self.images_dir):
                    messagebox.showerror("Appliquer",
                                         "Ce dossier contient les images source : "
                                         "elles seraient écrasées.")
                    return
            if do_merged.get():
                if not merged:
                    messagebox.showwarning("Appliquer", "Indiquez le CSV à écrire.")
                    return
                for reserved in (self.csv_path, self.corrections.path):
                    if reserved and os.path.abspath(merged) == os.path.abspath(reserved):
                        messagebox.showerror("Appliquer",
                                             "Choisissez un autre nom : ce fichier ne "
                                             "doit pas être écrasé.")
                        return
            win.destroy()
            if do_img.get():
                self._run_export(out_dir, merged if do_merged.get() else '')
            elif do_merged.get():
                self._export_merged(merged)

        self._mk_button(foot, "Appliquer", run, bg=COLORS['accent']).pack(side='right')
        self._mk_button(foot, "Fermer", win.destroy).pack(side='right', padx=6)
        self._mk_button(foot, "Enregistrer les corrections maintenant",
                        lambda: (self.corrections.__setattr__('dirty', True),
                                 self._autosave())).pack(side='left')
        win.bind('<Escape>', lambda e: win.destroy())

    def _choose_corrections_file(self) -> None:
        """Change de fichier de corrections (ou en reprend un existant)."""
        if not self.stations:
            messagebox.showinfo("Fichier de corrections", "Chargez d'abord un relevé.")
            return
        path = filedialog.asksaveasfilename(
            title="Fichier de corrections (créé ou repris)",
            initialdir=os.path.dirname(self.corrections.path or self.csv_path),
            initialfile=os.path.basename(self.corrections.path or ''),
            defaultextension='.csv',
            filetypes=[("Fichiers CSV", "*.csv"), ("Tous les fichiers", "*.*")],
            confirmoverwrite=False)
        if not path:
            return
        if self.csv_path and os.path.abspath(path) == os.path.abspath(self.csv_path):
            messagebox.showerror("Fichier de corrections",
                                 "Ce fichier est le relevé chargé : il doit rester intact.")
            return
        self.corrections.path = path
        paths = dict(self.cfg.get('corr_paths', {}))
        paths[self.csv_path] = path
        self.cfg['corr_paths'] = dict(list(paths.items())[-20:])
        save_config(self.cfg)
        if os.path.isfile(path) and messagebox.askyesno(
                "Fichier de corrections",
                f"{os.path.basename(path)} existe déjà.\n\n"
                "Reprendre les corrections qu'il contient ?"):
            try:
                n_ok, n_miss = self.corrections.load(self.by_photo, by_key=self.by_key)
                self._set_status(f"{n_ok} correction(s) reprises"
                                 + (f", {n_miss} sans correspondance" if n_miss else ''),
                                 COLORS['edit'])
                self.rebuild_graph()
            except Exception as exc:
                messagebox.showerror("Fichier de corrections", str(exc))
        else:
            self.corrections.dirty = True
            self._autosave()
        self._refresh_edit_panel()
        self._refresh_module()

    def _export_merged(self, path: str) -> bool:
        """Écrit un relevé complet corrigé, sans rien changer aux fichiers de travail."""
        try:
            n_mod, n_keep, added = write_corrected_csv(
                self.csv_path, path, self.stations,
                eye=float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)),
                mapping=self.csv_mapping)
        except Exception as exc:
            messagebox.showerror("Relevé corrigé", f"Écriture impossible :\n{exc}")
            return False
        self._set_status(f"CSV corrigé écrit : {n_mod} ligne(s) corrigée(s), "
                         f"{n_keep} inchangée(s) → {path}", COLORS['ok'])
        messagebox.showinfo("CSV corrigé", (
            f"{n_mod} ligne(s) corrigée(s), {n_keep} inchangée(s).\n\n{path}\n\n"
            + ("Colonne(s) ajoutée(s) en fin de ligne pour les valeurs corrigées "
               "(Δ nord, delta, hauteur instrument).\n\n" if added else "")
            + "Le CSV chargé n'est pas modifié."))
        return True

    def _run_export(self, out_dir: str, merged_after: str = '') -> None:
        """Applique les Δ nord aux images (copies), puis met à jour les fichiers."""
        todo = [s for s in self.stations if s.has_yaw() and self.store.has(s.photo)]
        absent = len(Corrections.pending_images(self.stations)) - len(todo)
        win = tk.Toplevel(self)
        win.title("Application de l'orientation aux images")
        win.configure(bg=COLORS['bg_dark'])
        self._attach_dialog(win)
        win.resizable(False, False)
        lbl = tk.Label(win, text=f"0 / {len(todo)}", font=F_UI, bg=COLORS['bg_dark'],
                       fg=COLORS['text'], padx=24, pady=10)
        lbl.pack()
        bar = ttk.Progressbar(win, length=420, maximum=max(1, len(todo)))
        bar.pack(padx=24, pady=4)
        cancel = threading.Event()
        self._mk_button(win, "Interrompre", cancel.set).pack(pady=8)

        def progress(done, total, photo):
            self._post(lambda: (bar.config(value=done),
                                lbl.config(text=f"{done} / {total} — {photo}")))

        def work():
            paths = {s.photo.lower(): self.store.path_of(s.photo) for s in todo}
            try:
                ok, skipped, errors = export_rotated_images(
                    self.stations, paths, out_dir,
                    workers=int(self.cfg.get('export_workers', 2)),
                    progress=progress, cancel=cancel)
            except Exception as exc:
                msg = str(exc)            # `exc` n'existe plus quand la lambda s'exécute
                self._post(lambda: (win.destroy(), messagebox.showerror("Export", msg)))
                return
            failed = {e.split(' : ')[0] for e in errors}
            applied = ({s.key for s in todo if s.photo not in failed}
                       if not cancel.is_set() else set())
            self._post(self._export_done, win, out_dir, ok, skipped + absent,
                       errors, applied, merged_after)

        threading.Thread(target=work, name='bubblenav-export', daemon=True).start()

    def _export_done(self, win, out_dir: str, ok: int, skipped: int, errors: List[str],
                     applied: set, merged_after: str) -> None:
        try:
            win.destroy()
        except Exception:
            pass
        msg = f"{ok} image(s) écrite(s) dans :\n{out_dir}"
        if skipped:
            msg += f"\n{skipped} non traitée(s) (interruption ou image absente)."
        if errors:
            msg += "\n\nErreurs :\n" + '\n'.join(errors[:10])
        self._set_status(f"Orientation appliquée : {ok} image(s) → {out_dir}",
                         COLORS['ok'] if not errors else COLORS['warning'])
        if applied:
            # Les images portent l'angle : la correction est consommée.
            for st in self.stations:
                if st.key in applied:
                    self.corrections.apply(st, yaw_fix=0.0, record=False)
            self.corrections.mark_applied(applied)
            self._autosave()
            msg += ("\n\nΔ nord remis à 0 et date d'application inscrite dans "
                    f"{os.path.basename(self.corrections.path)}.")
        if merged_after:
            if self._export_merged(merged_after):
                msg += "\nRelevé complet corrigé écrit."
        else:
            messagebox.showinfo("Orientation appliquée", msg)
        self._refresh_side()
        self._draw_overlay()
        if applied and messagebox.askyesno(
                "Orientation appliquée",
                msg + "\n\nBasculer le visualiseur sur le dossier des images "
                      "orientées ?"):
            self.set_images_dir(out_dir)
            self._request_render(force=True)

    # ═════════════════════════════════════════════════════════════════
    # PLAN (mini-carte)
    # ═════════════════════════════════════════════════════════════════
    def _plan_stations(self) -> List[Station]:
        floor = self.floor_var.get()
        return [s for s in self.stations if s.floor == floor] or self.stations

    def _set_plan_links(self) -> None:
        self.cfg['plan_links'] = self.plan_links_var.get()
        save_config(self.cfg)
        self._draw_plan()

    def _plan_fit(self) -> None:
        self._plan_view['fitted'] = False
        self._draw_plan()

    def _plan_transform(self, pts: Sequence[Station], w: int, h: int):
        pv = self._plan_view
        # Recadrage si rien n'est cadre, si le plancher change, ou si le plan
        # n'a plus la taille pour laquelle le cadrage avait ete calcule (par
        # exemple un chargement effectue avant l'affichage du visualiseur,
        # un redimensionnement ou un passage en plein ecran).
        if (not pv['fitted'] or self._plan_floor != self.floor_var.get()
                or pv.get('fit_size') != (w, h)):
            xs = [s.x for s in pts]
            ys = [s.y for s in pts]
            span_x = max(1e-3, max(xs) - min(xs))
            span_y = max(1e-3, max(ys) - min(ys))
            pv['scale'] = min((w - 30) / span_x, (h - 30) / span_y)
            pv['ox'] = pv['oy'] = 0.0
            pv['cx'] = (max(xs) + min(xs)) / 2.0
            pv['cy'] = (max(ys) + min(ys)) / 2.0
            pv['fitted'] = True
            pv['fit_size'] = (w, h)
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

        # Liens du plancher. Le réseau de navigation compte jusqu'à 8 liens
        # par bulle (12 m) : tracé en entier il devient une toile illisible.
        # Par défaut le plan n'en montre que le squelette.
        mode = self.plan_links_var.get() if hasattr(self, 'plan_links_var') else 'squelette'
        if mode == 'complet':
            # tout le plancher relié : on se limite aux liens de la portée
            segments = {(min(st.idx, lk.target), max(st.idx, lk.target))
                        for st in pts for lk in (self.links[st.idx] if self.links else [])
                        if lk.kind == 'same' and lk.dist_h <= self.params.radius}
        elif mode == 'squelette':
            segments = [(i, j) for i, j in self.plan_edges
                        if self.stations[i].floor == floor]
        else:
            segments = []
        for i, j in segments:
            a, b = self.stations[i], self.stations[j]
            x1, y1 = to_screen(a.x, a.y)
            x2, y2 = to_screen(b.x, b.y)
            self.plan.create_line(x1, y1, x2, y2, fill=COLORS['plan_link'], width=1)
        # liens de la bulle courante : là où mènent les pastilles affichées
        cur = self.station()
        if cur is not None and cur.floor == floor:
            cx_, cy_ = to_screen(cur.x, cur.y)
            for lk in self._visible_links(cur.idx):
                tgt = self.stations[lk.target]
                if tgt.floor == floor and lk.dist_h <= self.params.radius:
                    x2, y2 = to_screen(tgt.x, tgt.y)
                    self.plan.create_line(cx_, cy_, x2, y2, fill=COLORS['hot'],
                                          width=1, dash=(3, 2))

        cur_ = self.station()
        focus_local = ((cur_.parts().local or cur_.locator) if cur_ is not None
                       and self.filters.active and self.filters.same_local else None)
        for st in pts:
            x, y = to_screen(st.x, st.y)
            if -10 <= x <= w + 10 and -10 <= y <= h + 10:
                if focus_local is not None and (st.parts().local or st.locator) != focus_local:
                    col = '#3a3a3a'               # autre local : estompé
                elif not self.store.has(st.photo):
                    col = COLORS['plan_missing']
                elif self.cfg.get('color_mode', 'local') == 'local':
                    col = local_color(st.parts().local or st.floor)
                else:
                    col = COLORS['plan_pt']
                r = 2.5
                ring = ''
                if st.modified():                 # couleur du local, cerclée d'orange
                    r, ring = 3.5, COLORS['edit']
                self.plan.create_oval(x - r, y - r, x + r, y + r, fill=col,
                                      outline=ring, width=1.5 if ring else 1)
                if st.modified() and st.moved():
                    ox, oy = to_screen(st.ox, st.oy)
                    self.plan.create_line(ox, oy, x, y, fill=COLORS['edit'], width=1)
                if self.edit_mode and self.selected == st.idx:
                    self.plan.create_oval(x - 7, y - 7, x + 7, y + 7,
                                          outline=COLORS['sel'], width=2)
        if self.edit_mode:
            tgt = self._edit_target()
            if tgt is not None and tgt.floor == floor:
                ox, oy = to_screen(tgt.ox, tgt.oy)
                if tgt.moved():                  # fantôme + composantes ΔX / ΔY
                    tx, ty = to_screen(tgt.x, tgt.y)
                    self.plan.create_oval(ox - 5, oy - 5, ox + 5, oy + 5,
                                          outline=COLORS['hot'], dash=(2, 2))
                    for axis, (x1, y1, x2, y2), val in (
                            ('x', (ox, oy, tx, oy), tgt.x - tgt.ox),
                            ('y', (tx, oy, tx, ty), tgt.y - tgt.oy)):
                        if abs(val) < 1e-4:
                            continue
                        self.plan.create_line(x1, y1, x2, y2, fill=AXIS_COLORS[axis],
                                              width=2)
                        mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                        dx_, dy_ = (0, -9) if axis == 'x' else (6, 0)
                        self.plan.create_text(mx + dx_, my + dy_, font=F_TINY_B,
                                              anchor='s' if axis == 'x' else 'w',
                                              fill=AXIS_COLORS[axis],
                                              text=f"Δ{axis.upper()} {val:+.3f}"
                                              .replace('.', ','))
                hot = self._locked_axis() or getattr(self, '_plan_axis', None)
                for axis, (dx, dy) in (('x', (1, 0)), ('y', (0, -1))):
                    self.plan.create_line(ox - 18 * dx, oy - 18 * dy, ox + 18 * dx,
                                          oy + 18 * dy, fill=AXIS_COLORS[axis],
                                          width=2.5 if axis == hot else 1)
                    self.plan.create_text(ox + 25 * dx, oy + 25 * dy, text=axis.upper(),
                                          fill=AXIS_COLORS[axis], font=F_UI_B)

        cur = self.station()
        if cur is not None:
            # voisins mis en evidence (filtres compris)
            for lk in self._visible_links(cur.idx):
                tgt = self.stations[lk.target]
                if tgt.floor != floor or lk.dist_h > self.params.radius:
                    continue
                x, y = to_screen(tgt.x, tgt.y)
                self.plan.create_oval(x - 4, y - 4, x + 4, y + 4,
                                      outline=COLORS['hot'], width=1)
        self._draw_plan_cone()

        self.plan.create_text(w - 16, 16, text="N", fill='#ff6b6b', font=F_UI_B)
        self.plan.create_line(w - 16, 26, w - 16, 40, fill='#ff6b6b', width=2)
        self.plan.create_text(8, h - 10, anchor='w', font=F_UI, fill=COLORS['text_muted'],
                              text=f"{len(pts)} bulles · molette: zoom · clic droit: déplacer / ouvrir en B")
        self._draw_plan_tip()

    def _cone_signature(self) -> Optional[tuple]:
        """Tout ce dont dépend le camembert : point de vue, cap, champ, plan."""
        cur = self.station()
        if cur is None:
            return None
        pv = self._plan_view
        try:
            taille = (self.plan.winfo_width(), self.plan.winfo_height())
        except Exception:
            return None
        cmp_view = self.compare
        cmp_etat = ((cmp_view.idx, round(cmp_view.view.yaw, 2),
                     round(cmp_view.view.fov, 2)) if cmp_view is not None else None)
        return (cmp_etat, cur.idx, round(self.view.yaw, 2), round(self.view.fov, 2),
                self.floor_var.get(), round(cur.x, 3), round(cur.y, 3),
                round(cur.north_pct, 4), round(float(pv.get('scale', 1.0)), 4),
                round(float(pv.get('ox', 0.0)), 1), round(float(pv.get('oy', 0.0)), 1),
                round(float(pv.get('cx', 0.0)), 3), round(float(pv.get('cy', 0.0)), 3),
                taille, self.calib.mode, self.calib.sense, round(self.calib.offset, 3))

    def _sync_plan_cone(self) -> None:
        """Redessine le camembert dès que quelque chose a bougé.

        Appelée à chaque battement d'interface (~60 Hz) : rotation, zoom,
        changement de bulle, correction de position, recadrage du plan ou
        calibration sont couverts sans dépendre du pipeline de rendu — une
        image lente à décoder ne fige plus l'indicateur.
        """
        sig = self._cone_signature()
        if sig is not None and sig != self._cone_sig:
            self._draw_plan_cone()

    def _draw_plan_cone(self) -> None:
        """Position et champ de vision sur le plan.

        Calque séparé (étiquette « cone ») redessiné à chaque image affichée :
        le camembert suit donc la rotation de la vue et le zoom en direct, sans
        avoir à retracer tout le réseau.
        """
        self.plan.delete('cone')
        self._cone_sig = self._cone_signature()
        cur = self.station()
        if cur is None or not self.stations:
            return
        floor = self.floor_var.get()
        pts = self._plan_stations()
        if not pts:
            return
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        to_screen, _ = self._plan_transform(pts, w, h)
        rad = 34.0

        def cone(x, y, az, fov, fill, stipple):
            half = fov / 2.0
            plist = [x, y]
            for k in range(9):
                a = math.radians(az - half + k * (2 * half / 8))
                plist += [x + rad * math.sin(a), y - rad * math.cos(a)]
            self.plan.create_polygon(plist, fill=fill, outline='', stipple=stipple,
                                     tags='cone')
            a = math.radians(az)             # axe de visée
            self.plan.create_line(x, y, x + rad * math.sin(a), y - rad * math.cos(a),
                                  fill=fill, width=1, tags='cone')

        # vue A (le plan montre son plancher, sauf choix contraire dans la liste)
        x, y = to_screen(cur.x, cur.y)
        a_ici = not floor or cur.floor == floor
        view = self.view              # état courant : le cône ne suit pas le rendu
        if a_ici:
            cone(x, y, self.calib.azimuth(view.yaw, cur.north_pct), view.fov,
                 COLORS['plan_cone'], 'gray25')
            self.plan.create_oval(x - 5, y - 5, x + 5, y + 5, fill=COLORS['plan_here'],
                                  outline='#000000', tags='cone')

        # vue B : toujours accrochée au plan, même sur un autre plancher
        cmp_view = self.compare
        b = cmp_view.station() if cmp_view is not None else None
        if b is not None:
            bx, by = to_screen(b.x, b.y)
            b_ici = not floor or b.floor == floor
            cone(bx, by, self.calib.azimuth(cmp_view.view.yaw, b.north_pct),
                 cmp_view.view.fov, COLORS['sel'], 'gray12')
            if a_ici:
                self.plan.create_line(x, y, bx, by, fill=COLORS['sel'], width=1,
                                      dash=(3, 3), tags='cone')
            if b_ici:
                self.plan.create_oval(bx - 5, by - 5, bx + 5, by + 5, fill=COLORS['sel'],
                                      outline='#000000', tags='cone')
                label = "B"
            else:                          # autre niveau : repère creux + son plancher
                self.plan.create_oval(bx - 6, by - 6, bx + 6, by + 6, outline=COLORS['sel'],
                                      width=2, dash=(2, 2), tags='cone')
                label = f"B · {b.floor.split('(')[0].strip() or b.floor}"
            self.plan.create_text(bx + 1, by - 10, text=label, fill='#000000',
                                  font=F_UI_B, tags='cone')
            self.plan.create_text(bx, by - 11, text=label, fill=COLORS['sel'],
                                  font=F_UI_B, tags='cone')
        self._cone_sig = self._cone_signature()
        self.plan.tag_raise('plan_tip')

    # ── survol du plan : nom de la station ───────────────────────────
    def _on_plan_motion(self, event) -> None:
        self._set_plan_hover(self._plan_nearest(event, 9.0))

    def _set_plan_hover(self, idx: Optional[int]) -> None:
        if idx != self._plan_hover:
            self._plan_hover = idx
            self._draw_plan_tip()

    def _draw_plan_tip(self) -> None:
        """Nom de la station survolée, en bulle, au-dessus de son point."""
        self.plan.delete('plan_tip')
        idx = self._plan_hover
        if idx is None or not (0 <= idx < len(self.stations)):
            return
        st = self.stations[idx]
        if st.floor != self.floor_var.get():
            return
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        to_screen, _ = self._plan_transform(self._plan_stations(), w, h)
        x, y = to_screen(st.x, st.y)
        name = (f"{st.key} · {st.locator}" if st.key_explicit and st.key
                and st.key != st.locator else st.locator)
        self.plan.create_oval(x - 5, y - 5, x + 5, y + 5, outline='white', width=1.5,
                              tags='plan_tip')
        item = self.plan.create_text(x, y - 10, text=name, anchor='s', font=F_UI_B,
                                     fill=COLORS['text'], tags='plan_tip')
        x1, y1, x2, y2 = self.plan.bbox(item)
        dx = 4 - x1 if x1 < 4 else (w - 4 - x2 if x2 > w - 4 else 0)
        dy = 4 - y1 if y1 < 4 else 0
        if dy:                                   # trop haut : sous le point
            self.plan.itemconfigure(item, anchor='n')
            self.plan.coords(item, x, y + 10)
            x1, y1, x2, y2 = self.plan.bbox(item)
        if dx:
            self.plan.move(item, dx, 0)
            x1, x2 = x1 + dx, x2 + dx
        rect = self.plan.create_rectangle(x1 - 4, y1 - 1, x2 + 4, y2 + 1,
                                          fill=COLORS['tip_bg'], outline=COLORS['sel'],
                                          tags='plan_tip')
        self.plan.tag_lower(rect, item)
        self.plan.tag_raise('plan_tip')

    def _plan_nearest(self, event, max_px: float = 20.0) -> Optional[int]:
        pts = self._plan_stations()
        if not pts:
            return None
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        to_screen, _ = self._plan_transform(pts, w, h)
        best, best_d = None, max_px * max_px
        for st in pts:
            x, y = to_screen(st.x, st.y)
            d = (x - event.x) ** 2 + (y - event.y) ** 2
            if d < best_d:
                best, best_d = st.idx, d
        return best

    def _on_plan_press_left(self, event) -> None:
        self._plan_press = (event.x, event.y)
        self._plan_hit = None
        if not self.edit_mode:
            return
        idx = self._plan_nearest(event, 12.0)
        if idx is not None:
            self._set_target(idx)
            st = self.stations[idx]
            self.corrections.apply(st)                   # état avant le geste
            self._plan_hit = idx
            self._plan_start = (st.x, st.y)
            self._plan_axis = self._locked_axis()
            if self._plan_axis == 'z':
                self._set_status("Axe Z verrouillé : le plan ne règle que X et Y — "
                                 "glisser dans la vue", COLORS['edit'])

    def _on_plan_drag_left(self, event) -> None:
        """Vue de dessus : déplacement de la cible le long de X ou de Y."""
        if self._plan_hit is None or self._plan_axis == 'z':
            return
        mx, my = event.x - self._plan_press[0], event.y - self._plan_press[1]
        if self._plan_axis is None:
            if math.hypot(mx, my) < AXIS_AUTO_PX:
                return
            self._plan_axis = 'x' if abs(mx) >= abs(my) else 'y'
        scale = max(1e-9, float(self._plan_view.get('scale', 1.0)))
        x0, y0 = self._plan_start
        if self._plan_axis == 'x':
            x, y = x0 + round(mx / scale, 3), y0
        else:
            x, y = x0, y0 - round(my / scale, 3)
        self.corrections.apply(self.stations[self._plan_hit], x=x, y=y, record=False)
        self._refresh_edit_panel()
        self._draw_plan()
        self._draw_overlay()

    def _on_plan_release_left(self, event) -> None:
        if self._plan_hit is not None:
            st = self.stations[self._plan_hit]
            self._plan_hit = None
            self._plan_axis = None
            if self.corrections.drop_if_unchanged(st):      # simple clic : pas d'étape
                if self.journal and self.journal[-1] == ('edit',):
                    self.journal.pop()
                return
            self._after_edit(moved=True)
            return
        press = getattr(self, '_plan_press', None)
        if press and abs(event.x - press[0]) + abs(event.y - press[1]) <= 3 \
                and not self.edit_mode:
            idx = self._plan_nearest(event, 22.0)
            if idx is not None:
                self.goto(idx)

    def _on_plan_double(self, event) -> None:
        idx = self._plan_nearest(event, 22.0)
        if idx is not None:
            self.goto(idx)

    def _on_plan_press(self, event) -> None:
        self._plan_drag = (event.x, event.y, self._plan_view['ox'], self._plan_view['oy'])

    def _on_plan_release_right(self, event) -> None:
        """Clic droit sans glisser sur un point du plan : l'ouvrir dans la vue B."""
        start = self._plan_drag
        self._plan_drag = None
        if start and abs(event.x - start[0]) + abs(event.y - start[1]) <= 3:
            idx = self._plan_nearest(event, 12.0)
            if idx is not None:
                self.open_in_b(idx)

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
    def _dlg_mapping(self, path: str, need: "ColumnMappingNeeded") -> Optional[Dict[str, str]]:
        """Correspondance des colonnes, quand elle n'a pas pu être devinée.

        Aperçu des premières lignes, un choix de colonne par champ, et les
        colonnes déjà reconnues présélectionnées. Retourne {champ: « #n »} ou
        None si l'utilisateur annule. Le choix est mémorisé pour ce format.
        """
        win = tk.Toplevel(self)
        win.title("Colonnes du CSV")
        win.configure(bg=COLORS['bg_dark'])
        self._attach_dialog(win)
        win.resizable(True, False)
        tk.Label(win, text=os.path.basename(path), font=F_UI_B, bg=COLORS['bg_dark'],
                 fg=COLORS['accent']).pack(anchor='w', padx=14, pady=(12, 0))
        tk.Label(win, text=str(need).split('\n')[0] +
                 "\nIndiquez quelle colonne correspond à quoi ; le choix sera retenu "
                 "pour les fichiers de même format.",
                 font=F_UI, justify='left', anchor='w', bg=COLORS['bg_dark'],
                 fg=COLORS['text']).pack(fill='x', padx=14, pady=(2, 8))

        # aperçu
        apercu = tk.Text(win, height=min(7, len(need.sample) + 1), width=96, font=F_MONO,
                         bg=COLORS['card'], fg=COLORS['text'], relief='flat', wrap='none')
        widths = [max([len(str(h))] + [len(r[k]) if k < len(r) else 0 for r in need.sample])
                  for k, h in enumerate(need.header)]
        widths = [min(w, 22) for w in widths]
        def ligne(cells):
            return '  '.join(str(cells[k] if k < len(cells) else '')[:22].ljust(widths[k])
                             for k in range(len(need.header)))
        apercu.insert('end', ligne(need.header) + '\n')
        for r in need.sample:
            apercu.insert('end', ligne(r) + '\n')
        apercu.config(state='disabled')
        apercu.pack(fill='x', padx=14)

        choix = ["—"] + [f"{k + 1}. {h}" for k, h in enumerate(need.header)]
        grid = tk.Frame(win, bg=COLORS['bg_dark'])
        grid.pack(fill='x', padx=14, pady=10)
        vars_: Dict[str, tk.StringVar] = {}
        for i, (field_name, lib, oblig) in enumerate(MAPPING_FIELDS):
            tk.Label(grid, text=lib + (" *" if oblig else ""), font=F_UI, anchor='w',
                     width=28, bg=COLORS['bg_dark'],
                     fg=COLORS['text'] if oblig else COLORS['text_muted']
                     ).grid(row=i // 2, column=(i % 2) * 2, sticky='w', pady=2)
            k = need.guess.get(field_name)
            if field_name == 'key' and k is None:
                k = need.guess.get('photo')
            if field_name == 'photo' and need.guess.get('photo') == need.guess.get('key'):
                k = None
            var = tk.StringVar(value=choix[k + 1] if k is not None else "—")
            vars_[field_name] = var
            ttk.Combobox(grid, textvariable=var, values=choix, state='readonly', width=24,
                         style='BN.TCombobox').grid(row=i // 2, column=(i % 2) * 2 + 1,
                                                    padx=(4, 16), pady=2)
        result: Dict[str, Optional[Dict[str, str]]] = {'map': None}

        def valider():
            m: Dict[str, str] = {}
            for field_name, var in vars_.items():
                v = var.get()
                if v != "—":
                    m[field_name] = '#' + str(int(v.split('.', 1)[0]) - 1)
            manque = [lib for f, lib, oblig in MAPPING_FIELDS if oblig and f not in m]
            if manque:
                messagebox.showwarning("Colonnes du CSV", "À renseigner : " + ', '.join(manque),
                                       parent=win)
                return
            result['map'] = m
            win.destroy()

        foot = tk.Frame(win, bg=COLORS['bg_dark'])
        foot.pack(fill='x', padx=14, pady=(0, 12))
        self._mk_button(foot, "Valider", valider, bg=COLORS['accent']).pack(side='right')
        self._mk_button(foot, "Annuler", win.destroy).pack(side='right', padx=6)
        win.bind('<Return>', lambda e: valider())
        win.bind('<Escape>', lambda e: win.destroy())
        win.update_idletasks()
        try:
            win.grab_set()
        except Exception:
            pass
        win.focus_force()                    # Entrée / Échap actifs d'emblée
        self.wait_window(win)
        return result['map']

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
        self._attach_dialog(win)
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
            eye_changed = abs(float(eye_var.get()) - float(self.cfg.get(
                'eye_height', EYE_HEIGHT_DEFAULT))) > 1e-9
            self.cfg['eye_height'] = float(eye_var.get())
            if eye_changed:
                self.corrections.eye = float(eye_var.get())
                self.refresh_altimetry(delay_ms=250)
            self._draw_overlay()
            self._draw_plan()
            self._redraw_compare()
            if self.current >= 0:
                self._refresh_side()

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
        slider(cal, "Hauteur instrument (m)", eye_var, 0.0, 3.0, 0.01, apply_calib)
        tk.Label(cal, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'], anchor='w',
                 justify='left',
                 text="Hauteur de l'appareil au-dessus du sol, pour les bulles sans colonne\n"
                      "de hauteur instrument.").pack(fill='x')
        disc_var = tk.DoubleVar(value=float(self.cfg.get('disc_radius', DISC_RADIUS_M)))
        dmin_var = tk.DoubleVar(value=self.disc_bounds()[0])
        dmax_var = tk.DoubleVar(value=self.disc_bounds()[1])
        disc_note = tk.Label(cal, font=F_UI, bg=COLORS['bg_dark'],
                             fg=COLORS['text_muted'], anchor='w')

        def apply_disc(_=None):
            self.cfg['disc_radius'] = float(disc_var.get())
            self.cfg['disc_min_px'] = float(dmin_var.get())
            self.cfg['disc_max_px'] = float(dmax_var.get())
            r_min, r_max = self.disc_bounds()
            f = (self._frame_view or self.view).focal()
            rayon = float(disc_var.get())
            proche = f * rayon / max(1e-6, r_max)     # en deçà : taille plafonnée
            loin = f * rayon / max(1e-6, r_min)       # au delà : taille plancher
            disc_note.config(text=f"taille pleinement proportionnelle entre "
                                  f"{proche:.1f} m et {loin:.0f} m")
            self._draw_overlay()

        relief_var = tk.BooleanVar(value=self.relief())
        tk.Checkbutton(cal, text="Pastilles en relief (sphère ombrée)", variable=relief_var,
                       command=lambda: (self.cfg.__setitem__('disc_3d', bool(relief_var.get())),
                                        self._draw_overlay(),
                                        self.compare and self.compare._draw_overlay()),
                       font=F_UI, anchor='w', bg=COLORS['bg_dark'], fg=COLORS['text'],
                       selectcolor=COLORS['bg_light'], activebackground=COLORS['bg_dark'],
                       activeforeground=COLORS['text'], bd=0, highlightthickness=0
                       ).pack(fill='x', pady=(4, 0))
        slider(cal, "Rayon des pastilles (m)", disc_var, 0.10, 1.00, 0.01, apply_disc)
        slider(cal, "Taille mini (px)", dmin_var, DISC_PX_LIMITS[0], 40, 1, apply_disc)
        slider(cal, "Taille maxi (px)", dmax_var, 12, DISC_PX_LIMITS[1], 1, apply_disc)
        disc_note.pack(fill='x')
        apply_disc()

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
        slider(net, "Pastilles max", kmax_var, 1, 40, 1, apply_graph)
        slider(net, "Séparation angulaire (°)", ang_var, 0, 60, 1, apply_graph)
        slider(net, "Portée inter-plancher (m)", fr_var, 0, 20, 0.5, apply_graph)
        tk.Label(net, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'], anchor='w',
                 justify='left',
                 text="Une seule pastille par direction (séparation angulaire) : mettre 0° et\n"
                      "augmenter « Pastilles max » pour tout voir, ou touche T (toutes les\n"
                      "pastilles, sans élagage). Le champ de vision limite aussi l'affichage."
                 ).pack(fill='x')

        # ── Vue et molette ──────────────────────────────────────────
        vue = section("Vue")
        scope_d = tk.DoubleVar(value=self.filters.scope_dist)

        def apply_scope_dist(_=None):
            self.filters.scope_dist = float(scope_d.get())
            self.cfg['scope_dist'] = self.filters.scope_dist
            self._near_cache.clear()
            self._draw_overlay()
            self._redraw_compare()
            self._draw_plan()

        slider(vue, "Distance de voisinage (m)", scope_d, 3, 40, 1, apply_scope_dist)
        row = tk.Frame(vue, bg=COLORS['bg_dark'])
        row.pack(fill='x', pady=(4, 0))
        tk.Label(row, text="Pas de la molette H / Δ", width=22, anchor='w', font=F_UI,
                 bg=COLORS['bg_dark'], fg=COLORS['text']).pack(side='left')
        step_v = tk.DoubleVar(value=self.wheel_step())
        for txt, val in (("5 cm", 0.05), ("1 cm", 0.01)):
            tk.Radiobutton(row, text=txt, variable=step_v, value=val, font=F_UI,
                           command=lambda: self.cfg.__setitem__('wheel_step',
                                                                float(step_v.get())),
                           bg=COLORS['bg_dark'], fg=COLORS['text'],
                           selectcolor=COLORS['bg_light'], activebackground=COLORS['bg_dark'],
                           activeforeground=COLORS['text'], bd=0, highlightthickness=0
                           ).pack(side='left', padx=4)
        row = tk.Frame(vue, bg=COLORS['bg_dark'])
        row.pack(fill='x', pady=(4, 0))
        tk.Label(row, text="Qualité des images (px)", width=22, anchor='w', font=F_UI,
                 bg=COLORS['bg_dark'], fg=COLORS['text']).pack(side='left')
        qual = ttk.Combobox(row, textvariable=self.qual_var, width=8, state='readonly',
                            style='BN.TCombobox', values=[str(v) for v in SRC_WIDTH_CHOICES])
        qual.pack(side='left')
        qual.bind('<<ComboboxSelected>>', self._on_quality)

        # ── Performance ─────────────────────────────────────────────
        perf = section("Performance")
        cache_var = tk.IntVar(value=self.store.cache_size)

        def apply_cache(_=None):
            self.store.set_cache_size(int(cache_var.get()))
            self.cfg['cache_size'] = int(cache_var.get())

        slider(perf, "Bulles en mémoire", cache_var, 3, 60, 1, apply_cache)
        tk.Label(perf, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'],
                 anchor='w', text=(
                     f"source {self.store.src_width} px : {self.store.frame_mb():.0f} Mo "
                     f"par bulle, {self.store.effective_cache()} gardée(s) en mémoire")
                 ).pack(fill='x')
        row = tk.Frame(perf, bg=COLORS['bg_dark'])
        row.pack(fill='x', pady=(4, 0))
        tk.Label(row, text="Visualiseur à l'ouverture", width=22, anchor='w', font=F_UI,
                 bg=COLORS['bg_dark'], fg=COLORS['text']).pack(side='left')
        start_var = tk.StringVar(value=str(self.cfg.get('viewer_start', VIEWER_START_MODES[0])))
        cb_start = ttk.Combobox(row, textvariable=start_var, state='readonly', width=14,
                                style='BN.TCombobox', values=VIEWER_START_MODES)
        cb_start.pack(side='left')
        cb_start.bind('<<ComboboxSelected>>',
                      lambda e: self.cfg.__setitem__('viewer_start', start_var.get()))
        tk.Label(perf, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'], anchor='w',
                 text="F11 bascule le plein écran, Échap en sort.").pack(fill='x')
        exp_var = tk.IntVar(value=int(self.cfg.get('export_workers', 2)))
        slider(perf, "Tâches d'export d'images", exp_var, 1, 8, 1,
               lambda _=None: self.cfg.__setitem__('export_workers', int(exp_var.get())))
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
        """Aide (F1 ou ?) : tous les raccourcis, dans une fenêtre qui défile."""
        old = getattr(self, '_help_win', None)
        if old is not None and old.winfo_exists():
            old.lift()
            old.focus_force()
            return
        win = tk.Toplevel(self)
        self._help_win = win
        win.title(f"{APP_NAME} v{__version__} — raccourcis")
        win.configure(bg=COLORS['bg_dark'])
        self._attach_dialog(win)
        body = tk.Frame(win, bg=COLORS['bg_dark'])
        body.pack(fill='both', expand=True, padx=10, pady=(10, 4))
        sb = tk.Scrollbar(body)
        sb.pack(side='right', fill='y')
        txt = tk.Text(body, width=78, height=34, font=F_MONO, bg=COLORS['card'],
                      fg=COLORS['text'], relief='flat', wrap='none', yscrollcommand=sb.set,
                      padx=10, pady=8)
        txt.pack(side='left', fill='both', expand=True)
        sb.config(command=txt.yview)
        txt.tag_configure('titre', foreground=COLORS['accent'], font=F_UI_B)
        for line in HELP_TEXT.splitlines():
            txt.insert('end', line + '\n', 'titre' if line.isupper() or line.startswith(
                ('NAVIGATION', 'AFFICHAGE', 'COMPARAISON', 'PLAN', 'ÉDITION', 'FICHIERS'))
                else ())
        txt.config(state='disabled')
        self._mk_button(win, "Fermer", win.destroy, bg=COLORS['accent']
                        ).pack(anchor='e', padx=10, pady=(0, 10))
        win.bind('<Escape>', lambda e: win.destroy())

    # ═════════════════════════════════════════════════════════════════
    # FERMETURE
    # ═════════════════════════════════════════════════════════════════
    def _on_close(self) -> None:
        try:
            if self.stations and self.corrections.dirty:
                self.corrections.save(self.stations)
        except Exception:
            pass
        try:
            self.cfg['fov'] = self.view.fov
            self.cfg['show_labels'] = bool(self.labels_var.get())
            self.cfg['show_names'] = bool(self.names_var.get())
            self.cfg['show_heights'] = bool(self.heights_var.get())
            if self._viewer_visible() and not self._viewer_fullscreen():
                self.cfg['viewer_geometry'] = self.viewer.geometry()
            save_config(self.cfg)
        except Exception:
            pass
        if self.compare is not None:
            try:
                self.compare.close()
            except Exception:
                pass
        self._stop.set()
        for attr in ('_pump_job', '_idle_job', '_autosave_job', '_graph_job'):
            job = getattr(self, attr, None)
            if job:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attr, None)
        with self._cv:
            self._cv.notify_all()
        try:
            self.store.close()
        except Exception:
            pass
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# SECONDE VUE BULLE (COMPARAISON)
# ─────────────────────────────────────────────────────────────────────────────

class CompareView(tk.Frame if _TK_OK else object):
    """Seconde vue bulle, pour comparer deux points de vue.

    Elle partage tout le modèle avec la vue principale (relevé, réseau, filtres,
    calibration, corrections, cache d'images) et se contente d'un point de vue
    distinct. En mode « vue liée », elle regarde en permanence dans la même
    direction terrain que la vue principale : tourner d'un côté tourne des deux.
    """

    FOLLOW_MODES = ('aucun', 'même local, autre plancher', 'bulle la plus proche')

    def __init__(self, app: "BubbleNavApp", idx: int):
        super().__init__(app.views, bg=COLORS['bg_dark'])
        self.app = app
        self.idx = idx
        self.view = View(app.view.yaw, app.view.pitch, app.view.fov, 900, 560)
        self.hotspots: List[Hotspot] = []
        self.hidden_count = 0
        self._frame_view: Optional[View] = None
        self._tk_img = None
        self._shown_seq = -1
        self._drag = None
        self._hover: Optional[int] = None
        self._hover_xy = None
        self._idle_job = None
        self._closed = False
        self.linked = tk.BooleanVar(value=True)
        self.came_from: Optional[int] = None      # bulle quittée dans B
        self.follow = tk.StringVar(value=self.FOLLOW_MODES[0])

        self._build_ui()
        app.views.add(self, minsize=160, stretch='always')
        self.after(60, self._share_height)
        self.after(80, lambda: self.request_render(force=True))

    def _share_height(self) -> None:
        """Les deux vues se partagent la hauteur à parts égales."""
        try:
            h = self.app.views.winfo_height()
            if h > 100:
                self.app.views.sash_place(0, 0, h // 2)
        except Exception:
            pass

    # ── interface ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        bar = tk.Frame(self, bg=COLORS['bg_medium'])
        bar.pack(fill='x', side='top', pady=(4, 0))
        tk.Label(bar, text="Vue B", font=F_TITLE, bg=COLORS['bg_medium'],
                 fg=COLORS['sel']).pack(side='left', padx=(10, 8), pady=4)
        self.title_lbl = tk.Label(bar, text="—", font=F_UI_B, bg=COLORS['bg_medium'],
                                  fg=COLORS['text'])     # (détail de la bulle, gardé caché)
        self.app.build_view_header(bar, 'B')

        self.app._mk_button(bar, "✕", self.close,
                            tip="Fermer la vue B (touche C).").pack(side='right',
                                                                     padx=(4, 8), pady=4)
        self.app.tip(tk.Checkbutton(bar, text="Vue liée", variable=self.linked,
                       command=self._on_linked, font=F_UI, bg=COLORS['bg_medium'],
                       fg=COLORS['text'], selectcolor=COLORS['bg_light'], bd=0,
                       highlightthickness=0, activebackground=COLORS['bg_medium'],
                       activeforeground=COLORS['text']),
                     "Vue liée : B regarde la même direction terrain que A ; tourner "
                     "ou zoomer d'un côté agit sur les deux.").pack(side='right', padx=6)
        tk.Label(bar, text="Suivi de A", font=F_UI, bg=COLORS['bg_medium'],
                 fg=COLORS['text_muted']).pack(side='right', padx=(8, 2))
        cb = ttk.Combobox(bar, textvariable=self.follow, state='readonly', width=14,
                          style='BN.TCombobox', values=self.FOLLOW_MODES)
        cb.pack(side='right', pady=4)
        cb.bind('<<ComboboxSelected>>', lambda e: self.follow_a(self.app.current))
        self.app.tip(cb, "Quand A change de bulle, B suit : même local à un autre "
                         "plancher, ou la bulle la plus proche (aucun = B reste).")

        self.canvas = tk.Canvas(self, bg='#101010', highlightthickness=0, cursor='fleur')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', self._on_resize)
        self.canvas.bind('<ButtonPress-1>', self._on_press)
        self.canvas.bind('<B1-Motion>', self._on_drag)
        self.canvas.bind('<ButtonRelease-1>', self._on_release)
        self.canvas.bind('<Motion>', self._on_motion)
        self.canvas.bind('<MouseWheel>', self._on_wheel)
        self.canvas.bind('<Button-4>', lambda e: self._on_wheel(e, +1))
        self.canvas.bind('<Button-5>', lambda e: self._on_wheel(e, -1))
        self.canvas.bind('<Double-Button-1>', self._on_double)
        for seq in RIGHT_CLICK:
            self.canvas.bind(seq, self._on_right_click)

        self.status = tk.Label(self, text="", anchor='w', bg=COLORS['bg_medium'],
                               fg=COLORS['text_muted'], font=F_UI, padx=10, pady=3)
        self.status.pack(fill='x', side='bottom')

    # ── modele ───────────────────────────────────────────────────────
    def station(self) -> Optional[Station]:
        sts = self.app.stations
        return sts[self.idx] if 0 <= self.idx < len(sts) else None

    def goto(self, idx: int, keep_heading: bool = True, record: bool = False) -> None:
        if not (0 <= idx < len(self.app.stations)) or idx == self.idx:
            return
        prev = self.station()
        if prev is not None:
            self.came_from = prev.idx
        if record and prev is not None:          # geste de l'utilisateur : annulable
            self.app._journal_push(('nav_b', self.idx, self.view.yaw, self.view.pitch,
                                    self.view.fov))
        if keep_heading and prev is not None and not self.linked.get():
            az = self.app.calib.azimuth(self.view.yaw, prev.north_pct)
            self.view.yaw = self.app.calib.pano_yaw(az, self.app.stations[idx].north_pct)
        self.idx = idx
        if prev is not None and not self.linked.get() and self.app.cfg.get('focus_origin'):
            aim_at(self.view, self.app.calib, self.app.stations[idx], prev,
                   float(self.app.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)),
                   self.app.anchor())
        self.request_render(force=True)
        self._refresh_title()
        self.app._refresh_ctrlbar()
        self.app.store.prefetch([self.app.stations[lk.target].photo
                                 for lk in self.app.links[idx]]
                                if idx < len(self.app.links) else [])

    def go_back(self) -> None:
        """Vue B : revenir à la bulle précédente."""
        if self.came_from is not None and self.came_from != self.idx:
            self.goto(self.came_from, record=True)

    def look_back(self) -> None:
        """Vue B : regarder la bulle d'où l'on vient."""
        if self.came_from is not None and self.came_from != self.idx:
            self.aim_at_station(self.came_from)

    def aim_at_station(self, idx: int) -> None:
        """Tourne B vers une bulle (la vue liée est suspendue pour garder ce regard)."""
        b = self.station()
        if b is None or not (0 <= idx < len(self.app.stations)) or idx == b.idx:
            return
        if self.linked.get():
            self.linked.set(False)
            self._on_linked()
        aim_at(self.view, self.app.calib, b, self.app.stations[idx],
               float(self.app.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)), self.app.anchor())
        self.request_render(force=True)

    def copy_from_a(self) -> None:
        self.goto(self.app.current, record=True)

    def swap(self) -> None:
        """Échange les points de vue des deux fenêtres."""
        a, b = self.app.current, self.idx
        if a == b:
            return
        self.idx = a
        self.app.goto(b, keep_heading=True)
        self.request_render(force=True)
        self._refresh_title()

    def counterpart(self, idx_a: int) -> Optional[int]:
        """Bulle de B correspondant à la bulle A, selon le mode de suivi."""
        mode = self.follow.get()
        sts = self.app.stations
        if mode == self.FOLLOW_MODES[0] or not (0 <= idx_a < len(sts)):
            return None
        a = sts[idx_a]
        if mode == self.FOLLOW_MODES[1]:          # même local, autre plancher
            pa = a.parts()
            memes = [s for s in sts
                     if s.idx != a.idx and s.floor != a.floor
                     and s.parts().local == pa.local and pa.local]
            if not memes:
                return None
            exact = [s for s in memes if s.parts().index == pa.index]
            pool = exact or memes
            # on garde le niveau le plus proche, en privilegiant l'aplomb
            return min(pool, key=lambda s: (round(abs(s.z - a.z), 1),
                                            (s.x - a.x) ** 2 + (s.y - a.y) ** 2)).idx
        best, best_d = None, float('inf')          # bulle la plus proche
        for s in sts:
            if s.idx == a.idx:
                continue
            d = (s.x - a.x) ** 2 + (s.y - a.y) ** 2 + (s.z - a.z) ** 2
            if d < best_d:
                best, best_d = s.idx, d
        return best

    def follow_a(self, idx_a: int) -> None:
        cible = self.counterpart(idx_a)
        if cible is not None:
            self.goto(cible)

    # ── synchronisation avec la vue principale ───────────────────────
    def sync_signature(self) -> tuple:
        a = self.app.station()
        return (self.idx, bool(self.linked.get()), self.app.current,
                round(self.app.view.yaw, 2), round(self.app.view.pitch, 2),
                round(self.app.view.fov, 2), round(a.north_pct, 4) if a else 0.0,
                self.app.calib.mode, self.app.calib.sense, round(self.app.calib.offset, 3))

    def sync_from_a(self) -> None:
        """Aligne B sur la direction terrain de A (mode « vue liée »)."""
        if not self.linked.get():
            return
        a, b = self.app.station(), self.station()
        if a is None or b is None:
            return
        az = self.app.calib.azimuth(self.app.view.yaw, a.north_pct)
        yaw = self.app.calib.pano_yaw(az, b.north_pct)
        if (abs(wrap180(yaw - self.view.yaw)) < 1e-6
                and abs(self.view.pitch - self.app.view.pitch) < 1e-6
                and abs(self.view.fov - self.app.view.fov) < 1e-6):
            return
        self.view.yaw = yaw
        self.view.pitch = self.app.view.pitch
        self.view.fov = self.app.view.fov
        self.request_render()

    def _on_linked(self) -> None:
        if self.linked.get():
            self.sync_from_a()
        self._refresh_title()

    # ── rendu ────────────────────────────────────────────────────────
    def _on_resize(self, event) -> None:
        self.view.width = max(64, int(event.width))
        self.view.height = max(64, int(event.height))
        self.request_render(force=True)

    def request_render(self, force: bool = False, interactive: bool = False) -> None:
        st = self.station()
        if st is None or self._closed:
            return
        scale = DRAG_SCALE if interactive else 1.0
        w = max(64, int(self.view.width * scale))
        h = max(64, int(self.view.height * scale))
        rv = View(wrap180(self.view.yaw - st.yaw_fix), self.view.pitch, self.view.fov, w, h)
        dv = View(self.view.yaw, self.view.pitch, self.view.fov,
                  self.view.width, self.view.height)
        self.app.submit_render('B', self.idx, rv, dv, scale, self)
        if interactive:
            if self._idle_job:
                self.after_cancel(self._idle_job)
            self._idle_job = self.after(IDLE_FULL_MS,
                                        lambda: self.request_render(force=True))

    def publish(self, img, seq: int, rv: View, dv: View, idx: int, scale: float) -> None:
        if self._closed or seq <= self._shown_seq or idx != self.idx:
            return
        try:
            from PIL import Image, ImageTk
            if scale != 1.0 and (rv.width != self.view.width or rv.height != self.view.height):
                img = img.resize((max(1, self.view.width), max(1, self.view.height)),
                                 Image.BILINEAR)
            self._shown_seq = seq
            self._frame_view = View(dv.yaw, dv.pitch, dv.fov,
                                    self.view.width, self.view.height)
            self._tk_img = ImageTk.PhotoImage(img)
            self.canvas.delete('frame')
            self.canvas.create_image(0, 0, anchor='nw', image=self._tk_img, tags='frame')
            self.canvas.tag_lower('frame')
            self._draw_overlay()
        except Exception as exc:
            self.status.config(text=f"Affichage impossible : {exc}", fg=COLORS['error'])

    def publish_missing(self, seq: int, idx: int) -> None:
        if self._closed or seq <= self._shown_seq or idx != self.idx:
            return
        self._shown_seq = seq
        self._frame_view = View(self.view.yaw, self.view.pitch, self.view.fov,
                                self.view.width, self.view.height)
        self._tk_img = None
        self.canvas.delete('frame')
        self.canvas.create_rectangle(0, 0, self.view.width, self.view.height,
                                     fill='#181818', outline='', tags='frame')
        st = self.station()
        self.canvas.create_text(self.view.width // 2, self.view.height // 2,
                                text=f"Image introuvable\n{st.photo if st else ''}",
                                fill=COLORS['warning'], font=('Segoe UI', 13), tags='frame')
        self.canvas.tag_lower('frame')
        self._draw_overlay()

    # ── pastilles ────────────────────────────────────────────────────
    def _draw_overlay(self) -> None:
        view = self._frame_view
        self.canvas.delete('hs')
        self.canvas.delete('tip')
        if view is None:
            return
        app = self.app
        self.hotspots, self.hidden_count = compute_hotspots(
            app.stations, app.links, self.idx, view, app.calib, app.filters,
            app.store.has, float(app.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)),
            float(app.cfg.get('disc_radius', DISC_RADIUS_M)), *app.disc_bounds(),
            anchor=app.anchor())
        libres = app.declutter(self.hotspots)
        mires = set(app.mire_targets(self.idx, app.current, self.hotspots, self._hover))
        for i, hs in enumerate(self.hotspots):
            tgt = app.stations[hs.link.target]
            color = app.hotspot_color(hs.link, tgt)
            hovered = i == self._hover
            app.draw_hotspot(self.canvas, hs, color, hovered, mast=tgt.idx not in mires)
            tag = ("A" if tgt.idx == app.current else
                   "↩ origine" if tgt.idx == self.came_from else '')
            app.draw_marks(self.canvas, hs, tgt, color, hovered,
                           missing=not app.store.has(tgt.photo), tag=tag,
                           labels=i in libres, mire=tgt.idx in mires)
        me = self.station()
        if me is not None:
            app.ghost_of_active(self.canvas, view, me)
            par_bulle = {h.link.target: (k, h) for k, h in enumerate(self.hotspots)}
            for idx in app.mire_targets(self.idx, app.current, self.hotspots, self._hover):
                k, h = par_bulle.get(idx, (None, None))
                app.draw_mire(self.canvas, view, me, app.stations[idx],
                              COLORS['hot'] if idx == app.current else 'white',
                              hs=h, hovered=k is not None and k == self._hover)
        if self._hover is not None and self._hover_xy:
            self._draw_tooltip(self._hover_xy[0], self._hover_xy[1], self._hover)
        st = self.station()
        if st is not None:
            titre = f"B · {st.locator}   ({st.floor})"
            self.canvas.create_text(15, 13, text=titre, anchor='nw', fill='#000000',
                                    font=('Segoe UI', 12, 'bold'), tags='hs')
            self.canvas.create_text(14, 12, text=titre, anchor='nw', fill=COLORS['sel'],
                                    font=('Segoe UI', 12, 'bold'), tags='hs')
        self._refresh_title()

    def _refresh_title(self) -> None:
        st, a = self.station(), self.app.station()
        if st is None:
            return
        p = st.parts()
        detail = f"{st.locator}  ·  {st.floor}"
        if p.local:
            detail += f"  ·  local {p.local}"
        if p.date_lisible():
            detail += f"  ·  {p.date_lisible()}"
        self.title_lbl.config(text=detail)
        if a is not None and a.idx != st.idx:
            _, _, d3 = azimuth_elev(st.x - a.x, st.y - a.y, st.z - a.z)
            cap = self.app.calib.azimuth(self.view.yaw, st.north_pct)
            self.status.config(
                text=f"{'vue liée à A' if self.linked.get() else 'vue libre'} · "
                     f"cap {cap:+.1f}° · {len(self.hotspots)} pastille(s) · "
                     f"{d3:.2f} m de A ({a.locator}) · Δz {st.z - a.z:+.2f} m")
        else:
            self.status.config(text="même bulle que la vue principale")

    def _hotspot_at(self, x: float, y: float) -> Optional[int]:
        return hotspot_hit(self.hotspots, x, y, self.app.relief())

    def _on_right_click(self, event) -> None:
        """Clic droit sur une pastille de B : la sonder depuis B (comme Ctrl+clic)."""
        hit = self._hotspot_at(event.x, event.y)
        if hit is not None:
            self.app.sonde(self.hotspots[hit].link.target, src='B')

    # ── interactions ─────────────────────────────────────────────────
    def _on_press(self, event) -> None:
        self._press_xy = (event.x, event.y)
        app = self.app
        if getattr(app, '_space_down', False) and app.current >= 0 and self.idx != app.current:
            # Espace + glisser dans B : la pastille de la station active suit le curseur
            app._space_used = True
            if app.start_plan_drag(event, self._frame_view or self.view, self.idx,
                                   'pastille'):
                self._drag = None
                return
        self._drag = (event.x, event.y, self.view.yaw, self.view.pitch,
                      self.app.view.yaw, self.app.view.pitch)

    def _on_drag(self, event) -> None:
        hd = self.app._hs_drag
        if hd is not None and hd[0] == 'plan' and hd[1]['mode'] == 'pastille':
            self.app._drag_plan(event)
            return
        if self._drag is None:
            return
        x0, y0, yaw0, pitch0, ayaw0, apitch0 = self._drag
        deg = self.view.fov / max(1, self.view.width)
        dx, dy = (event.x - x0) * deg, (event.y - y0) * deg
        if self.linked.get():
            # en vue liée, tourner ici tourne les deux vues : on pilote A,
            # la synchronisation ramène B dans la foulée
            self.app.view.yaw = wrap180(ayaw0 - dx)
            self.app.view.pitch = clamp(apitch0 + dy, PITCH_MIN, PITCH_MAX)
            self.app._request_render(interactive=True)
        else:
            self.view.yaw = wrap180(yaw0 - dx)
            self.view.pitch = clamp(pitch0 + dy, PITCH_MIN, PITCH_MAX)
            self.request_render(interactive=True)

    def _on_release(self, event) -> None:
        hd = self.app._hs_drag
        if hd is not None and hd[0] == 'plan' and hd[1]['mode'] == 'pastille':
            self.app._end_plan_drag()
            return
        moved = 0
        if getattr(self, '_press_xy', None):
            moved = abs(event.x - self._press_xy[0]) + abs(event.y - self._press_xy[1])
        self._drag = None
        if moved <= 4:
            hit = self._hotspot_at(event.x, event.y)
            if hit is not None:
                if event.state & 0x0004:            # Ctrl+clic : sonder depuis B
                    self.app.sonde(self.hotspots[hit].link.target, src='B')
                else:
                    self.goto(self.hotspots[hit].link.target, record=True)
                return
        self.request_render(force=True)

    def _on_motion(self, event) -> None:
        self._hover_xy = (event.x, event.y)
        hit = self._hotspot_at(event.x, event.y)
        if hit != self._hover:
            self._hover = hit
            self.canvas.config(cursor='hand2' if hit is not None else 'fleur')
            self._draw_overlay()
        else:
            self._draw_tooltip(event.x, event.y, hit)

    def _draw_tooltip(self, x: int, y: int, hit: Optional[int]) -> None:
        self.canvas.delete('tip')
        if hit is None or hit >= len(self.hotspots):
            return
        if not self.app.tooltip_allowed():
            return
        lines, modified = self.app.tooltip_lines(self.hotspots[hit], self.station())
        self.app.draw_tooltip(self.canvas, x, y, lines, self.view.width, self.view.height,
                              modified)

    def _on_wheel(self, event, direction: int = 0) -> None:
        step = direction if direction else (1 if getattr(event, 'delta', 0) > 0 else -1)
        if self.app.wheel_alt(event, self.hotspots, self.idx, step):
            return
        if self.linked.get():
            self.app._zoom(-6 * step)
        else:
            self.view.fov = clamp(self.view.fov - 6 * step, FOV_MIN, FOV_MAX)
            self.request_render(interactive=True)

    def _on_double(self, event) -> None:
        if self._hotspot_at(event.x, event.y) is not None:
            return
        view = self._frame_view or self.view
        wx, wy, wz = _pano_ray(view, event.x, event.y)     # exact, grand angle compris
        dyaw = wrap180(math.degrees(math.atan2(wy, wx)) - view.yaw)
        dpitch = view.pitch - math.degrees(math.asin(clamp(wz, -1.0, 1.0)))
        if self.linked.get():
            self.app.view.yaw = wrap180(self.app.view.yaw + dyaw)
            self.app.view.pitch = clamp(self.app.view.pitch - dpitch, PITCH_MIN, PITCH_MAX)
            self.app._request_render(force=True)
        else:
            self.view.yaw = wrap180(self.view.yaw + dyaw)
            self.view.pitch = clamp(self.view.pitch - dpitch, PITCH_MIN, PITCH_MAX)
            self.request_render(force=True)

    # ── fermeture ────────────────────────────────────────────────────
    def close(self) -> None:
        self._closed = True
        if self._idle_job:
            try:
                self.after_cancel(self._idle_job)
            except Exception:
                pass
        with self.app._cv:
            self.app._reqs.pop('B', None)
        if self.app.compare is self:
            self.app.compare = None
            try:
                self.app.cmp_btn.config(bg=COLORS['bg_light'], fg=COLORS['text'])
                self.app._refresh_ctrlbar()
            except Exception:
                pass
        try:
            self.app.views.forget(self)
        except Exception:
            pass
        try:
            self.app._draw_plan()
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
            (75, 0, 60, 60.0, 0.0),
            # grand angle (projection stéréographique progressive)
            (0, -10, 150, 60.0, -20.0), (40, -30, 190, 125.0, -40.0),
            (-60, 10, 200, -150.0, 5.0)):
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
    err_l = max(abs(lens_inv(lens_g(math.radians(t), d), d) - math.radians(t))
                for d in (0.0, 0.3, 0.7, 1.0) for t in range(0, 85 if d == 0 else 100, 5))
    check("grand angle : projection inverse exacte", err_l < 1e-9, f"{err_l:.1e} rad")
    check("grand angle : transition continue à 110°",
          abs(View(0, 0, 110.0, 640, 400).focal() - View(0, 0, 110.001, 640, 400).focal()) < 0.05)

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

    # 6. Deplacement d'une pastille : ecran -> sol -> ecran
    print("\n6) Déplacement d'une pastille (écran ↔ sol)")
    cal = Calib('colonne', 1, 0.0)
    worst_az = worst_d = 0.0
    for view in (View(35.0, -22.0, 100.0, 1280, 720), View(-140.0, -35.0, 70.0, 900, 900),
                 View(35.0, -22.0, 160.0, 1280, 720), View(-140.0, -35.0, 195.0, 900, 900)):
        for az, dh, dz in ((10.0, 3.0, -1.65), (-40.0, 8.0, -1.65),
                           (95.0, 5.0, -0.50), (150.0, 12.0, -1.65)):
            elev = math.degrees(math.atan2(dz, dh))
            pr = project(view, cal.pano_yaw(az, 50.0), elev)
            if pr is None:
                continue
            back = ground_from_screen(view, pr[0], pr[1], cal, 50.0, dz)
            if back is None:
                check(f"sol visé (az={az}, d={dh})", False, "aucune intersection")
                continue
            worst_az = max(worst_az, abs(wrap180(back[0] - az)))
            worst_d = max(worst_d, abs(back[1] - dh))
    check("azimut retrouvé au pixel près", worst_az < 1e-6, f"écart max {worst_az:.2e}°")
    check("distance retrouvée au pixel près", worst_d < 1e-6, f"écart max {worst_d:.2e} m")
    flat = ground_from_screen(View(0, 0, 90, 640, 400), 320, 200, cal, 50.0, -1.65)
    check("regard horizontal : pas de point au sol", flat is None)

    # 7. Corrections : application, annulation, CSV corrige
    print("\n7) Fichier de corrections et relevé complet")
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix='bubblenav_')
    try:
        if csv_path and os.path.isfile(csv_path):
            work_csv = os.path.join(tmp, os.path.basename(csv_path))
            shutil.copy2(csv_path, work_csv)
            sts, _ = read_survey_csv(work_csv)
            by_photo = {s.photo: s for s in sts}
            corr = Corrections(work_csv)

            corr.apply(sts[0], x=sts[0].x + 0.123, dh=-0.05)
            corr.apply(sts[5], y=sts[5].y - 1.5)
            corr.apply(sts[9], yaw_fix=1.25)
            check("état modifié détecté", sts[0].moved() and sts[0].raised()
                  and sts[9].turned() and not sts[9].moved())
            check("comptage des corrections",
                  Corrections.counts(sts) == Bilan(xy=2, h=1, delta=0, nord=1),
                  Corrections.counts(sts).texte())

            # Les deux composantes en Z : hauteur station vs delta plancher
            s0 = sts[0]
            sol_avant = s0.ground(1.65)
            corr.apply(s0, dh=-0.05, record=False)
            check("hauteur station : la caméra bouge, le sol reste",
                  abs(s0.z - (s0.oz - 0.05)) < 1e-9 and abs(s0.ground(1.65) - sol_avant) < 1e-9,
                  f"z {s0.oz:.3f} -> {s0.z:.3f}, sol {s0.ground(1.65):.3f}")
            corr.apply(s0, dh=0.0, ddelta=0.12, record=False)
            check("delta plancher : caméra ET sol bougent",
                  abs(s0.z - (s0.oz + 0.12)) < 1e-9
                  and abs(s0.ground(1.65) - (sol_avant + 0.12)) < 1e-9,
                  f"z {s0.z:.3f}, sol {s0.ground(1.65):.3f}")
            check("Z = altitude d'origine + dH + dDelta", abs(s0.z - (s0.oz + s0.dh + s0.ddelta)) < 1e-12)
            corr.apply(s0, dh=-0.05, ddelta=0.0, record=False)
            check("images en attente de rotation",
                  [s.idx for s in Corrections.pending_images(sts)] == [9])

            corr.undo(by_photo)
            check("annulation (Ctrl+Z)", not sts[9].turned())
            corr.apply(sts[9], yaw_fix=1.25)

            before = open(work_csv, 'rb').read()
            side = corr.save(sts)
            check("fichier de corrections écrit",
                  bool(side) and side.endswith(Corrections.SUFFIX)
                  and os.path.isfile(side), os.path.basename(side or ''))
            corr_lines = _read_text(side).splitlines()
            check("une ligne par bulle corrigée seulement", len(corr_lines) == 4,
                  f"{len(corr_lines) - 1} ligne(s)")
            check("en-tête du fichier de corrections (patch QGIS)",
                  corr_lines[0].split(';') == list(Corrections.HEADER),
                  corr_lines[0][:80])
            champs = dict(zip(corr_lines[0].split(';'), corr_lines[1].split(';')))
            check("deltas séparés par nature dans le fichier",
                  abs(float(champs['dH station']) + 0.05) < 1e-6
                  and abs(float(champs['dDelta plancher'])) < 1e-6
                  and abs(float(champs['dZ']) + 0.05) < 1e-6
                  and abs(float(champs['dX']) - 0.123) < 1e-6,
                  f"dH {champs['dH station']} dDelta {champs['dDelta plancher']} dZ {champs['dZ']}")
            check("le relevé chargé n'est pas touché",
                  open(work_csv, 'rb').read() == before)

            sts2, _ = read_survey_csv(work_csv)
            by2 = {s.photo: s for s in sts2}
            n_ok, n_miss = Corrections(work_csv).load(by2)
            check("corrections relues et appliquées",
                  n_ok == 3 and n_miss == 0
                  and abs(by2[sts[0].photo].x - sts[0].x) < 5e-4
                  and abs(by2[sts[0].photo].dh + 0.05) < 5e-4
                  and abs(by2[sts[9].photo].yaw_fix - 1.25) < 5e-5,
                  f"{n_ok} appliquées, {n_miss} sans correspondance")
            check("corrections relues = état modifié",
                  by2[sts[0].photo].moved() and by2[sts[9].photo].turned()
                  and Corrections.counts(sts2) == Bilan(xy=2, h=1, delta=0, nord=1))

            # ancien format (Z absolu seul) : range en hauteur de station
            legacy = os.path.join(tmp, 'ancien_corrections.csv')
            with open(legacy, 'w', encoding='utf-8-sig', newline='') as fh:
                fh.write("Fichier photo;X;Y;Z;Delta Nord (deg)\r\n"
                         f"{sts[3].photo};{sts[3].ox:.3f};{sts[3].oy:.3f};{sts[3].oz + 0.2:.3f};0\r\n")
            sts_l, _ = read_survey_csv(work_csv)
            Corrections(work_csv, path=legacy).load({s.photo: s for s in sts_l})
            check("ancien fichier (Z seul) relu comme hauteur de station",
                  abs(sts_l[3].dh - 0.2) < 1e-6 and sts_l[3].raised() and not sts_l[3].shifted())

            with open(side, 'a', encoding='utf-8') as fh:
                fh.write("PHOTO_INCONNUE;X;1.0;2.0;3.0;0.0;;;;;\r\n")
            n_ok2, n_miss2 = Corrections(work_csv).load({s.photo: s for s in
                                                         read_survey_csv(work_csv)[0]})
            check("ligne sans correspondance signalée, pas fatale",
                  n_ok2 == 3 and n_miss2 == 1, f"{n_ok2}/{n_miss2}")

            corr.mark_applied([sts[9].photo])
            corr.apply(sts[9], yaw_fix=0.0, record=False)
            corr.save(sts)
            reread = Corrections(work_csv)
            reread.load({s.photo: s for s in read_survey_csv(work_csv)[0]})
            check("date d'application conservée",
                  sts[9].photo in reread.applied, str(reread.applied)[:60])
            corr.apply(sts[9], yaw_fix=1.25, record=False)

            out_csv = os.path.join(tmp, 'corrige.csv')
            n_mod, n_keep, added = write_corrected_csv(work_csv, out_csv, sts)
            check("lignes réécrites", n_mod == 3 and n_keep == len(sts) - 3,
                  f"{n_mod} modifiées, {n_keep} recopiées")
            check("colonne Δ nord ajoutée", added)

            src_lines = _read_text(work_csv).splitlines()
            dst_lines = _read_text(out_csv).splitlines()
            a_h = 'hcam' in auto_columns([norm_key(c) for c in src_lines[0].split(';')])
            check("colonnes ajoutées en fin d'en-tête (Δ nord, hauteur corrigée)",
                  dst_lines[0] == src_lines[0] + ';' + YAW_COLUMN
                  + ('' if a_h else ';Hauteur instrument'), dst_lines[0][-50:])
            n_src = len(src_lines[0].split(';'))
            check("les autres colonnes ne bougent pas",
                  all(';'.join(b.split(';')[:n_src]) == a for a, b in
                      zip(src_lines[1:], dst_lines[1:])
                      if not b.startswith(tuple(x.photo for x in sts if x.modified()))))
            if not a_h:
                h_col = [ln.split(';')[-1] for ln in dst_lines[1:4]]
                check("hauteur instrument écrite pour chaque bulle",
                      all(parse_float(v) is not None for v in h_col), str(h_col))

            sts3, _ = read_survey_csv(out_csv)
            check("valeurs X/Y/Z relues",
                  abs(sts3[0].x - sts[0].x) < 5e-4 and abs(sts3[0].z - sts[0].z) < 5e-4
                  and abs(sts3[5].y - sts[5].y) < 5e-4)
            check("orientation relue depuis le CSV",
                  abs(sts3[9].yaw_fix - 1.25) < 1e-6 and not sts3[9].turned()
                  and sts3[9].has_yaw(), f"{sts3[9].yaw_fix:+.4f}°")
            check("colonne % NORD intacte",
                  all(abs(a.north_pct - b.north_pct) < 1e-9 for a, b in zip(sts, sts3)))
            check("décimales d'origine conservées",
                  dst_lines[1].split(';')[2].count('.') == 1
                  and len(dst_lines[1].split(';')[2].split('.')[1]) ==
                  len(src_lines[1].split(';')[2].split('.')[1]),
                  dst_lines[1].split(';')[2])

            # deuxième passe : la colonne existe, seules les lignes changées bougent
            for st in sts3:
                st.ox, st.oy, st.oz, st.oyaw = st.x, st.y, st.z, st.yaw_fix
            Corrections(out_csv).apply(sts3[9], yaw_fix=2.5)
            out2 = os.path.join(tmp, 'corrige2.csv')
            n2, k2, added2 = write_corrected_csv(out_csv, out2, sts3)
            l2 = _read_text(out2).splitlines()
            diff2 = [i for i, (a, b) in enumerate(zip(dst_lines, l2)) if a != b]
            check("colonne existante réutilisée", not added2 and n2 == 1
                  and diff2 == [10], f"{n2} ligne(s), différences {diff2}")
            check("Δ nord mis à jour en place",
                  abs(read_survey_csv(out2)[0][9].yaw_fix - 2.5) < 1e-6)

            corr.revert_all(sts)
            check("réinitialisation complète", not Corrections.counts(sts).any())
            check("retour aux valeurs du fichier",
                  all(not s.modified() and abs(s.z - s.oz) < 1e-12 for s in sts))

        # 8. Rotation d'image : semantique et coherence avec le rendu
        print("\n8) Rotation d'image (correction d'orientation)")
        from PIL import Image
        w, h = 1024, 512
        rng = np.random.default_rng(3)
        base = (rng.random((h, w, 3)) * 60).astype(np.uint8)
        base[:, 300:316] = 250                      # bande repère
        src_img = os.path.join(tmp, 'pano.jpg')
        Image.fromarray(base).save(src_img, quality=95)

        delta = 360.0 * 40 / w                      # 40 px pile
        dst_img = os.path.join(tmp, 'pano_tourne.jpg')
        wid, shift = rotate_pano_file(src_img, dst_img, delta)
        check("décalage arrondi au pixel", (wid, shift) == (w, 40), f"{shift} px")

        rot = np.asarray(Image.open(dst_img).convert('RGB'))
        band = rot[:, :, 0].mean(axis=0)
        peak = int(np.argmax(np.convolve(band, np.ones(16) / 16, mode='same')))
        check("la bande repère se décale vers la droite", abs(peak - (308 + 40)) <= 2,
              f"colonne {peak} au lieu de {308 + 40}")

        renderer2 = PanoRenderer()
        v1 = View(25.0, -10.0, 90.0, 480, 320)
        a = renderer2.render(rot, v1).astype(float)
        b = renderer2.render(np.asarray(Image.open(src_img).convert('RGB')),
                             View(v1.yaw - delta, v1.pitch, v1.fov, v1.width, v1.height)
                             ).astype(float)
        ecart = float(np.abs(a - b).mean())
        check("image tournée ≡ vue décalée du même angle", ecart < 6.0,
              f"écart moyen {ecart:.2f}/255")

        # export selectif
        st_a = Station(0, 'pano', 'A', 0, 0, 0, 50, 'P0', yaw_fix=delta)  # Δ du CSV
        st_b = Station(1, 'autre', 'B', 1, 1, 0, 50, 'P0')
        out_dir = os.path.join(tmp, 'sortie')
        ok, skipped, errors = export_rotated_images(
            [st_a, st_b], {'pano': src_img, 'autre': src_img}, out_dir, workers=1)
        check("export limité aux images réorientées",
              ok == 1 and not errors and os.listdir(out_dir) == ['pano.jpg'],
              f"{ok} exportée(s), erreurs : {errors}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 9. Analyse du nom de fichier et filtres de pastilles
    print("\n9) Analyse du nom et filtres")
    p = parse_photo_name('CP1_GRA_TR6_BK_02_K256_20260416_01')
    check("découpage du nom",
          (p.campagne, p.site, p.tranche, p.ouvrage, p.etage, p.local, p.index)
          == ('CP1', 'GRA', 'TR6', 'BK', '02', 'K256', '01') and p.reconnu,
          f"{p.local} étage {p.etage} index {p.index}")
    check("date lisible", p.date_lisible() == '2026-04-16', p.date_lisible())
    check("locator reconstruit", p.locator() == 'K256_01', p.locator())
    p2 = parse_photo_name('SITE_03_L12_20250101_7_bis')
    check("segments de tête variables", (p2.etage, p2.local, p2.index, p2.reste)
          == ('03', 'L12', '7', ('bis',)), str(p2))
    p3 = parse_photo_name('photo_sans_convention')
    check("nom hors convention toléré", not p3.reconnu and p3.local == 'convention')
    check("nom vide toléré", parse_photo_name('') == NameParts())

    if csv_path and os.path.isfile(csv_path):
        sts, _ = read_survey_csv(csv_path)
        ok_names = sum(1 for st in sts if st.parts().reconnu)
        coherent = sum(1 for st in sts
                       if st.parts().locator() == st.locator
                       and (st.parts().etage_deduit
                            or st.parts().etage in st.floor.replace('PLANCHER ', '')[:2]))
        check("noms reconnus sur le relevé", ok_names == len(sts),
              f"{ok_names}/{len(sts)}")
        check("locator et étage cohérents avec le nom", coherent == len(sts),
              f"{coherent}/{len(sts)}")

        links = build_graph(sts, GraphParams())
        cur = sts[0]
        mine = links[cur.idx]
        flt = HotspotFilter(active=False, floor_mode='courant', max_dist=2.0)
        check("filtre inactif = tout passe",
              all(flt.accepts(cur, sts[lk.target], lk) for lk in mine))
        flt.active = True
        kept = [lk for lk in mine if flt.accepts(cur, sts[lk.target], lk)]
        check("filtre distance", all(lk.dist <= 2.0 for lk in kept)
              and len(kept) < len(mine), f"{len(kept)}/{len(mine)}")
        flt2 = HotspotFilter(active=True, inter_floor=False)
        check("filtre liens inter-planchers",
              all(lk.kind == 'same' for lk in mine
                  if flt2.accepts(cur, sts[lk.target], lk)))
        flt3 = HotspotFilter(active=True, floor_mode='courant')
        check("filtre plancher courant",
              all(sts[lk.target].floor == cur.floor for lk in mine
                  if flt3.accepts(cur, sts[lk.target], lk)))
        motif = cur.parts().local
        flt4 = HotspotFilter(active=True, local=motif[:3])
        gardes = [lk for lk in mine if flt4.accepts(cur, sts[lk.target], lk)]
        check("filtre local par préfixe",
              gardes and all(sts[lk.target].parts().local.startswith(motif[:3])
                             for lk in gardes), f"{motif[:3]} → {len(gardes)} pastille(s)")
        flt5 = HotspotFilter(active=True, local='ZZZ*')
        check("motif sans correspondance = aucune pastille",
              not [lk for lk in mine if flt5.accepts(cur, sts[lk.target], lk)])
        flt6 = HotspotFilter(active=True, hide_missing=True)
        check("filtre images absentes",
              not [lk for lk in mine if flt6.accepts(cur, sts[lk.target], lk, False)])

    # taille de pastille : décroissance en 1/distance
    view = View(0, -20, 105, 1600, 900)
    f = view.focal()
    r1 = clamp(f * DISC_RADIUS_M / 6.0, DISC_PX_MIN, DISC_PX_MAX)
    r2 = clamp(f * DISC_RADIUS_M / 18.0, DISC_PX_MIN, DISC_PX_MAX)
    check("pastille 3x plus loin = 3x plus petite (hors bornes)",
          abs(r1 / r2 - 3.0) < 1e-6, f"{r1:.1f} px à 6 m, {r2:.1f} px à 18 m")
    tailles = [clamp(f * DISC_RADIUS_M / d, DISC_PX_MIN, DISC_PX_MAX)
               for d in (0.4, 1.0, 2.0, 5.0, 10.0, 30.0, 200.0)]
    check("taille bornée à toute distance",
          all(DISC_PX_MIN <= t <= DISC_PX_MAX for t in tailles),
          f"{min(tailles):.0f} à {max(tailles):.0f} px de 0,4 m à 200 m")
    check("bornes utiles : cliquable et non envahissante",
          DISC_PX_MIN >= 9.0 and DISC_PX_MAX <= 40.0
          and DISC_PX_MAX >= 3 * DISC_PX_MIN,
          f"{DISC_PX_MIN:.0f} → {DISC_PX_MAX:.0f} px")
    check("pastille jamais plus large que 5 % de la vue",
          DISC_PX_MAX * 2 <= 0.05 * 1600 + 1e-9,
          f"{DISC_PX_MAX * 2:.0f} px de diamètre sur 1600 px")
    seuil = f * DISC_RADIUS_M / DISC_PX_MAX
    check("proportionnalité conservée au-delà de la portée utile", seuil < 6.0,
          f"plafonnée en deçà de {seuil:.1f} m seulement")
    ratio = View(0, 0, 50, 1600, 900).focal() / View(0, 0, 100, 1600, 900).focal()
    attendu = math.tan(math.radians(50)) / math.tan(math.radians(25))
    check("zoom : la pastille grossit du bon facteur", abs(ratio - attendu) < 1e-9,
          f"champ 100° → 50° : ×{ratio:.2f}")

    # 10. Mode « num scan » : cle immuable, nom projete, attributs explicites
    print("\n10) Mode num scan (clé immuable, nom projeté)")
    import shutil as _sh
    import tempfile as _tf
    tmp2 = _tf.mkdtemp(prefix='bubblenav_scan_')
    try:
        csv_scan = os.path.join(tmp2, 'scan.csv')
        with open(csv_scan, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write("Num scan;Fichier photo;X;Y;Z;% NORD;Plancher;Nom projeté;Local;Étage\r\n"
                     "0347;0347;10.000;20.000;1.650;50;PLANCHER 02;"
                     "CP1_GRA_TR6_BK_02_K256_20260416_01;K256;02\r\n"
                     "0348;0348;12.000;20.000;1.650;50;PLANCHER 02;"
                     "CP1_GRA_TR6_BK_02_K256_20260416_02;;\r\n"
                     "0349;0349;14.000;20.000;1.650;50;PLANCHER 02;;;\r\n")
        sts_scan, w_scan = read_survey_csv(csv_scan)
        check("CSV num scan lu", len(sts_scan) == 3 and not w_scan, f"{len(w_scan)} alerte(s)")
        a, b, c = sts_scan
        check("clé immuable = numéro de scan", a.key == '0347' and a.photo == '0347')
        check("attributs depuis le nom projeté",
              b.parts().local == 'K256' and b.parts().etage == '02'
              and b.parts().index == '02' and b.parts().date == '20260416',
              str(b.parts())[:70])
        check("colonnes explicites prioritaires", a.parts().local == 'K256'
              and a.parts().etage == '02' and a.locator == 'K256_01')
        check("sans projection : attributs vides, jamais d'erreur",
              c.parts().local == '' and c.locator == '0349' and not c.parts().reconnu)
        check("filtre local exploitable en mode num scan",
              HotspotFilter(active=True, local='K25').match_local(a)
              and not HotspotFilter(active=True, local='K25').match_local(c))

        # rattachement des images par numero de scan et nom projete
        for nom in ('0347.jpg', 'CP1_GRA_TR6_BK_02_K256_20260416_02.jpg', '349.jpg'):
            open(os.path.join(tmp2, nom), 'wb').write(b'\xff\xd8\xff\xd9')
        store2 = ImageStore()
        store2.set_paths(index_images(tmp2))
        alias = store2.bind_stations(sts_scan)
        check("photos rattachées par numéro, nom projeté et numéro sans zéros",
              all(store2.has(s.photo) for s in sts_scan) and alias == 2,
              f"{alias} alias")

        # corrections ecrites par cle, relues apres RENOMMAGE des photos
        corr2 = Corrections(csv_scan)
        corr2.apply(a, x=a.x + 0.25, yaw_fix=0.75)
        corr2.mark_applied([b.key])
        corr2.save(sts_scan)
        l = _read_text(corr2.path).splitlines()
        check("clé en tête du fichier de corrections",
              l[0].startswith('Cle;') and l[1].startswith('0347;0347;'), l[1][:30])

        csv_ren = os.path.join(tmp2, 'renomme.csv')
        with open(csv_ren, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write("Num scan;Fichier photo;X;Y;Z;% NORD;Plancher\r\n"
                     "0347;CP1_GRA_TR6_BK_02_K256_20260416_01;10.000;20.000;1.650;50;PLANCHER 02\r\n"
                     "0348;CP1_GRA_TR6_BK_02_K256_20260416_02;12.000;20.000;1.650;50;PLANCHER 02\r\n")
        sts_ren, _ = read_survey_csv(csv_ren)
        corr3 = Corrections(csv_ren, path=corr2.path)
        n_ok, n_miss = corr3.load({s.photo: s for s in sts_ren}, by_key={s.key.lower(): s for s in sts_ren})
        check("corrections retrouvées après renommage des photos (par clé)",
              n_ok == 2 and abs(sts_ren[0].x - 10.25) < 1e-9
              and abs(sts_ren[0].yaw_fix - 0.75) < 1e-9 and '0348' in corr3.applied,
              f"{n_ok} reprise(s), {n_miss} orpheline(s)")
        check("le nom projeté est relu comme nom de photo après renommage",
              sts_ren[0].parts().local == 'K256' and sts_ren[0].locator == 'K256_01')

        # releve complet corrige : correspondance par cle
        out3 = os.path.join(tmp2, 'complet.csv')
        n_mod, _, _ = write_corrected_csv(csv_ren, out3, sts_ren)
        rel3, _ = read_survey_csv(out3)
        check("relevé complet corrigé par clé", n_mod == 1 and abs(rel3[0].x - 10.25) < 5e-4)

        # colonnes Hauteur appareil / Delta du releve mises a jour par composante
        csv_hd = os.path.join(tmp2, 'hd.csv')
        with open(csv_hd, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write("Num scan;Fichier photo;X;Y;Z;Hauteur appareil;Delta plancher;% NORD;Plancher\r\n"
                     "0001;0001;1.000;2.000;3.250;1.600;0.000;50;P0\r\n"
                     "0002;0002;4.000;2.000;3.250;1.600;0.000;50;P0\r\n")
        sts_hd, _ = read_survey_csv(csv_hd)
        check("hauteur appareil et delta lus dans le relevé",
              sts_hd[0].h0 == 1.6 and sts_hd[0].delta0 == 0.0
              and abs(sts_hd[0].ground() - 1.65) < 1e-9)
        c_hd = Corrections(csv_hd)
        c_hd.apply(sts_hd[0], dh=0.05)
        c_hd.apply(sts_hd[1], ddelta=-0.15)
        out_hd = os.path.join(tmp2, 'hd_corrige.csv')
        write_corrected_csv(csv_hd, out_hd, sts_hd)
        rel_hd, _ = read_survey_csv(out_hd)
        check("relevé complet : Z, hauteur et delta mis à jour selon la composante",
              abs(rel_hd[0].oz - 3.30) < 5e-4 and abs(rel_hd[0].h0 - 1.65) < 5e-4
              and abs(rel_hd[0].delta0) < 5e-4
              and abs(rel_hd[1].oz - 3.10) < 5e-4 and abs(rel_hd[1].h0 - 1.60) < 5e-4
              and abs(rel_hd[1].delta0 + 0.15) < 5e-4,
              f"{rel_hd[0].oz:.3f}/{rel_hd[0].h0:.3f}/{rel_hd[0].delta0:+.3f} · "
              f"{rel_hd[1].oz:.3f}/{rel_hd[1].h0:.3f}/{rel_hd[1].delta0:+.3f}")

        dup = os.path.join(tmp2, 'dup.csv')
        with open(dup, 'w', encoding='utf-8', newline='') as fh:
            fh.write("Num scan;Fichier photo;X;Y\n0001;a;1;1\n0001;b;2;2\n")
        sts_dup, w_dup = read_survey_csv(dup)
        check("clé dupliquée signalée, jamais fatale", len(sts_dup) == 1 and len(w_dup) == 1)
    finally:
        _sh.rmtree(tmp2, ignore_errors=True)

    # 11. Pastilles en relief
    print("\n11) Pastilles en relief")
    t0 = time.perf_counter()
    img, (ax, ay) = sphere_sprite(COLORS['hot'], 24)
    dt = (time.perf_counter() - t0) * 1000
    arr = np.asarray(img)
    check("sprite RGBA généré", img.mode == 'RGBA' and arr.shape[2] == 4, f"{img.size}")
    check("génération rapide", dt < 40.0, f"{dt:.1f} ms")
    check("point du sol à l'intérieur de l'image", 0 < ax < img.width and 0 < ay < img.height)
    top = arr[:int(ay - 0.74 * 24 * 0.86), :, 3]
    check("la sphère est au-dessus du point du sol", top.max() > 200)
    check("ombre présente au sol, transparente", 30 < arr[int(ay) + 2, int(ax), 3] < 200,
          f"alpha {arr[int(ay) + 2, int(ax), 3]}")
    bright = arr[..., :3].max()
    check("reflet spéculaire plus clair que la couleur", bright > 0xD2, f"{bright}")
    img_h, (axh, ayh) = sphere_sprite(COLORS['hot'], 24, hover=True)
    arrh = np.asarray(img_h)
    check("survol : halo lumineux plus étendu que la sphère",
          img_h.width > img.width + 8 and arrh[..., 3][:int(ayh - 24), :].max() > 60,
          f"{img.width} → {img_h.width} px")
    check("survol : sphère plus claire",
          arrh[..., :3].astype(int).sum() / max(1, (arrh[..., 3] > 200).sum())
          > arr[..., :3].astype(int).sum() / max(1, (arr[..., 3] > 200).sum()))
    hs = Hotspot(Link(0, 5.0, 5.0, 0.0, 0.0), 100.0, 100.0, 20.0, 'x')
    check("clic sur la sphère (au-dessus du sol) reconnu",
          hotspot_hit([hs], 100, 70, True) == 0)
    check("clic sur l'ombre reconnu", hotspot_hit([hs], 100, 102, True) == 0)
    check("clic à côté refusé", hotspot_hit([hs], 160, 100, True) is None)
    check("mode plat : la zone haute n'est plus cliquable",
          hotspot_hit([hs], 100, 70, False) is None)

    # 12. CSV num scan peu strict
    print("\n12) CSV num scan peu strict")
    import tempfile as _tf3
    import shutil as _sh3
    tmp3 = _tf3.mkdtemp(prefix='bubblenav_csv_')
    try:
        def ecrit(nom: str, contenu: str) -> str:
            chemin = os.path.join(tmp3, nom)
            with open(chemin, 'w', encoding='utf-8-sig', newline='') as fh:
                fh.write(contenu)
            return chemin

        c1 = ecrit('a.csv', "N° scan;X;Y;Z;Plancher\r\n0347;589.15;73.45;1.65;P02\r\n"
                            "0348;591.00;73.45;1.65;P02\r\n")
        s1, w1 = read_survey_csv(c1)
        check("sans colonne « Fichier photo » : le N° scan sert d'identifiant",
              len(s1) == 2 and s1[0].photo == '0347' and s1[0].key == '0347'
              and s1[0].key_explicit and any('numéro de scan' in w for w in w1),
              f"{len(s1)} bulle(s)")

        c2 = ecrit('b.csv', "Numéro de scan,Est,Nord,Altitude\n12,10.5,20.25,3.1\n13,11,20,3.1\n")
        s2, _ = read_survey_csv(c2)
        check("intitulés variés, séparateur virgule", len(s2) == 2 and s2[1].key == '13'
              and abs(s2[0].x - 10.5) < 1e-9 and abs(s2[0].y - 20.25) < 1e-9 and s2[0].z == 3.1)

        c3 = ecrit('c.csv', "0347;589,15;73,45;1,65\r\n0348;591,00;73,45;1,65\r\n")
        try:
            read_survey_csv(c3)
            check("CSV sans en-tête détecté", False, "aucune demande de correspondance")
        except ColumnMappingNeeded as need:
            check("CSV sans en-tête détecté, correspondance devinée",
                  need.headerless and need.guess == {'key': 0, 'x': 1, 'y': 2, 'z': 3},
                  str(need.guess))
            s3, _ = read_survey_csv(c3, {k: f"#{v}" for k, v in need.guess.items()})
            check("CSV sans en-tête lu avec la correspondance",
                  len(s3) == 2 and s3[0].key == '0347' and abs(s3[0].x - 589.15) < 1e-9)
            out3 = os.path.join(tmp3, 'c_corrige.csv')
            s3[0].x += 0.1
            write_corrected_csv(c3, out3, s3, mapping={k: f"#{v}" for k, v in need.guess.items()})
            l3 = _read_text(out3).splitlines()
            check("relevé corrigé sans en-tête : aucune ligne ajoutée, valeur corrigée",
                  len(l3) == 2 and l3[0].split(';')[1] == '589,25' and l3[1] == "0348;591,00;73,45;1,65",
                  ' | '.join(l3))

        c4 = ecrit('d.csv', "PointID;Coord1;Coord2;Haut\r\nA1;1;2;3\r\nA2;4;5;6\r\n")
        try:
            read_survey_csv(c4)
            check("intitulés inconnus : correspondance demandée", False)
        except ColumnMappingNeeded as need:
            check("intitulés inconnus : correspondance demandée, jamais d'erreur sèche",
                  not need.headerless and need.signature == 'pointid|coord1|coord2|haut')
            s4, _ = read_survey_csv(c4, {'key': 'PointID', 'x': 'Coord1', 'y': 'Coord2',
                                         'z': 'Haut'})
            check("correspondance par intitulé", len(s4) == 2 and s4[1].y == 5.0)

        c6 = ecrit('f.csv', "N° scan;X;Y;Z\r\n1001;15.217126;6.562585;-1.850\r\n"
                            "1002;14.145717;9.039912;-1.850\r\n")
        s6, _ = read_survey_csv(c6)
        k6 = Corrections(c6)
        k6.apply(s6[0], yaw_fix=1.5)
        k6.apply(s6[1], x=s6[1].x + 0.0123)
        k6.save(s6)
        r6, _ = read_survey_csv(c6)
        Corrections(c6).load({s.photo: s for s in r6}, by_key={s.key.lower(): s for s in r6})
        check("coordonnées au micromètre : une bulle réorientée n'est pas « déplacée »",
              not r6[0].moved() and r6[0].x == 15.217126 and r6[0].turned(),
              f"x relu {r6[0].x!r}")
        check("coordonnées au micromètre : un déplacement revient exact",
              abs(r6[1].x - (14.145717 + 0.0123)) < 1e-9 and r6[1].moved(),
              f"{r6[1].x:.6f}")
        ancien = ecrit('f_ancien_corrections.csv',
                       "Cle;Fichier photo;X;Y;Z;Delta Nord (deg)\r\n1001;1001;15.217;6.563;-1.850;1.5\r\n")
        r7, _ = read_survey_csv(c6)
        Corrections(c6, path=ancien).load({s.photo: s for s in r7},
                                          by_key={s.key.lower(): s for s in r7})
        check("ancien fichier arrondi au mm : l'arrondi n'est pas pris pour un déplacement",
              not r7[0].moved() and not r7[0].z_changed() and r7[0].turned())

        c5 = ecrit('e.csv', "N° scan;X;Y;Z\r\n0347;1.000;2.000;3.000\r\n")
        s5, _ = read_survey_csv(c5)
        s5[0].x = 1.5
        out5 = os.path.join(tmp3, 'e_corrige.csv')
        n5, _, _ = write_corrected_csv(c5, out5, s5)
        check("relevé corrigé d'un CSV num scan sans colonne photo",
              n5 == 1 and _read_text(out5).splitlines()[1] == "0347;1.500;2.000;3.000")
    finally:
        _sh3.rmtree(tmp3, ignore_errors=True)

    # 13. Étage déduit du numéro de local
    print("\n13) Étage déduit du numéro de local")
    import tempfile as _tf4
    import shutil as _sh4
    tmp4 = _tf4.mkdtemp(prefix='bubblenav_etage_')
    try:
        c = os.path.join(tmp4, 'etages.csv')
        with open(c, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write("Num scan;Nom du Locator;X;Y;Z;% NORD;Plancher\r\n"
                     "1;R110b_01;0;0;-1.85;50;PLANCHER 01 (-03.50m)\r\n"
                     "2;R110b_02;1;0;-1.85;50;PLANCHER 01 (-03.50m)\r\n"
                     "3;R712_01;0;0;21.65;50;PLANCHER 07 (+20.00m)\r\n"
                     "4;R732_01;0;5;27.65;50;PLANCHER 08 (+24.00m)\r\n"
                     "5;R715_01;3;0;21.65;50;\r\n"
                     "6;R712_09;0;9;-6.85;50;\r\n"
                     "7;R910_01;5;5;-7.15;50;\r\n"
                     "8;SAP_01;6;6;9.65;50;\r\n")
        e, _ = read_survey_csv(c)
        pe = [x.parts() for x in e]
        check("plancher renseigné prioritaire", pe[0].etage == '01' and not pe[0].etage_deduit)
        check("local sur deux niveaux signalé (R732 au plancher 08)",
              pe[3].etage == '08' and pe[3].niveau_local == '07')
        check("plancher vide : étage déduit du chiffre des centaines",
              pe[4].etage == '07' and pe[4].etage_deduit, f"R715 → {pe[4].etage}")
        check("local sans numéro à 3 chiffres : pas de déduction", pe[7].etage == '')
        inc = check_floor_coherence(e)
        check("étage déduit cohérent avec Z : aucun signalement", 4 not in inc)
        check("étage déduit contredit par Z : signalé", 5 in inc and 'hors de la plage' in inc[5],
              inc.get(5, ''))
        check("étage déduit sans plancher correspondant : signalé",
              6 in inc and 'aucun plancher 09' in inc[6], inc.get(6, ''))
        check("filtre plancher inchangé : les bulles sans plancher restent groupées",
              e[5].floor == e[6].floor == '—')
    finally:
        _sh4.rmtree(tmp4, ignore_errors=True)

    # 14. Repère XYZ, glisser sur un axe, squelette du plan
    print("\n14) Repère XYZ, glisser sur un axe, squelette du plan")
    import random as _rnd
    rng = _rnd.Random(7)
    err_px = err_t = 0.0
    n_ok = 0
    for _ in range(2000):
        cal = Calib(rng.choice(('colonne', 'centre')), rng.choice((1, -1)), rng.uniform(-40, 40))
        pct = rng.uniform(0, 100)
        v = View(rng.uniform(-180, 180), rng.uniform(-60, 10), rng.uniform(60, 110), 1280, 800)
        p = (rng.uniform(-8, 8), rng.uniform(-8, 8), rng.uniform(-2.5, 0.5))
        axis = AXES[rng.choice('xyz')]
        t_true = rng.uniform(-1.5, 1.5)
        q = tuple(p[i] + axis[i] * t_true for i in range(3))
        pr = project_point(v, cal, pct, *q)
        if pr is None or not (0 <= pr[0] <= v.width and 0 <= pr[1] <= v.height):
            continue
        ray = screen_ray(v, pr[0], pr[1], cal, pct)
        back = project_point(v, cal, pct, *(r * 5.0 for r in ray))
        err_px = max(err_px, math.hypot(back[0] - pr[0], back[1] - pr[1]))
        t = axis_param(ray, p, axis)
        if t is None:
            continue
        n_ok += 1
        err_t = max(err_t, abs(t - t_true))
    check("rayon écran ↔ projection d'un point (aller-retour)", err_px < 1e-6,
          f"écart max {err_px:.1e} px")
    check("abscisse sur l'axe retrouvée sous le curseur", n_ok > 100 and err_t < 1e-6,
          f"{n_ok} cas, écart max {err_t:.1e} m")
    check("axe vu dans l'axe du regard : geste ignoré",
          axis_param((0.0, 1.0, 0.0), (0.0, 5.0, -1.6), AXES['y']) is None)
    grid = [Station(idx=k, photo=f"g{k}", locator=f"G{k}", x=float(k % 6) * 2.0,
                    y=float(k // 6) * 2.0, z=1.65, north_pct=50.0, floor='P')
            for k in range(36)]
    g_links = build_graph(grid, GraphParams(radius=6.0))
    sk = plan_skeleton(grid, g_links)
    longest = max(math.hypot(grid[i].x - grid[j].x, grid[i].y - grid[j].y) for i, j in sk)
    check("squelette d'une grille : maillage de 2 m, sans diagonale",
          len(sk) == 60 and longest < 2.0 + 1e-9, f"{len(sk)} traits, plus long {longest:.2f} m")
    if 'links' in locals() and stations:
        sk = plan_skeleton(stations, links)
        full = {(min(st.idx, lk.target), max(st.idx, lk.target))
                for st in stations for lk in links[st.idx] if lk.kind == 'same'}

        def n_comp(edges) -> int:
            parent = list(range(len(stations)))

            def root(a: int) -> int:
                while parent[a] != a:
                    parent[a] = parent[parent[a]]
                    a = parent[a]
                return a
            for i, j in edges:
                parent[root(i)] = root(j)
            return len({root(i) for i in range(len(stations))})
        check("squelette du relevé : connexe comme le réseau, 2 à 4 fois plus léger",
              set(sk) <= full and n_comp(sk) == n_comp(full)
              and 2.0 <= len(full) / max(1, len(sk)) <= 4.5,
              f"{len(full)} → {len(sk)} traits")

    # 15. Altimétrie : Z = Z plancher + delta + hauteur instrument
    print("\n15) Altimétrie : Z plancher + delta + hauteur instrument")
    import tempfile as _tf5
    import shutil as _sh5
    tmp5 = _tf5.mkdtemp(prefix='bubblenav_alti_')
    try:
        c = os.path.join(tmp5, 'alti.csv')
        with open(c, 'w', encoding='utf-8-sig', newline='') as fh:
            fh.write("Num scan;Nom du Locator;X;Y;Z;Z plancher;Delta;Hauteur instrument;"
                     "% NORD;Plancher\r\n"
                     "1;R110_01;0;0;1.650;0.00;0;1.65;50;PLANCHER 01 (+00.00m)\r\n"
                     "2;R110_02;3;0;1.900;0.00;+0.25;1.65;50;PLANCHER 01 (+00.00m)\r\n"
                     "3;R110_03;6;0;2.500;0.00;-0.10;1.50;50;PLANCHER 01 (+00.00m)\r\n"
                     "4;R210_01;0;5;5.650;4.00;0;;50;PLANCHER 02 (+04.00m)\r\n"
                     "5;SAP_01;9;9;7.777;;;;50;\r\n")
        a, _ = read_survey_csv(c)
        check("colonnes Z plancher / Delta / Hauteur instrument reconnues",
              a[1].floor_alt == 0.0 and a[1].floor_alt_src == 'colonne'
              and a[1].delta_col and a[1].delta0 == 0.25 and a[2].h0 == 1.50)
        apply_altimetry(a, 1.60)
        check("Z = plancher + Δ + H (Z du CSV ignoré)",
              abs(a[1].z - 1.90) < 1e-9 and abs(a[2].z - 1.40) < 1e-9
              and abs(a[3].z - 5.60) < 1e-9,
              f"{a[1].z:.2f} / {a[2].z:.2f} / {a[3].z:.2f} (H défaut 1.60)")
        check("sans altitude de plancher : Z du CSV conservé", abs(a[4].z - 7.777) < 1e-9)
        corr = Corrections(c, eye=1.60)
        corr.apply(a[2], ddelta=+0.05)
        corr.apply(a[2], dh=+0.02)
        check("corrections : Δ et H s'ajoutent au calcul",
              abs(a[2].z - 1.47) < 1e-9 and abs(a[2].delta(1.60) + 0.05) < 1e-9
              and abs(a[2].height(1.60) - 1.52) < 1e-9 and abs(a[2].ground(1.60) + 0.05) < 1e-9)
        out = os.path.join(tmp5, 'alti_corrige.csv')
        write_corrected_csv(c, out, a, eye=1.60)
        b, _ = read_survey_csv(out)
        check("CSV corrigé : Z, Delta et Hauteur de la bulle corrigée",
              abs(b[2].z_csv - 1.47) < 5e-4 and abs(b[2].delta0 + 0.05) < 5e-4
              and abs(b[2].h0 - 1.52) < 5e-4, f"Z {b[2].z_csv} Δ {b[2].delta0} H {b[2].h0}")
        check("CSV corrigé : Z faux remplacé par le calcul, même sans correction",
              abs(b[3].z_csv - 5.60) < 5e-4 and abs(b[0].z_csv - 1.65) < 5e-4
              and abs(b[1].z_csv - 1.90) < 5e-4 and abs(b[4].z_csv - 7.777) < 5e-4,
              f"{b[3].z_csv} / {b[0].z_csv} / {b[4].z_csv}")
        apply_altimetry(b, 1.60)
        check("CSV corrigé relu : mêmes altitudes",
              all(abs(p.z - q.z) < 5e-4 for p, q in zip(a, b)))
    finally:
        _sh5.rmtree(tmp5, ignore_errors=True)

    # 16. Format terrain : Z plancher, Delta, Hauteur/cm, Zcorrige, PlancherMS
    print("\n16) Format terrain (Z plancher, Delta, Hauteur/cm, Zcorrige)")
    import tempfile as _tf6
    import shutil as _sh6
    tmp6 = _tf6.mkdtemp(prefix='bubblenav_terrain_')
    try:
        c = os.path.join(tmp6, 'terrain.csv')
        src_txt = ("\ufeffNum scan;Locator;X;Y;Z;Delta;Hauteur/cm;Zcorrige;% NORD;PlancherMS\r\n"
                   "1001;R110b_01;15.217;6.563;-3.500;;165.000;-1.850;50;PLANCHER 01 (-03.50m)\r\n"
                   "2108;R348_01;3.667;7.057;4.000;-0.400;165.000;5.250;50;PLANCHER 03 (+04.00m)\r\n"
                   "1052;R732_01;-8.440;-8.710;-8.500;;165.000;-6.850;50;\r\n")
        with open(c, 'w', encoding='utf-8', newline='') as fh:
            fh.write(src_txt)
        t, _ = read_survey_csv(c)
        check("Z = plancher, Zcorrige = point de vue, hauteur en cm, PlancherMS lu",
              t[1].floor_alt == 4.0 and t[1].z_csv == 5.25 and abs(t[1].h0 - 1.65) < 1e-9
              and t[1].delta0 == -0.4 and t[0].floor.startswith('PLANCHER 01')
              and t[2].floor_alt == -8.5,
              f"plancher {t[1].floor_alt} Δ {t[1].delta0} H {t[1].h0} « {t[0].floor} »")
        apply_altimetry(t, 1.65)
        check("Z calculé = Zcorrige du fichier", all(abs(x.z - x.z_csv) < 1e-9 for x in t))
        out = os.path.join(tmp6, 'terrain_corrige.csv')
        n_mod, n_keep, _ = write_corrected_csv(c, out, t)
        check("sans correction : CSV corrigé identique au fichier",
              n_mod == 0 and open(out, 'rb').read() == open(c, 'rb').read())
        Corrections(c).apply(t[0], dh=0.05, ddelta=-0.20)
        write_corrected_csv(c, out, t)
        ligne = _read_text(out).splitlines()[1].split(';')
        check("correction réécrite dans les unités du fichier (cm)",
              ligne[5] == '-0.200' and ligne[6] == '170.000' and ligne[7] == '-2.000',
              ';'.join(ligne[4:8]))
        # regarder d'où l'on vient : la pastille visée tombe au centre de l'écran
        v = View(0.0, 0.0, 90.0, 800, 600)
        cal = Calib('colonne', 1, 0.0)
        a1 = Station(idx=0, photo='a', locator='A', x=0, y=0, z=1.65, north_pct=37.0, floor='P')
        b1 = Station(idx=1, photo='b', locator='B', x=3, y=4, z=1.65, north_pct=62.0, floor='P')
        aim_at(v, cal, b1, a1, 1.65, 'sol')
        dx, dy = a1.x - b1.x, a1.y - b1.y
        pr = project(v, cal.pano_yaw(math.degrees(math.atan2(dx, dy)), b1.north_pct),
                     math.degrees(math.atan2(a1.ground(1.65) - b1.z, math.hypot(dx, dy))))
        check("regarder d'où l'on vient : pastille au centre",
              pr is not None and abs(pr[0] - 400) < 1e-6 and abs(pr[1] - 300) < 1e-6,
              f"{pr[0]:.3f}, {pr[1]:.3f}" if pr else "hors champ")
    finally:
        _sh6.rmtree(tmp6, ignore_errors=True)

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

    # Le module principal s'ouvre toujours ; le relevé et les images s'y
    # choisissent, et le visualiseur apparaît dès qu'un relevé est chargé.
    app = BubbleNavApp(cfg, csv_path, images_dir)
    app.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
