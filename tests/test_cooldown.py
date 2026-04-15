"""Tests für den Bau-Cooldown-Tracker und dessen Integration in die Strategy."""
from __future__ import annotations

import time

import pytest

from icewars_bot import cooldown
from icewars_bot.config import (
    Config, AuthConfig, BrowserConfig, BotConfig, StrategyConfig,
)
from icewars_bot.state import GameState, Rates
from icewars_bot.strategy import Strategy, PRODUCTION_BUILDINGS


def make_config() -> Config:
    return Config(
        auth=AuthConfig(username="test", password="test", game_url="http://localhost"),
        browser=BrowserConfig(),
        bot=BotConfig(),
        strategy=StrategyConfig(aggression="balanced"),
    )


@pytest.fixture(autouse=True)
def _clear_cooldowns():
    """Stellt sicher, dass jeder Test mit einem leeren Cooldown-Tracker startet."""
    cooldown.reset()
    yield
    cooldown.reset()


# ---------- Basis-Funktionalität ----------


def test_no_cooldown_on_first_failure():
    activated = cooldown.record_failure("iron_mine")
    assert activated is False
    assert cooldown.is_on_cooldown("iron_mine") is False
    assert cooldown.failure_count("iron_mine") == 1


def test_cooldown_activates_on_second_failure():
    cooldown.record_failure("iron_mine")
    activated = cooldown.record_failure("iron_mine")
    assert activated is True
    assert cooldown.is_on_cooldown("iron_mine") is True
    assert cooldown.failure_count("iron_mine") == 2
    assert cooldown.remaining_seconds("iron_mine") > 0


def test_success_clears_cooldown_and_failures():
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("iron_mine")
    assert cooldown.is_on_cooldown("iron_mine") is True

    cooldown.record_success("iron_mine")
    assert cooldown.is_on_cooldown("iron_mine") is False
    assert cooldown.failure_count("iron_mine") == 0


def test_cooldown_is_per_building():
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("iron_mine")
    assert cooldown.is_on_cooldown("iron_mine") is True
    assert cooldown.is_on_cooldown("steel_mill") is False


def test_cooldown_duration_is_one_hour_by_default():
    assert cooldown.COOLDOWN_SECONDS == 3600
    assert cooldown.FAILURE_THRESHOLD == 2


def test_active_cooldowns_lists_all():
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("steel_mill")  # nur 1 Fehler → kein Cooldown

    active = cooldown.active_cooldowns()
    assert "iron_mine" in active
    assert "steel_mill" not in active
    assert active["iron_mine"] > 0


def test_cooldown_expires(monkeypatch):
    """Nach Ablauf der Dauer darf das Gebäude wieder versucht werden."""
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("iron_mine")
    assert cooldown.is_on_cooldown("iron_mine") is True

    # Monotonic-Zeit um 2 h vorspulen
    original = time.monotonic
    offset = cooldown.COOLDOWN_SECONDS + 60
    monkeypatch.setattr(
        cooldown.time, "monotonic", lambda: original() + offset
    )

    assert cooldown.is_on_cooldown("iron_mine") is False
    # Eintrag wurde aufgeräumt
    assert cooldown.failure_count("iron_mine") == 0


def test_reset_clears_everything():
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("iron_mine")
    cooldown.record_failure("steel_mill")

    cooldown.reset()

    assert cooldown.is_on_cooldown("iron_mine") is False
    assert cooldown.failure_count("iron_mine") == 0
    assert cooldown.failure_count("steel_mill") == 0


# ---------- Strategy-Integration ----------


def test_strategy_skips_cooldowned_production_building():
    """Wenn iron_mine_small auf Cooldown ist, aber Eisen UND Stahl negativ,
    soll stattdessen das Stahlwerk gebaut werden."""
    cooldown.record_failure("iron_mine_small")
    cooldown.record_failure("iron_mine_small")
    assert cooldown.is_on_cooldown("iron_mine_small") is True

    strategy = Strategy(make_config())
    state = GameState(
        max_build_slots=2, build_queue=[],
        population_free=100, population_max=400, satisfaction=0.90,
        rates=Rates(
            iron=-5,     # kritisch negativ → würde iron_mine bauen
            steel=-3,    # auch negativ → Ausweichziel
            chemicals=10, ice=10, water=10, energy=10, vv4a=10,
            credits=10, fp=10,
        ),
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert len(build_actions) == 1
    # Nicht iron_mine_small (Cooldown) — stattdessen steel_works_small
    assert build_actions[0].params["building_type"] == "steel_works_small"


def test_strategy_falls_through_when_all_production_on_cooldown():
    """Wenn alle negativen Produktionsgebäude auf Cooldown sind, geht die
    Strategy zur nächsten Priorität über (hier: build_next_building)."""
    cooldown.record_failure("iron_mine_small")
    cooldown.record_failure("iron_mine_small")

    strategy = Strategy(make_config())
    state = GameState(
        max_build_slots=2, build_queue=[],
        population_free=100, population_max=400, satisfaction=0.90,
        rates=Rates(
            iron=-5,  # negativ, aber iron_mine_small auf Cooldown
            steel=10, chemicals=10, ice=10, water=10, energy=10,
            vv4a=10, credits=10, fp=10,
        ),
    )
    actions = strategy.decide(state)
    # Sollte NICHT iron_mine_small bauen
    for a in actions:
        if a.type == "build_specific":
            assert a.params["building_type"] != "iron_mine_small"


def test_cooldown_integration_all_production_buildings_defined():
    """Sanity-Check: jedes PRODUCTION_BUILDINGS kann auf Cooldown gesetzt werden."""
    for resource, (btype, _) in PRODUCTION_BUILDINGS.items():
        cooldown.record_failure(btype)
        cooldown.record_failure(btype)
        assert cooldown.is_on_cooldown(btype) is True
        cooldown.record_success(btype)
        assert cooldown.is_on_cooldown(btype) is False
