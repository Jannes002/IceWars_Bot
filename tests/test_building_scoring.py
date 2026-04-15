"""Tests für die neue Kosten/Nutzen/Bauzeit-Scoring-Logik der Strategy.

Diese Tests basieren auf echten Daten aus `/api/city/` des Spiels:
Zelt (35 gebaut) braucht 30.814s für +15 Pop → 0.00049 Pop/s
Kleines Wohnhaus (7 gebaut) braucht 117s für +60 Pop → 0.513 Pop/s
→ Wohnhaus ist ~1000× effizienter und muss gewählt werden.
"""
from __future__ import annotations

import pytest

from icewars_bot import cooldown
from icewars_bot.config import (
    Config, AuthConfig, BrowserConfig, BotConfig, StrategyConfig,
)
from icewars_bot.state import (
    BuildingInfo, Capacity, GameState, Rates, Resources,
)
from icewars_bot.strategy import (
    Strategy,
    SCORE_DIVERSIFY_K,
    SCORE_TIME_ALPHA,
    _filter_buildable,
    _pick_best,
    _score_benefit_per_second,
    build_scoring_snapshot,
)


def make_config() -> Config:
    return Config(
        auth=AuthConfig(username="t", password="t", game_url="http://localhost"),
        browser=BrowserConfig(),
        bot=BotConfig(),
        strategy=StrategyConfig(aggression="balanced"),
    )


@pytest.fixture(autouse=True)
def _reset_cooldowns():
    cooldown.reset()
    yield
    cooldown.reset()


# ── Echte Daten aus dem Dump vom 2026-04-08 ──────────────────────────────────
def tent() -> BuildingInfo:
    return BuildingInfo(
        type="tent", name="Zelt", category="housing",
        count=35, level=35,
        upgrade_cost={"iron": 80, "water": 40},
        worker_cost=0, build_time_sec=30814,
        can_afford=True, reqs_met=True,
        next_level_effect={"population_max": 15},
    )


def house_small() -> BuildingInfo:
    return BuildingInfo(
        type="house_small", name="Kleines Wohnhaus", category="housing",
        count=7, level=7,
        upgrade_cost={"iron": 300, "steel": 200, "water": 200, "chemicals": 100},
        worker_cost=0, build_time_sec=117,
        can_afford=True, reqs_met=True,
        next_level_effect={"population_max": 60, "water_rate": -3, "chemicals_rate": -1},
    )


def iron_mine_small() -> BuildingInfo:
    return BuildingInfo(
        type="iron_mine_small", name="Kleine Eisenmine", category="production",
        count=25, level=25,
        upgrade_cost={"iron": 150, "water": 50},
        worker_cost=5, build_time_sec=7940,
        can_afford=True, reqs_met=True,
        next_level_effect={"iron_rate": 25, "workers": 5, "eco_points": -1, "satisfaction": -0.01},
    )


def steel_works_small() -> BuildingInfo:
    return BuildingInfo(
        type="steel_works_small", name="Kleines Stahlwerk", category="production",
        count=3, level=3,
        upgrade_cost={"iron": 200, "water": 100, "chemicals": 60},
        worker_cost=15, build_time_sec=117,
        can_afford=True, reqs_met=True,
        next_level_effect={"steel_rate": 40, "iron_rate": -30, "workers": 15},
    )


def iron_storage_small() -> BuildingInfo:
    return BuildingInfo(
        type="iron_storage_small", name="Eisenlager", category="storage",
        count=1, level=1,
        upgrade_cost={"iron": 500, "water": 400, "chemicals": 200},
        worker_cost=0, build_time_sec=75,
        can_afford=True, reqs_met=True,
        next_level_effect={"iron_capacity": 5000},
    )


def outhouse() -> BuildingInfo:
    return BuildingInfo(
        type="outhouse", name="Plumpsklo", category="social",
        count=29, level=29,
        upgrade_cost={"iron": 40, "water": 20},
        worker_cost=0, build_time_sec=60,
        can_afford=True, reqs_met=True,
        next_level_effect={"satisfaction": 0.01, "water_rate": -1.5, "eco_points": 1},
    )


def park() -> BuildingInfo:
    return BuildingInfo(
        type="park", name="Park", category="social",
        count=1, level=1,
        upgrade_cost={"iron": 100, "steel": 50, "credits": 20},
        worker_cost=0, build_time_sec=300,
        can_afford=True, reqs_met=True,
        next_level_effect={"satisfaction": 0.05, "energy_rate": -9, "credits_rate": -10, "eco_points": 10},
    )


def solar_panels() -> BuildingInfo:
    return BuildingInfo(
        type="solar_panels", name="Solarplatten", category="energy",
        count=5, level=5,
        upgrade_cost={"iron": 200, "chemicals": 150},
        worker_cost=5, build_time_sec=150,
        can_afford=True, reqs_met=True,
        next_level_effect={"energy_rate": 22.5, "workers": 5},
    )


def state_with(*buildings: BuildingInfo, **overrides) -> GameState:
    defaults = dict(
        max_build_slots=2,
        build_queue=[],
        buildings=list(buildings),
        population_free=100,
        population_max=400,
        satisfaction=0.90,
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
#  Scoring-Grundfunktionen
# ═══════════════════════════════════════════════════════════════════════════════


def test_score_benefit_per_second_positive():
    """Neue Formel: (benefit / time^alpha) * 1/(1+count/K).

    Für house_small (benefit=60, time=117s, count=7):
      time_weight = 117 ** 0.7 ≈ 28.035
      diversify   = 1 / (1 + 7/3) = 0.3
      score       = 60/28.035 * 0.3 ≈ 0.642
    """
    b = house_small()
    score = _score_benefit_per_second(b, "population_max")
    expected = (60 / (117 ** SCORE_TIME_ALPHA)) * (1 / (1 + 7 / SCORE_DIVERSIFY_K))
    assert score == pytest.approx(expected, rel=1e-4)
    assert score > 0


def test_diversification_penalty_grows_with_count():
    """Ein häufig gebauter Typ erhält einen geringeren Score als derselbe
    Typ bei count=0 — die Diversifikation dämpft weichen ab."""
    base = house_small()
    base.count = 0
    score_fresh = _score_benefit_per_second(base, "population_max")

    built = house_small()
    built.count = 20
    score_builtup = _score_benefit_per_second(built, "population_max")

    assert score_fresh > score_builtup
    # bei count=20 ist der Faktor 1/(1+20/3) ≈ 0.13 — massiv kleiner
    ratio = score_builtup / score_fresh
    assert ratio < 0.2


def test_scoring_prefers_fresh_high_tier_over_overbuilt_cheap():
    """Realdaten: iron_mine_small (count=35, 73k s) vs. iron_mine_large
    (count=0, 180s, 6.4× Rate) — die frische Alternative muss gewinnen."""
    old = BuildingInfo(
        type="iron_mine_small", name="Kleine Eisenmine", category="production",
        count=35, level=35,
        upgrade_cost={"iron": 150, "water": 50},
        worker_cost=5, build_time_sec=73955,
        can_afford=True, reqs_met=True,
        next_level_effect={"iron_rate": 25},
    )
    new = BuildingInfo(
        type="iron_mine_large", name="Große Eisenmine", category="production",
        count=0, level=0,
        upgrade_cost={"iron": 1500, "steel": 500},
        worker_cost=20, build_time_sec=180,
        can_afford=True, reqs_met=True,
        next_level_effect={"iron_rate": 160},
    )
    assert (
        _score_benefit_per_second(new, "iron_rate")
        > _score_benefit_per_second(old, "iron_rate")
    )


def test_score_returns_zero_when_effect_missing():
    b = house_small()
    assert _score_benefit_per_second(b, "steel_rate") == 0


def test_score_returns_zero_when_negative():
    # steel_works_small hat iron_rate: -30 (Verbrauch)
    b = steel_works_small()
    assert _score_benefit_per_second(b, "iron_rate") == 0


def test_filter_buildable_excludes_unbuildable():
    a = house_small()
    b = BuildingInfo(
        type="iron_mine_large", name="Große Eisenmine", category="production",
        build_time_sec=180, can_afford=False, reqs_met=False,
    )
    result = _filter_buildable([a, b])
    assert a in result
    assert b not in result


def test_filter_buildable_excludes_cooldown():
    b = house_small()
    cooldown.record_failure("house_small")
    cooldown.record_failure("house_small")
    result = _filter_buildable([b])
    assert result == []


# ═══════════════════════════════════════════════════════════════════════════════
#  Housing: Das Kern-Problem aus dem Bug-Report
# ═══════════════════════════════════════════════════════════════════════════════


def test_housing_picks_small_house_over_tent():
    """Reproduziert den Bug-Report: der Bot soll NICHT mehr Zelte bauen,
    wenn Kleines Wohnhaus 1000× effizienter pro Sekunde ist."""
    strategy = Strategy(make_config())
    state = state_with(
        tent(), house_small(),
        population_free=10, population_max=400,  # dringend Wohnraum nötig
        satisfaction=0.9,
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "house_small"


def test_housing_falls_back_to_tent_when_no_house():
    """Wenn nur Zelt baubar ist, wählt der Bot eben Zelt."""
    strategy = Strategy(make_config())
    state = state_with(
        tent(),
        population_free=10, population_max=400,
        satisfaction=0.9,
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "tent"


# ═══════════════════════════════════════════════════════════════════════════════
#  Happiness
# ═══════════════════════════════════════════════════════════════════════════════


def test_happiness_picks_park_over_outhouse_when_credits_ok():
    """Park gibt 0.05 Zufriedenheit in 300s = 0.000167/s,
    Plumpsklo 0.01 in 60s = 0.000167/s. Park wird knapp bevorzugt
    — aber wichtiger: beide werden korrekt verglichen statt nur der
    erste genommen. Prüfen wir mit verschobenen Zahlen."""
    strategy = Strategy(make_config())
    # Park künstlich attraktiver machen → viel kürzere Bauzeit
    p = park()
    p.build_time_sec = 100  # 0.05/100 = 0.0005/s
    state = state_with(
        outhouse(), p,
        satisfaction=0.40,  # unter warn-Schwelle → happiness
        population_free=200, population_max=400,
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "park"


def test_happiness_skips_credit_heavy_when_credits_tight():
    """Park kostet Credits + -10 credits_rate → bei knappen Credits überspringen."""
    strategy = Strategy(make_config())
    state = state_with(
        outhouse(), park(),
        satisfaction=0.40,
        population_free=200, population_max=400,
        credits_rate=-5,
        resources=Resources(credits=10),  # unter warn-balance
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert build_actions[0].params["building_type"] == "outhouse"


# ═══════════════════════════════════════════════════════════════════════════════
#  Storage
# ═══════════════════════════════════════════════════════════════════════════════


def test_storage_picks_best_for_overflowing_resource():
    strategy = Strategy(make_config())
    state = state_with(
        iron_storage_small(),
        resources=Resources(iron=9500, steel=500, chemicals=500, water=500,
                            ice=500, energy=500, vv4a=500, credits=500, fp=500),
        capacity=Capacity(iron=10000, steel=10000, chemicals=10000, water=10000,
                          ice=10000, energy=10000, vv4a=10000),
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_storage"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "iron_storage_small"


# ═══════════════════════════════════════════════════════════════════════════════
#  Negative Rate Fix mit echten Daten
# ═══════════════════════════════════════════════════════════════════════════════


def test_negative_rate_uses_correct_building_type():
    """Der alte Code hätte 'iron_mine' gebaut (existiert nicht).
    Neue Logik muss 'iron_mine_small' aus den Live-Daten wählen."""
    strategy = Strategy(make_config())
    state = state_with(
        iron_mine_small(), steel_works_small(),
        rates=Rates(iron=-5, steel=10, chemicals=10, ice=10, water=10,
                    energy=10, vv4a=10, credits=10, fp=10),
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "iron_mine_small"


def test_negative_energy_rate_picks_from_energy_category():
    """Energie-Gebäude sind in Kategorie 'energy', nicht 'production'."""
    strategy = Strategy(make_config())
    state = state_with(
        solar_panels(),
        rates=Rates(iron=10, steel=10, chemicals=10, ice=10, water=10,
                    energy=-20, vv4a=10, credits=10, fp=10),
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "solar_panels"


# ═══════════════════════════════════════════════════════════════════════════════
#  Integration mit Cooldown
# ═══════════════════════════════════════════════════════════════════════════════


def test_build_scoring_snapshot_sorted_by_score():
    """Snapshot-Helper für Dashboard: liefert sortierte Liste baubarer
    Gebäude mit ihrem jeweils besten Effekt-Key."""
    state = state_with(tent(), house_small(), iron_mine_small(), iron_storage_small())
    rows = build_scoring_snapshot(state, limit=10)
    assert len(rows) >= 2
    # Nach score absteigend sortiert
    scores = [r["score"] for r in rows]
    assert scores == sorted(scores, reverse=True)
    # Jede Zeile enthält die Pflichtfelder
    for r in rows:
        assert "type" in r and "name" in r
        assert "effect_label" in r and r["score"] > 0


def test_cooldown_hides_building_from_scoring():
    """Wenn 'house_small' auf Cooldown ist, muss Tent gewählt werden."""
    cooldown.record_failure("house_small")
    cooldown.record_failure("house_small")

    strategy = Strategy(make_config())
    state = state_with(
        tent(), house_small(),
        population_free=10, population_max=400,
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    assert build_actions[0].params["building_type"] == "tent"
