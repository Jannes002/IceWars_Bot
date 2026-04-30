"""Persistenter Speicher für bekannte Planeten/Kolonien.

Speichert die Planetenliste in data/planets.json damit der Bot nach einem
Neustart alle bekannten Planeten sofort kennt.

Felder pro Planet (persistent):
    id, name, coords, planet_type, _last_visited

Felder die beim Start zurückgesetzt werden:
    _build_free_at  (Bauwarteschlangen-Status unbekannt nach Neustart)
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLANETS_PATH = Path("data/planets.json")
_PERSIST_KEYS = {"id", "name", "coords", "planet_type", "_last_visited"}

_lock = threading.Lock()


def _load_raw() -> dict:
    if not PLANETS_PATH.exists():
        return {"known": [], "excluded": []}
    try:
        with open(PLANETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if "known" not in data:
            data["known"] = []
        if "excluded" not in data:
            data["excluded"] = []
        return data
    except Exception as e:
        logger.error("planets.json Ladefehler: %s", e)
        return {"known": [], "excluded": []}


def _save_raw(data: dict) -> None:
    PLANETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PLANETS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load() -> tuple[list[dict], set[int]]:
    """Gibt (known_planets, excluded_ids) zurück.

    known_planets: Liste von Dicts mit id, name, coords usw.
    excluded_ids:  Set von city_ids die dauerhaft ignoriert werden.

    _build_free_at wird auf 0 gesetzt (nach Neustart unbekannt).
    """
    with _lock:
        data = _load_raw()
        planets = []
        for p in data.get("known", []):
            entry = {k: v for k, v in p.items() if k in _PERSIST_KEYS}
            entry.setdefault("_last_visited", 0)
            entry["_build_free_at"] = 0   # nach Neustart unbekannt
            planets.append(entry)
        excluded = set(int(x) for x in data.get("excluded", []))
        return planets, excluded


def save(planet_cities: list[dict]) -> None:
    """Speichert die aktuelle Planetenliste (nur persistente Felder)."""
    with _lock:
        data = _load_raw()
        data["known"] = [
            {k: v for k, v in p.items() if k in _PERSIST_KEYS}
            for p in planet_cities
            if p.get("id")
        ]
        _save_raw(data)
        logger.debug("Planeten gespeichert: %d Einträge", len(data["known"]))


def remove(city_id: int) -> None:
    """Entfernt einen Planeten und merkt ihn als dauerhaft ausgeschlossen."""
    with _lock:
        data = _load_raw()
        data["known"] = [p for p in data["known"] if p.get("id") != city_id]
        excluded = set(int(x) for x in data.get("excluded", []))
        excluded.add(city_id)
        data["excluded"] = sorted(excluded)
        _save_raw(data)
        logger.info("Planet %d entfernt und ausgeschlossen.", city_id)


def get_all() -> list[dict]:
    """Gibt alle bekannten Planeten zurück (nur für Dashboard-Nutzung)."""
    with _lock:
        return _load_raw().get("known", [])
