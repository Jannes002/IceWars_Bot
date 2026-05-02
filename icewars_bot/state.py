from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Resources:
    iron: float = 0
    steel: float = 0
    chemicals: float = 0
    ice: float = 0
    water: float = 0
    energy: float = 0
    vv4a: float = 0
    credits: float = 0
    fp: float = 0  # Forschungspunkte


@dataclass
class Rates:
    """Ressourcen-Produktionsraten pro Stunde."""
    iron: float = 0
    steel: float = 0
    chemicals: float = 0
    ice: float = 0
    water: float = 0
    energy: float = 0
    vv4a: float = 0
    credits: float = 0
    fp: float = 0


@dataclass
class Capacity:
    """Maximale Lagerkapazität pro Ressource."""
    iron: float = 0
    steel: float = 0
    chemicals: float = 0
    ice: float = 0
    water: float = 0
    energy: float = 0
    vv4a: float = 0

    def fill_ratio(self, resource: str, resources: "Resources") -> float:
        """Gibt den Füllstand einer Ressource zurück (0.0 – 1.0). 0 wenn keine Kapazität bekannt."""
        cap = getattr(self, resource, 0)
        if cap <= 0:
            return 0.0
        val = getattr(resources, resource, 0)
        return min(float(val) / float(cap), 1.0)


@dataclass
class BuildQueueItem:
    building_type: str
    name: str
    finish_time: str = ""
    remaining_sec: int = 0


@dataclass
class BuildingInfo:
    """Live-Daten eines baubaren Gebäudes aus /api/city/ → city.buildings.

    ``next_level_effect`` enthält den MARGINALEN Nutzen des nächsten Baus
    (z.B. ``{"iron_rate": 25, "workers": 5, "eco_points": -1}``), während
    ``effect`` die kumulierte Wirkung aller bisherigen Instanzen zeigt.
    """
    type: str
    name: str
    category: str                  # production, storage, energy, housing, social, ...
    count: int = 0                 # Anzahl bereits gebauter Instanzen
    level: int = 0
    upgrade_cost: dict[str, float] = field(default_factory=dict)
    worker_cost: int = 0           # benötigte freie Bevölkerung
    build_time_sec: int = 0
    can_afford: bool = False
    reqs_met: bool = False
    p_restricted: bool = False     # Planet-Typ erlaubt dieses Gebäude nicht
    research_missing: bool = False
    in_queue: bool = False         # gerade in der Bauwarteschlange
    effect: dict[str, float] = field(default_factory=dict)
    next_level_effect: dict[str, float] = field(default_factory=dict)

    @property
    def is_buildable(self) -> bool:
        """True wenn das Gebäude *jetzt* gebaut werden kann."""
        return (
            self.can_afford
            and self.reqs_met
            and not self.p_restricted
            and not self.research_missing
        )


@dataclass
class ActiveResearch:
    """Laufende Forschung mit Restzeit."""
    type: str
    name: str
    finish_time: str = ""
    remaining_sec: int = 0


@dataclass
class ResearchItem:
    type: str
    name: str
    evo: int = 0
    fp_cost: float = 0
    res_cost: dict = field(default_factory=dict)
    is_researched: bool = False
    has_prereq: bool = False
    can_afford: bool = False          # Server-seitig berechnet (FP + Ressourcen)
    time_sec: int = 0                 # Forschungsdauer in Sekunden
    unlocks_buildings: list[str] = field(default_factory=list)   # Namen der freischaltbaren Gebäude
    unlocks_ships: list[str] = field(default_factory=list)       # Namen der freischaltbaren Schiffe


@dataclass
class GameState:
    city_id: int = 0
    city_name: str = ""
    coords: str = ""
    planet_type: str = ""
    resources: Resources = field(default_factory=Resources)
    rates: Rates = field(default_factory=Rates)
    capacity: Capacity = field(default_factory=Capacity)
    build_slots: int = 0
    max_build_slots: int = 2
    build_queue: list[BuildQueueItem] = field(default_factory=list)
    buildings: list[BuildingInfo] = field(default_factory=list)
    research: list[ResearchItem] = field(default_factory=list)
    active_research: Optional[ActiveResearch] = None
    research_lab_busy: bool = False

    # Bevölkerung
    population_free: int = 0
    population_total: int = 0
    population_max: int = 0

    # Zufriedenheit & Lebensbedingungen
    satisfaction: float = 1.0       # 0.0–1.0+ (API-Wert, z.B. 0.84 = 84 %)
    satisfaction_raw: float = 1.0   # Rohwert vor Modifikatoren
    living_conditions: float = 100  # Lebensbedingungen in % (z.B. 123.5)
    eco_points: int = 0             # Umweltpunkte (negativ = schlecht)

    # Wirtschaft
    credits_rate: float = 0         # Credits/h (negativ = Verlust)

    points: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    # Kolonieliste aus der API (alle Städte des Spielers).
    # Jeder Eintrag hat mindestens: id, name, coords.
    colonies: list = field(default_factory=list)

    @property
    def free_pop_ratio(self) -> float:
        """Freie Bevölkerung als Anteil der maximalen Bevölkerung (0.0 – 1.0)."""
        if self.population_max <= 0:
            return 0.0
        return self.population_free / self.population_max


def parse_state(raw: dict[str, Any]) -> GameState:
    """Wandelt rohe API-Daten in ein strukturiertes GameState um."""
    city = raw.get("city", {})
    res_raw = city.get("resources", {})
    rates_raw = city.get("rates", {})

    resources = Resources(
        iron=float(res_raw.get("iron", 0)),
        steel=float(res_raw.get("steel", 0)),
        chemicals=float(res_raw.get("chemicals", 0)),
        ice=float(res_raw.get("ice", 0)),
        water=float(res_raw.get("water", 0)),
        energy=float(res_raw.get("energy", 0)),
        vv4a=float(res_raw.get("vv4a", 0)),
        credits=float(res_raw.get("credits", 0)),
        fp=float(city.get("fp", 0)),
    )

    rates = Rates(
        iron=float(rates_raw.get("iron", 0)),
        steel=float(rates_raw.get("steel", 0)),
        chemicals=float(rates_raw.get("chemicals", 0)),
        ice=float(rates_raw.get("ice", 0)),
        water=float(rates_raw.get("water", 0)),
        energy=float(rates_raw.get("energy", 0)),
        vv4a=float(rates_raw.get("vv4a", 0)),
        credits=float(rates_raw.get("credits", 0)),
        fp=float(rates_raw.get("research_points", 0)),
    )

    cap_raw = city.get("capacity", {})
    capacity = Capacity(
        iron=float(cap_raw.get("iron", 0)),
        steel=float(cap_raw.get("steel", 0)),
        chemicals=float(cap_raw.get("chemicals", 0)),
        ice=float(cap_raw.get("ice", 0)),
        water=float(cap_raw.get("water", 0)),
        energy=float(cap_raw.get("energy", 0)),
        vv4a=float(cap_raw.get("vv4a", 0)),
    )

    build_queue = [
        BuildQueueItem(
            building_type=b.get("type", ""),
            name=b.get("name", ""),
            finish_time=b.get("finish_time", ""),
            remaining_sec=int(b.get("remaining_sec", 0)),
        )
        for b in city.get("build_queue", [])
    ]

    buildings = [
        BuildingInfo(
            type=str(b.get("type", "")),
            name=str(b.get("name", "")),
            category=str(b.get("category", "")),
            count=int(b.get("count", 0)),
            level=int(b.get("level", 0)),
            upgrade_cost={k: float(v) for k, v in (b.get("upgrade_cost") or {}).items()},
            worker_cost=int(b.get("worker_cost", 0)),
            build_time_sec=int(b.get("build_time_sec", 0)),
            can_afford=bool(b.get("can_afford", False)),
            reqs_met=bool(b.get("reqs_met", False)),
            p_restricted=bool(b.get("p_restricted", False)),
            research_missing=bool(b.get("research_missing", False)),
            in_queue=bool(b.get("in_queue", False)),
            effect={k: float(v) for k, v in (b.get("effect") or {}).items()},
            next_level_effect={k: float(v) for k, v in (b.get("next_level_effect") or {}).items()},
        )
        for b in (city.get("buildings") or [])
    ]

    # Forschungsliste mit allen relevanten Feldern
    research = [
        ResearchItem(
            type=r.get("type", ""),
            name=r.get("name", ""),
            evo=int(r.get("evo", 0)),
            fp_cost=float(r.get("fp_cost", 0)),
            res_cost=r.get("res_cost", {}),
            is_researched=bool(r.get("is_researched", False)),
            has_prereq=bool(r.get("has_prereq", False)),
            can_afford=bool(r.get("can_afford", False)),
            time_sec=int(r.get("time_sec", 0)),
            unlocks_buildings=[b.get("name", "") for b in r.get("unlocks_b_data", [])],
            unlocks_ships=[s.get("name", "") for s in r.get("unlocks_s_data", [])],
        )
        for r in raw.get("research", [])
    ]

    # Aktive Forschung
    active_research: Optional[ActiveResearch] = None
    active_raw = raw.get("active_research")
    if active_raw:
        active_research = ActiveResearch(
            type=active_raw.get("type", ""),
            name=active_raw.get("name", ""),
            finish_time=active_raw.get("finish_time", ""),
            remaining_sec=int(active_raw.get("remaining_sec", 0)),
        )

    research_lab_busy = bool(raw.get("research_lab_busy", active_research is not None))

    # Kolonieliste (andere Städte des Spielers)
    # API gibt entweder Liste von Dicts {"id": X, ...} oder Integer-IDs [X, Y, ...]
    colonies_raw = city.get("colonies", [])
    colonies = []
    for c in (colonies_raw or []):
        if isinstance(c, dict) and c.get("id"):
            colonies.append(dict(c))
        elif isinstance(c, (int, float)) and c:
            colonies.append({"id": int(c)})

    return GameState(
        city_id=int(city.get("id", 0)),
        city_name=str(city.get("name", "")),
        coords=str(city.get("coords", "")),
        planet_type=str(city.get("planet_type", "")),
        resources=resources,
        rates=rates,
        capacity=capacity,
        build_slots=int(city.get("build_slots_used", 0)),
        max_build_slots=int(city.get("max_parallel_builds", 2)),
        build_queue=build_queue,
        buildings=buildings,
        research=research,
        active_research=active_research,
        research_lab_busy=research_lab_busy,
        population_free=int(city.get("population_free", 0)),
        population_total=int(city.get("population_total", 0)),
        population_max=int(city.get("population_max", city.get("population", {}).get("max", 0))),
        satisfaction=float(city.get("satisfaction", 1.0)),
        satisfaction_raw=float(city.get("satisfaction_raw", 1.0)),
        living_conditions=float(city.get("living_conditions", 100)),
        eco_points=int(city.get("eco_points", 0)),
        credits_rate=float(rates_raw.get("credits", 0)),
        points=int(city.get("points", 0)),
        raw=raw,
        colonies=colonies,
    )
