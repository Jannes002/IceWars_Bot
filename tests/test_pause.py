"""Tests für die Pause-Funktion und den Queue-Filter-Fix im Live-Pfad
von _check_storage.

Zwei Ziele:
  1. Pausierte Ressource → kein Lager, kein Produktionsbau, keine Priorität.
  2. Wenn ein Lager bereits in der Bauwarteschlange steht, darf der Live-Pfad
     nicht erneut dasselbe Lager vorschlagen (gegen den Eis-Wasser-Spam).
"""
from __future__ import annotations

import pytest

from icewars_bot import goals as G
from icewars_bot.config import (
    AuthConfig, BotConfig, BrowserConfig, Config, StrategyConfig,
)
from icewars_bot.state import (
    BuildingInfo, BuildQueueItem, Capacity, GameState, Rates, Resources,
)
from icewars_bot.strategy import Strategy


def make_config(aggression: str = "balanced") -> Config:
    return Config(
        auth=AuthConfig(username="t", password="t", game_url="http://localhost"),
        browser=BrowserConfig(), bot=BotConfig(),
        strategy=StrategyConfig(aggression=aggression),
    )


def ice_water_storage(count: int = 5, level: int = 5) -> BuildingInfo:
    return BuildingInfo(
        type="ice_water_storage", name="Eis-/Wasserlager", category="storage",
        count=count, level=level,
        upgrade_cost={"iron": 400, "steel": 200, "chemicals": 100},
        worker_cost=0, build_time_sec=300,
        can_afford=True, reqs_met=True,
        next_level_effect={"ice_capacity": 10000, "water_capacity": 10000},
    )


def iron_storage_small(count: int = 1, level: int = 1) -> BuildingInfo:
    return BuildingInfo(
        type="iron_storage_small", name="Eisenlager", category="storage",
        count=count, level=level,
        upgrade_cost={"iron": 500, "water": 400, "chemicals": 200},
        worker_cost=0, build_time_sec=75,
        can_afford=True, reqs_met=True,
        next_level_effect={"iron_capacity": 5000},
    )


def iron_mine_small(count: int = 5) -> BuildingInfo:
    return BuildingInfo(
        type="iron_mine_small", name="Kleine Eisenmine", category="production",
        count=count, level=count,
        upgrade_cost={"iron": 150, "water": 50},
        worker_cost=5, build_time_sec=300,
        can_afford=True, reqs_met=True,
        next_level_effect={"iron_rate": 25, "workers": 5},
    )


def make_state(**overrides) -> GameState:
    defaults = dict(
        max_build_slots=2, build_queue=[],
        buildings=[],
        population_free=200, population_max=400, satisfaction=0.90,
        resources=Resources(iron=1000, steel=1000, chemicals=1000, water=1000,
                            ice=1000, energy=1000, vv4a=1000, credits=500, fp=500),
        capacity=Capacity(iron=10000, steel=10000, chemicals=10000, water=10000,
                          ice=10000, energy=10000, vv4a=10000),
        rates=Rates(iron=10, steel=10, chemicals=10, ice=10,
                    water=10, energy=10, vv4a=10, credits=10, fp=10),
    )
    defaults.update(overrides)
    return GameState(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
#  Pause: Lager (_check_storage)
# ═══════════════════════════════════════════════════════════════════════════════


def test_paused_resource_skips_storage_live():
    """Ice ist bei 95 % voll, aber pausiert → Bot baut KEIN Lager."""
    G.update({"paused_resources": ["ice", "water"]})
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[ice_water_storage()],
        resources=Resources(ice=9500, water=9500,
                            iron=0, steel=0, chemicals=0, energy=0, vv4a=0,
                            credits=500, fp=500),
    )
    assert strategy._check_storage(state) is None


def test_paused_resource_skips_storage_legacy():
    """Gleicher Test, aber ohne live Buildings (Legacy-Pfad)."""
    G.update({"paused_resources": ["iron"]})
    strategy = Strategy(make_config())
    state = make_state(
        resources=Resources(iron=9500,
                            steel=0, chemicals=0, water=0, ice=0,
                            energy=0, vv4a=0, credits=500, fp=500),
    )
    assert strategy._check_storage(state) is None


def test_unpaused_resource_still_triggers_storage():
    """Wenn nur ice pausiert ist, aber chemicals voll → trotzdem Lager bauen."""
    G.update({"paused_resources": ["ice", "water"]})
    strategy = Strategy(make_config())
    state = make_state(
        resources=Resources(chemicals=9500, ice=9500, water=9500,
                            iron=0, steel=0, energy=0, vv4a=0,
                            credits=500, fp=500),
    )
    action = strategy._check_storage(state)
    assert action is not None
    # Die einzige noch aktive übervolle Ressource ist chemicals
    assert action.params["resource"] == "chemicals"


# ═══════════════════════════════════════════════════════════════════════════════
#  Queue-Filter-Fix (BUGFIX): Live-Pfad
# ═══════════════════════════════════════════════════════════════════════════════


def test_storage_not_triggered_when_already_in_queue_live_path():
    """Reproduziert den User-Bug: ice_water_storage ist im Bau, ice noch
    bei 0.85 → der Live-Pfad darf NICHT dasselbe Lager erneut planen."""
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[ice_water_storage()],
        resources=Resources(ice=8500, water=4000,
                            iron=0, steel=0, chemicals=0, energy=0, vv4a=0,
                            credits=500, fp=500),
        build_queue=[
            BuildQueueItem("ice_water_storage", "Eis-/Wasserlager",
                           finish_time="2026-04-16T12:00:00")
        ],
    )
    assert strategy._check_storage(state) is None


def test_other_resource_storage_still_picks_when_one_is_queued():
    """ice_water_storage im Bau, aber iron bei 0.95 → iron-Lager muss kommen."""
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[ice_water_storage(), iron_storage_small()],
        resources=Resources(ice=8500, water=4000, iron=9500,
                            steel=0, chemicals=0, energy=0, vv4a=0,
                            credits=500, fp=500),
        build_queue=[
            BuildQueueItem("ice_water_storage", "Eis-/Wasserlager",
                           finish_time="2026-04-16T12:00:00")
        ],
    )
    action = strategy._check_storage(state)
    assert action is not None
    assert action.params["resource"] == "iron"
    assert action.params["building_type"] == "iron_storage_small"


# ═══════════════════════════════════════════════════════════════════════════════
#  Pause: Produktion (_fix_negative_rate)
# ═══════════════════════════════════════════════════════════════════════════════


def test_paused_resource_skips_negative_rate_fix_live():
    """iron-Rate ist -5, aber iron pausiert → Bot überspringt den Fix."""
    G.update({"paused_resources": ["iron"]})
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[iron_mine_small()],
        rates=Rates(iron=-5, steel=10, chemicals=10, ice=10, water=10,
                    energy=10, vv4a=10, credits=10, fp=10),
    )
    assert strategy._fix_negative_rate(state) is None


def test_paused_resource_skips_negative_rate_fix_legacy():
    """Gleicher Test im Legacy-Pfad (keine state.buildings)."""
    G.update({"paused_resources": ["iron"]})
    strategy = Strategy(make_config())
    state = make_state(
        rates=Rates(iron=-5, steel=10, chemicals=10, ice=10, water=10,
                    energy=10, vv4a=10, credits=10, fp=10),
    )
    assert strategy._fix_negative_rate(state) is None


def test_non_paused_negative_rate_still_triggers():
    """water pausiert, aber iron=-5 → Bot baut iron_mine_small."""
    G.update({"paused_resources": ["water"]})
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[iron_mine_small()],
        rates=Rates(iron=-5, steel=10, chemicals=10, ice=10, water=-10,
                    energy=10, vv4a=10, credits=10, fp=10),
    )
    action = strategy._fix_negative_rate(state)
    assert action is not None
    assert action.params["building_type"] == "iron_mine_small"


# ═══════════════════════════════════════════════════════════════════════════════
#  Pause: Priorität/Balanced (_build_best_growth)
# ═══════════════════════════════════════════════════════════════════════════════


def test_paused_priority_falls_back_to_balanced():
    """priority_resource=ice und ice pausiert → der Modus wechselt auf balanced."""
    G.update({"priority_resource": "ice", "paused_resources": ["ice"]})
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[iron_mine_small()],
        rates=Rates(iron=5, steel=10, chemicals=10, ice=5, water=5,
                    energy=10, vv4a=10, credits=10, fp=10),
    )
    action = strategy._build_best_growth(state)
    # Prioritäts-Modus hätte nach ice_rate gesucht → kein Treffer.
    # Balanced-Modus greift zurück und wählt iron_mine_small für iron (positiv, schwach).
    assert action is not None
    assert action.params["building_type"] == "iron_mine_small"
    assert "Priorität" not in action.params.get("reason", "")


def test_balanced_skips_paused_weak_resources():
    """Auch im balanced-Modus darf eine pausierte Ressource nicht als
    Ziel-Ressource gewählt werden."""
    G.update({"paused_resources": ["iron"]})
    strategy = Strategy(make_config())
    # iron ist am schwächsten, würde normal gewählt — aber pausiert.
    state = make_state(
        buildings=[iron_mine_small()],
        rates=Rates(iron=1, steel=5, chemicals=10, ice=10, water=10,
                    energy=10, vv4a=10, credits=10, fp=10),
    )
    action = strategy._build_best_growth(state)
    # Da nur iron_mine_small im Inventar ist und iron gesperrt ist, findet
    # die Funktion kein passendes Gebäude → None ist das korrekte Ergebnis
    # (der caller fällt dann auf build_next_building zurück).
    assert action is None


# ═══════════════════════════════════════════════════════════════════════════════
#  End-to-End: Strategy.decide mit Pause
# ═══════════════════════════════════════════════════════════════════════════════


def test_decide_respects_pause_end_to_end():
    """Integrationstest: ice übervoll + pausiert → decide() baut KEIN
    ice_water_storage, sondern fällt durch auf einen normalen Bau."""
    G.update({"paused_resources": ["ice", "water"]})
    strategy = Strategy(make_config())
    state = make_state(
        buildings=[ice_water_storage()],
        resources=Resources(ice=9500, water=9500,
                            iron=500, steel=500, chemicals=500,
                            energy=500, vv4a=100, credits=500, fp=500),
    )
    actions = strategy.decide(state)
    storage_actions = [a for a in actions if a.type == "build_storage"]
    assert storage_actions == [], "Pausierte Ressource darf kein Lager auslösen"
