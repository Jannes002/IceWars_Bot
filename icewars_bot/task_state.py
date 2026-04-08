"""Zentraler, thread-sicherer Live-Status des Bots.

Wird vom Bot-Loop geschrieben und vom Dashboard-Thread gelesen.
Kein Datenbankzugriff — reiner In-Memory-State.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from .strategy import building_display_name


@dataclass
class TaskEntry:
    """Eine einzelne geplante oder laufende Aktion."""
    action_type: str          # build_specific | build_storage | build_next_building | start_research
    label: str                # Menschenlesbare Beschreibung, z.B. "Zelt bauen"
    reason: str = ""          # Warum? z.B. "Bevölkerung zu niedrig"
    status: str = "pending"   # pending | running | done | failed


@dataclass
class QueueEntry:
    """Ein laufender Bau- oder Forschungsauftrag aus dem Spiel."""
    slot_type: str        # build | research
    name: str             # z.B. "Eisenmine"
    finish_time: str      # z.B. "2026-04-07T16:30:00" (ISO, kann leer sein)
    remaining_sec: int = 0  # Verbleibende Sekunden (Fallback wenn finish_time fehlt)


@dataclass
class BotTaskState:
    """Aktueller Live-Status aller Bot-Aufgaben."""
    # Geplante Aktionen dieser Runde (von Strategy.decide())
    planned: list[TaskEntry] = field(default_factory=list)

    # Aktuell ausgeführte Aktion
    current: Optional[TaskEntry] = None

    # Was im Spiel gerade in der Warteschlange ist (Bauten + Forschung)
    game_queue: list[QueueEntry] = field(default_factory=list)

    # Bot-Status
    bot_status: str = "starting"   # starting | running | idle | error | stopped
    last_turn_at: Optional[float] = None   # Unix-Timestamp der letzten Runde
    turn_number: int = 0
    last_error: str = ""

    def to_dict(self) -> dict:
        return {
            "bot_status": self.bot_status,
            "last_turn_at": self.last_turn_at,
            "turn_number": self.turn_number,
            "last_error": self.last_error,
            "current": {
                "action_type": self.current.action_type,
                "label": self.current.label,
                "reason": self.current.reason,
                "status": self.current.status,
            } if self.current else None,
            "planned": [
                {
                    "action_type": t.action_type,
                    "label": t.label,
                    "reason": t.reason,
                    "status": t.status,
                }
                for t in self.planned
            ],
            "game_queue": [
                {
                    "slot_type": q.slot_type,
                    "name": q.name,
                    "finish_time": q.finish_time,
                    "remaining_sec": q.remaining_sec,
                }
                for q in self.game_queue
            ],
        }


# ── Globaler Singleton mit Lock ───────────────────────────────────────────────

_lock = threading.Lock()
_state = BotTaskState()


def get() -> dict:
    """Gibt eine thread-sichere Kopie des aktuellen Status zurück."""
    with _lock:
        return _state.to_dict()


def update_planned(tasks: list[TaskEntry]) -> None:
    with _lock:
        _state.planned = list(tasks)
        _state.current = None


def set_running(task: TaskEntry) -> None:
    with _lock:
        _state.current = task
        # Status in der planned-Liste aktualisieren
        for t in _state.planned:
            if t.action_type == task.action_type and t.label == task.label:
                t.status = "running"
                break


def set_done(task: TaskEntry, success: bool) -> None:
    with _lock:
        for t in _state.planned:
            if t.action_type == task.action_type and t.label == task.label:
                t.status = "done" if success else "failed"
                break
        _state.current = None


def update_game_queue(build_items: list, active_research) -> None:
    """Aktualisiert die In-Game-Warteschlange (Bauten + Forschung)."""
    with _lock:
        entries = []
        for b in build_items:
            btype = b.building_type
            # Name: API-Name bevorzugen, sonst aus Mapping, sonst Typ-String
            name = b.name or building_display_name(btype, btype)
            entries.append(QueueEntry(
                slot_type="build",
                name=name,
                finish_time=getattr(b, "finish_time", ""),
                remaining_sec=getattr(b, "remaining_sec", 0),
            ))
        if active_research:
            entries.append(QueueEntry(
                slot_type="research",
                name=active_research.name,
                finish_time=getattr(active_research, "finish_time", ""),
                remaining_sec=getattr(active_research, "remaining_sec", 0),
            ))
        _state.game_queue = entries


def set_status(status: str, error: str = "") -> None:
    with _lock:
        _state.bot_status = status
        _state.last_error = error


def tick(turn_number: int) -> None:
    with _lock:
        _state.last_turn_at = time.time()
        _state.turn_number = turn_number
        _state.bot_status = "running"
        _state.last_error = ""
