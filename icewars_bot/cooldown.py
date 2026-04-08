"""Bau-Cooldown-Tracker.

Wenn ein bestimmtes Gebäude mehrfach nicht gebaut werden konnte
(z.B. weil Voraussetzungen fehlen, Ressourcen zu knapp sind oder der
Button deaktiviert ist), wird es für eine Weile auf Cooldown gesetzt,
damit der Bot nicht in einer Endlosschleife dasselbe fehlschlagende
Gebäude versucht und in der Zwischenzeit andere Aktionen ausführen
kann.

Ablauf:
- `record_failure(btype)` nach jedem fehlgeschlagenen Bauversuch
- Nach `FAILURE_THRESHOLD` (Default: 2) Fehlversuchen wird ein
  Cooldown von `COOLDOWN_SECONDS` (Default: 3600 = 1 h) gesetzt.
- `is_on_cooldown(btype)` in der Strategy abfragen, um das Gebäude
  zu überspringen.
- `record_success(btype)` löscht Cooldown und Fehlerzähler.

Thread-safe über ein Modul-Lock.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Ab so vielen Fehlversuchen wird der Cooldown aktiviert.
FAILURE_THRESHOLD = 2

# Dauer des Cooldowns in Sekunden (1 Stunde).
COOLDOWN_SECONDS = 3600


@dataclass
class _Entry:
    failure_count: int = 0
    cooldown_until: float = 0.0  # monotonic-Zeitstempel; 0 = kein Cooldown
    last_failure_reason: str = ""


_lock = threading.Lock()
_entries: dict[str, _Entry] = {}


def record_failure(building_type: str, reason: str = "") -> bool:
    """Registriert einen fehlgeschlagenen Bauversuch.

    Gibt ``True`` zurück, wenn der Cooldown durch diesen Fehlversuch
    neu aktiviert wurde (also: jetzt erstmalig auf Cooldown gesetzt).
    """
    if not building_type:
        return False
    now = time.monotonic()
    with _lock:
        entry = _entries.setdefault(building_type, _Entry())
        entry.failure_count += 1
        if reason:
            entry.last_failure_reason = reason

        if entry.failure_count >= FAILURE_THRESHOLD and entry.cooldown_until <= now:
            entry.cooldown_until = now + COOLDOWN_SECONDS
            logger.warning(
                "Cooldown aktiviert für '%s' nach %d Fehlversuchen — pausiere %d min.",
                building_type, entry.failure_count, COOLDOWN_SECONDS // 60,
            )
            return True
    return False


def record_success(building_type: str) -> None:
    """Löscht den Fehlerzähler und Cooldown nach erfolgreichem Bau."""
    if not building_type:
        return
    with _lock:
        if building_type in _entries:
            del _entries[building_type]


def is_on_cooldown(building_type: str) -> bool:
    """Prüft ob das Gebäude aktuell auf Cooldown ist.

    Abgelaufene Einträge werden automatisch aufgeräumt.
    """
    if not building_type:
        return False
    now = time.monotonic()
    with _lock:
        entry = _entries.get(building_type)
        if not entry:
            return False
        if entry.cooldown_until > now:
            return True
        # Cooldown ist abgelaufen → Fehlerzähler zurücksetzen und neu versuchen.
        if entry.cooldown_until > 0:
            logger.info(
                "Cooldown für '%s' abgelaufen — Gebäude wird wieder freigegeben.",
                building_type,
            )
            del _entries[building_type]
        return False


def remaining_seconds(building_type: str) -> int:
    """Gibt die verbleibende Cooldown-Zeit in Sekunden zurück (0 = kein Cooldown)."""
    if not building_type:
        return 0
    now = time.monotonic()
    with _lock:
        entry = _entries.get(building_type)
        if not entry or entry.cooldown_until <= now:
            return 0
        return int(entry.cooldown_until - now)


def failure_count(building_type: str) -> int:
    """Gibt den aktuellen Fehlerzähler für ein Gebäude zurück."""
    if not building_type:
        return 0
    with _lock:
        entry = _entries.get(building_type)
        return entry.failure_count if entry else 0


def active_cooldowns() -> dict[str, int]:
    """Gibt alle aktuell aktiven Cooldowns zurück: building_type → Restsekunden."""
    now = time.monotonic()
    result: dict[str, int] = {}
    with _lock:
        for btype, entry in list(_entries.items()):
            rem = int(entry.cooldown_until - now)
            if rem > 0:
                result[btype] = rem
            elif entry.cooldown_until > 0 and entry.cooldown_until <= now:
                # Aufräumen abgelaufener Einträge
                del _entries[btype]
    return result


def reset() -> None:
    """Löscht alle Cooldowns und Fehlerzähler (für Tests/Dashboard-Reset)."""
    with _lock:
        _entries.clear()
