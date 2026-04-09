"""Tests für Bevölkerung, Zufriedenheit, Credits und Bau-Priorisierung."""
import pytest
from icewars_bot.config import Config, AuthConfig, BrowserConfig, BotConfig, StrategyConfig
from icewars_bot.state import (
    GameState, Resources, Rates, Capacity, BuildQueueItem, ActiveResearch,
)
from icewars_bot.strategy import (
    Strategy, SATISFACTION_MIN, SATISFACTION_WARN,
    POP_FREE_MIN, POP_FREE_MAX, CREDITS_WARN_BALANCE,
)


def cfg() -> Config:
    return Config(
        auth=AuthConfig(username="t", password="t", game_url="http://x"),
        browser=BrowserConfig(), bot=BotConfig(), strategy=StrategyConfig(),
    )


def gs(**kwargs) -> GameState:
    defaults = dict(
        max_build_slots=2,
        population_free=80, population_total=400, population_max=400,
        satisfaction=0.90, credits_rate=0, eco_points=0,
        resources=Resources(credits=100),
        # Alle Raten positiv per Default — sonst triggert _fix_negative_rate
        rates=Rates(iron=10, steel=10, chemicals=10, ice=10,
                    water=10, energy=10, vv4a=10, credits=10, fp=10),
    )
    defaults.update(kwargs)
    return GameState(**defaults)


# ═══════════════════════════════════════════════════════════════════════════════
#  free_pop_ratio property
# ═══════════════════════════════════════════════════════════════════════════════

def test_free_pop_ratio():
    state = gs(population_free=80, population_max=400)
    assert state.free_pop_ratio == pytest.approx(0.20)


def test_free_pop_ratio_zero_max():
    state = gs(population_max=0)
    assert state.free_pop_ratio == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
#  Priorität 1: Zufriedenheit
# ═══════════════════════════════════════════════════════════════════════════════

def test_critical_satisfaction_triggers_happiness_build():
    s = Strategy(cfg())
    state = gs(satisfaction=-0.05)
    actions = s.decide(state)
    assert actions[0].type == "build_specific"
    assert actions[0].params["building_type"] in ("outhouse", "scout_camp", "park", "asylum")


def test_low_satisfaction_triggers_happiness_build():
    s = Strategy(cfg())
    state = gs(satisfaction=0.30)  # < SATISFACTION_WARN (50%)
    actions = s.decide(state)
    assert actions[0].type == "build_specific"


def test_good_satisfaction_no_happiness_build():
    s = Strategy(cfg())
    state = gs(satisfaction=0.80)
    actions = s.decide(state)
    # Kein Zufriedenheits-Gebäude — normaler Bau
    assert actions[0].type != "build_specific" or "Zufriedenheit" not in actions[0].params.get("reason", "")


# ═══════════════════════════════════════════════════════════════════════════════
#  Priorität: Credits-Schutz bei Zufriedenheitsbau
# ═══════════════════════════════════════════════════════════════════════════════

def test_credits_tight_only_builds_outhouse():
    """Bei knappen Credits sollte Plumpsklo (ohne Credits-Kosten) gewählt werden."""
    s = Strategy(cfg())
    state = gs(satisfaction=0.10, credits_rate=-10, resources=Resources(credits=5))
    actions = s.decide(state)
    assert actions[0].type == "build_specific"
    assert actions[0].params["building_type"] == "outhouse"


# ═══════════════════════════════════════════════════════════════════════════════
#  Priorität 2: Bevölkerung
# ═══════════════════════════════════════════════════════════════════════════════

def test_low_population_triggers_housing():
    s = Strategy(cfg())
    # 10% frei → unter 20%-Schwelle
    state = gs(population_free=40, population_max=400, satisfaction=0.90)
    actions = s.decide(state)
    assert actions[0].type == "build_specific"
    assert actions[0].params["building_type"] in ("tent", "house_small", "house_medium")


def test_adequate_population_no_housing():
    s = Strategy(cfg())
    # 25% frei → im Sollbereich
    state = gs(population_free=100, population_max=400, satisfaction=0.90)
    actions = s.decide(state)
    # Sollte normaler Bau oder Lager sein, nicht Wohngebäude
    if actions[0].type == "build_specific":
        assert actions[0].params["building_type"] not in ("tent", "house_small")


# ═══════════════════════════════════════════════════════════════════════════════
#  Priorität 3: Lager (bereits existierende Tests in test_storage.py)
# ═══════════════════════════════════════════════════════════════════════════════

def test_storage_before_normal_build():
    s = Strategy(cfg())
    state = gs(
        resources=Resources(iron=950, credits=100),
        capacity=Capacity(iron=1000),
    )
    actions = s.decide(state)
    assert actions[0].type == "build_storage"


# ═══════════════════════════════════════════════════════════════════════════════
#  Priorität 4: Normaler Bau als Fallback
# ═══════════════════════════════════════════════════════════════════════════════

def test_normal_build_when_all_ok():
    s = Strategy(cfg())
    state = gs(
        satisfaction=0.90,
        population_free=100, population_max=400,
        resources=Resources(iron=100, credits=100),
        capacity=Capacity(iron=5000),
    )
    actions = s.decide(state)
    assert actions[0].type == "build_next_building"


# ═══════════════════════════════════════════════════════════════════════════════
#  Gesamtpriorität: Zufriedenheit > Bevölkerung > Lager > Normal
# ═══════════════════════════════════════════════════════════════════════════════

def test_satisfaction_beats_population():
    """Zufriedenheit hat Vorrang vor Bevölkerungsbau."""
    s = Strategy(cfg())
    state = gs(
        satisfaction=-0.10,
        population_free=10, population_max=400,  # auch schlecht
    )
    actions = s.decide(state)
    assert actions[0].type == "build_specific"
    assert "Zufriedenheit" in actions[0].params.get("reason", "")


def test_population_beats_storage():
    """Bevölkerung hat Vorrang vor Lagerbau."""
    s = Strategy(cfg())
    state = gs(
        satisfaction=0.90,
        population_free=10, population_max=400,
        resources=Resources(iron=950, credits=100),
        capacity=Capacity(iron=1000),
    )
    actions = s.decide(state)
    assert actions[0].type == "build_specific"
    assert actions[0].params["building_type"] in ("tent", "house_small", "house_medium")


def test_no_build_action_when_slots_full():
    s = Strategy(cfg())
    state = gs(
        build_queue=[
            BuildQueueItem("eisenmine", "Eisenmine"),
            BuildQueueItem("stahlwerk", "Stahlwerk"),
        ],
    )
    actions = s.decide(state)
    build_actions = [a for a in actions if a.type in ("build_specific", "build_storage", "build_next_building")]
    assert len(build_actions) == 0


def test_skip_housing_already_in_queue():
    s = Strategy(cfg())
    state = gs(
        population_free=10, population_max=400,
        satisfaction=0.90,
        max_build_slots=3,  # 3 Slots damit 2 belegt + 1 frei
        build_queue=[
            BuildQueueItem("tent", "Zelt"),
            BuildQueueItem("house_medium", "Mittleres Wohnhaus"),
        ],
    )
    actions = s.decide(state)
    # tent + house already in queue → should try house_small
    if actions[0].type == "build_specific":
        assert actions[0].params["building_type"] == "house_small"
