"""SQLite-Datenbank für historische Spielwerte.

Speichert alle 5 Minuten einen Snapshot aller Ressourcen, Raten,
Kapazitäten, Bevölkerung und Wirtschaftswerte.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .state import GameState

logger = logging.getLogger(__name__)

DB_PATH = Path("data/icewars_history.db")
RECORD_INTERVAL_S = 300  # alle 5 Minuten


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def _connect(path: Path = DB_PATH):
    _ensure_dir(path)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(path: Path = DB_PATH) -> None:
    """Erstellt die Datenbanktabellen falls sie nicht existieren."""
    with _connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                epoch           REAL    NOT NULL,

                -- Ressourcen
                iron            REAL DEFAULT 0,
                steel           REAL DEFAULT 0,
                chemicals       REAL DEFAULT 0,
                ice             REAL DEFAULT 0,
                water           REAL DEFAULT 0,
                energy          REAL DEFAULT 0,
                vv4a            REAL DEFAULT 0,
                credits         REAL DEFAULT 0,
                fp              REAL DEFAULT 0,

                -- Produktionsraten (pro Stunde)
                iron_rate       REAL DEFAULT 0,
                steel_rate      REAL DEFAULT 0,
                chemicals_rate  REAL DEFAULT 0,
                ice_rate        REAL DEFAULT 0,
                water_rate      REAL DEFAULT 0,
                energy_rate     REAL DEFAULT 0,
                vv4a_rate       REAL DEFAULT 0,
                credits_rate    REAL DEFAULT 0,
                fp_rate         REAL DEFAULT 0,

                -- Lagerkapazität
                iron_cap        REAL DEFAULT 0,
                steel_cap       REAL DEFAULT 0,
                chemicals_cap   REAL DEFAULT 0,
                ice_cap         REAL DEFAULT 0,
                water_cap       REAL DEFAULT 0,
                energy_cap      REAL DEFAULT 0,
                vv4a_cap        REAL DEFAULT 0,

                -- Bevölkerung
                population_free   INTEGER DEFAULT 0,
                population_total  INTEGER DEFAULT 0,
                population_max    INTEGER DEFAULT 0,

                -- Wirtschaft & Umwelt
                satisfaction      REAL DEFAULT 0,
                living_conditions REAL DEFAULT 0,
                eco_points        INTEGER DEFAULT 0,
                points            INTEGER DEFAULT 0,

                -- Build-Queue Info
                build_slots_used  INTEGER DEFAULT 0,
                build_slots_max   INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_snapshots_epoch
                ON snapshots(epoch);

            CREATE TABLE IF NOT EXISTS highscores (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                epoch           REAL    NOT NULL,
                category        TEXT    NOT NULL,  -- points, research, fleet, economy
                rank            INTEGER NOT NULL,
                username        TEXT    NOT NULL,
                user_id         INTEGER NOT NULL,
                alliance        TEXT,
                value           REAL    NOT NULL,
                detail          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_highscores_epoch
                ON highscores(epoch);
            CREATE INDEX IF NOT EXISTS idx_highscores_user_cat
                ON highscores(username, category);

            CREATE TABLE IF NOT EXISTS bot_sessions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time      TEXT    NOT NULL,
                start_epoch     REAL    NOT NULL,
                end_time        TEXT,
                end_epoch       REAL,
                turns_completed INTEGER DEFAULT 0,
                actions_executed INTEGER DEFAULT 0,
                actions_failed  INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS build_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                epoch       REAL    NOT NULL,
                event_type  TEXT    NOT NULL,  -- build | research
                type_key    TEXT    NOT NULL,  -- building_type oder research_type
                name        TEXT    NOT NULL   -- Anzeigename
            );

            CREATE INDEX IF NOT EXISTS idx_build_events_epoch
                ON build_events(epoch);
            CREATE INDEX IF NOT EXISTS idx_build_events_type
                ON build_events(event_type, type_key);

            CREATE TABLE IF NOT EXISTS activity_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                epoch       REAL    NOT NULL,
                category    TEXT    NOT NULL,
                title       TEXT    NOT NULL,
                detail      TEXT    DEFAULT '',
                city_id     INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_activity_log_epoch
                ON activity_log(epoch);
            CREATE INDEX IF NOT EXISTS idx_activity_log_category
                ON activity_log(category);
        """)
    logger.info("Datenbank initialisiert: %s", path.resolve())


def record_snapshot(state: GameState, path: Path = DB_PATH) -> None:
    """Speichert den aktuellen Spielzustand als Snapshot."""
    now = datetime.now(timezone.utc)
    epoch = time.time()

    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO snapshots (
                timestamp, epoch,
                iron, steel, chemicals, ice, water, energy, vv4a, credits, fp,
                iron_rate, steel_rate, chemicals_rate, ice_rate, water_rate,
                energy_rate, vv4a_rate, credits_rate, fp_rate,
                iron_cap, steel_cap, chemicals_cap, ice_cap, water_cap,
                energy_cap, vv4a_cap,
                population_free, population_total, population_max,
                satisfaction, living_conditions, eco_points, points,
                build_slots_used, build_slots_max
            ) VALUES (
                ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?
            )
        """, (
            now.isoformat(), epoch,
            state.resources.iron, state.resources.steel, state.resources.chemicals,
            state.resources.ice, state.resources.water, state.resources.energy,
            state.resources.vv4a, state.resources.credits, state.resources.fp,
            state.rates.iron, state.rates.steel, state.rates.chemicals,
            state.rates.ice, state.rates.water, state.rates.energy,
            state.rates.vv4a, state.credits_rate, state.rates.fp,
            state.capacity.iron, state.capacity.steel, state.capacity.chemicals,
            state.capacity.ice, state.capacity.water, state.capacity.energy,
            state.capacity.vv4a,
            state.population_free, state.population_total, state.population_max,
            state.satisfaction, state.living_conditions, state.eco_points, state.points,
            len(state.build_queue), state.max_build_slots,
        ))

    logger.debug("Snapshot gespeichert (epoch=%.0f)", epoch)


def get_last_stop_epoch(path: Path = DB_PATH) -> Optional[float]:
    """Gibt end_epoch der letzten sauber beendeten Session zurück, oder None."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT MAX(end_epoch) as last_stop FROM bot_sessions WHERE end_epoch IS NOT NULL"
        ).fetchone()
    return float(row["last_stop"]) if row and row["last_stop"] else None


def start_session(path: Path = DB_PATH) -> int:
    """Startet eine neue Bot-Session und gibt die Session-ID zurück."""
    now = datetime.now(timezone.utc)
    epoch = time.time()
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO bot_sessions (start_time, start_epoch) VALUES (?, ?)",
            (now.isoformat(), epoch),
        )
        session_id = cur.lastrowid
    logger.info("Bot-Session #%d gestartet.", session_id)
    return session_id


def end_session(session_id: int, stats: dict, path: Path = DB_PATH) -> None:
    """Beendet eine Bot-Session mit Statistiken."""
    now = datetime.now(timezone.utc)
    epoch = time.time()
    with _connect(path) as conn:
        conn.execute("""
            UPDATE bot_sessions
            SET end_time = ?, end_epoch = ?,
                turns_completed = ?, actions_executed = ?, actions_failed = ?
            WHERE id = ?
        """, (
            now.isoformat(), epoch,
            stats.get("turns", 0), stats.get("executed", 0), stats.get("failed", 0),
            session_id,
        ))
    logger.info("Bot-Session #%d beendet.", session_id)


# ── Query-Funktionen für Dashboard ──────────────────────────────────────────

def get_snapshots(
    from_epoch: Optional[float] = None,
    to_epoch: Optional[float] = None,
    limit: int = 10000,
    path: Path = DB_PATH,
) -> list[dict]:
    """Gibt Snapshots als Liste von Dicts zurück."""
    conditions = []
    params: list[Any] = []

    if from_epoch is not None:
        conditions.append("epoch >= ?")
        params.append(from_epoch)
    if to_epoch is not None:
        conditions.append("epoch <= ?")
        params.append(to_epoch)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM snapshots {where} ORDER BY epoch ASC LIMIT ?",
            params + [limit],
        ).fetchall()

    return [dict(row) for row in rows]


def get_sessions(
    from_epoch: Optional[float] = None,
    to_epoch: Optional[float] = None,
    path: Path = DB_PATH,
) -> list[dict]:
    """Gibt Bot-Sessions als Liste von Dicts zurück."""
    conditions = []
    params: list[Any] = []

    if from_epoch is not None:
        conditions.append("(end_epoch IS NULL OR end_epoch >= ?)")
        params.append(from_epoch)
    if to_epoch is not None:
        conditions.append("start_epoch <= ?")
        params.append(to_epoch)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM bot_sessions {where} ORDER BY start_epoch ASC",
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def get_latest_snapshot(path: Path = DB_PATH) -> Optional[dict]:
    """Gibt den neuesten Snapshot zurück."""
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM snapshots ORDER BY epoch DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_snapshot_count(path: Path = DB_PATH) -> int:
    """Gibt die Anzahl der Snapshots zurück."""
    with _connect(path) as conn:
        row = conn.execute("SELECT COUNT(*) as cnt FROM snapshots").fetchone()
    return row["cnt"] if row else 0


# ── Highscore ───────────────────────────────────────────────────────────────

def record_highscores(highscore_data: dict, path: Path = DB_PATH) -> None:
    """Speichert einen kompletten Highscore-Snapshot (alle Kategorien)."""
    now = datetime.now(timezone.utc)
    epoch = time.time()

    rows = []
    for category, data in highscore_data.items():
        for entry in data.get("entries", []):
            rows.append((
                now.isoformat(), epoch, category,
                entry.get("rank", 0),
                entry.get("username", ""),
                entry.get("user_id", 0),
                entry.get("alliance"),
                float(entry.get("value", 0)),
                entry.get("detail", ""),
            ))

    if not rows:
        return

    with _connect(path) as conn:
        conn.executemany("""
            INSERT INTO highscores (
                timestamp, epoch, category,
                rank, username, user_id, alliance, value, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

    logger.debug("Highscore gespeichert: %d Einträge über %d Kategorien",
                 len(rows), len(highscore_data))


def get_highscores(
    from_epoch: Optional[float] = None,
    to_epoch: Optional[float] = None,
    category: Optional[str] = None,
    username: Optional[str] = None,
    path: Path = DB_PATH,
) -> list[dict]:
    """Gibt Highscore-Einträge als Liste von Dicts zurück."""
    conditions = []
    params: list[Any] = []

    if from_epoch is not None:
        conditions.append("epoch >= ?")
        params.append(from_epoch)
    if to_epoch is not None:
        conditions.append("epoch <= ?")
        params.append(to_epoch)
    if category is not None:
        conditions.append("category = ?")
        params.append(category)
    if username is not None:
        conditions.append("username = ?")
        params.append(username)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM highscores {where} ORDER BY epoch ASC, category, rank",
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def get_highscore_timeline(
    username: str,
    category: str = "points",
    from_epoch: Optional[float] = None,
    to_epoch: Optional[float] = None,
    path: Path = DB_PATH,
) -> list[dict]:
    """Gibt die Rang-Entwicklung eines Spielers über Zeit zurück.

    Dedupliziert auf einen Eintrag pro Snapshot-Zeitpunkt.
    """
    conditions = ["username = ?", "category = ?"]
    params: list[Any] = [username, category]

    if from_epoch is not None:
        conditions.append("epoch >= ?")
        params.append(from_epoch)
    if to_epoch is not None:
        conditions.append("epoch <= ?")
        params.append(to_epoch)

    where = f"WHERE {' AND '.join(conditions)}"

    with _connect(path) as conn:
        rows = conn.execute(
            f"""SELECT epoch, timestamp, rank, value, alliance
                FROM highscores {where}
                GROUP BY epoch
                ORDER BY epoch ASC""",
            params,
        ).fetchall()

    return [dict(row) for row in rows]


def get_latest_highscore(category: str = "points", path: Path = DB_PATH) -> list[dict]:
    """Gibt das neueste Highscore-Board einer Kategorie zurück."""
    with _connect(path) as conn:
        # Neuesten Epoch finden
        row = conn.execute(
            "SELECT MAX(epoch) as max_epoch FROM highscores WHERE category = ?",
            (category,),
        ).fetchone()

        if not row or row["max_epoch"] is None:
            return []

        rows = conn.execute(
            "SELECT * FROM highscores WHERE category = ? AND epoch = ? ORDER BY rank",
            (category, row["max_epoch"]),
        ).fetchall()

    return [dict(r) for r in rows]


# ── Build / Research Events ──────────────────────────────────────────────────

def record_build_event(
    event_type: str,
    type_key: str,
    name: str,
    path: Path = DB_PATH,
) -> None:
    """Speichert ein Bau- oder Forschungsereignis.

    Args:
        event_type: 'build' oder 'research'
        type_key:   Gebäude- oder Forschungstyp-Schlüssel (z.B. 'iron_mine')
        name:       Anzeigename (z.B. 'Eisenmine Stufe 3')
    """
    now = datetime.now(timezone.utc)
    epoch = time.time()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO build_events (timestamp, epoch, event_type, type_key, name) VALUES (?, ?, ?, ?, ?)",
            (now.isoformat(), epoch, event_type, type_key, name),
        )
    logger.debug("Build-Event: %s '%s' (%s)", event_type, name, type_key)


# ── Activity Log ────────────────────────────────────────────────────────────

def record_activity(
    category: str,
    title: str,
    detail: str = "",
    city_id: int = 0,
    path: Path = DB_PATH,
) -> None:
    """Speichert einen Aktivitätseintrag.

    Kategorien: bot_start | bot_stop | bot_pause | bot_resume |
                bot_action | build_complete | research_complete |
                human_action | error
    """
    now = datetime.now(timezone.utc)
    epoch = time.time()
    with _connect(path) as conn:
        conn.execute(
            "INSERT INTO activity_log (timestamp, epoch, category, title, detail, city_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now.isoformat(), epoch, category, title, detail, city_id),
        )
    logger.debug("Activity: [%s] %s", category, title)


def get_activity_log(
    from_epoch: Optional[float] = None,
    to_epoch: Optional[float] = None,
    categories: Optional[list] = None,
    limit: int = 200,
    offset: int = 0,
    path: Path = DB_PATH,
) -> list[dict]:
    """Gibt Aktivitätslog-Einträge zurück (neueste zuerst)."""
    conditions: list[str] = []
    params: list[Any] = []

    if from_epoch is not None:
        conditions.append("epoch >= ?")
        params.append(from_epoch)
    if to_epoch is not None:
        conditions.append("epoch <= ?")
        params.append(to_epoch)
    if categories:
        placeholders = ",".join("?" * len(categories))
        conditions.append(f"category IN ({placeholders})")
        params.extend(categories)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM activity_log {where} ORDER BY epoch DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

    return [dict(row) for row in rows]


def get_build_events(
    from_epoch: Optional[float] = None,
    to_epoch: Optional[float] = None,
    event_type: Optional[str] = None,
    path: Path = DB_PATH,
) -> list[dict]:
    """Gibt Bau-/Forschungsereignisse als Liste von Dicts zurück."""
    conditions: list[str] = []
    params: list[Any] = []

    if from_epoch is not None:
        conditions.append("epoch >= ?")
        params.append(from_epoch)
    if to_epoch is not None:
        conditions.append("epoch <= ?")
        params.append(to_epoch)
    if event_type is not None:
        conditions.append("event_type = ?")
        params.append(event_type)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with _connect(path) as conn:
        rows = conn.execute(
            f"SELECT * FROM build_events {where} ORDER BY epoch DESC LIMIT 500",
            params,
        ).fetchall()

    return [dict(row) for row in rows]
