from icewars_bot.config import Config, AuthConfig, BrowserConfig, BotConfig, StrategyConfig
from icewars_bot.state import (
    GameState, Resources, Rates, ResearchItem, BuildQueueItem, ActiveResearch
)
from icewars_bot.strategy import Strategy, Action, _research_priority


def make_config(aggression: str = "balanced") -> Config:
    return Config(
        auth=AuthConfig(username="test", password="test", game_url="http://localhost"),
        browser=BrowserConfig(),
        bot=BotConfig(),
        strategy=StrategyConfig(aggression=aggression),
    )


def research_item(type_: str, name: str, *, researched=False, prereq=True, affordable=True,
                  fp_cost=500, unlocks_b=None, unlocks_s=None) -> ResearchItem:
    return ResearchItem(
        type=type_, name=name, fp_cost=fp_cost,
        is_researched=researched, has_prereq=prereq, can_afford=affordable,
        unlocks_buildings=unlocks_b or [], unlocks_ships=unlocks_s or [],
    )


# ---------- Gebäudebau ----------

def test_build_action_when_slots_free():
    strategy = Strategy(make_config())
    state = GameState(max_build_slots=2, build_queue=[],
                      population_free=100, population_max=400, satisfaction=0.90)
    actions = strategy.decide(state)
    assert any(a.type == "build_next_building" for a in actions)


def test_no_build_when_all_slots_full():
    strategy = Strategy(make_config())
    state = GameState(max_build_slots=2, build_queue=[
        BuildQueueItem("eisenmine", "Eisenmine"),
        BuildQueueItem("stahlwerk", "Stahlwerk"),
    ])
    actions = strategy.decide(state)
    assert not any(a.type == "build_next_building" for a in actions)


# ---------- Forschung: Labor-Status ----------

def test_no_research_when_lab_busy():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=True,
        active_research=ActiveResearch("astronomie", "Astronomie", remaining_sec=600),
        research=[research_item("bergbau", "Bergbau", affordable=True)],
    )
    actions = strategy.decide(state)
    assert not any(a.type == "start_research" for a in actions)


def test_research_started_when_lab_free():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=False,
        research=[research_item("bergbau", "Bergbau", affordable=True)],
    )
    actions = strategy.decide(state)
    assert any(a.type == "start_research" for a in actions)


# ---------- Forschung: Auswahl-Logik ----------

def test_no_research_if_not_affordable():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=False,
        research=[research_item("bergbau", "Bergbau", affordable=False)],
    )
    actions = strategy.decide(state)
    assert not any(a.type == "start_research" for a in actions)


def test_no_research_if_already_done():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=False,
        research=[research_item("bergbau", "Bergbau", researched=True, affordable=True)],
    )
    actions = strategy.decide(state)
    assert not any(a.type == "start_research" for a in actions)


def test_no_research_if_prereq_missing():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=False,
        research=[research_item("advanced", "Fortgeschritten", prereq=False, affordable=True)],
    )
    actions = strategy.decide(state)
    assert not any(a.type == "start_research" for a in actions)


def test_research_uses_can_afford_not_manual_fp_check():
    """can_afford=True reicht — kein manueller FP-Vergleich."""
    strategy = Strategy(make_config())
    # fp=0 aber can_afford=True (Server sagt es ist leistbar)
    state = GameState(
        resources=Resources(fp=0),
        research_lab_busy=False,
        research=[research_item("bergbau", "Bergbau", affordable=True, fp_cost=9999)],
    )
    actions = strategy.decide(state)
    assert any(a.type == "start_research" for a in actions)


# ---------- Forschung: Priorität ----------

def test_priority_list_item_beats_unlisted():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=False,
        research=[
            research_item("astronomie", "Astronomie", affordable=True, fp_cost=700),        # in Liste
            research_item("unbekannt_xyz", "Unbekanntes XYZ", affordable=True, fp_cost=100), # nicht in Liste
        ],
    )
    actions = strategy.decide(state)
    research_actions = [a for a in actions if a.type == "start_research"]
    assert len(research_actions) == 1
    assert research_actions[0].params["research_type"] == "astronomie"


def test_more_unlocks_beats_fewer_unlocks_outside_priority_list():
    strategy = Strategy(make_config())
    state = GameState(
        research_lab_busy=False,
        research=[
            research_item("aaa", "AAA", affordable=True, unlocks_b=[], fp_cost=100),
            research_item("bbb", "BBB", affordable=True, unlocks_b=["Gebäude1", "Gebäude2"], fp_cost=200),
        ],
    )
    actions = strategy.decide(state)
    research_actions = [a for a in actions if a.type == "start_research"]
    assert research_actions[0].params["research_type"] == "bbb"


def test_research_priority_function():
    from icewars_bot.strategy import RESEARCH_PRIORITY
    in_list = research_item(RESEARCH_PRIORITY[0], "Test", affordable=True)
    not_in_list = research_item("xyz_unknown", "Unknown", affordable=True)
    assert _research_priority(in_list) < _research_priority(not_in_list)
