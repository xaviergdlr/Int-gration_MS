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
import json
import math
import queue
import re
import sys
import threading
import time
import unicodedata
from bisect import insort
from collections import OrderedDict, defaultdict
from datetime import datetime
from dataclasses import dataclass, field
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
    'export_workers': 2,       # panoramas 16000x8000 : ~800 Mo par tache
    'corr_paths': {},          # releve -> fichier de corrections choisi
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

    def delta(self) -> float:
        """Décalage du sol local par rapport au plancher, correction comprise."""
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
            if self.attrs:
                import dataclasses
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
    'photo': ('fichierphoto', 'fichier', 'photo', 'image', 'nomimage',
              'nomphoto', 'filename', 'file', 'name'),
    'locator': ('nomdulocator', 'locator', 'nomlocator', 'station', 'point',
                'nomdupoint', 'nom', 'id'),
    'x': ('x', 'e', 'est', 'easting', 'xm', 'coordx'),
    'y': ('y', 'n', 'nord', 'northing', 'ym', 'coordy'),
    'z': ('z', 'altitude', 'alt', 'elevation', 'zm', 'coordz', 'zcamera'),
    'hcam': ('hauteurappareil', 'hauteurcamera', 'hauteurstation', 'hcam',
             'hauteur', 'h', 'hcamera', 'happareil'),
    'delta': ('delta', 'deltaplancher', 'decalageplancher', 'deltasol',
              'surelevation', 'marche', 'deltaz'),
    'dh': ('dhstation', 'dh', 'dhauteur', 'correctionhauteur', 'dhauteurstation'),
    'ddelta': ('ddeltaplancher', 'ddelta', 'correctiondelta', 'ddeltasol'),
    'north': ('pctnord', 'nordpct', 'pct', 'nordpourcent', 'cap', 'heading',
              'azimut', 'orientation'),
    'floor': ('plancher', 'niveau', 'etage', 'level', 'floor', 'dalle'),
    # Correction d'orientation : colonne dediee, ajoutee par l'outil si absente.
    'dnord': ('deltanorddeg', 'deltanord', 'dnord', 'correctionnord',
              'rotationimage', 'nordcorrection', 'deltanordo'),
    # Cle immuable (numero de scan) : survit au renommage des photos.
    'key': ('numscan', 'numeroscan', 'nscan', 'scan', 'numero', 'num', 'cle',
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
    'index': ('index', 'indice', 'numimage', 'numphoto'),
}

YAW_COLUMN = 'Delta Nord (deg)'   # intitule ecrit si la colonne n'existe pas


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
        dnord = parse_float(cell(row, 'dnord')) or 0.0
        h0 = parse_float(cell(row, 'hcam'))
        delta0 = parse_float(cell(row, 'delta')) or 0.0
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
            h0=h0, delta0=delta0,
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

    def accepts(self, current: Station, target: Station, link: "Link",
                has_image: bool = True) -> bool:
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
        return ' · '.join(bits) if bits else "actifs (tout passe)"


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
# CORRECTIONS : POSITION XYZ (CSV) ET ORIENTATION (IMAGE)
# ─────────────────────────────────────────────────────────────────────────────

def ground_from_screen(view: View, col: float, row: float, calib: Calib,
                       north_pct: float, dz: float,
                       max_dist: float = 80.0) -> Optional[Tuple[float, float]]:
    """Point du sol visé à l'écran : (azimut deg, distance horizontale m).

    `dz` est l'altitude du plan visé par rapport à la caméra (négatif = sous
    l'observateur). Calcul exact : intersection du rayon caméra avec ce plan.
    Retourne None si le rayon ne rencontre pas le plan (regard trop horizontal).
    """
    f = view.focal()
    xg = col - view.width / 2.0
    yg = row - view.height / 2.0
    norm = math.sqrt(f * f + xg * xg + yg * yg)
    xc, yc, zc = f / norm, xg / norm, -yg / norm

    yr, pr = math.radians(view.yaw), math.radians(view.pitch)
    cy, sy = math.cos(yr), math.sin(yr)
    cp, sp = math.cos(pr), math.sin(pr)
    # monde = Rz(yaw) . Ry(-pitch) . camera   (même convention que le rendu)
    wx = cy * cp * xc - sy * yc - cy * sp * zc
    wy = sy * cp * xc + cy * yc - sy * sp * zc
    wz = sp * xc + cp * zc

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
            lines.append(self.DELIM.join((
                st.key, st.photo, st.locator,
                f"{st.x:.3f}", f"{st.y:.3f}", f"{st.z:.3f}",
                f"{st.x - st.ox:+.3f}", f"{st.y - st.oy:+.3f}",
                f"{st.dh:+.3f}", f"{st.ddelta:+.3f}", f"{st.z - st.oz:+.3f}",
                f"{st.yaw_fix:.4f}",
                f"{st.height(self.eye):.3f}", f"{st.delta():+.3f}",
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
                    values[axis] = v
            dh = parse_float(cell('dh'))
            dd = parse_float(cell('ddelta'))
            if dh is None and dd is None:
                # ancien format : seule l'altitude camera etait ecrite ; on la
                # range en hauteur de station, la lecture la plus courante
                z = parse_float(cell('z'))
                if z is not None:
                    dh = z - st.oz
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
                        eye: float = EYE_HEIGHT_DEFAULT) -> Tuple[int, int, bool]:
    """Écrit une copie du CSV portant les corrections : X/Y/Z et Δ nord.

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
    header = [norm_key(c) for c in next(csv.reader([head_body], delimiter=delim))]
    col: Dict[str, int] = {}
    for field_name in ('photo', 'x', 'y', 'z', 'dnord', 'key', 'hcam', 'delta'):
        for alias in COL_ALIASES[field_name]:
            if alias in header:
                col[field_name] = header.index(alias)
                break
    if 'photo' not in col:
        raise ValueError("Colonne « Fichier photo » introuvable dans le CSV source.")
    by_key = {st.key.lower(): st for st in stations}

    def cellules(st: Station):
        """Cellules à réécrire pour une bulle déplacée ou remontée."""
        out = [('x', st.x), ('y', st.y), ('z', st.z)]
        if 'hcam' in col:
            out.append(('hcam', st.height(eye)))
        if 'delta' in col:
            out.append(('delta', st.delta()))
        return out

    need_yaw = (any(st.has_yaw() or st.turned() for st in stations)
                if write_yaw is None else bool(write_yaw))
    add_col = need_yaw and 'dnord' not in col
    by_photo = {st.photo.lower(): st for st in stations}

    out: List[str] = [head_body + (delim + YAW_COLUMN if add_col else '') + head_eol]
    n_mod = n_keep = 0
    for raw in lines[1:]:
        body = raw.rstrip('\r\n')
        eol = raw[len(body):]
        if not body.strip():
            out.append(raw)
            continue
        try:
            fields = next(csv.reader([body], delimiter=delim))
        except Exception:
            out.append(body + (delim if add_col else '') + eol)
            n_keep += 1
            continue
        st = None
        if 'key' in col and col['key'] < len(fields) and fields[col['key']].strip():
            st = by_key.get(fields[col['key']].strip().lower())
        if st is None and col['photo'] < len(fields):
            st = by_photo.get(base_name(fields[col['photo']]).lower())
        touch = st is not None and (st.moved() or st.z_changed()
                                    or (need_yaw and st.turned()))
        if not touch and not add_col:
            out.append(raw)
            n_keep += 1
            continue

        yaw_txt = ''
        if need_yaw:
            value = st.yaw_fix if st is not None else 0.0
            i = col.get('dnord', -1)
            sample = fields[i] if 0 <= i < len(fields) else ''
            yaw_txt = _format_like(sample, value, default_decimals=4)

        if '"' in body:                       # ligne avec guillemets : réécriture csv
            if st is not None and (st.moved() or st.z_changed()):
                for name, value in cellules(st):
                    i = col.get(name, -1)
                    if 0 <= i < len(fields):
                        fields[i] = _format_like(fields[i], value)
            if need_yaw and not add_col and 0 <= col.get('dnord', -1) < len(fields):
                fields[col['dnord']] = yaw_txt
            if add_col:
                fields.append(yaw_txt)
            import io
            buf = io.StringIO()
            csv.writer(buf, delimiter=delim, lineterminator='').writerow(fields)
            out.append(buf.getvalue() + eol)
        else:                                  # cas courant : substitution en place
            parts = body.split(delim)
            if st is not None and (st.moved() or st.z_changed()):
                for name, value in cellules(st):
                    i = col.get(name, -1)
                    if 0 <= i < len(parts):
                        parts[i] = _format_like(parts[i], value)
            if need_yaw and not add_col and 0 <= col.get('dnord', -1) < len(parts):
                parts[col['dnord']] = yaw_txt
            if add_col:
                parts.append(yaw_txt)
            out.append(delim.join(parts) + eol)
        if touch:
            n_mod += 1
        else:
            n_keep += 1

    tmp = dst_csv + '.tmp'
    with open(tmp, 'w', encoding=enc, newline='') as fh:
        fh.write(''.join(out))
    os.replace(tmp, dst_csv)
    return n_mod, n_keep, add_col


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


def compute_hotspots(stations: Sequence[Station], links: Sequence["Link"],
                     idx: int, view: View, calib: Calib, filters: HotspotFilter,
                     has_image, eye: float = EYE_HEIGHT_DEFAULT,
                     disc: float = DISC_RADIUS_M, r_min: float = DISC_PX_MIN,
                     r_max: float = DISC_PX_MAX) -> Tuple[List["Hotspot"], int]:
    """Pastilles projetées dans une vue, filtres compris.

    Fonction partagée par la vue principale et la vue de comparaison : les deux
    obtiennent exactement la même géométrie.

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
        dz = tgt.ground(eye) - st.z        # pastille posée au sol de la cible
        dh = lk.dist_h
        elev = math.degrees(math.atan2(dz, dh)) if dh > 1e-6 else (90.0 if dz > 0 else -90.0)
        pr = project(view, calib.pano_yaw(lk.azimuth, st.north_pct), elev)
        if pr is None:
            continue
        col, row, _ = pr
        if not (-80 <= col <= view.width + 80 and -80 <= row <= view.height + 80):
            continue
        # rayon a l'ecran = focale x rayon physique / distance, borne des deux cotes
        radius = clamp(f * disc / max(lk.dist, 0.35), r_min, r_max)
        out.append(Hotspot(lk, col, row, radius, tgt.locator))
    out.sort(key=lambda h: -h.link.dist)     # les plus lointaines dessinees d'abord
    return out, masques


SPHERE_RADIUS = 0.74     # rayon de la sphere, en fraction du rayon de pastille
SHADOW_RX, SHADOW_RY = 1.00, 0.36   # demi-axes de l'ombre au sol (fractions)
SPHERE_LIFT = 0.86       # centre de la sphere au-dessus du sol (fraction de rs)
HALO_RADIUS = 1.75       # rayon du halo de survol (fraction de rs)
HALO_COLOR = (255, 255, 235)


def sphere_sprite(color: str, r: int, hover: bool = False, ss: int = 2):
    """Pastille en relief : sphère éclairée reposant sur son ombre portée.

    Retourne (image RGBA PIL, (ax, ay)) où (ax, ay) est le point du sol dans
    l'image — c'est lui qui se place sur la position projetée de la bulle.
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
    shadow_a = np.clip((1.0 - d) / 0.35, 0, 1) * 0.55
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
    return img, (cx / ss, gy / ss)


def hotspot_hit(hotspots: Sequence["Hotspot"], x: float, y: float,
                relief: bool = True) -> Optional[int]:
    """Pastille sous le curseur : sphère (au-dessus du sol) ou ombre au sol."""
    best, best_d = None, float('inf')
    for i, hs in enumerate(hotspots):
        r = hs.radius
        rx, ry = r + HIT_SLACK_PX, r * 0.55 + HIT_SLACK_PX
        d = ((x - hs.col) / rx) ** 2 + ((y - hs.row) / ry) ** 2
        if relief:
            rs = SPHERE_RADIUS * r
            cy = hs.row - rs * SPHERE_LIFT
            ds = ((x - hs.col) ** 2 + (y - cy) ** 2) / (rs + HIT_SLACK_PX) ** 2
            d = min(d, ds)
        if d <= 1.0 and d < best_d:
            best, best_d = i, d
    return best


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
        self.filters = HotspotFilter(
            active=bool(cfg.get('filter_active', False)),
            floor_mode=str(cfg.get('filter_floor', 'tous')),
            max_dist=float(cfg.get('filter_dist', 0.0)),
            local=str(cfg.get('filter_local', '')),
            inter_floor=bool(cfg.get('filter_inter', True)),
            hide_missing=bool(cfg.get('filter_hide_missing', False)))
        self.hidden_count = 0
        self.focus_idx: Optional[int] = None      # bulle décrite dans le panneau
        self._hover: Optional[int] = None
        # Edition
        self.corrections = Corrections()
        self.by_photo: Dict[str, Station] = {}
        self.by_key: Dict[str, Station] = {}
        self.selected: Optional[int] = None      # bulle en cours de modification
        self._hs_drag = None                     # (idx station, dz, mode)
        self._sync_ui = False                    # garde anti-boucle des widgets
        self._hover_xy = None
        self._cone_sig = None                    # état du camembert du plan
        self._sprites: "OrderedDict[tuple, tuple]" = OrderedDict()   # sphères
        self._cmp_sig = None                     # état de synchro de la vue B
        self._last_current = -1
        self._plan_hit = None                    # deplacement sur le plan
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

        self._mk_button(bar, "Aide", self._dlg_help).pack(side='right', padx=(3, 10), pady=4)
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
        self.plan.bind('<ButtonPress-3>', self._on_plan_press)
        self.plan.bind('<B3-Motion>', self._on_plan_drag)
        self.plan.bind('<MouseWheel>', self._on_plan_wheel)
        self.plan.bind('<Button-4>', lambda e: self._on_plan_wheel(e, +1))
        self.plan.bind('<Button-5>', lambda e: self._on_plan_wheel(e, -1))

        btns = tk.Frame(side, bg=COLORS['bg_medium'])
        btns.pack(fill='x', padx=10, pady=6)
        self._mk_button(btns, "Recadrer", self._plan_fit).pack(side='left')
        self._mk_button(btns, "◀ Retour", self.go_back).pack(side='left', padx=6)

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
        self.bind('<c>', lambda e: self._toggle_compare())
        self.bind('<C>', lambda e: self._toggle_compare())
        self.bind('<f>', lambda e: self._toggle_filters())
        self.bind('<F>', lambda e: self._toggle_filters())
        self.bind('<e>', lambda e: self._toggle_edit())
        self.bind('<E>', lambda e: self._toggle_edit())
        self.bind('<Control-z>', lambda e: self._undo_edit())
        self.bind('<Control-s>', lambda e: self._dlg_apply())
        self.bind('<Prior>', lambda e: self._bump('dh', +1))
        self.bind('<Next>', lambda e: self._bump('dh', -1))
        self.bind('<Shift-Prior>', lambda e: self._bump('ddelta', +1))
        self.bind('<Shift-Next>', lambda e: self._bump('ddelta', -1))
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
        self.f_floor_cb.config(values=['tous', 'courant'] + self.floors)
        self.by_photo = {s.photo: s for s in stations}
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
        self.rebuild_graph()

        msg = f"{len(stations)} bulles · {len(self.floors)} planchers · {os.path.basename(path)}"
        if warns:
            msg += f" · {len(warns)} ligne(s) ignorée(s)"
        anomalies = sum(1 for st in stations if st.parts().anomalies())
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
            float(self.cfg.get('disc_radius', DISC_RADIUS_M)), *self.disc_bounds())
        self.hidden_count = masques
        return out

    def _sprite(self, color: str, r: float, hover: bool = False):
        """Sphère ombrée prête à afficher, mise en cache par couleur et taille."""
        rq = max(3, int(round(r / 2.0) * 2))          # pas de 2 px : cache compact
        key = (color, rq, hover)
        hit = self._sprites.get(key)
        if hit is not None:
            return hit
        from PIL import ImageTk
        img, (ax, ay) = sphere_sprite(color, rq, hover)
        entry = (ImageTk.PhotoImage(img), ax, ay)
        if len(self._sprites) >= 400:
            self._sprites.pop(next(iter(self._sprites)))
        self._sprites[key] = entry
        return entry

    def relief(self) -> bool:
        return bool(self.cfg.get('disc_3d', True))

    def draw_hotspot(self, canvas, hs: "Hotspot", color: str, hovered: bool,
                     selected: bool = False) -> None:
        """Dessine une pastille (relief ou plate) sur un canevas."""
        r = hs.radius * (1.25 if hovered else 1.0)
        if self.relief():
            photo, ax, ay = self._sprite(color, r, hovered)
            canvas.create_image(hs.col - ax, hs.row - ay, anchor='nw', image=photo,
                                tags='hs')
            if selected:
                rs = SPHERE_RADIUS * r * 1.35
                cy = hs.row - SPHERE_RADIUS * r * SPHERE_LIFT
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

    def label_y(self, hs: "Hotspot", hovered: bool) -> float:
        r = hs.radius * (1.25 if hovered else 1.0)
        return hs.row + (SHADOW_RY * r if self.relief() else 0.55 * r) + 10

    def glyph_y(self, hs: "Hotspot", hovered: bool) -> float:
        r = hs.radius * (1.25 if hovered else 1.0)
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
        show_lbl = bool(self.labels_var.get())
        if self.edit_mode:
            self._draw_edit_refs(view)
        for i, hs in enumerate(self.hotspots):
            lk = hs.link
            tgt = self.stations[lk.target]
            color = {'same': COLORS['hot'], 'up': COLORS['hot_up'],
                     'down': COLORS['hot_down']}[lk.kind]
            missing = not self.store.has(tgt.photo)
            if missing:
                color = COLORS['plan_missing']
            if tgt.modified():
                color = COLORS['edit']
            hovered = (i == self._hover)
            self.draw_hotspot(self.canvas, hs, color, hovered,
                              selected=self.edit_mode and self.selected == tgt.idx)
            if lk.kind != 'same':
                self.canvas.create_text(hs.col, self.glyph_y(hs, hovered),
                                        text='▲' if lk.kind == 'up' else '▼',
                                        fill=color, font=('Segoe UI', 11, 'bold'), tags='hs')
            if show_lbl or hovered:
                txt = human_dist(lk.dist)
                if hovered:
                    txt = f"{hs.label} · {txt}"
                    if missing:
                        txt += " · image absente"
                ty = self.label_y(hs, hovered)
                self.canvas.create_text(hs.col + 1, ty + 1, text=txt, fill='#000000',
                                        font=F_UI, tags='hs')
                self.canvas.create_text(hs.col, ty, text=txt,
                                        fill='white' if hovered else '#e8e8e8',
                                        font=F_UI_B if hovered else F_UI, tags='hs')
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
        if st.turned():
            title += f"   Δnord {st.yaw_fix:+.2f}°"
        self.canvas.create_text(15, 13, text=title, anchor='nw', fill='#000000',
                                font=('Segoe UI', 12, 'bold'), tags='hs')
        self.canvas.create_text(14, 12, text=title, anchor='nw',
                                fill=COLORS['edit'] if st.modified() else COLORS['hot'],
                                font=('Segoe UI', 12, 'bold'), tags='hs')
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
                text="MODE ÉDITION — Maj+glisser : tourner l'image · "
                     "glisser une pastille : la déplacer · Ctrl+glisser : bulle active")
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
        """Contenu de l'infobulle d'une pastille vue depuis `origin`."""
        lk = hs.link
        tgt = self.stations[lk.target]
        p = tgt.parts()
        sens = {'same': 'même plancher', 'up': 'niveau au-dessus',
                'down': 'niveau en dessous'}[lk.kind]
        lines = [
            tgt.locator,
            f"photo        {tgt.photo}"
            + (f"   (clé {tgt.key})" if tgt.key_explicit or tgt.key != tgt.photo else ''),
            f"local        {p.local or '—'}   étage {p.etage or '—'}   "
            f"index {p.index or '—'}",
            f"prise de vue {p.date_lisible() or '—'}",
            f"distance 3D  {lk.dist:.2f} m",
            f"horizontale  {lk.dist_h:.2f} m",
            f"Δ altitude   {lk.dz:+.2f} m",
            f"azimut       {lk.azimuth:+.1f}°",
            f"X / Y / Z    {tgt.x:.2f} / {tgt.y:.2f} / {tgt.z:.2f}",
            f"plancher     {tgt.floor}  ({sens})",
            f"image        {'présente' if self.store.has(tgt.photo) else 'ABSENTE'}",
        ]
        if origin is not None and origin.idx != tgt.idx:
            lines.append(f"cap depuis   {self.calib.pano_yaw(lk.azimuth, origin.north_pct):+.1f}°"
                         " dans l'image")
        if tgt.modified():
            marks = []
            if tgt.moved():
                marks.append(f"XY déplacé de {math.hypot(tgt.x - tgt.ox, tgt.y - tgt.oy):.2f} m")
            if tgt.raised():
                marks.append(f"hauteur {tgt.dh:+.3f} m")
            if tgt.shifted():
                marks.append(f"delta {tgt.ddelta:+.3f} m")
            if tgt.turned():
                marks.append(f"image tournée de {tgt.yaw_fix:+.2f}°")
            lines.append("modifié      " + ' · '.join(marks))
        return lines, tgt.modified()

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
        if self.edit_mode and self.current >= 0:
            eye = float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT))
            st = self.station()
            ctrl = bool(event.state & 0x0004)
            shift = bool(event.state & 0x0001)
            if shift:                                    # tourner l'image
                self.corrections.apply(st)               # état avant le geste
                self._hs_drag = ('yaw', self.current, st.yaw_fix, event.x)
                return
            if ctrl:                                     # deplacer la bulle active
                pos = self._ground_target(event.x, event.y, -st.height(eye))
                if pos is not None:
                    self._set_target(None)
                    self.corrections.apply(st)           # état avant le geste
                    self._hs_drag = ('active', self.current, -st.height(eye),
                                     pos[0], pos[1], st.x, st.y)
                    return
            hit = self._hotspot_at(event.x, event.y)
            if hit is not None:
                tgt = self.stations[self.hotspots[hit].link.target]
                self._set_target(tgt.idx)
                self.corrections.apply(tgt)              # état avant le geste
                self._hs_drag = ('pastille', tgt.idx, tgt.ground(eye) - st.z)
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
        self._zoom(-6 * step)

    def _on_double(self, event) -> None:
        """Double-clic : rejoint la pastille visée, sinon recentre la vue."""
        hit = self._hotspot_at(event.x, event.y)
        if hit is not None:
            if self.edit_mode:
                self.goto(self.hotspots[hit].link.target)
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
        ent = tk.Entry(row, textvariable=self.f_local, font=F_MONO, width=20,
                       bg=COLORS['bg_light'], fg=COLORS['text'], relief='flat',
                       insertbackground=COLORS['text'])
        ent.pack(side='left')
        ent.bind('<KeyRelease>', self._on_filter_change)
        tk.Label(body, text="ex. K256, W25*  — préfixe suffisant", font=F_UI,
                 bg=COLORS['card'], fg=COLORS['text_muted'], anchor='w'
                 ).pack(fill='x', padx=(70, 0))

        row = tk.Frame(body, bg=COLORS['card'])
        row.pack(fill='x', pady=(2, 0))
        self.f_inter = tk.BooleanVar(value=self.filters.inter_floor)
        self.f_missing = tk.BooleanVar(value=self.filters.hide_missing)
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
        self.compare = CompareView(self, depart)
        self.cmp_btn.config(bg=COLORS['sel'], fg='#101010')
        self._cone_sig = None
        self._set_status("Vue de comparaison ouverte — « Vue liée » fait tourner "
                         "les deux vues ensemble", COLORS['sel'])

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

    def _reset_filters(self) -> None:
        self.filter_var.set(False)
        self.f_floor.set('tous')
        self.f_dist.set(0.0)
        self.f_local.set('')
        self.f_inter.set(True)
        self.f_missing.set(False)
        self._on_filter_change()

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
            ('étage', p.etage), ('local', p.local), ('index', p.index)) if v)
        if detail:
            lignes.append(detail)
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
        lignes.append(f"sol      {st.ground(eye):.2f}  ·  hauteur {st.height(eye):.2f}"
                      + (f"  ·  delta {st.delta():+.2f}" if st.delta() else ''))
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
              "glisser une pastille = la déplacer au sol ;\n"
              "Ctrl + glisser = déplacer la bulle active.\n"
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
        self.z_lbl.config(text=(f"Z caméra {st.z:.3f}  ·  sol {st.ground(eye):.3f}  ·  "
                                f"hauteur {st.height(eye):.2f}  ·  delta {st.delta():+.2f}"))
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
        if self._autosave_job:
            self.after_cancel(self._autosave_job)
        self._autosave_job = self.after(1200, self._autosave)

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
        st = self.corrections.undo(self.by_photo)
        if st is None:
            self._set_status("Rien à annuler")
            return
        self._after_edit(moved=True, turned=True)
        self._set_status(f"Annulation sur {st.locator}", COLORS['edit'])

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

    # ── deplacements a la souris ─────────────────────────────────────
    def _ground_target(self, x: float, y: float, dz: float
                       ) -> Optional[Tuple[float, float]]:
        """Point du sol visé, exprimé en (Est, Nord) absolus."""
        view = self._frame_view or self.view
        st = self.station()
        if st is None:
            return None
        res = ground_from_screen(view, x, y, self.calib, st.north_pct, dz)
        if res is None:
            return None
        az, dist = res
        a = math.radians(az)
        return st.x + dist * math.sin(a), st.y + dist * math.cos(a)

    def _drag_edit(self, event) -> None:
        kind = self._hs_drag[0]
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
        if kind == 'pastille':
            _, idx, dz = self._hs_drag
            pos = self._ground_target(event.x, event.y, dz)
            if pos is None:
                return
            self.corrections.apply(self.stations[idx], x=pos[0], y=pos[1], record=False)
        elif kind == 'active':
            _, idx, dz, wx0, wy0, sx0, sy0 = self._hs_drag
            pos = self._ground_target(event.x, event.y, dz)
            if pos is None:
                return
            st = self.stations[idx]
            self.corrections.apply(st, x=sx0 + (wx0 - pos[0]), y=sy0 + (wy0 - pos[1]),
                                   record=False)
        self._refresh_edit_panel()
        self._draw_overlay()
        self._draw_plan()

    def _end_edit_drag(self) -> None:
        kind = self._hs_drag[0] if self._hs_drag else None
        self._hs_drag = None
        if kind is None:
            return
        self._after_edit(moved=kind in ('pastille', 'active'), turned=kind == 'yaw')

    # ── application par lot ──────────────────────────────────────────
    def _dlg_apply(self) -> None:
        """Bilan des corrections, puis traitements par lot en une passe."""
        if not self.stations:
            return
        self._autosave()
        bilan = Corrections.counts(self.stations)
        pending = Corrections.pending_images(self.stations)
        if not (bilan.any() or pending):
            messagebox.showinfo("Appliquer", "Aucune correction en attente.")
            return

        win = tk.Toplevel(self)
        win.title("Corrections — bilan et application")
        win.configure(bg=COLORS['bg_dark'])
        win.transient(self)
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
        do_merged = tk.BooleanVar(value=False)

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

        check("Écrire aussi un relevé complet corrigé  (copie du CSV chargé)",
              do_merged).pack(fill='x', padx=14, pady=(8, 2))
        tk.Label(win, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'],
                 anchor='w', justify='left', text=(
                     "Facultatif : relevé d'origine + corrections fusionnés, pour une "
                     "chaîne qui attend un CSV unique.\n"
                     "Le relevé chargé et le fichier de corrections restent inchangés.")
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

    def _export_merged(self, path: str) -> bool:
        """Écrit un relevé complet corrigé, sans rien changer aux fichiers de travail."""
        try:
            n_mod, n_keep, added = write_corrected_csv(
                self.csv_path, path, self.stations,
                eye=float(self.cfg.get('eye_height', EYE_HEIGHT_DEFAULT)))
        except Exception as exc:
            messagebox.showerror("Relevé corrigé", f"Écriture impossible :\n{exc}")
            return False
        self._set_status(f"Relevé corrigé écrit : {n_mod} ligne(s) modifiée(s), "
                         f"{n_keep} recopiée(s) → {path}", COLORS['ok'])
        messagebox.showinfo("Relevé corrigé", (
            f"{n_mod} ligne(s) corrigée(s), {n_keep} recopiée(s) à l'identique.\n\n{path}\n\n"
            + ("Colonne « " + YAW_COLUMN + " » ajoutée pour l'orientation ; "
               "« % NORD » reste inchangée.\n\n" if added else "")
            + "Le relevé chargé et le fichier de corrections ne sont pas modifiés."))
        return True

    def _run_export(self, out_dir: str, merged_after: str = '') -> None:
        """Applique les Δ nord aux images (copies), puis met à jour les fichiers."""
        todo = [s for s in self.stations if s.has_yaw() and self.store.has(s.photo)]
        absent = len(Corrections.pending_images(self.stations)) - len(todo)
        win = tk.Toplevel(self)
        win.title("Application de l'orientation aux images")
        win.configure(bg=COLORS['bg_dark'])
        win.transient(self)
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
                self._post(lambda: (win.destroy(), messagebox.showerror("Export", str(exc))))
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
                r = 2.5
                if st.modified():
                    col, r = COLORS['edit'], 3.5
                self.plan.create_oval(x - r, y - r, x + r, y + r, fill=col, outline='')
                if st.modified() and st.moved():
                    ox, oy = to_screen(st.ox, st.oy)
                    self.plan.create_line(ox, oy, x, y, fill=COLORS['edit'], width=1)
                if self.edit_mode and self.selected == st.idx:
                    self.plan.create_oval(x - 7, y - 7, x + 7, y + 7,
                                          outline=COLORS['sel'], width=2)

        cur = self.station()
        if cur is not None:
            # voisins mis en evidence (filtres compris)
            for lk in self._visible_links(cur.idx):
                tgt = self.stations[lk.target]
                if tgt.floor != floor:
                    continue
                x, y = to_screen(tgt.x, tgt.y)
                self.plan.create_oval(x - 4, y - 4, x + 4, y + 4,
                                      outline=COLORS['hot'], width=1)
        self._draw_plan_cone()

        self.plan.create_text(w - 16, 16, text="N", fill='#ff6b6b', font=F_UI_B)
        self.plan.create_line(w - 16, 26, w - 16, 40, fill='#ff6b6b', width=2)
        self.plan.create_text(8, h - 10, anchor='w', font=F_UI, fill=COLORS['text_muted'],
                              text=f"{len(pts)} bulles · molette: zoom · clic droit: déplacer")

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
        if floor and cur.floor != floor:
            return                       # le plan montre un autre niveau
        pts = self._plan_stations()
        if not pts:
            return
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        to_screen, _ = self._plan_transform(pts, w, h)
        x, y = to_screen(cur.x, cur.y)
        view = self.view              # état courant : le cône ne suit pas le rendu
        az = self.calib.azimuth(view.yaw, cur.north_pct)
        half = view.fov / 2.0
        rad = 34.0
        plist = [x, y]
        for k in range(9):
            a = math.radians(az - half + k * (2 * half / 8))
            plist += [x + rad * math.sin(a), y - rad * math.cos(a)]
        self.plan.create_polygon(plist, fill=COLORS['plan_cone'], outline='',
                                 stipple='gray25', tags='cone')
        a = math.radians(az)             # axe de visée
        self.plan.create_line(x, y, x + rad * math.sin(a), y - rad * math.cos(a),
                              fill=COLORS['plan_cone'], width=1, tags='cone')
        self.plan.create_oval(x - 5, y - 5, x + 5, y + 5, fill=COLORS['plan_here'],
                              outline='#000000', tags='cone')

        cmp_view = self.compare      # repère de la seconde vue, si elle est ouverte
        b = cmp_view.station() if cmp_view is not None else None
        if b is not None and b.floor == floor:
            bx, by = to_screen(b.x, b.y)
            azb = self.calib.azimuth(cmp_view.view.yaw, b.north_pct)
            halfb = cmp_view.view.fov / 2.0
            plb = [bx, by]
            for k in range(9):
                ab = math.radians(azb - halfb + k * (2 * halfb / 8))
                plb += [bx + rad * math.sin(ab), by - rad * math.cos(ab)]
            self.plan.create_polygon(plb, fill=COLORS['sel'], outline='',
                                     stipple='gray12', tags='cone')
            self.plan.create_line(x, y, bx, by, fill=COLORS['sel'], width=1,
                                  dash=(3, 3), tags='cone')
            self.plan.create_oval(bx - 5, by - 5, bx + 5, by + 5, fill=COLORS['sel'],
                                  outline='#000000', tags='cone')
            self.plan.create_text(bx, by - 11, text="B", fill=COLORS['sel'],
                                  font=F_UI_B, tags='cone')
        self._cone_sig = self._cone_signature()

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
            self.corrections.apply(self.stations[idx])   # état avant le geste
            self._plan_hit = idx

    def _on_plan_drag_left(self, event) -> None:
        """Vue de dessus : positionnement X/Y direct de la cible."""
        if self._plan_hit is None:
            return
        pts = self._plan_stations()
        w = max(50, int(self.plan.winfo_width()))
        h = max(50, int(self.plan.winfo_height()))
        _, to_world = self._plan_transform(pts, w, h)
        wx, wy = to_world(event.x, event.y)
        self.corrections.apply(self.stations[self._plan_hit], x=wx, y=wy, record=False)
        self._refresh_edit_panel()
        self._draw_plan()
        self._draw_overlay()

    def _on_plan_release_left(self, event) -> None:
        if self._plan_hit is not None:
            self._plan_hit = None
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
        tk.Label(perf, font=F_UI, bg=COLORS['bg_dark'], fg=COLORS['text_muted'],
                 anchor='w', text=(
                     f"source {self.store.src_width} px : {self.store.frame_mb():.0f} Mo "
                     f"par bulle, {self.store.effective_cache()} gardée(s) en mémoire")
                 ).pack(fill='x')
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
            "COMPARAISON  (touche C)\n"
            "  • Seconde vue bulle dans sa propre fenêtre\n"
            "  • « Vue liée » : les deux vues regardent la même direction terrain,\n"
            "    tourner ou zoomer d'un côté agit sur les deux\n"
            "  • « Suivi de A » : la vue B se place automatiquement sur le même\n"
            "    local à un autre plancher, ou sur la bulle la plus proche\n"
            "  • « A → B » recopie la bulle courante · « ⇄ » échange les deux vues\n"
            "  • Les pastilles de B restent cliquables pour s'y déplacer seul\n\n"
            "PLAN\n"
            "  • Clic gauche            : aller sur la bulle la plus proche\n"
            "  • Molette                : zoom · clic droit glissé : déplacer\n"
            "  • Liste « Plancher »     : changer de niveau (bulle la plus proche)\n\n"
            "PASTILLES\n"
            "  jaune = même plancher · bleu ▲ = niveau au-dessus\n"
            "  violet ▼ = niveau en dessous · rouge sombre = image absente\n\n"
            "ÉDITION  (touche E) — rien n'est modifié sur le disque en direct\n"
            "  • Cible = bulle active, ou pastille cliquée\n"
            "  • Maj + glisser dans la vue : tourner l'image sous les pastilles.\n"
            "    L'angle est une DONNÉE, écrite dans le FICHIER DE CORRECTIONS\n"
            "    (relevé_corrections.csv) et appliquée à l'affichage. Le relevé\n"
            "    chargé et les images d'origine ne sont jamais modifiés.\n"
            "  • Glisser une pastille : la déplacer au sol (azimut + éloignement)\n"
            "  • Ctrl + glisser : déplacer la bulle active elle-même\n"
            "  • Glisser un point du plan : position X/Y en vue de dessus\n"
            "  • Champs X/Y/Z, pas réglable, Page haut/bas pour l'altitude\n"
            "  • Corrections enregistrées en continu dans leur propre fichier ;\n"
            "    « Fichier… » permet d'en reprendre un autre\n"
            "  • Ctrl+Z annule · « Réinit. » revient aux valeurs du relevé\n"
            "  • « Appliquer / enregistrer… » (Ctrl+S) : bilan, puis rotation des\n"
            "    images dans un NOUVEAU dossier (Δ nord alors remis à 0) et, en\n"
            "    option, écriture d'un relevé complet corrigé\n"
            "  • Les croix bleues sont les bulles voisines non retenues comme\n"
            "    pastilles : elles servent de repères pour juger l'orientation\n\n"
            "Si les pastilles ne tombent pas au bon endroit, ouvrez « Réglages… »\n"
            "et ajustez la calibration de l'azimut (effet immédiat)."
        ))

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

class CompareView(tk.Toplevel if _TK_OK else object):
    """Seconde vue bulle, pour comparer deux points de vue.

    Elle partage tout le modèle avec la vue principale (relevé, réseau, filtres,
    calibration, corrections, cache d'images) et se contente d'un point de vue
    distinct. En mode « vue liée », elle regarde en permanence dans la même
    direction terrain que la vue principale : tourner d'un côté tourne des deux.
    """

    FOLLOW_MODES = ('aucun', 'même local, autre plancher', 'bulle la plus proche')

    def __init__(self, app: "BubbleNavApp", idx: int):
        super().__init__(app)
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
        self.follow = tk.StringVar(value=self.FOLLOW_MODES[0])

        self.title("BubbleNav — vue de comparaison")
        self.configure(bg=COLORS['bg_dark'])
        self.geometry("920x620")
        self.minsize(420, 320)
        self._build_ui()
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.bind('<Escape>', lambda e: self.close())
        self.after(0, self._place_beside)
        self.after(40, lambda: self.request_render(force=True))

    def _place_beside(self) -> None:
        """Se place à côté de la fenêtre principale, sans la recouvrir."""
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            ax, ay = self.app.winfo_rootx(), self.app.winfo_rooty()
            aw = self.app.winfo_width()
            w, h = 920, 620
            x = ax + aw + 8
            if x + w > sw:
                if sw - x >= 460:               # on rétrécit plutôt que recouvrir A
                    w = sw - x - 8
                else:                           # écran trop étroit : on cadre à droite
                    x = max(0, sw - w - 8)
            y = max(0, min(ay, sh - h - 40))
            self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    # ── interface ────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        bar = tk.Frame(self, bg=COLORS['bg_medium'])
        bar.pack(fill='x', side='top')
        tk.Label(bar, text="Vue B", font=F_TITLE, bg=COLORS['bg_medium'],
                 fg=COLORS['sel']).pack(side='left', padx=(10, 8), pady=4)
        self.title_lbl = tk.Label(bar, text="—", font=F_UI_B, bg=COLORS['bg_medium'],
                                  fg=COLORS['text'])
        self.title_lbl.pack(side='left', padx=4)

        self.app._mk_button(bar, "⇄ Échanger", self.swap).pack(side='right', padx=4, pady=4)
        self.app._mk_button(bar, "A → B", self.copy_from_a).pack(side='right', padx=4, pady=4)
        tk.Checkbutton(bar, text="Vue liée", variable=self.linked,
                       command=self._on_linked, font=F_UI, bg=COLORS['bg_medium'],
                       fg=COLORS['text'], selectcolor=COLORS['bg_light'], bd=0,
                       highlightthickness=0, activebackground=COLORS['bg_medium'],
                       activeforeground=COLORS['text']).pack(side='right', padx=6)
        tk.Label(bar, text="Suivi de A", font=F_UI, bg=COLORS['bg_medium'],
                 fg=COLORS['text_muted']).pack(side='right', padx=(8, 2))
        cb = ttk.Combobox(bar, textvariable=self.follow, state='readonly', width=22,
                          style='BN.TCombobox', values=self.FOLLOW_MODES)
        cb.pack(side='right', pady=4)
        cb.bind('<<ComboboxSelected>>', lambda e: self.follow_a(self.app.current))

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

        self.status = tk.Label(self, text="", anchor='w', bg=COLORS['bg_medium'],
                               fg=COLORS['text_muted'], font=F_UI, padx=10, pady=3)
        self.status.pack(fill='x', side='bottom')

    # ── modele ───────────────────────────────────────────────────────
    def station(self) -> Optional[Station]:
        sts = self.app.stations
        return sts[self.idx] if 0 <= self.idx < len(sts) else None

    def goto(self, idx: int, keep_heading: bool = True) -> None:
        if not (0 <= idx < len(self.app.stations)) or idx == self.idx:
            return
        prev = self.station()
        if keep_heading and prev is not None and not self.linked.get():
            az = self.app.calib.azimuth(self.view.yaw, prev.north_pct)
            self.view.yaw = self.app.calib.pano_yaw(az, self.app.stations[idx].north_pct)
        self.idx = idx
        self.request_render(force=True)
        self._refresh_title()
        self.app.store.prefetch([self.app.stations[lk.target].photo
                                 for lk in self.app.links[idx]]
                                if idx < len(self.app.links) else [])

    def copy_from_a(self) -> None:
        self.goto(self.app.current)

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
            float(app.cfg.get('disc_radius', DISC_RADIUS_M)), *app.disc_bounds())
        for i, hs in enumerate(self.hotspots):
            tgt = app.stations[hs.link.target]
            color = {'same': COLORS['hot'], 'up': COLORS['hot_up'],
                     'down': COLORS['hot_down']}[hs.link.kind]
            if not app.store.has(tgt.photo):
                color = COLORS['plan_missing']
            if tgt.modified():
                color = COLORS['edit']
            hovered = i == self._hover
            app.draw_hotspot(self.canvas, hs, color, hovered)
            txt = f"{hs.label} · {human_dist(hs.link.dist)}" if hovered \
                else human_dist(hs.link.dist)
            ty = app.label_y(hs, hovered)
            self.canvas.create_text(hs.col + 1, ty + 1, text=txt, fill='#000000',
                                    font=F_UI, tags='hs')
            self.canvas.create_text(hs.col, ty, text=txt, fill='#e8e8e8',
                                    font=F_UI, tags='hs')
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

    # ── interactions ─────────────────────────────────────────────────
    def _on_press(self, event) -> None:
        self._drag = (event.x, event.y, self.view.yaw, self.view.pitch,
                      self.app.view.yaw, self.app.view.pitch)
        self._press_xy = (event.x, event.y)

    def _on_drag(self, event) -> None:
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
        moved = 0
        if getattr(self, '_press_xy', None):
            moved = abs(event.x - self._press_xy[0]) + abs(event.y - self._press_xy[1])
        self._drag = None
        if moved <= 4:
            hit = self._hotspot_at(event.x, event.y)
            if hit is not None:
                self.goto(self.hotspots[hit].link.target)
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
        lines, modified = self.app.tooltip_lines(self.hotspots[hit], self.station())
        self.app.draw_tooltip(self.canvas, x, y, lines, self.view.width, self.view.height,
                              modified)

    def _on_wheel(self, event, direction: int = 0) -> None:
        step = direction if direction else (1 if getattr(event, 'delta', 0) > 0 else -1)
        if self.linked.get():
            self.app._zoom(-6 * step)
        else:
            self.view.fov = clamp(self.view.fov - 6 * step, FOV_MIN, FOV_MAX)
            self.request_render(interactive=True)

    def _on_double(self, event) -> None:
        if self._hotspot_at(event.x, event.y) is not None:
            return
        view = self._frame_view or self.view
        f = view.focal()
        dyaw = math.degrees(math.atan2(event.x - view.width / 2.0, f))
        dpitch = math.degrees(math.atan2(event.y - view.height / 2.0, f))
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

    # 6. Deplacement d'une pastille : ecran -> sol -> ecran
    print("\n6) Déplacement d'une pastille (écran ↔ sol)")
    cal = Calib('colonne', 1, 0.0)
    worst_az = worst_d = 0.0
    for view in (View(35.0, -22.0, 100.0, 1280, 720), View(-140.0, -35.0, 70.0, 900, 900)):
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
            check("colonne ajoutée en fin d'en-tête",
                  dst_lines[0] == src_lines[0] + ';' + YAW_COLUMN, dst_lines[0][-40:])
            check("les autres colonnes ne bougent pas",
                  all(b.rsplit(';', 1)[0] == a for a, b in
                      zip(src_lines[1:], dst_lines[1:])
                      if not b.rsplit(';', 1)[0].startswith(
                          tuple(x.photo for x in sts if x.modified()))))

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
                       and st.parts().etage in st.floor.replace('PLANCHER ', '')[:2])
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
