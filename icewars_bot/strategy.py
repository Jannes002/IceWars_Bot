from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .state import BuildingInfo, Capacity, GameState, ResearchItem
from . import goals as G
from . import cooldown

logger = logging.getLogger(__name__)


@dataclass
class Action:
    type: str
    params: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return f"Action({self.type}, {self.params})"


# ═══════════════════════════════════════════════════════════════════════════════
#  Konstanten & Schwellwerte
# ═══════════════════════════════════════════════════════════════════════════════

STORAGE_THRESHOLD = 0.80  # Lager bauen wenn Füllstand ≥ 80 %

# Bevölkerung: freie Siedler als Anteil von population_max
POP_FREE_MIN = 0.20  # Unter 20 % → Wohnraum bauen
POP_FREE_MAX = 0.40  # Über 40 % → kein weiterer Wohnraum

# Zufriedenheit / Umwelt
SATISFACTION_MIN = 0.0   # Unter 0 % → sofort gegensteuern
SATISFACTION_WARN = 0.50  # Unter 50 % → vorsorglich Zufriedenheits-Gebäude bauen

# Credits: wenn Rate negativ und Bestand < N → warnen / keine teuren Gebäude
CREDITS_WARN_BALANCE = 50.0

# ── Scoring-Parameter (Bauzeit + Diversifikation) ─────────────────────────────
# Die Bauzeit eines Gebäudetyps wächst serverseitig stark mit der Anzahl bereits
# gebauter Exemplare. Eine reine benefit/time-Formel bestraft deshalb die
# nächste Stufe eines häufig gebauten Gebäudes übermäßig stark und bleibt
# gleichzeitig bei kleinen, bereits überbauten Typen "kleben".
#
# Lösung: abgeflachte Zeit + Diversifikations-Strafe.
#   score = (benefit / build_time_sec ** TIME_ALPHA)
#           * (1 / (1 + count / DIVERSIFY_K))
#
# - TIME_ALPHA < 1.0 → lange Bauzeiten wirken milder im Nenner,
#   damit hochwertige Gebäude mit langer Bauzeit fair mitverglichen werden.
# - DIVERSIFY_K  "weicher" Count-Faktor → bei count==K halbiert sich der Score,
#   sodass der Bot auf frische, noch selten gebaute Alternativen umschwenkt,
#   sobald deren absoluter Nutzen konkurrenzfähig wird.
SCORE_TIME_ALPHA: float = 0.7
SCORE_DIVERSIFY_K: float = 3.0

# ── Ressource → Lagergebäude (Legacy-Fallback: wenn Live-API keine Daten liefert)
# Priorität: kleinstes vor größerem/Bunker (werden nach Verfügbarkeit gewählt).
STORAGE_BUILDINGS: dict[str, tuple[str, str]] = {
    "iron":      ("iron_storage_small",  "Eisenlager"),
    "steel":     ("steel_storage_small", "Stahllager"),
    "chemicals": ("chem_storage_small",  "Chemielager"),
    "ice":       ("ice_water_storage",   "Eis-/Wasserlager"),
    "water":     ("ice_water_storage",   "Eis-/Wasserlager"),
    "energy":    ("energy_storage",      "Energielager"),
    "vv4a":      ("vv4a_storage_small",  "VV4A-Lager"),
}

# ── Ressource → Bunkergebäude ─────────────────────────────────────────────────
BUNKER_BUILDINGS: dict[str, tuple[str, str]] = {
    "iron":      ("iron_bunker",      "Eisenbunker"),
    "steel":     ("steel_bunker",     "Stahlbunker"),
    "chemicals": ("chem_bunker",      "Chemiebunker"),
    "ice":       ("ice_water_bunker", "Eis-/Wasserbunker"),
    "water":     ("ice_water_bunker", "Eis-/Wasserbunker"),
    "energy":    ("energy_bunker",    "Energiebunker"),
    "vv4a":      ("vv4a_bunker",      "VV4A-Bunker"),
}

# ── Ressource → Bunker-Effekt-Key in next_level_effect ────────────────────────
# Wird in _check_bunker genutzt um das beste Bunkergebäude via _pick_best zu
# finden. Die Keys entsprechen den tatsächlichen Feldern im Live-API-Response
# (nicht "_bunker_capacity" wie fälschlicherweise früher angenommen).
_BUNKER_EFFECT_KEYS: dict[str, str] = {
    "iron":      "iron_bunker",
    "steel":     "steel_bunker",
    "chemicals": "chemicals_bunker",   # Achtung: Gebäudetyp heißt "chem_bunker"
    "ice":       "ice_bunker",         # ice_water_bunker hat BEIDE Keys (ice+water)
    "water":     "water_bunker",
    "energy":    "energy_bunker",
    "vv4a":      "vv4a_bunker",
}

# ── Ressource → Produktionsgebäude (Legacy-Fallback für negative Rate) ───────
PRODUCTION_BUILDINGS: dict[str, tuple[str, str]] = {
    "iron":      ("iron_mine_small",       "Kleine Eisenmine"),
    "steel":     ("steel_works_small",     "Kleines Stahlwerk"),
    "chemicals": ("chem_factory_small",    "Kleine Chemiefabrik"),
    "ice":       ("ice_crusher_design",    "Eiscrusher Design"),
    "water":     ("tauchsieder",           "Tauchsieder MK IV"),
    "energy":    ("solar_panels",          "Solarplatten"),
    "vv4a":      ("vv4a_works",            "VV4A-Werk"),
    "fp":        ("research_lab",          "Forschungslabor"),
}

# ── Wohngebäude (bestes zuerst — für den Legacy-Fallback) ────────────────────
# Live-Daten aus /api/city/ (139 Gebäude, Stand 2026-04):
# villa_complex=20000, settlement_complex=10000, orbital_habitat=6000,
# clone_center=5000, house_large=2500, gasgiant_housing=2500,
# pop_center=1200, asteroid_platform=600, house_medium=300,
# biosphere=200, asteroid_housing=120, house_small=60, tent=15
HOUSING_BUILDINGS: list[tuple[str, str, int]] = [
    # (building_type, name, pop_added) — absteigend nach Siedlerzahl sortiert.
    ("villa_complex",       "Villenkomplex",                 20000),
    ("settlement_complex",  "Siedlungskomplex",              10000),
    ("orbital_habitat",     "Orbitales Habitat",              6000),
    ("clone_center",        "Klonzentrum",                    5000),
    ("house_large",         "Großes Wohnhaus",                2500),
    ("gasgiant_housing",    "Gasgiganten-Wohnanlage",         2500),
    ("pop_center",          "Bevölkerungszentrum",            1200),
    ("asteroid_platform",   "Asteroiden-Wohnplatf.",           600),
    ("house_medium",        "Mittleres Wohnhaus",              300),
    ("biosphere",           "Biosphäre",                       200),
    ("asteroid_housing",    "Asteroiden-Wohnanlage",           120),
    ("house_small",         "Kleines Wohnhaus",                 60),
    ("tent",                "Zelt",                             15),
]

# ── Zufriedenheits-Gebäude (bestes zuerst — für den Legacy-Fallback) ─────────
# Live-Daten: pizza_large=0.15, spoon_monument=0.15, tavern=0.10,
# city_center=0.10, beauty_salon=0.08, teen_disco=0.06, park=0.05,
# statue=0.05, biosphere=0.05, university=0.05, scout_camp=0.05,
# pizza_small=0.05, waffle_stand=0.04, asylum=0.02, flower_beds=0.02,
# district_heat=0.01, outhouse=0.01
HAPPINESS_BUILDINGS: list[tuple[str, str, float]] = [
    # (building_type, name, satisfaction_api_value * 100)
    ("pizza_large",     "5-Sterne Pizzalokal",  15.0),
    ("spoon_monument",  "Löffelmonument",        15.0),
    ("tavern",          "Taverne",               10.0),
    ("city_center",     "Stadtzentrum",          10.0),
    ("beauty_salon",    "Schönheitssalon",        8.0),
    ("teen_disco",      "Teen-Disco",             6.0),
    ("biosphere",       "Biosphäre",              5.0),
    ("park",            "Park",                   5.0),
    ("statue",          "Statue",                 5.0),
    ("university",      "Universität",            5.0),
    ("scout_camp",      "Pfadfindercamp",         5.0),
    ("pizza_small",     "24h Pizza",              5.0),
    ("waffle_stand",    "Waffelstand",            4.0),
    ("headquarters",    "Hauptquartier",          3.0),
    ("asylum",          "Irrenanstalt",           2.0),
    ("flower_beds",     "Blumenrabatte",          2.0),
    ("outhouse",        "Plumpsklo",              1.0),
    ("district_heat",   "Fernwärmekraftwerk",     1.0),
]

# ── Gebäudetyp → Anzeigename (für Build-Queue-Anzeige und neue-Gebäude-Erkennung) ─
# Vollständige Liste aller 139 Live-Gebäude (Stand 2026-04, /api/city/).
# Hinweis: Gebäude die hier NICHT stehen, lösen eine Telegram-Meldung aus.
BUILDING_NAMES: dict[str, str] = {
    # ── Wohngebäude ─────────────────────────────────────────────────────────
    "tent":                  "Zelt",
    "house_small":           "Kleines Wohnhaus",
    "house_medium":          "Mittleres Wohnhaus",
    "house_large":           "Großes Wohnhaus",
    "pop_center":            "Bevölkerungszentrum",
    "settlement_complex":    "Siedlungskomplex",
    "villa_complex":         "Villenkomplex",
    "clone_center":          "Klonzentrum",
    "orbital_habitat":       "Orbitales Habitat",
    "asteroid_housing":      "Asteroiden-Wohnanlage",
    "asteroid_platform":     "Asteroiden-Wohnplatf.",
    "gasgiant_housing":      "Gasgiganten-Wohnanlage",
    # ── Soziales / Zufriedenheit ────────────────────────────────────────────
    "outhouse":              "Plumpsklo",
    "park":                  "Park",
    "flower_beds":           "Blumenrabatte",
    "asylum":                "Irrenanstalt",
    "scout_camp":            "Pfadfindercamp",
    "tavern":                "Taverne",
    "beauty_salon":          "Schönheitssalon",
    "teen_disco":            "Teen-Disco",
    "pizza_small":           "24h Pizza",
    "pizza_large":           "5-Sterne Pizzalokal",
    "waffle_stand":          "Waffelstand",
    "statue":                "Statue",
    "spoon_monument":        "Löffelmonument",
    # ── Energie ─────────────────────────────────────────────────────────────
    "solar_panels":          "Solarplatten",
    "solar_plant":           "Solarkraftwerk",
    "combustion_plant":      "Verbrennungskraftwerk",
    "district_heat":         "Fernwärmekraftwerk",
    "fuel_cell":             "Brennstoffzelle",
    "nuclear_plant":         "Atomkraftwerk",
    "fusion_plant":          "Fusionskraftwerk",
    "orbital_solar":         "Orbitales Solarkraftwerk",
    # ── Produktion ──────────────────────────────────────────────────────────
    "iron_mine_small":       "Kleine Eisenmine",
    "iron_mine_large":       "Große Eisenmine",
    "iron_complex":          "Eisenkomplex",
    "asteroid_mine":         "Asteroidenmine",
    "moon_mine":             "Mondbergwerk",
    "steel_works_small":     "Kleines Stahlwerk",
    "steel_works_large":     "Großes Stahlwerk",
    "steel_complex":         "Kleiner Stahlkomplex",
    "chem_factory_small":    "Kleine Chemiefabrik",
    "chem_factory_large":    "Große Chemiefabrik",
    "chem_complex":          "Chemiekomplex",
    "tauchsieder":           "Tauchsieder MK IV",
    "water_plant":           "Wasserwerk",
    "distillery":            "Destillationsanlage",
    "ice_crusher_design":    "Eiscrusher Design",
    "ice_crusher_large":     "Großer Eiscrusher",
    "crusher_v3a":           "Crusher V3A",
    "crusher_v3b":           "Crusher V3B",
    "vv4a_works":            "VV4A-Werk",
    "vv4a_works_large":      "Großes VV4A-Werk",
    "biosphere":             "Biosphäre",
    # ── Forschung ───────────────────────────────────────────────────────────
    "research_lab":          "Forschungslabor",
    "research_complex":      "Forschungskomplex",
    "university":            "Universität",
    "library":               "Intergalaktische Bibliothek",
    "supercomputer":         "Supercomputer",
    "quantum_computer":      "Quantencomputer",
    "orbital_lab":           "Orbitales Labor",
    "underground_lab":       "Unterirdisches Forschkomplex",
    "volcano_lab":           "Vulkanlabor",
    # ── Lager (regulär) ─────────────────────────────────────────────────────
    "iron_storage_small":    "Eisenlager",
    "iron_storage_large":    "Großes Eisenlager",
    "steel_storage_small":   "Stahllager",
    "steel_storage_large":   "Großes Stahllager",
    "chem_storage_small":    "Chemielager",
    "chem_storage_lg":       "Großes Chemielager",
    "ice_water_storage":     "Eis-/Wasserlager",
    "energy_storage":        "Energielager",
    "energy_storage_lg":     "Großes Energielager",
    "vv4a_storage_small":    "VV4A-Lager",
    "vv4a_storage_large":    "Großes VV4A-Lager",
    # ── Lager (Bunker) ───────────────────────────────────────────────────────
    "iron_bunker":           "Eisenbunker",
    "steel_bunker":          "Stahlbunker",
    "chem_bunker":           "Chemiebunker",
    "ice_water_bunker":      "Eis-/Wasserbunker",
    "energy_bunker":         "Energiebunker",
    "vv4a_bunker":           "VV4A-Bunker",
    "pop_bunker":            "Bevölkerungsbunker",
    # ── Infrastruktur ───────────────────────────────────────────────────────
    "city_center":           "Stadtzentrum",
    "headquarters":          "Hauptquartier",
    "colonize_center":       "Kolonisierungszentrum",
    "comm_small":            "Kleine Kommunikationsanlage",
    "comm_large":            "Große Kommunikationsanlage",
    "recycler":              "Recycler",
    "orbital_recycler":      "Orbitaler Recycler",
    "robotics":              "Roboterzentrale",
    "structural":            "Strukturbauprogramm",
    "terraforming":          "Terraforming",
    "orbital_lift":          "Orbitaler Aufzug",
    # ── Wirtschaft / Economy ────────────────────────────────────────────────
    "bank":                  "Bank",
    "trade_company":         "Handelsgesellschaft",
    "commerce_temple":       "Kommerztempel",
    "sirius_corp":           "Sirius Corporation",
    "imperial_tax":          "Imperiale Steuerbehörde",
    "gold_mine":             "Goldmine",
    "spaceport":             "Planetarer Raumhafen",
    "bureaucracy":           "Verwaltungskomplex",
    # ── Militär ─────────────────────────────────────────────────────────────
    "telescope":             "Orbitales Teleskop",
    "long_telescope":        "Lang-Teleskop",
    "deep_telescope":        "Tiefenteleskop",
    "observatory":           "Sternenwarte",
    "base_scanner":          "Planetarer Basisscanner",
    "fleet_scanner":         "Flottenscanner",
    "orbital_scanner":       "Orbitaler Galaxiescanner",
    "fog_machine":           "Nebelmaschine",
    "fleet_control":         "Flottenkontrollzentrum",
    "km_defence_fab":        "K&M Abwehr Fabrik",
    # ── Verteidigung ────────────────────────────────────────────────────────
    "defence_position":      "Verteidigungsstellung",
    "kb_alpha":              "Kampfbasis Alpha",
    "shield_alpha":          "Planetarschild Alpha",
    "shield_beta":           "Planetarschild Beta",
    "mass_driver":           "Massentreiber",
    "moon_defence":          "Mondverteidigung",
    "combat_control":        "Kampfbasiskontrolle",
    "planetary_armor":       "Panzer-Update Planetar",
    "orbital_armor":         "Panzer-Update Orbital",
    "base_def_orbital":      "Orb. Basenverteidigung",
    "orb_def_control":       "Orb. Verteidigungskontrolle",
    "orb_def_coord":         "Orb. Verteidigungskoordinator",
    # ── Werften ─────────────────────────────────────────────────────────────
    "launch_pad":            "Startrampe",
    "shipyard_plan_small":   "Kleine Planetarwerft",
    "shipyard_plan_mid":     "Mittlere Planetarwerft",
    "shipyard_large":        "Große Werft",
    "shipyard_orb_small":    "Kleine Orbitalwerft",
    "shipyard_orb_mid":      "Mittlere Orbitalwerft",
    "shipyard_dn":           "DN-Werft",
    "shipyard_complex":      "Orbitaler Werftkomplex",
    # ── Shop-Pakete (nicht baubar, nur zur Erkennung) ───────────────────────
    "kit_iron":              "Eisen-Hilfspaket",
    "kit_steel":             "Stahl-Hilfspaket",
    "kit_chem":              "Chemie-Hilfspaket",
    "kit_ice":               "Eis-Hilfspaket",
    "kit_water":             "Wasser-Paket",
    "kit_energy":            "Energie-Brennstäbe",
    "kit_lurch":             "Lurch-Paket",
    "kit_scout":             "Scout-Paket",
    "kit_systrans":          "Systrans-Paket",
}

def building_display_name(btype: str, fallback: str = "") -> str:
    """Gibt den deutschen Anzeigenamen eines Gebäudetyps zurück."""
    return BUILDING_NAMES.get(btype, fallback or btype)

# ── Forschungspriorität ───────────────────────────────────────────────────────
RESEARCH_PRIORITY: list[str] = [
    "verbesserte_eisenfoerderung",
    "verbesserte_solarenergie",
    "verbesserter_wasserabbau",
    "chemieprozesse",
    "metallurgie",
    "kuehlsysteme",
    "lagerbau",
    "staedte_bau",
    "staedte_bau2",
    "forschungsmethoden",
    "grundlegende_raketentechnik",
    "raumfahrt",
    "fortgeschrittene_raumfahrt",
    "astronomie",
]


def _research_priority(item: ResearchItem) -> int:
    try:
        return RESEARCH_PRIORITY.index(item.type)
    except ValueError:
        unlocks = len(item.unlocks_buildings) + len(item.unlocks_ships)
        return len(RESEARCH_PRIORITY) + (100 - unlocks) * 1000 + int(item.fp_cost)


def _can_afford_research(item: ResearchItem, state: GameState, current_fp: Optional[float] = None) -> bool:
    """Prüft ob eine Forschung JETZT leistbar ist.

    Primär vertrauen wir auf ``ResearchItem.can_afford`` (Server-seitig
    berechnet und meistens aktuell). Zusätzlich akzeptieren wir ein Item,
    wenn sowohl ``fp_cost`` als auch alle ``res_cost``-Einträge mit dem
    aktuellen ``GameState`` abgedeckt sind — so überwinden wir einen
    eventuellen Scraper-Cache-Stale bei dem can_afford=False fälschlich
    gesetzt war.
    """
    if item.can_afford:
        return True
    if current_fp is None:
        current_fp = float(state.resources.fp)
    if float(item.fp_cost) > current_fp + 1e-6:
        return False
    for resource, amount in (item.res_cost or {}).items():
        have = float(getattr(state.resources, resource, 0))
        if float(amount) > have + 1e-6:
            return False
    return True


def _missing_research_resources(item: ResearchItem, state: GameState) -> list[str]:
    """Liefert eine Liste 'resource(need/have)' für nicht ausreichende Ressourcen."""
    missing: list[str] = []
    for resource, amount in (item.res_cost or {}).items():
        have = float(getattr(state.resources, resource, 0))
        need = float(amount)
        if need > have + 1e-6:
            missing.append(f"{resource}({need:.0f}/{have:.0f})")
    return missing


# ═══════════════════════════════════════════════════════════════════════════════
#  Scoring-Engine: wählt das beste Gebäude einer Kategorie anhand von
#  Nutzen, Bauzeit und Ressourcen-Kosten aus den Live-API-Daten.
# ═══════════════════════════════════════════════════════════════════════════════

# Gewicht für Ressourcen-Kosten im Score (kleiner = Bauzeit dominiert stärker).
# Formel: score = nutzen / max(bauzeit_s, 1)  (Primärer Faktor ist Nutzen/Zeit)
# Kosten werden nur als Tiebreaker benutzt, da `can_afford` schon filtert.

def _categories_for_resource_rate() -> tuple[str, ...]:
    """Kategorien, in denen Ressourcen-Produktionsgebäude liegen können.

    Infrastructure enthält Sonder-Gebäude (market für credits_rate, school/
    university für research/fp, hospital-verwandte etc.), die ebenfalls
    Raten erhöhen und deshalb im Scoring berücksichtigt werden sollen.
    """
    return ("production", "energy", "economy", "research", "infrastructure")


# ── Mapping: logischer Ressourcenname → API-Effect-Key in Buildings ───────────
_EFFECT_KEY_MAP: dict[str, str] = {
    "credits": "credits_rate",
}


def _effect_key_for(resource: str) -> str:
    """Gibt den korrekten Effect-Key für eine Ressource zurück."""
    return _EFFECT_KEY_MAP.get(resource, f"{resource}_rate")


def _filter_buildable(
    buildings: list[BuildingInfo],
    *,
    categories: Optional[tuple[str, ...]] = None,
) -> list[BuildingInfo]:
    """Liefert nur Gebäude die JETZT gebaut werden können (Ressourcen, Voraussetzungen,
    Planeten-Typ, kein aktiver Cooldown)."""
    out: list[BuildingInfo] = []
    for b in buildings:
        if categories is not None and b.category not in categories:
            continue
        if not b.is_buildable:
            continue
        if b.build_time_sec <= 0:
            continue
        if cooldown.is_on_cooldown(b.type):
            continue
        out.append(b)
    return out


def _score_building_benefit(b: BuildingInfo, benefit: float) -> float:
    """Bewertet ein Gebäude anhand eines bekannten Nutzens (benefit).

    Kombiniert abgeflachte Bauzeit (TIME_ALPHA) mit einer Diversifikations-
    Strafe, die mit ``b.count`` wächst. Siehe Modul-Dokumentation der
    Scoring-Parameter oben.
    """
    if benefit <= 0 or b.build_time_sec <= 0:
        return 0.0
    time_weight = max(float(b.build_time_sec), 1.0) ** SCORE_TIME_ALPHA
    diversification = 1.0 / (1.0 + float(max(b.count, 0)) / SCORE_DIVERSIFY_K)
    return (benefit / time_weight) * diversification


def _score_benefit_per_second(b: BuildingInfo, effect_key: str) -> float:
    """Score eines Gebäudes anhand eines Effekt-Keys aus ``next_level_effect``.

    Gibt 0 zurück wenn das Gebäude diesen Effekt nicht hat oder die Bauzeit
    fehlt. Verwendet die neue kombinierte Formel (Bauzeit-Abflachung +
    Diversifikation).
    """
    benefit = float(b.next_level_effect.get(effect_key, 0))
    return _score_building_benefit(b, benefit)


def build_scoring_snapshot(state: GameState, *, limit: int = 25) -> list[dict[str, Any]]:
    """Erzeugt eine nach Score sortierte Liste aller baubaren Gebäude.

    Für das Dashboard: zeigt pro Gebäude den besten Effect-Key (z. B.
    iron_rate, population_max, satisfaction, <resource>_capacity) und den
    mit der neuen Formel berechneten Score — nachvollziehbar, warum ein
    Gebäude gerade bevorzugt wird.
    """
    if not state.buildings:
        return []

    # Kandidaten-Effekt-Keys pro Kategorie
    rate_keys = [
        ("iron", "iron_rate"),
        ("steel", "steel_rate"),
        ("chemicals", "chemicals_rate"),
        ("ice", "ice_rate"),
        ("water", "water_rate"),
        ("energy", "energy_rate"),
        ("vv4a", "vv4a_rate"),
        ("fp", "fp_rate"),
        ("credits", "credits_rate"),
    ]
    cap_keys = [
        ("iron", "iron_capacity"),
        ("steel", "steel_capacity"),
        ("chemicals", "chemicals_capacity"),
        ("ice", "ice_capacity"),
        ("water", "water_capacity"),
        ("energy", "energy_capacity"),
        ("vv4a", "vv4a_capacity"),
    ]

    out: list[dict[str, Any]] = []
    for b in _filter_buildable(state.buildings):
        best_score = 0.0
        best_label = ""
        best_benefit = 0.0
        best_key = ""
        # Raten
        for label, key in rate_keys:
            v = float(b.next_level_effect.get(key, 0))
            if v <= 0:
                continue
            s = _score_building_benefit(b, v)
            if s > best_score:
                best_score, best_label, best_benefit, best_key = s, f"+{v:.1f} {label}/h", v, key
        # Bevölkerung
        pop = float(b.next_level_effect.get("population_max", 0))
        if pop > 0:
            s = _score_building_benefit(b, pop)
            if s > best_score:
                best_score, best_label, best_benefit, best_key = s, f"+{pop:.0f} Siedler", pop, "population_max"
        # Zufriedenheit (Werte liegen als 0..1 vor → ×100 für Vergleich/Lesbarkeit)
        sat = float(b.next_level_effect.get("satisfaction", 0))
        if sat > 0:
            s = _score_building_benefit(b, sat * 100)
            if s > best_score:
                best_score, best_label, best_benefit, best_key = s, f"+{sat*100:.1f}% Zufriedenheit", sat * 100, "satisfaction"
        # Lagerkapazität
        for label, key in cap_keys:
            v = float(b.next_level_effect.get(key, 0))
            if v <= 0:
                continue
            s = _score_building_benefit(b, v)
            if s > best_score:
                best_score, best_label, best_benefit, best_key = s, f"+{v:.0f} {label}-Lager", v, key

        if best_score <= 0:
            continue

        out.append({
            "type": b.type,
            "name": b.name or BUILDING_NAMES.get(b.type, b.type),
            "category": b.category,
            "count": int(b.count),
            "level": int(b.level),
            "build_time_sec": int(b.build_time_sec),
            "effect_key": best_key,
            "effect_label": best_label,
            "benefit": round(best_benefit, 3),
            "score": round(best_score, 6),
        })

    out.sort(key=lambda r: r["score"], reverse=True)
    return out[:limit]


def _pick_best(
    buildings: list[BuildingInfo],
    *,
    effect_key: str,
    categories: tuple[str, ...],
) -> Optional[tuple[BuildingInfo, float]]:
    """Wählt das Gebäude mit dem höchsten Nutzen/Sekunde für ``effect_key``.

    Gibt ``(building, score)`` zurück oder ``None`` wenn kein passender
    Kandidat verfügbar ist.
    """
    candidates: list[tuple[float, BuildingInfo]] = []
    for b in _filter_buildable(buildings, categories=categories):
        score = _score_benefit_per_second(b, effect_key)
        if score > 0:
            candidates.append((score, b))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    score, best = candidates[0]
    return best, score


# ═══════════════════════════════════════════════════════════════════════════════
#  Strategy
# ═══════════════════════════════════════════════════════════════════════════════

class Strategy:
    """Regelbasierte Kolonie-Entwicklungsstrategie.

    Prioritäten (höchste zuerst):
    1. Zufriedenheit retten (kritisch < 0 %)
    2. NEGATIVE TENDENZ FIXEN — jede Ressource/FP mit Rate ≤ 0 → Produktionsbau
    3. Zufriedenheit vorsorglich (< warn-Schwelle)
    4. Bevölkerung sichern (freie Siedler < 20 % → Wohnraum bauen)
    5. Lager erweitern (Ressource ≥ 80 % der Kapazität)
    6. Normaler Gebäudebau (Produktion steigern)
    7. Forschung starten
    """

    def __init__(self, config: Config) -> None:
        self._config = config

    # ──────────────────────────────────────────────────────────────────────
    #  Hauptentscheidung
    # ──────────────────────────────────────────────────────────────────────

    def decide(self, state: GameState) -> list[Action]:
        actions: list[Action] = []

        self._log_overview(state)

        free_slots = state.max_build_slots - len(state.build_queue)

        if free_slots > 0:
            build_action = self._decide_build(state)
            if build_action:
                actions.append(build_action)
        else:
            logger.info(
                "Alle Bauslots belegt (%d/%d).",
                len(state.build_queue), state.max_build_slots,
            )

        # Forschung (unabhängig von Bauslots)
        research_action = self._decide_research(state)
        if research_action:
            actions.append(research_action)

        if not actions:
            logger.info("Nichts zu tun — warte auf nächste Runde.")

        logger.info("Geplante Aktionen: %s", [str(a) for a in actions])
        return actions

    # ──────────────────────────────────────────────────────────────────────
    #  Bau-Priorisierung
    # ──────────────────────────────────────────────────────────────────────

    def _decide_build(self, state: GameState) -> Optional[Action]:
        """Entscheidet was gebaut wird — in Prioritätsreihenfolge."""

        # Ziele live aus goals.py einlesen
        sat_critical = G.satisfaction_critical()
        sat_warn     = G.satisfaction_warn()
        pop_min      = G.pop_free_min()

        # 1) KRITISCH: Zufriedenheit retten
        if state.satisfaction < sat_critical:
            action = self._build_happiness(state, reason="KRITISCH: Zufriedenheit unter 0 %!")
            if action:
                return action

        # 2) NEGATIVE TENDENZ — jede Ressource/FP die schrumpft sofort fixen
        neg_action = self._fix_negative_rate(state)
        if neg_action:
            return neg_action

        # 3) WARNUNG: Zufriedenheit niedrig
        if state.satisfaction < sat_warn:
            action = self._build_happiness(state, reason=f"Zufriedenheit niedrig ({state.satisfaction*100:.0f}%)")
            if action:
                return action

        # 4) Bevölkerung zu wenig freie Siedler?
        if state.free_pop_ratio < pop_min:
            action = self._build_housing(state)
            if action:
                return action

        # 5) Lager voll?
        storage_action = self._check_storage(state)
        if storage_action:
            return storage_action

        # 5b) Bunker zu klein?
        bunker_action = self._check_bunker(state)
        if bunker_action:
            return bunker_action

        # 6) Normaler Gebäudebau — beste Ressourcenproduktion steigern
        action = self._build_best_growth(state)
        if action:
            return action

        # 7) Ultimativer Fallback: irgendein baubares Gebäude (alter Pfad)
        logger.info(
            "%d freie Bauslot(s) — nutze Fallback (erstes 'Bauen').",
            state.max_build_slots - len(state.build_queue),
        )
        return Action("build_next_building", {"aggression": self._config.strategy.aggression})

    def _build_best_growth(self, state: GameState) -> Optional[Action]:
        """Wählt im Normalfall das Gebäude mit der höchsten Effizienz aus.

        Berücksichtigt priority_resource aus den Zielen:
        - "balanced" → 3 schwächste Ressourcen vergleichen (Standardverhalten)
        - "iron"/"fp"/… → nur Gebäude für diese Ressource suchen

        Präferiert Produktions-Ressourcen mit niedrigen Beständen / niedrigen
        Raten — sonst einfach das produktivste Gebäude (beliebiger Ressource)
        pro Sekunde Bauzeit.
        """
        if not state.buildings:
            return None

        priority = G.priority_resource()

        # Wenn die gewählte Priorität pausiert ist, fällt der Modus auf
        # "balanced" zurück — sonst würde der Bot gar nichts mehr bauen.
        if priority != "balanced" and G.is_resource_paused(priority):
            logger.info(
                "Priorität '%s' ist pausiert — falle auf balanced zurück.",
                priority,
            )
            priority = "balanced"

        # ── Prioritäts-Modus: nur die gewählte Ressource fördern ──────────
        if priority != "balanced" and priority in (
            "iron", "steel", "chemicals", "ice", "water",
            "energy", "vv4a", "fp", "credits",
        ):
            eff_key = _effect_key_for(priority)
            picked = _pick_best(
                state.buildings,
                effect_key=eff_key,
                categories=_categories_for_resource_rate(),
            )
            if picked:
                best, score = picked
                rate_gain = float(best.next_level_effect.get(eff_key, 0))
                logger.info(
                    "Prioritätsbau (%s): '%s' (+%.1f/h, %.0fs, Score=%.4f).",
                    priority, best.name, rate_gain, best.build_time_sec, score,
                )
                return Action("build_specific", {
                    "building_type": best.type,
                    "building_name": best.name,
                    "reason": f"Priorität: {priority} steigern",
                })
            logger.info("Priorität %s — kein baubares Gebäude, Fallback balanced.", priority)

        # ── Balanced-Modus: 3 schwächste Ressourcen vergleichen ───────────
        # Pausierte Ressourcen werden komplett übersprungen — der Bot zieht
        # dann eben eine der anderen schwachen Ressourcen.
        weak_resources: list[tuple[float, str]] = []
        for resource in ("iron", "steel", "chemicals", "ice", "water",
                         "energy", "vv4a", "fp"):
            if G.is_resource_paused(resource):
                continue
            rate = getattr(state.rates, resource, 0)
            if rate > 0:
                weak_resources.append((rate, resource))
        weak_resources.sort()  # schwächste zuerst

        best_overall: Optional[tuple[float, BuildingInfo, str]] = None
        for _, resource in weak_resources[:3]:  # nur die 3 schwächsten ansehen
            picked = _pick_best(
                state.buildings,
                effect_key=_effect_key_for(resource),
                categories=_categories_for_resource_rate(),
            )
            if not picked:
                continue
            b, score = picked
            if best_overall is None or score > best_overall[0]:
                best_overall = (score, b, resource)

        if best_overall is None:
            return None

        score, best, resource = best_overall
        rate_gain = float(best.next_level_effect.get(_effect_key_for(resource), 0))
        logger.info(
            "Produktionsbau: '%s' (+%.1f %s/h, %.0fs, Score=%.4f).",
            best.name, rate_gain, resource, best.build_time_sec, score,
        )
        return Action("build_specific", {
            "building_type": best.type,
            "building_name": best.name,
            "reason": f"Produktion {resource} steigern",
        })

    # ──────────────────────────────────────────────────────────────────────
    #  Negative Tendenz fixen (Top-Priorität)
    # ──────────────────────────────────────────────────────────────────────

    def _fix_negative_rate(self, state: GameState) -> Optional[Action]:
        """Wenn eine Ressourcen-Rate ≤ 0 ist → bestes Produktionsgebäude bauen.

        Reihenfolge: schlimmste (negativste) Rate zuerst. Das beste Gebäude
        pro Ressource wird anhand der Live-API-Daten gewählt (Nutzen/Sekunde,
        gefiltert nach Cooldown, Voraussetzungen, leistbar).
        """
        # Keine Live-Daten → alter Pfad (Fallback für Tests / DOM-Probleme)
        if not state.buildings:
            return self._fix_negative_rate_legacy(state)

        # Alle Ressourcen + FP prüfen, sortiert nach Rate (negativste zuerst).
        # Pausierte Ressourcen werden übersprungen — der Bot soll für sie
        # keine Produktionsgebäude mehr anstoßen, selbst wenn die Rate fällt.
        resources = ("iron", "steel", "chemicals", "ice", "water",
                     "energy", "vv4a", "fp", "credits")
        check: list[tuple[float, str]] = []
        for resource in resources:
            if G.is_resource_paused(resource):
                continue
            rate = getattr(state.rates, resource, 0)
            if rate <= 0:
                check.append((rate, resource))
        if not check:
            return None
        check.sort()

        for rate, resource in check:
            rate_key = _effect_key_for(resource)
            picked = _pick_best(
                state.buildings,
                effect_key=rate_key,
                categories=_categories_for_resource_rate(),
            )
            if not picked:
                logger.info(
                    "Negative %s-Rate (%+.1f/h) — kein baubares %s-Gebäude verfügbar.",
                    resource, rate, resource,
                )
                continue
            best, score = picked
            logger.warning(
                "Negative %s-Rate (%+.1f/h) → baue '%s' "
                "(+%.1f %s/Bau, %.0fs, Score=%.4f).",
                resource, rate, best.name,
                best.next_level_effect.get(rate_key, 0),
                resource, best.build_time_sec, score,
            )
            return Action("build_specific", {
                "building_type": best.type,
                "building_name": best.name,
                "reason": f"{resource} Rate {rate:+.1f}/h",
            })
        return None

    def _fix_negative_rate_legacy(self, state: GameState) -> Optional[Action]:
        """Fallback auf die alte hardcodierte Logik wenn keine Live-Daten da sind."""
        check: list[tuple[float, str]] = []
        for resource in PRODUCTION_BUILDINGS.keys():
            if G.is_resource_paused(resource):
                continue
            rate = getattr(state.rates, resource, 0)
            if rate <= 0:
                check.append((rate, resource))
        if not check:
            return None
        check.sort()
        queued_types = {q.building_type for q in state.build_queue}
        for rate, resource in check:
            btype, bname = PRODUCTION_BUILDINGS[resource]
            if btype in queued_types:
                continue
            if cooldown.is_on_cooldown(btype):
                continue
            logger.warning(
                "[Legacy] Negative Tendenz: %s = %+.1f/h → baue '%s'.",
                resource, rate, bname,
            )
            return Action("build_specific", {
                "building_type": btype,
                "building_name": bname,
                "reason": f"{resource} Rate {rate:+.1f}/h",
            })
        return None

    # ──────────────────────────────────────────────────────────────────────
    #  Zufriedenheit
    # ──────────────────────────────────────────────────────────────────────

    def _build_happiness(self, state: GameState, reason: str) -> Optional[Action]:
        """Baut das effizienteste Zufriedenheits-Gebäude (max. Satisfaction/Sekunde)."""
        logger.warning("Zufriedenheits-Check: %s", reason)

        credits_tight = (state.credits_rate < 0 and state.resources.credits < G.credits_warn_balance())

        if state.buildings:
            # Alle sozialen Gebäude mit positivem Zufriedenheits-Effekt
            candidates: list[tuple[float, BuildingInfo]] = []
            for b in _filter_buildable(state.buildings, categories=("social",)):
                benefit = float(b.next_level_effect.get("satisfaction", 0))
                if benefit <= 0:
                    continue
                # Credits-Kosten filter wenn knapp
                if credits_tight:
                    credits_cost = float(b.upgrade_cost.get("credits", 0))
                    credits_drain = float(b.next_level_effect.get("credits_rate", 0))
                    if credits_cost > 0 or credits_drain < 0:
                        logger.debug("'%s' — Credits knapp, überspringe.", b.name)
                        continue
                score = _score_building_benefit(b, benefit)
                if score > 0:
                    candidates.append((score, b))

            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                score, best = candidates[0]
                sat_gain = float(best.next_level_effect.get("satisfaction", 0)) * 100
                logger.info(
                    "Baue Zufriedenheits-Gebäude: '%s' (+%.1f%%, %.0fs, Score=%.5f) — %s",
                    best.name, sat_gain, best.build_time_sec, score, reason,
                )
                return Action("build_specific", {
                    "building_type": best.type,
                    "building_name": best.name,
                    "reason": f"Zufriedenheit ({state.satisfaction*100:.0f}%)",
                })
            logger.warning("Kein baubares Zufriedenheits-Gebäude in den Live-Daten.")
            return None

        # Legacy-Fallback
        for btype, bname, bonus in HAPPINESS_BUILDINGS:
            if credits_tight and btype in ("scout_camp", "park", "asylum"):
                continue
            if any(q.building_type == btype for q in state.build_queue):
                continue
            if cooldown.is_on_cooldown(btype):
                continue
            logger.info("[Legacy] Baue Zufriedenheits-Gebäude: '%s' (+%.0f%%) — %s", bname, bonus, reason)
            return Action("build_specific", {
                "building_type": btype,
                "building_name": bname,
                "reason": f"Zufriedenheit ({state.satisfaction*100:.0f}%)",
            })
        logger.warning("Kein Zufriedenheits-Gebäude verfügbar.")
        return None

    # ──────────────────────────────────────────────────────────────────────
    #  Bevölkerung
    # ──────────────────────────────────────────────────────────────────────

    def _build_housing(self, state: GameState) -> Optional[Action]:
        """Baut das effizienteste Wohngebäude (max. Siedler pro Sekunde Bauzeit).

        Nutzt Live-API-Daten wenn verfügbar — gewählt wird das Gebäude aus
        Kategorie ``housing`` mit dem höchsten ``population_max/Sekunde``.
        """
        pct = state.free_pop_ratio * 100
        logger.warning(
            "Bevölkerungs-Check: nur %.0f%% freie Siedler (%d/%d) — Soll: %d–%d%%",
            pct, state.population_free, state.population_max,
            int(G.pop_free_min() * 100), int(G.pop_free_max() * 100),
        )

        if state.buildings:
            picked = _pick_best(
                state.buildings,
                effect_key="population_max",
                categories=("housing",),
            )
            if picked:
                best, score = picked
                pop_gain = int(best.next_level_effect.get("population_max", 0))
                logger.info(
                    "Baue Wohngebäude: '%s' (+%d Siedler, %.0fs, Score=%.4f) "
                    "— Freie Bevölkerung zu niedrig.",
                    best.name, pop_gain, best.build_time_sec, score,
                )
                return Action("build_specific", {
                    "building_type": best.type,
                    "building_name": best.name,
                    "reason": f"Bevölkerung ({state.population_free}/{state.population_max} frei)",
                })
            logger.warning("Kein baubares Wohngebäude in den Live-Daten gefunden.")
            return None

        # Legacy-Fallback
        for btype, bname, pop_add in HOUSING_BUILDINGS:
            if any(q.building_type == btype for q in state.build_queue):
                continue
            if cooldown.is_on_cooldown(btype):
                continue
            logger.info("[Legacy] Baue Wohngebäude: '%s' (+%d)", bname, pop_add)
            return Action("build_specific", {
                "building_type": btype,
                "building_name": bname,
                "reason": f"Bevölkerung ({state.population_free}/{state.population_max} frei)",
            })
        logger.warning("Kein Wohngebäude verfügbar.")
        return None

    # ──────────────────────────────────────────────────────────────────────
    #  Lager
    # ──────────────────────────────────────────────────────────────────────

    def _check_storage(self, state: GameState) -> Optional[Action]:
        """Baut ein Lager wenn eine Ressource ≥ 80 % voll ist.

        Wählt das effizienteste Lager (max. Kapazität/Sekunde) aus der
        Kategorie ``storage`` anhand der Live-API-Daten.
        """
        if not G.is_auto_storage_enabled():
            logger.debug("Auto-Lager deaktiviert — überspringe.")
            return None
        # 1) Übervolle Ressourcen identifizieren (sortiert nach Füllstand).
        #    Pausierte Ressourcen werden hier bereits aussortiert — so löst
        #    z.B. ice=0.95 KEIN Lager mehr aus, wenn 'ice' pausiert ist.
        overflowing: list[tuple[float, str]] = []
        for resource in ("iron", "steel", "chemicals", "ice", "water", "energy", "vv4a"):
            if G.is_resource_paused(resource):
                continue
            ratio = state.capacity.fill_ratio(resource, state.resources)
            if ratio >= G.storage_threshold():
                overflowing.append((ratio, resource))
        if not overflowing:
            return None
        overflowing.sort(reverse=True)

        # Schon in der Bauwarteschlange stehende Lager NICHT erneut triggern —
        # sonst baut der Bot z.B. zweimal hintereinander das ice_water_storage,
        # obwohl der erste Bau noch läuft.
        queued_types = {item.building_type for item in state.build_queue}

        if state.buildings:
            # Für jede übervolle Ressource das beste Lager finden
            for ratio, resource in overflowing:
                cap_key = f"{resource}_capacity"
                picked = _pick_best(
                    state.buildings,
                    effect_key=cap_key,
                    categories=("storage",),
                )
                if not picked:
                    continue
                best, score = picked
                if best.type in queued_types:
                    logger.info(
                        "Lager '%s' schon in der Queue — überspringe %s-Trigger.",
                        best.type, resource,
                    )
                    continue
                cap_gain = int(best.next_level_effect.get(cap_key, 0))
                logger.warning(
                    "Lager-Alarm: %s %.0f%% voll → baue '%s' "
                    "(+%d %s-Kapazität, %.0fs, Score=%.4f).",
                    resource, ratio * 100, best.name, cap_gain,
                    resource, best.build_time_sec, score,
                )
                return Action("build_storage", {
                    "building_type": best.type,
                    "building_name": best.name,
                    "resource": resource,
                    "fill_ratio": round(ratio, 3),
                })
            logger.warning("Lager übervoll aber kein baubares Lager in den Live-Daten.")
            return None

        # Legacy-Fallback auf hardcodierte Liste
        for ratio, resource in overflowing:
            entry = STORAGE_BUILDINGS.get(resource)
            if not entry:
                continue
            btype, bname = entry
            if btype in queued_types:
                continue
            if cooldown.is_on_cooldown(btype):
                continue
            logger.warning(
                "[Legacy] Lager-Alarm: %s %.0f%% voll → '%s'",
                resource, ratio * 100, bname,
            )
            return Action("build_storage", {
                "building_type": btype,
                "building_name": bname,
                "resource": resource,
                "fill_ratio": round(ratio, 3),
            })
        return None

    # ──────────────────────────────────────────────────────────────────────
    #  Bunker
    # ──────────────────────────────────────────────────────────────────────

    def _check_bunker(self, state: GameState) -> Optional[Action]:
        """Baut einen Bunker wenn die Bunkerkapazität unter dem Schwellwert liegt.

        Der Schwellwert wird pro Ressource in den Goals als Anteil der
        Lagerkapazität definiert (z.B. 0.20 = 20 % der Lagerkapazität als Bunker).
        """
        thresholds = G.bunker_thresholds()
        queued_types = {item.building_type for item in state.build_queue}

        for resource, threshold in thresholds.items():
            if threshold <= 0.0:
                continue
            if G.is_resource_paused(resource):
                continue

            # Ziel-Bunkerkapazität = Lagerkapazität × Schwellwert
            storage_cap = getattr(state.capacity, resource, 0)
            if storage_cap <= 0:
                continue
            target_bunker = storage_cap * threshold

            # Aktuelle Bunkerkapazität aus state.bunker_capacity
            # Keys können resource-Name oder resource+"_bunker" sein
            current_bunker = 0.0
            bc = state.bunker_capacity
            if isinstance(bc, dict):
                current_bunker = float(
                    bc.get(resource, bc.get(f"{resource}_bunker", 0)) or 0
                )

            if current_bunker >= target_bunker:
                logger.debug(
                    "Bunker %s OK: %.0f/%.0f (Ziel: %.0f%%)",
                    resource, current_bunker, target_bunker, threshold * 100,
                )
                continue

            logger.info(
                "Bunker-Alarm %s: %.0f < %.0f (Ziel %.0f%% von %.0f Lagerkapazität)",
                resource, current_bunker, target_bunker, threshold * 100, storage_cap,
            )

            # Bunker-Gebäude aus Live-API finden
            if state.buildings:
                # Effekt-Key aus Mapping — z.B. "iron_bunker", "chemicals_bunker"
                # Bunker-Gebäude liegen in der Kategorie "storage" (nicht "bunker"!)
                eff_key = _BUNKER_EFFECT_KEYS.get(resource, f"{resource}_bunker")
                picked = _pick_best(
                    state.buildings,
                    effect_key=eff_key,
                    categories=("storage",),
                )
                if picked:
                    best, score = picked
                    if best.type not in queued_types:
                        return Action("build_specific", {
                            "building_type": best.type,
                            "building_name": best.name,
                            "reason": f"Bunker {resource} {current_bunker:.0f}/{target_bunker:.0f} (Ziel {threshold*100:.0f}%)",
                        })

            # Legacy-Fallback
            entry = BUNKER_BUILDINGS.get(resource)
            if entry:
                btype, bname = entry
                if btype not in queued_types and not cooldown.is_on_cooldown(btype):
                    return Action("build_specific", {
                        "building_type": btype,
                        "building_name": bname,
                        "reason": f"Bunker {resource} {current_bunker:.0f}/{target_bunker:.0f} (Ziel {threshold*100:.0f}%)",
                    })

        return None

    # ──────────────────────────────────────────────────────────────────────
    #  Forschung
    # ──────────────────────────────────────────────────────────────────────

    def _decide_research(self, state: GameState) -> Optional[Action]:
        if not G.is_auto_research_enabled():
            logger.debug("Auto-Forschung deaktiviert — überspringe.")
            return None
        if state.research_lab_busy:
            if state.active_research:
                logger.info(
                    "Labor belegt: '%s' (noch %ds).",
                    state.active_research.name,
                    state.active_research.remaining_sec,
                )
            return None

        chosen = self._pick_research(state)
        if not chosen:
            pending = [r for r in state.research if not r.is_researched and r.has_prereq]
            if not pending:
                logger.info("Alle verfügbaren Forschungen abgeschlossen.")
            else:
                too_expensive = [r for r in pending if not r.can_afford]
                logger.info(
                    "Keine leistbare Forschung. %d zu teuer: %s",
                    len(too_expensive),
                    ", ".join(f"{r.name} ({r.fp_cost:.0f} FP)" for r in too_expensive[:3]),
                )
            return None

        unlocks_info = ""
        if chosen.unlocks_buildings:
            unlocks_info = f" → schaltet frei: {', '.join(chosen.unlocks_buildings)}"
        elif chosen.unlocks_ships:
            unlocks_info = f" → schaltet frei: {', '.join(chosen.unlocks_ships)}"

        logger.info(
            "Starte Forschung: '%s' (FP: %.0f, Dauer: %ds)%s",
            chosen.name, chosen.fp_cost, chosen.time_sec, unlocks_info,
        )
        return Action("start_research", {
            "research_type": chosen.type,
            "name": chosen.name,
        })

    def _pick_research(self, state: GameState) -> Optional[ResearchItem]:
        """Wählt die günstigste leistbare Forschung.

        Regel (User-Anforderung): sobald eine Forschung leistbar ist, wird
        sie auch gestartet — primär nach FP-Kosten aufsteigend, sekundär nach
        RESEARCH_PRIORITY als Tiebreaker. Die frühere Logik hatte
        ``_research_priority`` als Hauptkriterium — Items außerhalb der
        Priority-Liste wurden deshalb zugunsten teurer Prio-Items ignoriert.
        """
        pending = [
            r for r in state.research
            if not r.is_researched and r.has_prereq
        ]
        if not pending:
            return None

        current_fp = float(state.resources.fp)
        affordable: list[ResearchItem] = []
        blocked: list[tuple[ResearchItem, str]] = []
        for r in pending:
            if not _can_afford_research(r, state, current_fp):
                # Grund für Log sammeln
                if r.fp_cost > current_fp:
                    blocked.append((r, f"FP {r.fp_cost:.0f}>{current_fp:.0f}"))
                else:
                    missing = _missing_research_resources(r, state)
                    blocked.append((r, "Ress: " + ", ".join(missing) if missing else "Ress. knapp"))
                continue
            affordable.append(r)

        if not affordable:
            if blocked:
                sample = ", ".join(f"{r.name}({reason})" for r, reason in blocked[:3])
                logger.info(
                    "Keine Forschung leistbar — %d wartend. Beispiel: %s",
                    len(blocked), sample,
                )
            return None

        # Primär: günstigste FP-Kosten. Sekundär: Priority-Index (älteres Ranking).
        affordable.sort(key=lambda r: (float(r.fp_cost), _research_priority(r)))
        chosen = affordable[0]
        logger.info(
            "Auswahl Forschung: '%s' (FP=%.0f) aus %d leistbaren Kandidaten.",
            chosen.name, chosen.fp_cost, len(affordable),
        )
        return chosen

    # ──────────────────────────────────────────────────────────────────────
    #  Logging
    # ──────────────────────────────────────────────────────────────────────

    def _log_overview(self, state: GameState) -> None:
        pop_pct = state.free_pop_ratio * 100
        pop_status = "OK" if G.pop_free_min() <= state.free_pop_ratio <= G.pop_free_max() else "!"
        sat_status = "OK" if state.satisfaction >= G.satisfaction_warn() else "!"
        cred_status = "OK" if state.credits_rate >= 0 or state.resources.credits > G.credits_warn_balance() else "!"

        logger.info(
            "%s | Eisen=%.0f Stahl=%.0f Eis=%.0f E=%.0f FP=%.0f | "
            "Bev=%d/%d (%.0f%% frei)%s | Zuf=%.0f%%%s | Cr=%.0f (%.1f/h)%s | "
            "Umwelt=%d | LB=%.0f%% | Pkt=%d",
            state.coords,
            state.resources.iron, state.resources.steel,
            state.resources.ice, state.resources.energy, state.resources.fp,
            state.population_free, state.population_max, pop_pct, pop_status,
            state.satisfaction * 100, sat_status,
            state.resources.credits, state.credits_rate, cred_status,
            state.eco_points, state.living_conditions, state.points,
        )
