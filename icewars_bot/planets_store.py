"""Persistenter Speicher für bekannte Planeten/Kolonien.

Speichert die Planetenliste in data/planets.json damit der Bot nach einem
Neustart alle bekannten Planeten sofort kennt.

Felder pro Planet (persistent):
    id, name, coords, planet_type, _last_visited

Felder die beim Start zurückgesetzt werden:
    _build_free_at  (Bauwarteschlangen-Status unbekannt nach Neustart)

Datei-Sicherheit:
    Schreibvorgänge sind atomar (temp-Datei + os.replace), damit der Bot bei
    einem harten Kill (OOM, SIGKILL) niemals eine leere/korrupte planets.json
    hinterlässt.  Eine .bak-Datei enthält den letzten guten Stand als Fallback.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLANETS_PATH = Path("data/planets.json")
_BACKUP_PATH  = Path("data/planets.json.bak")
_PERSIST_KEYS = {"id", "name", "coords", "planet_type", "_last_visited"}

_lock = threading.Lock()


# ── Interne Helfer ────────────────────────────────────────────────────────────

def _load_file(path: Path) -> dict | None:
    """Liest und parst eine JSON-Datei. Gibt None zurück wenn nicht vorhanden oder korrupt."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Ungültiges Format (kein Dict)")
        data.setdefault("known", [])
        data.setdefault("excluded", [])
        return data
    except Exception as e:
        logger.error("planets_store: Fehler beim Laden von %s: %s", path.name, e)
        return None


def _load_raw() -> dict:
    """Lädt planets.json, fällt auf Backup zurück wenn nötig.

    Gibt immer ein valides Dict zurück — niemals None.
    """
    # 1. Primärdatei versuchen
    data = _load_file(PLANETS_PATH)
    if data is not None:
        return data

    # 2. Backup versuchen (falls Primärdatei fehlt oder korrupt ist)
    if PLANETS_PATH.exists():
        # Datei existiert, aber ist korrupt → Backup versuchen
        backup = _load_file(_BACKUP_PATH)
        if backup is not None:
            logger.warning(
                "planets.json korrupt — verwende Backup (%d Planeten).",
                len(backup.get("known", [])),
            )
            return backup
        logger.error(
            "planets.json korrupt und kein Backup verfügbar — starte mit leerer Liste."
        )
    # Datei existiert schlicht nicht (Erststart)
    return {"known": [], "excluded": []}


def _save_raw(data: dict) -> None:
    """Schreibt data atomar in PLANETS_PATH.

    Strategie: in eine temp-Datei schreiben, dann os.replace() (atomar auf POSIX).
    Die zuletzt erfolgreiche Datei wird zusätzlich als .bak gesichert.
    """
    PLANETS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLANETS_PATH.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        # Vor dem Überschreiben ein Backup der letzten guten Datei anlegen
        if PLANETS_PATH.exists():
            try:
                import shutil
                shutil.copy2(PLANETS_PATH, _BACKUP_PATH)
            except Exception as e:
                logger.debug("Backup-Kopie fehlgeschlagen (nicht kritisch): %s", e)
        # Atomares Umbenennen — ersetzt PLANETS_PATH in einem Schritt
        os.replace(tmp, PLANETS_PATH)
    except Exception:
        # Temp-Datei aufräumen falls vorhanden
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── Öffentliche API ───────────────────────────────────────────────────────────

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
            if not isinstance(p, dict) or not p.get("id"):
                continue
            entry = {k: v for k, v in p.items() if k in _PERSIST_KEYS}
            entry.setdefault("_last_visited", 0)
            entry["_build_free_at"] = 0
            planets.append(entry)
        excluded = set(int(x) for x in data.get("excluded", []) if x)
        logger.info(
            "Planeten geladen: %d bekannt, %d ausgeschlossen (Quelle: %s)",
            len(planets),
            len(excluded),
            "planets.json" if PLANETS_PATH.exists() else "leer",
        )
        return planets, excluded


def save(planet_cities: list[dict]) -> None:
    """Speichert die aktuelle Planetenliste (nur persistente Felder).

    Schreibt atomar — die Datei ist niemals in einem Zwischenzustand.
    """
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
        excluded = set(int(x) for x in data.get("excluded", []) if x)
        excluded.add(city_id)
        data["excluded"] = sorted(excluded)
        _save_raw(data)
        logger.info("Planet %d entfernt und ausgeschlossen.", city_id)


def get_all() -> list[dict]:
    """Gibt alle bekannten Planeten zurück (nur für Dashboard-Nutzung)."""
    with _lock:
        return _load_raw().get("known", [])
