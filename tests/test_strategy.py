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
    state = GameState(
        max_build_slots=2, build_queue=[],
        population_free=100, population_max=400, satisfaction=0.90,
        # Positive Raten — sonst triggert _fix_negative_rate
        rates=Rates(iron=10, steel=10, chemicals=10, ice=10,
                    water=10, energy=10, vv4a=10, credits=10, fp=10),
    )
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

def test_cheapest_affordable_wins_regardless_of_priority_list():
    """Neue Policy: immer günstigstes leistbares Item forschen, egal ob es
    in RESEARCH_PRIORITY steht oder nicht. Priority-Liste ist nur Tiebreaker
    bei identischen FP-Kosten."""
    strategy = Strategy(make_config())
    state = GameState(
        resources=Resources(fp=10000),
        research_lab_busy=False,
        research=[
            research_item("astronomie", "Astronomie", affordable=True, fp_cost=700),        # in Liste, teuer
            research_item("unbekannt_xyz", "Unbekanntes XYZ", affordable=True, fp_cost=100), # nicht in Liste, günstig
        ],
    )
    actions = strategy.decide(state)
    research_actions = [a for a in actions if a.type == "start_research"]
    assert len(research_actions) == 1
    assert research_actions[0].params["research_type"] == "unbekannt_xyz"


def test_research_triggered_when_can_afford_stale_but_fp_available():
    """Regressionstest: User-Bug — auto-research hat nichts gestartet, obwohl
    genug FP da waren. Ursache war veralteter ``can_afford``-Cache.
    Neue Logik prüft FP direkt am GameState."""
    strategy = Strategy(make_config())
    state = GameState(
        resources=Resources(fp=2000),
        research_lab_busy=False,
        research=[
            # Server-seitig als 'nicht leistbar' markiert, aber reale FP reichen
            research_item("cheapo", "Cheap Research", affordable=False, fp_cost=1500),
        ],
    )
    actions = strategy.decide(state)
    research_actions = [a for a in actions if a.type == "start_research"]
    assert research_actions, "sollte starten, weil FP am state verfügbar"
    assert research_actions[0].params["research_type"] == "cheapo"


def test_cheapest_wins_over_more_unlocks():
    """Günstigste FP-Kosten gewinnt — auch wenn das teurere Item mehr Gebäude
    freischaltet. User: 'immer das günstigste verfügbare'."""
    strategy = Strategy(make_config())
    state = GameState(
        resources=Resources(fp=10000),
        research_lab_busy=False,
        research=[
            research_item("aaa", "AAA", affordable=True, unlocks_b=[], fp_cost=100),
            research_item("bbb", "BBB", affordable=True, unlocks_b=["Gebäude1", "Gebäude2"], fp_cost=200),
        ],
    )
    actions = strategy.decide(state)
    research_actions = [a for a in actions if a.type == "start_research"]
    assert research_actions[0].params["research_type"] == "aaa"


def test_research_priority_function():
    from icewars_bot.strategy import RESEARCH_PRIORITY
    in_list = research_item(RESEARCH_PRIORITY[0], "Test", affordable=True)
    not_in_list = research_item("xyz_unknown", "Unknown", affordable=True)
    assert _research_priority(in_list) < _research_priority(not_in_list)


# ── Negative Tendenz fixen (Top-Priorität) ────────────────────────────────────

def _good_state(**kwargs) -> GameState:
    """Realistische 'gesunde' Stadt — alle Raten positiv, Bevölkerung OK."""
    base = dict(
        max_build_slots=2, build_queue=[],
        population_free=100, population_max=400, satisfaction=0.90,
        rates=Rates(iron=10, steel=10, chemicals=10, ice=10,
                    water=10, energy=10, vv4a=10, credits=10, fp=10),
    )
    base.update(kwargs)
    return GameState(**base)


def test_negative_iron_rate_builds_iron_mine():
    strategy = Strategy(make_config())
    state = _good_state(rates=Rates(iron=-5, steel=10, chemicals=10, ice=10,
                                     water=10, energy=10, vv4a=10, credits=10, fp=10))
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert build_actions, "sollte ein Produktionsgebäude bauen"
    assert build_actions[0].params["building_type"] == "iron_mine_small"


def test_negative_energy_rate_builds_solar():
    strategy = Strategy(make_config())
    state = _good_state(rates=Rates(iron=10, steel=10, chemicals=10, ice=10,
                                     water=10, energy=-3, vv4a=10, credits=10, fp=10))
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert build_actions
    assert build_actions[0].params["building_type"] == "solar_panels"


def test_worst_negative_rate_first():
    """Wenn mehrere Raten negativ sind, wird die schlimmste zuerst gefixt."""
    strategy = Strategy(make_config())
    state = _good_state(rates=Rates(iron=-1, steel=10, chemicals=10, ice=10,
                                     water=-50, energy=10, vv4a=10, credits=10, fp=10))
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert build_actions[0].params["building_type"] == "tauchsieder"  # -50 ist schlimmer als -1


def test_skip_when_production_already_in_queue():
    """Wenn das Produktionsgebäude schon im Bau ist, weiter zur nächsten Ressource."""
    strategy = Strategy(make_config())
    state = _good_state(
        rates=Rates(iron=-1, steel=10, chemicals=10, ice=10,
                    water=-2, energy=10, vv4a=10, credits=10, fp=10),
        build_queue=[BuildQueueItem("tauchsieder", "Tauchsieder")],
    )
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    # Tauchsieder schon im Bau → nimmt iron_mine_small als nächste negative Rate
    assert build_actions[0].params["building_type"] == "iron_mine_small"


def test_zero_rate_also_triggers():
    """Eine Rate von 0 ist nicht positiv und sollte ebenfalls triggern."""
    strategy = Strategy(make_config())
    state = _good_state(rates=Rates(iron=0, steel=10, chemicals=10, ice=10,
                                     water=10, energy=10, vv4a=10, credits=10, fp=10))
    actions = strategy.decide(state)
    build_actions = [a for a in actions if a.type == "build_specific"]
    assert build_actions[0].params["building_type"] == "iron_mine_small"


def test_all_positive_falls_through_to_normal_build():
    strategy = Strategy(make_config())
    state = _good_state()  # alle Raten = 10
    actions = strategy.decide(state)
    assert any(a.type == "build_next_building" for a in actions)
