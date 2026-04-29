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
    bot_status: str = "starting"   # starting | running | idle | error | stopped | paused
    last_turn_at: Optional[float] = None   # Unix-Timestamp der letzten Runde
    turn_number: int = 0
    last_error: str = ""
    paused: bool = False

    # Empfohlene Aktion (statt sofortiger Ausführung)
    recommended_action: Optional[dict] = None

    # Execute-Anfrage: gesetzt vom Dashboard, gelesen vom Bot-Loop
    execute_requested: bool = False
    execute_action: Optional[dict] = None  # Kopie von recommended_action zum Ausführen

    # Ergebnis der letzten Ausführung: "ok" oder Fehlermeldung
    last_execute_result: Optional[str] = None

    # Spendenempfehlungen: Liste von pending-Spenden (gesetzt von _resource_monitor)
    # Jeder Eintrag: {"resource": str, "amount": int, "label": str, "pct": int}
    donate_recommended: list = field(default_factory=list)

    # Spenden-Ausführungsanfragen (vom Dashboard getriggert)
    # Jeder Eintrag: {"resource": str, "amount": int}
    donate_requests: list = field(default_factory=list)

    # Scoring-Transparenz: Snapshot der aktuell vom Bot verglichenen Gebäude.
    # Wird von Strategy.decide() per ``build_scoring_snapshot`` erzeugt und
    # vom Dashboard unter /api/scoring ausgelesen.
    scoring_snapshot: list = field(default_factory=list)

    # Seen-Sets: Gebäude-Typen und Forschungen, die der Bot bereits gemeldet hat.
    # Verhindern, dass pro Turn erneute Telegram-Benachrichtigungen gesendet werden.
    seen_unknown_building_types: set = field(default_factory=set)
    seen_researched_types: set = field(default_factory=set)

    # Kolonien-Snapshots: city_id → kompakter Status-Dict für das Dashboard.
    # Wird nach jedem Scrape für die aktuelle Stadt aktualisiert.
    colonies_snapshots: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "bot_status": self.bot_status,
            "paused": self.paused,
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
            "recommended_action": self.recommended_action,
            "execute_requested": self.execute_requested,
            "last_execute_result": self.last_execute_result,
            "donate_recommended": list(self.donate_recommended),
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
        if not _state.paused:
            _state.bot_status = "running"
        _state.last_error = ""


def is_paused() -> bool:
    with _lock:
        return _state.paused


def set_paused(paused: bool) -> None:
    with _lock:
        _state.paused = paused
        _state.bot_status = "paused" if paused else "running"


# ── Empfohlene Aktion + Execute-Request ──────────────────────────────────────

def set_recommended_action(action_dict: Optional[dict]) -> None:
    """Setzt die empfohlene Aktion (vom Bot-Loop, nach strategy.decide)."""
    with _lock:
        _state.recommended_action = action_dict


def request_execute() -> bool:
    """Vom Dashboard aufgerufen. Kopiert recommended_action → execute_action, setzt Flag.

    Gibt False zurück wenn keine Empfehlung verfügbar oder bereits eine Anfrage läuft.
    """
    with _lock:
        if _state.recommended_action is None or _state.execute_requested:
            return False
        _state.execute_action = dict(_state.recommended_action)
        _state.execute_requested = True
        return True


def has_execute_request() -> bool:
    with _lock:
        return _state.execute_requested


def consume_execute_request() -> Optional[dict]:
    """Vom Bot-Loop aufgerufen. Gibt execute_action zurück und löscht Flag."""
    with _lock:
        if not _state.execute_requested:
            return None
        action = _state.execute_action
        _state.execute_requested = False
        _state.execute_action = None
        return action


def set_execute_result(result: str) -> None:
    with _lock:
        _state.last_execute_result = result


# ── Spendenempfehlungen + Donate-Requests ────────────────────────────────────

def add_donate_recommended(resource: str, amount: int, label: str, pct: int) -> None:
    """Fügt eine Spendenempfehlung hinzu (oder ersetzt vorhandene für diese Ressource)."""
    with _lock:
        _state.donate_recommended = [
            d for d in _state.donate_recommended if d["resource"] != resource
        ]
        _state.donate_recommended.append(
            {"resource": resource, "amount": amount, "label": label, "pct": pct}
        )


def clear_donate_recommended(resource: str) -> None:
    with _lock:
        _state.donate_recommended = [
            d for d in _state.donate_recommended if d["resource"] != resource
        ]


def request_donate(resource: str, amount: int) -> None:
    """Vom Dashboard aufgerufen. Stellt eine Spendenanforderung in die Queue."""
    with _lock:
        _state.donate_requests.append({"resource": resource, "amount": amount})


def consume_donate_request() -> Optional[dict]:
    """Vom Bot-Loop aufgerufen. Gibt das erste pending Spendenrequest zurück."""
    with _lock:
        if not _state.donate_requests:
            return None
        return _state.donate_requests.pop(0)


def has_donate_request() -> bool:
    with _lock:
        return len(_state.donate_requests) > 0


# ── Scoring-Snapshot + Diff-Sets ─────────────────────────────────────────────

def set_scoring_snapshot(rows: list) -> None:
    """Speichert den zuletzt von Strategy.decide() erzeugten Scoring-Snapshot."""
    with _lock:
        _state.scoring_snapshot = list(rows)


def get_scoring_snapshot() -> list:
    with _lock:
        return list(_state.scoring_snapshot)


def mark_building_seen(btype: str) -> bool:
    """Merkt sich einen Gebäude-Typ als 'schon gesehen'. True, falls neu."""
    with _lock:
        if btype in _state.seen_unknown_building_types:
            return False
        _state.seen_unknown_building_types.add(btype)
        return True


def mark_research_seen(rtype: str) -> bool:
    """Merkt sich eine abgeschlossene Forschung. True, falls neu."""
    with _lock:
        if rtype in _state.seen_researched_types:
            return False
        _state.seen_researched_types.add(rtype)
        return True


def set_colony_snapshot(city_id: int, snapshot: dict) -> None:
    """Aktualisiert den Kompakt-Status einer Kolonie (z.B. nach jedem Scrape)."""
    with _lock:
        _state.colonies_snapshots[city_id] = snapshot


def get_colonies_snapshots() -> dict:
    """Gibt alle gespeicherten Kolonien-Snapshots zurück (city_id → dict)."""
    with _lock:
        return dict(_state.colonies_snapshots)


def initialize_seen_research(rtypes: list) -> None:
    """Initialisiert das seen-Set beim Bot-Start mit dem bereits abgeschlossenen Research.

    Verhindert, dass beim ersten Turn eine Flut an 'neuen' Benachrichtigungen kommt.
    """
    with _lock:
        _state.seen_researched_types.update(rtypes)
