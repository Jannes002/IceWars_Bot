from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import Config
from .state import Capacity, GameState, ResearchItem
from . import goals as G

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

# ── Ressource → Lagergebäude ──────────────────────────────────────────────────
STORAGE_BUILDINGS: dict[str, tuple[str, str]] = {
    "iron":      ("iron_storage_small",  "Eisenlager"),
    "steel":     ("steel_storage_small", "Stahllager"),
    "chemicals": ("chem_storage_small",  "Chemielager"),
    "ice":       ("ice_water_storage",   "Eis-/Wasserlager"),
    "water":     ("ice_water_storage",   "Eis-/Wasserlager"),
    "energy":    ("energy_storage",      "Energielager"),
}

# ── Wohngebäude (billig → teuer) ─────────────────────────────────────────────
HOUSING_BUILDINGS: list[tuple[str, str, int]] = [
    # (building_type, name, pop_added)
    ("tent",        "Zelt",             15),
    ("house_small", "Kleines Wohnhaus", 60),
]

# ── Zufriedenheits-Gebäude (billig → teuer) ──────────────────────────────────
HAPPINESS_BUILDINGS: list[tuple[str, str, float]] = [
    # (building_type, name, satisfaction_bonus_percent)
    ("outhouse",    "Plumpsklo",      1.0),
    ("scout_camp",  "Pfadfindercamp", 5.0),
    ("park",        "Park",           5.0),
    ("asylum",      "Irrenanstalt",   2.0),
]

# ── Gebäudetyp → Anzeigename (für Build-Queue-Anzeige) ───────────────────────
BUILDING_NAMES: dict[str, str] = {
    # Wohngebäude
    "tent":               "Zelt",
    "house_small":        "Kleines Wohnhaus",
    "house":              "Wohnhaus",
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


# ═══════════════════════════════════════════════════════════════════════════════
#  Strategy
# ═══════════════════════════════════════════════════════════════════════════════

class Strategy:
    """Regelbasierte Kolonie-Entwicklungsstrategie.

    Prioritäten (höchste zuerst):
    1. Zufriedenheit retten (< 0 % → sofort, < 50 % → vorsorglich)
    2. Bevölkerung sichern (freie Siedler < 20 % → Wohnraum bauen)
    3. Lager erweitern (Ressource ≥ 80 % der Kapazität)
    4. Normaler Gebäudebau (Produktion steigern)
    5. Forschung starten
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

        # 2) WARNUNG: Zufriedenheit niedrig
        if state.satisfaction < sat_warn:
            action = self._build_happiness(state, reason=f"Zufriedenheit niedrig ({state.satisfaction*100:.0f}%)")
            if action:
                return action

        # 3) Bevölkerung zu wenig freie Siedler?
        if state.free_pop_ratio < pop_min:
            action = self._build_housing(state)
            if action:
                return action

        # 4) Lager voll?
        storage_action = self._check_storage(state)
        if storage_action:
            return storage_action

        # 5) Normaler Gebäudebau
        logger.info(
            "%d freie Bauslot(s) — normaler Produktionsbau.",
            state.max_build_slots - len(state.build_queue),
        )
        return Action("build_next_building", {"aggression": self._config.strategy.aggression})

    # ──────────────────────────────────────────────────────────────────────
    #  Zufriedenheit
    # ──────────────────────────────────────────────────────────────────────

    def _build_happiness(self, state: GameState, reason: str) -> Optional[Action]:
        """Baut ein Zufriedenheits-Gebäude. Bevorzugt günstige mit wenig Credits-Verbrauch."""
        logger.warning("Zufriedenheits-Check: %s", reason)

        # Wenn Credits knapp → nur Gebäude ohne Credits-Kosten (Plumpsklo)
        credits_tight = (state.credits_rate < 0 and state.resources.credits < G.credits_warn_balance())

        for btype, bname, bonus in HAPPINESS_BUILDINGS:
            # Skip credit-heavy buildings when credits are low
            if credits_tight and btype in ("scout_camp", "park", "asylum"):
                logger.debug("Überspringe '%s' — Credits knapp.", bname)
                continue

            # Prüfe ob schon im Bau
            if any(q.building_type == btype for q in state.build_queue):
                continue

            logger.info(
                "Baue Zufriedenheits-Gebäude: '%s' (+%.0f%%) — %s",
                bname, bonus, reason,
            )
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
        """Baut Wohngebäude wenn freie Bevölkerung < 20 % von max."""
        pct = state.free_pop_ratio * 100
        logger.warning(
            "Bevölkerungs-Check: nur %.0f%% freie Siedler (%d/%d) — Soll: %d–%d%%",
            pct, state.population_free, state.population_max,
            int(G.pop_free_min() * 100), int(G.pop_free_max() * 100),
        )

        for btype, bname, pop_add in HOUSING_BUILDINGS:
            if any(q.building_type == btype for q in state.build_queue):
                continue

            logger.info(
                "Baue Wohngebäude: '%s' (+%d Einwohner) — Freie Bevölkerung zu niedrig.",
                bname, pop_add,
            )
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
        """Gibt eine build_storage-Aktion zurück wenn eine Ressource ≥ 80 % voll ist."""
        queued_types = {
            item.building_type for item in state.build_queue
            if "storage" in item.building_type
        }

        overflowing: list[tuple[float, str, str, str]] = []
        seen_btypes: set[str] = set()

        for resource, (btype, bname) in STORAGE_BUILDINGS.items():
            if btype in queued_types or btype in seen_btypes:
                continue
            ratio = state.capacity.fill_ratio(resource, state.resources)
            if ratio >= G.storage_threshold():
                overflowing.append((ratio, resource, btype, bname))
                seen_btypes.add(btype)

        if not overflowing:
            return None

        overflowing.sort(reverse=True)
        ratio, resource, btype, bname = overflowing[0]

        warn_parts = [f"{r} {rat*100:.0f}%" for rat, r, _, _ in overflowing]
        logger.warning(
            "Lager-Alarm! %d Ressource(n) ≥ %d%%: %s → baue '%s'",
            len(overflowing), int(G.storage_threshold() * 100),
            ", ".join(warn_parts), bname,
        )
        return Action("build_storage", {
            "building_type": btype,
            "building_name": bname,
            "resource": resource,
            "fill_ratio": round(ratio, 3),
        })

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
        candidates = [
            r for r in state.research
            if not r.is_researched and r.has_prereq and r.can_afford
        ]
        if not candidates:
            return None
        candidates.sort(key=_research_priority)
        return candidates[0]

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
