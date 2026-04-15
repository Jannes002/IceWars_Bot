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

# ── Ressource → Produktionsgebäude (Legacy-Fallback für negative Rate) ───────
PRODUCTION_BUILDINGS: dict[str, tuple[str, str]] = {
    "iron":      ("iron_mine_small",       "Kleine Eisenmine"),
    "steel":     ("steel_works_small",     "Kleines Stahlwerk"),
    "chemicals": ("chem_factory_small",    "Kleine Chemiefabrik"),
    "ice":       ("ice_crusher_design",    "Eisbrecher"),
    "water":     ("tauchsieder",           "Tauchsieder"),
    "energy":    ("solar_panels",          "Solarplatten"),
    "vv4a":      ("vv4a_works",            "VV4A-Werk"),
    "fp":        ("research_lab",          "Forschungslabor"),
}

# ── Wohngebäude (bestes zuerst — für den Legacy-Fallback) ────────────────────
HOUSING_BUILDINGS: list[tuple[str, str, int]] = [
    # (building_type, name, pop_added) — absteigend nach Siedlerzahl sortiert.
    # Live-API liefert zusätzlich settlement_complex/villa_complex/asteroid_housing usw.
    ("house_large",         "Großes Wohnhaus",      2500),
    ("pop_center",          "Siedlungszentrum",     1200),
    ("villa_complex",       "Villenkomplex",         600),
    ("settlement_complex",  "Siedlungskomplex",      500),
    ("house_medium",        "Mittleres Wohnhaus",    300),
    ("house_small",         "Kleines Wohnhaus",       60),
    ("tent",                "Zelt",                   15),
]

# ── Zufriedenheits-Gebäude (bestes zuerst — für den Legacy-Fallback) ─────────
HAPPINESS_BUILDINGS: list[tuple[str, str, float]] = [
    # (building_type, name, satisfaction_bonus_percent) — absteigend nach Nutzen.
    # Live-API liefert zusätzlich pizza_small/pizza_large/teen_disco/waffle_stand u.a.
    ("tavern",      "Taverne",       10.0),
    ("theater",     "Theater",       10.0),
    ("cinema",      "Kino",          15.0),
    ("teen_disco",  "Teen-Disco",     8.0),
    ("beauty_salon","Beautysalon",    7.0),
    ("pizza_large", "Pizza-Palast",   6.0),
    ("scout_camp",  "Pfadfindercamp", 5.0),
    ("park",        "Park",           5.0),
    ("pizza_small", "Pizzeria",       3.0),
    ("asylum",      "Irrenanstalt",   2.0),
    ("outhouse",    "Plumpsklo",      1.0),
]

# ── Gebäudetyp → Anzeigename (für Build-Queue-Anzeige) ───────────────────────
BUILDING_NAMES: dict[str, str] = {
    # Wohngebäude
    "tent":               "Zelt",
    "house_small":        "Kleines Wohnhaus",
    "house_medium":       "Mittleres Wohnhaus",
    "house_large":        "Großes Wohnhaus",
    "apartment":          "Apartmentblock",
    # Zufriedenheit
    "outhouse":           "Plumpsklo",
    "scout_camp":         "Pfadfindercamp",
    "park":               "Park",
    "asylum":             "Irrenanstalt",
    "tavern":             "Taverne",
    "theater":            "Theater",
    "cinema":             "Kino",
    # Lager
    "iron_storage_small": "Eisenlager",
    "steel_storage_small":"Stahllager",
    "chem_storage_small": "Chemielager",
    "ice_water_storage":  "Eis-/Wasserlager",
    "energy_storage":     "Energielager",
    "vv4a_storage":       "VV4A-Lager",
    # Produktion
    "iron_mine":          "Eisenmine",
    "steel_mill":         "Stahlwerk",
    "chem_plant":         "Chemiewerk",
    "ice_mine":           "Eisabbau",
    "water_pump":         "Wasserpumpe",
    "solar_plant":        "Solaranlage",
    "vv4a_plant":         "VV4A-Anlage",
    # Sonstiges
    "research_lab":       "Forschungslabor",
    "market":             "Marktplatz",
    "hospital":           "Krankenhaus",
    "school":             "Schule",
    "university":         "Universität",
    "spaceport":          "Raumhafen",
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
        weak_resources: list[tuple[float, str]] = []
        for resource in ("iron", "steel", "chemicals", "ice", "water",
                         "energy", "vv4a", "fp"):
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

        # Alle Ressourcen + FP prüfen, sortiert nach Rate (negativste zuerst)
        resources = ("iron", "steel", "chemicals", "ice", "water",
                     "energy", "vv4a", "fp", "credits")
        check: list[tuple[float, str]] = []
        for resource in resources:
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
        # 1) Übervolle Ressourcen identifizieren (sortiert nach Füllstand)
        overflowing: list[tuple[float, str]] = []
        for resource in ("iron", "steel", "chemicals", "ice", "water", "energy", "vv4a"):
            ratio = state.capacity.fill_ratio(resource, state.resources)
            if ratio >= G.storage_threshold():
                overflowing.append((ratio, resource))
        if not overflowing:
            return None
        overflowing.sort(reverse=True)

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
        queued_types = {
            item.building_type for item in state.build_queue
            if "storage" in item.building_type
        }
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
    #  Forschung
    # ──────────────────────────────────────────────────────────────────────

    def _decide_research(self, state: GameState) -> Optional[Action]:
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
