"""Tests für die Lager-Logik (80%-Schwelle, build_storage-Aktion)."""
import pytest
from icewars_bot.config import Config, AuthConfig, BrowserConfig, BotConfig, StrategyConfig
from icewars_bot.state import GameState, Resources, Capacity, parse_state
from icewars_bot.strategy import Strategy, STORAGE_THRESHOLD, STORAGE_BUILDINGS


def make_config() -> Config:
    return Config(
        auth=AuthConfig(username="test", password="test", game_url="http://localhost"),
        browser=BrowserConfig(), bot=BotConfig(), strategy=StrategyConfig(),
    )


def make_state(res: dict, cap: dict, build_queue=None) -> GameState:
    return GameState(
        resources=Resources(**{k: float(v) for k, v in res.items()}),
        capacity=Capacity(**{k: float(v) for k, v in cap.items()}),
        build_queue=build_queue or [],
        max_build_slots=2,
        population_free=100, population_max=400,  # 25% frei — im Soll
        satisfaction=0.90,
    )


# ── Capacity.fill_ratio ──────────────────────────────────────────────────────

def test_fill_ratio_normal():
    cap = Capacity(iron=2000)
    res = Resources(iron=1600)
    assert cap.fill_ratio("iron", res) == pytest.approx(0.80)


def test_fill_ratio_zero_capacity():
    cap = Capacity(iron=0)
    res = Resources(iron=500)
    assert cap.fill_ratio("iron", res) == 0.0


def test_fill_ratio_capped_at_1():
    cap = Capacity(iron=1000)
    res = Resources(iron=2000)
    assert cap.fill_ratio("iron", res) == 1.0


# ── parse_state kapazität ────────────────────────────────────────────────────

def test_parse_state_capacity():
    city = {
        "id": 1, "name": "T", "coords": "1:1:1", "planet_type": "ice",
        "fp": 0, "points": 0, "population_free": 0, "population_total": 0,
        "max_parallel_builds": 2,
        "resources": {"iron": 1600, "steel": 0, "chemicals": 0,
                      "ice": 0, "water": 0, "energy": 0, "vv4a": 0, "credits": 0},
        "rates": {},
        "capacity": {"iron": 2000, "steel": 1000, "chemicals": 0,
                     "ice": 500, "water": 500, "energy": 800, "vv4a": 0},
    }
    state = parse_state({"city": city})
    assert state.capacity.iron == 2000
    assert state.capacity.fill_ratio("iron", state.resources) == pytest.approx(0.80)


# ── Strategy._check_storage ──────────────────────────────────────────────────

def test_no_storage_action_below_threshold():
    strategy = Strategy(make_config())
    # 79% → unter Schwelle
    state = make_state(
        res={"iron": 790}, cap={"iron": 1000}
    )
    action = strategy._check_storage(state)
    assert action is None


def test_storage_action_at_threshold():
    strategy = Strategy(make_config())
    state = make_state(res={"iron": 800}, cap={"iron": 1000})
    action = strategy._check_storage(state)
    assert action is not None
    assert action.type == "build_storage"
    assert action.params["building_type"] == "iron_storage_small"
    assert action.params["resource"] == "iron"
    assert action.params["fill_ratio"] == pytest.approx(0.80)


def test_storage_action_above_threshold():
    strategy = Strategy(make_config())
    state = make_state(res={"energy": 950}, cap={"energy": 1000})
    action = strategy._check_storage(state)
    assert action is not None
    assert action.params["building_type"] == "energy_storage"


def test_most_full_resource_chosen():
    strategy = Strategy(make_config())
    # iron 95%, steel 82%, chemicals 81% → iron is most urgent
    state = make_state(
        res={"iron": 950, "steel": 820, "chemicals": 810},
        cap={"iron": 1000, "steel": 1000, "chemicals": 1000},
    )
    action = strategy._check_storage(state)
    assert action is not None
    assert action.params["resource"] == "iron"


def test_ice_water_share_one_building():
    strategy = Strategy(make_config())
    # Beide über Schwelle, aber dasselbe Lager
    state = make_state(
        res={"ice": 900, "water": 850},
        cap={"ice": 1000, "water": 1000},
    )
    action = strategy._check_storage(state)
    assert action is not None
    assert action.params["building_type"] == "ice_water_storage"
    # Nur EINE Aktion, nicht zwei
    actions = Strategy(make_config()).decide(state)
    storage_actions = [a for a in actions if a.type == "build_storage"]
    assert len(storage_actions) == 1


def test_no_storage_if_already_in_queue():
    from icewars_bot.state import BuildQueueItem
    strategy = Strategy(make_config())
    state = make_state(
        res={"iron": 900}, cap={"iron": 1000},
        build_queue=[BuildQueueItem("iron_storage_small", "Eisenlager")],
    )
    action = strategy._check_storage(state)
    assert action is None


# ── Strategy.decide: Lager hat Vorrang vor normalem Bau ─────────────────────

def test_storage_beats_normal_build_in_decide():
    strategy = Strategy(make_config())
    state = make_state(res={"steel": 900}, cap={"steel": 1000})
    actions = strategy.decide(state)
    # Erster Aktion muss build_storage sein, nicht build_next_building
    assert actions[0].type == "build_storage"
    assert actions[0].params["building_type"] == "steel_storage_small"


def test_normal_build_when_no_storage_needed():
    strategy = Strategy(make_config())
    state = make_state(res={"iron": 100}, cap={"iron": 2000})
    actions = strategy.decide(state)
    assert actions[0].type == "build_next_building"


# ── STORAGE_BUILDINGS Vollständigkeit ────────────────────────────────────────

def test_storage_buildings_map_entries():
    assert "iron" in STORAGE_BUILDINGS
    assert "steel" in STORAGE_BUILDINGS
    assert "chemicals" in STORAGE_BUILDINGS
    assert "ice" in STORAGE_BUILDINGS
    assert "water" in STORAGE_BUILDINGS
    assert "energy" in STORAGE_BUILDINGS
    # ice und water zeigen auf dasselbe Gebäude
    assert STORAGE_BUILDINGS["ice"][0] == STORAGE_BUILDINGS["water"][0]
