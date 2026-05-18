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

    # Auto-Forschung: wenn False startet der Bot keine neuen Forschungen.
    "auto_research_enabled": True,

    # Auto-Lager: wenn False baut der Bot keine Lagergebäude mehr.
    "auto_storage_enabled": True,

    # Bunker: Mindest-Bunkerkapazität als Anteil der Lagerkapazität (0.0 = deaktiviert)
    "bunker_thresholds": {
        "iron":      0.0,
        "steel":     0.0,
        "chemicals": 0.0,
        "ice":       0.0,
        "water":     0.0,
        "energy":    0.0,
        "vv4a":      0.0,
    },

    # Telegram-Benachrichtigungen: pro Typ ein/ausschalten
    "notify_daily_report":      True,   # 📊 Tagesbericht (täglich 11:30)
    "notify_rank_change":       True,   # 📈📉 Rang-Änderung
    "notify_new_planet":        True,   # 🌍 Neuer Planet/Kolonie entdeckt
    "notify_build_complete":    True,   # 🏗️ Gebäude fertig gebaut
    "notify_new_building_type": True,   # 🏗 Neues Gebäude freigeschaltet (unbekannter Typ)
    "notify_research_complete": True,   # ✅ Forschung abgeschlossen
    "notify_browser_restart":   True,   # ⚠️ Browser-Neustart / Bot-Fehler
    "notify_donation":          True,   # 🤝 Allianz-Spende

    # Auto-Spende: Mindestmenge je Ressource ab der eine Spende ausgelöst wird.
    # Der Bot spendet nur, wenn der aktuelle Bestand >= diesem Wert ist.
    # 0 = kein Minimum (immer spenden wenn der Füllstand-Schwellwert erreicht ist).
    "donate_min_amounts": {
        "iron":      0,
        "steel":     0,
        "chemicals": 0,
        "ice":       0,
        "water":     0,
        "energy":    0,
        "vv4a":      0,
    },

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
        merged["resource_targets"]  = {**DEFAULTS["resource_targets"],  **stored.get("resource_targets",  {})}
        merged["bunker_thresholds"] = {**DEFAULTS["bunker_thresholds"], **stored.get("bunker_thresholds", {})}
        merged["donate_min_amounts"] = {**DEFAULTS["donate_min_amounts"], **stored.get("donate_min_amounts", {})}
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
        if "bunker_thresholds" in patch and isinstance(patch["bunker_thresholds"], dict):
            _goals["bunker_thresholds"].update(patch["bunker_thresholds"])
            patch = {k: v for k, v in patch.items() if k != "bunker_thresholds"}
        if "donate_min_amounts" in patch and isinstance(patch["donate_min_amounts"], dict):
            _goals["donate_min_amounts"].update(patch["donate_min_amounts"])
            patch = {k: v for k, v in patch.items() if k != "donate_min_amounts"}
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


def is_auto_research_enabled() -> bool:
    """True wenn der Bot automatisch Forschungen starten darf (Standard: True)."""
    return bool(get().get("auto_research_enabled", DEFAULTS["auto_research_enabled"]))


def is_auto_storage_enabled() -> bool:
    """True wenn der Bot automatisch Lager bauen darf (Standard: True)."""
    return bool(get().get("auto_storage_enabled", DEFAULTS["auto_storage_enabled"]))


def is_resource_paused(resource: str) -> bool:
    """True wenn der Bot für ``resource`` aktuell weder Lager noch Produktion
    bauen soll. Unbekannte/leere Namen → False (kein Pausen-Effekt)."""
    if not resource:
        return False
    return resource in paused_resources()


def notification_enabled(key: str) -> bool:
    """True wenn die Benachrichtigung mit ``key`` aktiviert ist (Standard: True)."""
    return bool(get().get(key, True))


def bunker_thresholds() -> dict[str, float]:
    """Bunker-Schwellwerte pro Ressource (0.0 = deaktiviert)."""
    raw = get().get("bunker_thresholds", DEFAULTS["bunker_thresholds"])
    result = dict(DEFAULTS["bunker_thresholds"])
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in result:
                result[k] = float(v)
    return result


def donate_min_amounts() -> dict[str, float]:
    """Mindestmengen je Ressource ab denen eine Auto-Spende ausgelöst wird (0 = kein Minimum)."""
    raw = get().get("donate_min_amounts", DEFAULTS["donate_min_amounts"])
    result = dict(DEFAULTS["donate_min_amounts"])
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in result:
                result[k] = float(v)
    return result
