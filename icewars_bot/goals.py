"""Ziele (Sollwerte) die der Bot einhalten soll.

Werden als JSON in data/goals.json gespeichert und können über das
Dashboard live angepasst werden. Die Strategy liest die Ziele bei
jeder Entscheidung neu ein — Änderungen wirken sofort.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

GOALS_PATH = Path("data/goals.json")

# ── Standardwerte (entsprechen den bisherigen Hardcoded-Konstanten) ───────────
DEFAULTS: dict[str, Any] = {
    # Zufriedenheit: API-Wert als Dezimalzahl (1.0 = 100 %, 1.5 = 150 %)
    "satisfaction_warn":        0.50,   # < 50 % → Zufriedenheitsgebäude bauen
    "satisfaction_critical":    0.0,    # < 0 %  → sofort gegensteuern

    # Lebensbedingungen in % (z.B. 100 = normal, 150 = Ziel)
    "living_conditions_target": 100.0,

    # Bevölkerung: Anteil von population_max der frei sein soll (0–1)
    "pop_free_min":  0.20,   # < 20 % → Wohnraum bauen
    "pop_free_max":  0.40,   # > 40 % → kein weiterer Wohnraum nötig

    # Credits: wenn Rate negativ und Bestand < Schwelle → keine teuren Gebäude
    "credits_warn_balance": 50.0,

    # Lager: ab welchem Füllstand ein neues Lager gebaut wird (0–1)
    "storage_threshold": 0.80,

    # Produktions-Priorität: welche Ressource bevorzugt gefördert wird.
    # Gültige Werte: "balanced" (Standard), "iron", "steel", "chemicals",
    #                "ice", "water", "energy", "vv4a", "fp", "credits"
    "priority_resource": "balanced",

    # Pausierte Ressourcen: für diese baut der Bot weder Lager noch
    # Produktionsgebäude und überspringt sie auch bei Priorität/Balanced.
    # Gültige Einträge: "iron", "steel", "chemicals", "ice", "water",
    #                   "energy", "vv4a" (fp/credits bleiben immer aktiv).
    "paused_resources": [],

    # Mindest-Ressourcenmengen (Display-Ziele im Dashboard, keine Bot-Logik)
    "resource_targets": {
        "iron":      0.0,
        "steel":     0.0,
        "chemicals": 0.0,
        "ice":       0.0,
        "water":     0.0,
        "energy":    0.0,
        "vv4a":      0.0,
        "credits":   0.0,
        "fp":        0.0,
    },
}

_lock = threading.RLock()
_goals: dict[str, Any] = {}


def _load_from_disk() -> dict[str, Any]:
    """Lädt Ziele aus JSON, füllt fehlende Felder mit Defaults."""
    if not GOALS_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(GOALS_PATH, encoding="utf-8") as f:
            stored = json.load(f)
        # Defaults als Basis, gespeicherte Werte überschreiben
        merged = dict(DEFAULTS)
        merged.update(stored)
        merged["resource_targets"] = {**DEFAULTS["resource_targets"], **stored.get("resource_targets", {})}
        return merged
    except Exception as e:
        logger.error("goals.json Ladefehler: %s — nutze Defaults.", e)
        return dict(DEFAULTS)


def _save_to_disk(data: dict) -> None:
    GOALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GOALS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _ensure_loaded() -> None:
    global _goals
    if not _goals:
        _goals = _load_from_disk()


# ── Öffentliche API ───────────────────────────────────────────────────────────

def get() -> dict[str, Any]:
    """Gibt eine Kopie aller aktuellen Ziele zurück."""
    with _lock:
        _ensure_loaded()
        return json.loads(json.dumps(_goals))  # deep copy


def update(patch: dict[str, Any]) -> dict[str, Any]:
    """Überschreibt einzelne Felder und speichert auf Disk.

    ``resource_targets`` wird tiefgehend gemergt, nicht ersetzt.
    Listen-Felder (z. B. ``paused_resources``) werden komplett ersetzt —
    genau das gewünschte Verhalten für UI-Toggles.
    Gibt die kompletten aktualisierten Ziele zurück.
    """
    with _lock:
        _ensure_loaded()
        if "resource_targets" in patch and isinstance(patch["resource_targets"], dict):
            _goals["resource_targets"].update(patch["resource_targets"])
            patch = {k: v for k, v in patch.items() if k != "resource_targets"}
        _goals.update(patch)
        _save_to_disk(_goals)
        logger.info("Ziele aktualisiert und gespeichert.")
        return json.loads(json.dumps(_goals))


def reset() -> dict[str, Any]:
    """Setzt alle Ziele auf Standardwerte zurück."""
    with _lock:
        _goals.clear()
        _goals.update(DEFAULTS)
        _save_to_disk(_goals)
        return json.loads(json.dumps(_goals))


# ── Einzelne Zugriffs-Helfer für die Strategy ────────────────────────────────

def satisfaction_warn() -> float:
    return float(get().get("satisfaction_warn", DEFAULTS["satisfaction_warn"]))

def satisfaction_critical() -> float:
    return float(get().get("satisfaction_critical", DEFAULTS["satisfaction_critical"]))

def pop_free_min() -> float:
    return float(get().get("pop_free_min", DEFAULTS["pop_free_min"]))

def pop_free_max() -> float:
    return float(get().get("pop_free_max", DEFAULTS["pop_free_max"]))

def credits_warn_balance() -> float:
    return float(get().get("credits_warn_balance", DEFAULTS["credits_warn_balance"]))

def storage_threshold() -> float:
    return float(get().get("storage_threshold", DEFAULTS["storage_threshold"]))

def priority_resource() -> str:
    return str(get().get("priority_resource", DEFAULTS["priority_resource"]))


def paused_resources() -> list[str]:
    """Liste der aktuell pausierten Ressourcen (defensiv gefiltert auf gültige Strings)."""
    raw = get().get("paused_resources", DEFAULTS["paused_resources"])
    if not isinstance(raw, list):
        return []
    return [str(r) for r in raw if isinstance(r, str) and r]


def is_resource_paused(resource: str) -> bool:
    """True wenn der Bot für ``resource`` aktuell weder Lager noch Produktion
    bauen soll. Unbekannte/leere Namen → False (kein Pausen-Effekt)."""
    if not resource:
        return False
    return resource in paused_resources()
