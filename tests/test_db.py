"""Tests für die SQLite-Datenbank (Snapshots, Sessions, Queries)."""
import time
import pytest
from pathlib import Path

from icewars_bot.state import GameState, Resources, Rates, Capacity
from icewars_bot.db import (
    init_db, record_snapshot, start_session, end_session,
    get_snapshots, get_sessions, get_latest_snapshot, get_snapshot_count,
    record_highscores, get_highscores, get_highscore_timeline, get_latest_highscore,
    record_build_event, get_build_events,
)


@pytest.fixture
def tmp_db(tmp_path):
    """Erstellt eine temporäre Datenbank."""
    db = tmp_path / "test.db"
    init_db(db)
    return db


def make_state(**kwargs) -> GameState:
    defaults = dict(
        resources=Resources(iron=1000, steel=500, chemicals=200, ice=300,
                           water=150, energy=800, vv4a=50, credits=75, fp=120),
        rates=Rates(iron=100, steel=50, chemicals=20, ice=30,
                   water=15, energy=80, vv4a=5, credits=-10, fp=12),
        capacity=Capacity(iron=2000, steel=1000, chemicals=500, ice=600,
                         water=300, energy=1500, vv4a=100),
        population_free=80, population_total=350, population_max=400,
        satisfaction=0.85, living_conditions=110.5, eco_points=-3,
        credits_rate=-10, points=1500, max_build_slots=2,
    )
    defaults.update(kwargs)
    return GameState(**defaults)


# ── init_db ─────────────────────────────────────────────────────────────────

def test_init_db_creates_file(tmp_path):
    db = tmp_path / "sub" / "test.db"
    init_db(db)
    assert db.exists()


def test_init_db_idempotent(tmp_db):
    # Zweimaliges init soll keinen Fehler geben
    init_db(tmp_db)


# ── record_snapshot ─────────────────────────────────────────────────────────

def test_record_snapshot(tmp_db):
    state = make_state()
    record_snapshot(state, tmp_db)
    count = get_snapshot_count(tmp_db)
    assert count == 1


def test_snapshot_contains_all_values(tmp_db):
    state = make_state()
    record_snapshot(state, tmp_db)
    snap = get_latest_snapshot(tmp_db)

    assert snap["iron"] == 1000
    assert snap["steel"] == 500
    assert snap["chemicals"] == 200
    assert snap["ice"] == 300
    assert snap["water"] == 150
    assert snap["energy"] == 800
    assert snap["vv4a"] == 50
    assert snap["credits"] == 75
    assert snap["fp"] == 120

    assert snap["iron_rate"] == 100
    assert snap["credits_rate"] == -10

    assert snap["iron_cap"] == 2000

    assert snap["population_free"] == 80
    assert snap["population_total"] == 350
    assert snap["population_max"] == 400

    assert snap["satisfaction"] == pytest.approx(0.85)
    assert snap["living_conditions"] == pytest.approx(110.5)
    assert snap["eco_points"] == -3
    assert snap["points"] == 1500


def test_multiple_snapshots(tmp_db):
    for i in range(5):
        record_snapshot(make_state(resources=Resources(iron=i * 100)), tmp_db)

    assert get_snapshot_count(tmp_db) == 5
    snaps = get_snapshots(path=tmp_db)
    assert len(snaps) == 5
    # Chronologische Reihenfolge
    assert snaps[0]["epoch"] <= snaps[-1]["epoch"]


# ── Sessions ────────────────────────────────────────────────────────────────

def test_start_and_end_session(tmp_db):
    sid = start_session(tmp_db)
    assert sid >= 1

    end_session(sid, {"turns": 10, "executed": 8, "failed": 2}, tmp_db)

    sessions = get_sessions(path=tmp_db)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["turns_completed"] == 10
    assert s["actions_executed"] == 8
    assert s["actions_failed"] == 2
    assert s["end_epoch"] is not None


def test_multiple_sessions(tmp_db):
    s1 = start_session(tmp_db)
    end_session(s1, {"turns": 5, "executed": 3, "failed": 0}, tmp_db)
    s2 = start_session(tmp_db)

    sessions = get_sessions(path=tmp_db)
    assert len(sessions) == 2
    # Zweite Session noch offen
    assert sessions[1]["end_epoch"] is None


# ── Queries mit Zeitfilter ──────────────────────────────────────────────────

def test_snapshot_time_filter(tmp_db):
    state = make_state()
    t1 = time.time()
    record_snapshot(state, tmp_db)

    # Nur Snapshots nach jetzt+1000 → leer
    snaps = get_snapshots(from_epoch=t1 + 1000, path=tmp_db)
    assert len(snaps) == 0

    # Alles ab vor 1 Sekunde → sollte den Snapshot enthalten
    snaps = get_snapshots(from_epoch=t1 - 1, path=tmp_db)
    assert len(snaps) == 1


def test_get_latest_snapshot_empty(tmp_db):
    assert get_latest_snapshot(tmp_db) is None


def test_get_snapshot_count_empty(tmp_db):
    assert get_snapshot_count(tmp_db) == 0


# ── Highscores ──────────────────────────────────────────────────────────────

SAMPLE_HS = {
    "points": {
        "category": "points",
        "title": "Gesamtpunkte",
        "entries": [
            {"rank": 1, "username": "admin", "user_id": 12, "alliance": "[THX]", "value": 23480, "detail": "2 Planeten"},
            {"rank": 2, "username": "Mycrowd", "user_id": 32, "alliance": "[THX]", "value": 8295, "detail": "2 Planeten"},
            {"rank": 3, "username": "admin12345", "user_id": 38, "alliance": "[SCAM]", "value": 1035, "detail": "1 Planet"},
        ],
    },
    "research": {
        "category": "research",
        "title": "Forschung",
        "entries": [
            {"rank": 1, "username": "admin", "user_id": 12, "alliance": "[THX]", "value": 175, "detail": ""},
            {"rank": 2, "username": "admin12345", "user_id": 38, "alliance": "[SCAM]", "value": 50, "detail": ""},
        ],
    },
}


def test_record_highscores(tmp_db):
    record_highscores(SAMPLE_HS, tmp_db)
    rows = get_highscores(path=tmp_db)
    assert len(rows) == 5  # 3 points + 2 research


def test_highscore_filter_by_category(tmp_db):
    record_highscores(SAMPLE_HS, tmp_db)
    rows = get_highscores(category="points", path=tmp_db)
    assert len(rows) == 3
    assert all(r["category"] == "points" for r in rows)


def test_highscore_filter_by_username(tmp_db):
    record_highscores(SAMPLE_HS, tmp_db)
    rows = get_highscores(username="admin12345", path=tmp_db)
    assert len(rows) == 2  # einmal in points, einmal in research


def test_latest_highscore(tmp_db):
    record_highscores(SAMPLE_HS, tmp_db)
    latest = get_latest_highscore("points", tmp_db)
    assert len(latest) == 3
    assert latest[0]["rank"] == 1
    assert latest[0]["username"] == "admin"


def test_highscore_timeline(tmp_db):
    # Zwei Zeitpunkte simulieren
    record_highscores(SAMPLE_HS, tmp_db)
    time.sleep(0.05)
    # Zweites Recording mit verbessertem Rang
    hs2 = {
        "points": {
            "category": "points",
            "entries": [
                {"rank": 1, "username": "admin", "user_id": 12, "alliance": "[THX]", "value": 24000},
                {"rank": 2, "username": "admin12345", "user_id": 38, "alliance": "[SCAM]", "value": 9000},
            ],
        },
    }
    record_highscores(hs2, tmp_db)

    tl = get_highscore_timeline("admin12345", "points", path=tmp_db)
    assert len(tl) == 2
    assert tl[0]["rank"] == 3  # erster Snapshot: Rang 3
    assert tl[1]["rank"] == 2  # zweiter Snapshot: Rang 2


def test_latest_highscore_empty(tmp_db):
    assert get_latest_highscore("points", tmp_db) == []


# ── Build Events ─────────────────────────────────────────────────────────────

def test_record_and_get_build_events(tmp_db):
    record_build_event("build", "iron_mine", "Eisenmine", tmp_db)
    record_build_event("research", "mining_tech", "Bergbau-Tech", tmp_db)
    events = get_build_events(path=tmp_db)
    assert len(events) == 2
    types = {e["event_type"] for e in events}
    assert types == {"build", "research"}


def test_build_events_filter_by_type(tmp_db):
    record_build_event("build", "tent", "Zelt", tmp_db)
    record_build_event("research", "energy_1", "Energieforschung", tmp_db)
    builds = get_build_events(event_type="build", path=tmp_db)
    assert all(e["event_type"] == "build" for e in builds)
    assert len(builds) == 1


def test_build_events_filter_by_time(tmp_db):
    import time as _t
    t0 = _t.time()
    record_build_event("build", "storage", "Lager", tmp_db)
    t1 = _t.time()
    events = get_build_events(from_epoch=t0, to_epoch=t1 + 1, path=tmp_db)
    assert len(events) == 1
    assert events[0]["type_key"] == "storage"


def test_build_events_name_stored(tmp_db):
    record_build_event("build", "water_pump", "Wasserpumpe Stufe 2", tmp_db)
    events = get_build_events(path=tmp_db)
    assert events[0]["name"] == "Wasserpumpe Stufe 2"
    assert events[0]["type_key"] == "water_pump"
