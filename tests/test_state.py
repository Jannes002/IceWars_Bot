from icewars_bot.state import parse_state, ActiveResearch


def _city(**kwargs):
    base = {
        "id": 1, "name": "Test", "coords": "1:1:1",
        "planet_type": "ice", "resources": {}, "rates": {},
        "max_parallel_builds": 2, "fp": 0, "points": 0,
        "population_free": 10, "population_total": 100,
    }
    base.update(kwargs)
    return base


def test_parse_empty_raw():
    state = parse_state({})
    assert state.city_id == 0
    assert state.resources.iron == 0
    assert state.resources.fp == 0
    assert state.research_lab_busy is False
    assert state.active_research is None


def test_parse_resources():
    city = _city(
        resources={"iron": 2000, "steel": 1000, "ice": 1500, "energy": 800,
                   "chemicals": 500, "water": 600, "vv4a": 100, "credits": 250},
        rates={"iron": 120, "steel": 130, "research_points": 38},
        fp=8454,
        points=960,
    )
    state = parse_state({"city": city})
    assert state.resources.iron == 2000
    assert state.resources.fp == 8454
    assert state.rates.fp == 38
    assert state.points == 960


def test_parse_build_queue():
    city = _city(build_queue=[
        {"type": "eisenmine", "name": "Eisenmine", "finish_time": "2026-04-07T12:00:00"}
    ])
    state = parse_state({"city": city})
    assert len(state.build_queue) == 1
    assert state.build_queue[0].building_type == "eisenmine"


def test_parse_research_can_afford():
    research_items = [
        {"type": "bautechnik", "name": "Bautechnik", "evo": 0, "fp_cost": 250,
         "res_cost": {}, "is_researched": True, "has_prereq": True, "can_afford": False,
         "time_sec": 500, "unlocks_b_data": [], "unlocks_s_data": []},
        {"type": "astronomie", "name": "Astronomie", "evo": 0, "fp_cost": 700,
         "res_cost": {}, "is_researched": False, "has_prereq": True, "can_afford": True,
         "time_sec": 1400, "unlocks_b_data": [{"name": "Sternenwarte"}], "unlocks_s_data": []},
    ]
    state = parse_state({"city": _city(), "research": research_items})
    assert state.research[0].is_researched is True
    assert state.research[0].can_afford is False
    assert state.research[1].can_afford is True
    assert state.research[1].unlocks_buildings == ["Sternenwarte"]


def test_parse_active_research():
    state = parse_state({
        "city": _city(),
        "active_research": {
            "type": "astronomie",
            "name": "Astronomie",
            "remaining_sec": 1100,
            "finish_time": "2026-04-07T12:50:00",
        },
        "research_lab_busy": True,
    })
    assert state.research_lab_busy is True
    assert state.active_research is not None
    assert state.active_research.type == "astronomie"
    assert state.active_research.remaining_sec == 1100


def test_parse_no_active_research():
    state = parse_state({"city": _city(), "research_lab_busy": False})
    assert state.research_lab_busy is False
    assert state.active_research is None
