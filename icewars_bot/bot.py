from __future__ import annotations

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .actions import ActionExecutor
from .auth import Authenticator
from .browser import BrowserManager
from .config import Config
from .db import (
    init_db, record_snapshot, record_highscores, record_build_event,
    record_activity,
    start_session, end_session, get_last_stop_epoch, RECORD_INTERVAL_S,
)
from .scraper import GameScraper
from .state import BuildQueueItem, GameState, Resources, parse_state
from .strategy import Action, BUILDING_NAMES, Strategy, build_scoring_snapshot, building_display_name
from .telegram import TelegramNotifier, make_notifier
from . import task_state as ts
from . import cooldown
from . import goals as G
from . import planets_store

logger = logging.getLogger(__name__)

STATUS_INTERVAL_S = 180   # Statusmeldung alle 180 Sekunden


def _action_to_task(action: Action) -> ts.TaskEntry:
    """Wandelt eine Strategy-Action in ein lesbares TaskEntry um."""
    p = action.params
    if action.type == "build_specific":
        label = f"{p.get('building_name', p.get('building_type', '?'))} bauen"
        reason = p.get("reason", "")
    elif action.type == "build_storage":
        res = p.get("resource", "?")
        pct = int(p.get("fill_ratio", 0) * 100)
        label = f"Lager bauen: {p.get('building_name', res)}"
        reason = f"{res} {pct}% voll"
    elif action.type == "build_next_building":
        label = "Nächstes Gebäude bauen"
        reason = "Normale Produktion"
    elif action.type == "start_research":
        label = f"Forschen: {p.get('name', p.get('research_type', '?'))}"
        reason = ""
    else:
        label = action.type
        reason = ""
    return ts.TaskEntry(action_type=action.type, label=label, reason=reason)
AUTH_CHECK_INTERVAL_S = 900  # Login-Check alle 15 Minuten


@dataclass
class SessionStats:
    """Verfolgt den Fortschritt seit Bot-Start."""
    start_time: float = field(default_factory=time.monotonic)
    start_wall: float = field(default_factory=time.time)

    # Ressourcen beim Start (werden beim ersten Scrape gesetzt)
    initial_resources: Optional[Resources] = None
    initial_points: int = 0

    # Zähler
    turns_completed: int = 0
    actions_executed: int = 0
    actions_failed: int = 0
    browser_restarts: int = 0

    def uptime_str(self) -> str:
        elapsed = int(time.monotonic() - self.start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}h {m}m {s}s"
        elif m:
            return f"{m}m {s}s"
        return f"{s}s"

    def progress_str(self, current: GameState) -> str:
        if self.initial_resources is None:
            return "(noch keine Vergleichsdaten)"
        res = current.resources
        init = self.initial_resources
        lines = [
            f"  Laufzeit        : {self.uptime_str()}",
            f"  Runden          : {self.turns_completed}",
            f"  Aktionen        : {self.actions_executed} ausgeführt, {self.actions_failed} fehlgeschlagen",
            f"  Browser-Neustarts: {self.browser_restarts}",
            "",
            f"  Punkte          : {self.initial_points} → {current.points}  "
            f"(+{current.points - self.initial_points})",
            "",
            "  Ressourcen-Änderung seit Start:",
            f"    Eisen      : {init.iron:>10.0f} → {res.iron:>10.0f}  ({res.iron - init.iron:+.0f})",
            f"    Stahl      : {init.steel:>10.0f} → {res.steel:>10.0f}  ({res.steel - init.steel:+.0f})",
            f"    Eis        : {init.ice:>10.0f} → {res.ice:>10.0f}  ({res.ice - init.ice:+.0f})",
            f"    Wasser     : {init.water:>10.0f} → {res.water:>10.0f}  ({res.water - init.water:+.0f})",
            f"    Chemikalien: {init.chemicals:>10.0f} → {res.chemicals:>10.0f}  ({res.chemicals - init.chemicals:+.0f})",
            f"    Energie    : {init.energy:>10.0f} → {res.energy:>10.0f}  ({res.energy - init.energy:+.0f})",
            f"    VV4A       : {init.vv4a:>10.0f} → {res.vv4a:>10.0f}  ({res.vv4a - init.vv4a:+.0f})",
            f"    Credits    : {init.credits:>10.1f} → {res.credits:>10.1f}  ({res.credits - init.credits:+.1f})",
            f"    FP         : {init.fp:>10.0f} → {res.fp:>10.0f}  ({res.fp - init.fp:+.0f})",
        ]
        return "\n".join(lines)


class BotLoop:
    def __init__(
        self,
        browser: BrowserManager,
        scraper: GameScraper,
        strategy: Strategy,
        executor: ActionExecutor,
        auth: Authenticator,
        config: Config,
    ) -> None:
        self._browser = browser
        self._scraper = scraper
        self._strategy = strategy
        self._executor = executor
        self._auth = auth
        self._config = config
        self._consecutive_failures = 0
        self._stats = SessionStats()
        self._last_state: Optional[GameState] = None
        self._last_raw: Optional[dict] = None
        self._session_id: Optional[int] = None

        # Telegram
        self._tg: Optional[TelegramNotifier] = make_notifier(config)
        self._last_rank: Optional[int] = None          # letzter bekannter Rang (Gesamtpunkte)
        self._last_queue: list[BuildQueueItem] = []    # Bauwarteschlange vorherige Runde
        self._low_resources: set[str] = set()          # Ressourcen unter 15%-Schwelle (bereits gemeldet)
        self._donated_resources: set[str] = set()      # Ressourcen über 95%-Schwelle (Spende bereits ausgelöst)
        self._last_auto_build_time: float = 0.0        # Zeitpunkt des letzten Auto-Builds

        # Multi-Planet-Rotation
        known, excluded = planets_store.load()
        self._planet_cities: list[dict] = known
        self._excluded_planet_ids: set[int] = excluded
        self._current_city_idx: int = 0
        self._last_planet_switch: float = 0.0   # Unix-Timestamp des letzten Wechsels
        if known:
            logger.info("Planeten aus Speicher geladen: %d bekannt, %d ausgeschlossen",
                        len(known), len(excluded))

    async def run(self) -> None:
        logger.info("Bot startet...")
        ts.set_status("starting")

        # Datenbank initialisieren
        init_db()
        self._session_id = start_session()
        record_activity("bot_start", "Bot gestartet", f"Session #{self._session_id}")

        # Ausfallzeit ermitteln, bevor die neue Session startet
        last_stop = get_last_stop_epoch()
        downtime_s = (time.time() - last_stop) if last_stop else None

        await self._browser.start()

        if not await self._auth.ensure_logged_in():
            logger.error("Login fehlgeschlagen. Beende.")
            await self._browser.stop()
            return

        if downtime_s is None or downtime_s > 300:
            logger.info("Bot gestartet (Erststart oder Ausfallzeit >5 min).")
        else:
            logger.info("Bot neugestartet nach %.0f s (Update-Neustart).", downtime_s)

        logger.info("Hauptschleife aktiv. Stoppen mit Ctrl+C.")
        try:
            # Status-Reporter, DB-Recorder und Auth-Checker als parallele Tasks
            status_task  = asyncio.create_task(self._status_reporter())
            db_task      = asyncio.create_task(self._db_recorder())
            auth_task    = asyncio.create_task(self._auth_checker())
            daily_task   = asyncio.create_task(self._daily_reporter())
            tg_task      = asyncio.create_task(self._telegram_listener())
            res_task     = asyncio.create_task(self._resource_monitor())
            planet_task  = asyncio.create_task(self._hourly_planet_scan())
            try:
                while True:
                    await self._run_turn()
                    # Warte turn_delay_s Sekunden, aber prüfe jede Sekunde auf
                    # Dashboard-Anfragen für schnelle Reaktion (≤1s)
                    delay = int(self._config.bot.turn_delay_s)
                    for _ in range(delay):
                        await asyncio.sleep(1)
                        if ts.has_execute_request() or ts.has_donate_request():
                            logger.debug("Vorzeitiger Wake-up durch Dashboard-Anfrage")
                            break
            finally:
                for t in (status_task, db_task, auth_task, daily_task, tg_task, res_task, planet_task):
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
        except asyncio.CancelledError:
            pass
        finally:
            # Letzte Daten speichern
            self._record_to_db()
            self._end_db_session()
            self._log_final_status()
            ts.set_status("stopped")
            record_activity(
                "bot_stop", "Bot gestoppt",
                f"{self._stats.turns_completed} Runden, {self._stats.actions_executed} Aktionen",
            )
            logger.info("Bot gestoppt.")
            await self._browser.stop()

    async def _status_reporter(self) -> None:
        """Gibt alle STATUS_INTERVAL_S Sekunden einen Fortschrittsbericht aus."""
        while True:
            await asyncio.sleep(STATUS_INTERVAL_S)
            self._log_status()

    async def _db_recorder(self) -> None:
        """Speichert alle RECORD_INTERVAL_S Sekunden einen Snapshot in die DB."""
        while True:
            await asyncio.sleep(RECORD_INTERVAL_S)
            self._record_to_db()

    async def _auth_checker(self) -> None:
        """Prueft alle 15 Minuten ob der Bot noch eingeloggt ist."""
        while True:
            await asyncio.sleep(AUTH_CHECK_INTERVAL_S)
            try:
                logged_in = await self._auth._is_logged_in()
                if logged_in:
                    logger.debug("Auth-Check: noch eingeloggt.")
                else:
                    logger.warning("Auth-Check: Session abgelaufen — Re-Login...")
                    success = await self._auth.ensure_logged_in()
                    if not success:
                        logger.error("Re-Login fehlgeschlagen — Browser-Neustart.")
                        self._stats.browser_restarts += 1
                        await self._browser.restart()
                        await self._auth.ensure_logged_in()
            except Exception as e:
                logger.error("Auth-Check: %s", type(e).__name__)

    async def _daily_reporter(self) -> None:
        """Sendet jeden Tag um 11:30 Uhr einen Statusbericht via Telegram."""
        while True:
            now = datetime.datetime.now()
            target = now.replace(hour=11, minute=30, second=0, microsecond=0)
            if now >= target:
                target += datetime.timedelta(days=1)
            wait_sec = (target - now).total_seconds()
            logger.debug("Tagesbericht in %.0f Minuten.", wait_sec / 60)
            await asyncio.sleep(wait_sec)
            await self._send_daily_report()

    async def _send_daily_report(self) -> None:
        """Baut die tägliche Statusnachricht und sendet sie."""
        state = self._last_state
        if state is None:
            return
        r = state.resources
        rt = state.rates
        rank_str = f"Platz {self._last_rank}" if self._last_rank else "unbekannt"
        today = datetime.datetime.now().strftime("%d.%m.%Y")

        def _fmt(val: float, rate: float, unit: str = "") -> str:
            return f"{val:>12,.0f}{unit}  ({rate:+,.0f}/h)"

        lines = [
            f"📊 <b>Tagesbericht {today}</b>",
            "",
            f"🏆 Rang: <b>{rank_str}</b>  |  Punkte: {state.points:,}",
            f"👥 Bevölkerung: {state.population_free:,} frei / {state.population_max:,} max"
            f"  |  😊 {state.satisfaction * 100:.0f} %",
            "",
            "<b>Ressourcen:</b>",
            f"  ⛏ Eisen      : {_fmt(r.iron,       rt.iron)}",
            f"  🔩 Stahl      : {_fmt(r.steel,      rt.steel)}",
            f"  🧪 Chemikalien: {_fmt(r.chemicals,  rt.chemicals)}",
            f"  🧊 Eis        : {_fmt(r.ice,        rt.ice)}",
            f"  💧 Wasser     : {_fmt(r.water,      rt.water)}",
            f"  ⚡ Energie    : {_fmt(r.energy,     rt.energy)}",
            f"  💎 VV4A       : {_fmt(r.vv4a,       rt.vv4a)}",
            f"  💰 Credits    : {r.credits:>12,.1f}  ({rt.credits:+,.1f}/h)",
            f"  🔬 FP         : {_fmt(r.fp,         rt.fp)}",
        ]
        await self._notify("\n".join(lines))
        logger.info("Tagesbericht gesendet.")

    async def _send_highscore(self) -> None:
        """Sendet die Gesamtpunkte-Highscore-Liste via Telegram."""
        raw = self._last_raw or {}
        entries = raw.get("highscore", {}).get("points", {}).get("entries", [])
        if not entries:
            await self._notify("⚠️ Keine Highscore-Daten verfügbar — Bot noch nicht lange genug aktiv.")
            return

        own_name = self._config.auth.username.lower()
        lines = ["🏆 <b>Highscore — Gesamtpunkte</b>", ""]

        for e in sorted(entries, key=lambda x: int(x.get("rank", 999))):
            rank = e.get("rank", "?")
            name = e.get("username", "?")
            value = int(e.get("value", 0))
            alliance = e.get("alliance", "") or ""
            alliance_str = f" [{alliance}]" if alliance else ""

            # Eigenen Spieler hervorheben
            if name.lower() == own_name:
                lines.append(f"➡️ <b>{rank}. {name}{alliance_str} — {value:,} Pkt.</b>")
            else:
                lines.append(f"    {rank}. {name}{alliance_str} — {value:,} Pkt.")

        # Telegram-Limit: 4096 Zeichen — bei zu langer Liste kürzen
        msg = "\n".join(lines)
        if len(msg) > 4000:
            msg = msg[:3990] + "\n…(Liste gekürzt)"
        await self._notify(msg)

    # Ressourcen-Monitoring je Ressource mit Kapazität
    _MONITORED_RESOURCES: dict[str, str] = {
        "iron":      "Eisen ⛏",
        "steel":     "Stahl 🔩",
        "chemicals": "Chemikalien 🧪",
        "ice":       "Eis 🧊",
        "water":     "Wasser 💧",
        "energy":    "Energie ⚡",
        "vv4a":      "VV4A 💎",
    }
    _RES_LOW_THRESHOLD    = 0.15   # Alarm senden wenn unter 15 %
    _RES_OK_THRESHOLD     = 0.20   # Entwarnung wenn wieder über 20 %
    _RES_HIGH_THRESHOLD   = 0.95   # Spende auslösen wenn über 95 %
    _RES_DONATE_RESET     = 0.85   # Spende wieder erlaubt wenn unter 85 % gefallen
    _RES_DONATE_FRACTION  = 0.10   # 10 % des aktuellen Bestands spenden
    _RES_CHECK_INTERVAL   = 180    # Prüfintervall: 3 Minuten

    async def _resource_monitor(self) -> None:
        """Prüft alle 3 Minuten ob eine Ressource unter 15 % der Lagerkapazität sinkt.

        Sendet einmalig Alarm — wartet auf Erholung über 20 %, dann erst wieder.
        """
        while True:
            await asyncio.sleep(self._RES_CHECK_INTERVAL)
            state = self._last_state
            if state is None:
                continue
            await self._check_resource_levels(state)

    async def _check_resource_levels(self, state) -> None:
        for res, label in self._MONITORED_RESOURCES.items():
            ratio = state.capacity.fill_ratio(res, state.resources)
            if ratio <= 0:
                continue  # keine Kapazitätsdaten → überspringen

            current_val = int(getattr(state.resources, res, 0))
            cap_val = int(getattr(state.capacity, res, 0))
            rate_val = float(getattr(state.rates, res, 0))
            pct = int(ratio * 100)
            rate_str = f"{rate_val:+,.0f}/h"

            # ── Niedriger Füllstand (<15 %) ──────────────────────────────
            if ratio < self._RES_LOW_THRESHOLD and res not in self._low_resources:
                self._low_resources.add(res)
                logger.warning("Ressource niedrig: %s %.0f%%", res, ratio * 100)

            elif ratio >= self._RES_OK_THRESHOLD and res in self._low_resources:
                self._low_resources.discard(res)
                logger.info("Ressource erholt: %s %.0f%%", res, ratio * 100)

            # ── Hoher Füllstand (>95 %) → Spendenempfehlung ins Dashboard ──
            if ratio > self._RES_HIGH_THRESHOLD and res not in self._donated_resources:
                donate_amount = int(current_val * self._RES_DONATE_FRACTION)
                if donate_amount <= 0:
                    continue
                self._donated_resources.add(res)
                logger.info("Allianz-Spende empfohlen: %s %d (%.0f%% voll)", res, donate_amount, pct)
                ts.add_donate_recommended(res, donate_amount, label, pct)

            elif ratio < self._RES_DONATE_RESET and res in self._donated_resources:
                # Unter 85 % gefallen → Empfehlung zurückziehen
                self._donated_resources.discard(res)
                ts.clear_donate_recommended(res)
                logger.debug("Spende-Reset: %s (%.0f%%)", res, ratio * 100)

    _PLANET_SCAN_INTERVAL_S: int = 3600  # stündlich

    async def _hourly_planet_scan(self) -> None:
        """Prüft stündlich ob neue Planeten/Kolonien im Spiel vorhanden sind.

        Vergleich über city_id — alle Kolonien aus dem API-Response werden mit
        den bekannten Planeten abgeglichen. Neu gefundene werden direkt zur
        Planetenliste hinzugefügt, damit sie nicht nächste Stunde erneut gemeldet werden.
        """
        await asyncio.sleep(self._PLANET_SCAN_INTERVAL_S)
        while True:
            state = self._last_state
            if state is not None:
                known_ids = {c.get("id") for c in self._planet_cities if c.get("id")}

                # Alle dem Spiel bekannten Kolonien sammeln
                all_colonies: list[dict] = list(state.colonies or [])
                if state.city_id:
                    # aktuelle Stadt ergänzen falls noch nicht enthalten
                    if state.city_id not in {c.get("id") for c in all_colonies}:
                        all_colonies.append({
                            "id": state.city_id,
                            "name": state.city_name or "",
                            "coords": state.coords or "",
                        })

                new_planets: list[dict] = []
                for col in all_colonies:
                    city_id = col.get("id")
                    if not city_id:
                        continue
                    if city_id in self._excluded_planet_ids:
                        continue
                    if city_id not in known_ids:
                        new_planets.append(col)
                        known_ids.add(city_id)

                count = len(self._planet_cities)
                record_activity(
                    "bot_action",
                    f"Planeten-Check: {count} bekannt",
                    f"{len(new_planets)} neu entdeckt" if new_planets else "keine neuen Planeten",
                )

                if new_planets:
                    # Neu gefundene Planeten sofort zur Liste hinzufügen
                    for col in new_planets:
                        self._planet_cities.append(dict(col))
                    planets_store.save(self._planet_cities)

                    names = [
                        f"{c.get('name', 'Unbekannt')} ({c.get('coords', '?')})"
                        for c in new_planets
                    ]
                    logger.info("Stündlicher Scan: neue Planeten gefunden: %s", ", ".join(names))
                    await self._notify(
                        "🌍 <b>Neuer Planet entdeckt (stündlicher Scan)</b>\n" +
                        "\n".join(f"• {n}" for n in names)
                    )
                else:
                    logger.debug("Stündlicher Planeten-Scan: keine neuen Planeten.")

            await asyncio.sleep(self._PLANET_SCAN_INTERVAL_S)

    async def _telegram_listener(self) -> None:
        """Long-polling für Telegram-Befehle (/stop, /start).

        Nur Nachrichten vom konfigurierten chat_id werden verarbeitet.
        Läuft dauerhaft parallel zum Bot-Loop.
        """
        if not self._tg:
            return
        offset = 0
        logger.info("Telegram-Listener gestartet (wartet auf /stop und /start).")
        while True:
            updates = await self._tg.get_updates(offset=offset, timeout=30)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                if chat_id != self._tg.chat_id:
                    continue  # fremde Chats ignorieren
                text = msg.get("text", "").strip().lower()
                if text == "/stop":
                    ts.set_paused(True)
                    logger.info("Telegram: Bot pausiert via /stop")
                    record_activity("bot_pause", "Bot pausiert", "via Telegram /stop")
                    await self._notify(
                        "⏸️ <b>Bot pausiert.</b>\n"
                        "Alle Entscheidungen werden ausgesetzt.\n"
                        "Sende /start zum Fortfahren."
                    )
                elif text == "/start":
                    ts.set_paused(False)
                    logger.info("Telegram: Bot fortgesetzt via /start")
                    record_activity("bot_resume", "Bot fortgesetzt", "via Telegram /start")
                    await self._notify("▶️ <b>Bot läuft wieder.</b>")
                elif text == "/status":
                    logger.info("Telegram: Statusbericht angefordert via /status")
                    await self._send_daily_report()
                elif text == "/highscore":
                    logger.info("Telegram: Highscore angefordert via /highscore")
                    await self._send_highscore()
                elif text == "/spenden":
                    logger.info("Telegram: Spende angefordert via /spenden")
                    await self._execute_telegram_donate()
                elif text.startswith("/priorität") or text.startswith("/prioritaet"):
                    await self._handle_telegram_priority(text)
                elif text == "/hilfe" or text == "/help":
                    await self._send_help()

    def _record_to_db(self) -> None:
        """Schreibt den aktuellen GameState + Highscores in die SQLite-DB."""
        if self._last_state is None:
            return
        try:
            record_snapshot(self._last_state)
        except Exception as e:
            logger.error("DB-Snapshot: %s", type(e).__name__)

        try:
            hs_data = (self._last_raw or {}).get("highscore", {})
            if hs_data:
                record_highscores(hs_data)
        except Exception as e:
            logger.error("DB-Highscore: %s", type(e).__name__)

    def _record_action_event(self, action: Action) -> None:
        """Speichert einen erfolgreichen Bau- oder Forschungsstart in der DB."""
        try:
            p = action.params
            if action.type in ("build_specific", "build_storage"):
                btype = p.get("building_type", "")
                name  = p.get("building_name", "") or building_display_name(btype, btype)
                record_build_event("build", btype, name)
            elif action.type == "build_next_building":
                # Name erst nach dem Bau bekannt — Typ aus den Parametern
                record_build_event("build", "auto", p.get("label", "Nächstes Gebäude"))
            elif action.type == "start_research":
                rtype = p.get("research_type", "")
                name  = p.get("name", rtype)
                record_build_event("research", rtype, name)
        except Exception as e:
            logger.debug("DB-BuildEvent: %s", type(e).__name__)

    def _end_db_session(self) -> None:
        """Beendet die aktuelle DB-Session mit Statistiken."""
        if self._session_id is None:
            return
        try:
            end_session(self._session_id, {
                "turns": self._stats.turns_completed,
                "executed": self._stats.actions_executed,
                "failed": self._stats.actions_failed,
            })
        except Exception as e:
            logger.error("DB Session-Ende: %s", type(e).__name__)

    def _log_status(self) -> None:
        sep = "=" * 60
        if self._last_state:
            msg = self._stats.progress_str(self._last_state)
        else:
            msg = f"  Laufzeit: {self._stats.uptime_str()} — noch keine Spieldaten"
        logger.info(
            "\n%s\n  *** STATUS-BERICHT (alle %ds) ***\n%s\n%s",
            sep, STATUS_INTERVAL_S, msg, sep,
        )

    def _log_final_status(self) -> None:
        sep = "=" * 60
        if self._last_state:
            msg = self._stats.progress_str(self._last_state)
        else:
            msg = f"  Laufzeit: {self._stats.uptime_str()}"
        logger.info(
            "\n%s\n  *** ABSCHLUSS-BERICHT ***\n%s\n%s",
            sep, msg, sep,
        )

    # ──────────────────────────────────────────────────────────────────────
    #  Telegram-Hilfsmethoden
    # ──────────────────────────────────────────────────────────────────────

    async def _notify(self, text: str) -> None:
        """Sendet eine Telegram-Nachricht — tut nichts wenn Telegram deaktiviert."""
        if self._tg:
            await self._tg.send(text)

    def _extract_rank(self, raw: dict) -> Optional[int]:
        """Liest den eigenen Rang aus den Highscore-Daten (Kategorie 'points')."""
        username = self._config.auth.username.lower()
        entries = raw.get("highscore", {}).get("points", {}).get("entries", [])
        for entry in entries:
            if entry.get("username", "").lower() == username:
                return int(entry.get("rank", 0)) or None
        return None

    async def _check_rank_change(self, raw: dict) -> None:
        """Vergleicht den aktuellen Rang mit dem letzten — sendet bei Änderung."""
        current_rank = self._extract_rank(raw)
        if current_rank is None:
            return
        if self._last_rank is not None and current_rank != self._last_rank:
            diff = self._last_rank - current_rank
            arrow = "📈" if diff > 0 else "📉"
            direction = "verbessert" if diff > 0 else "verschlechtert"
            msg = (
                f"{arrow} <b>Rang-Änderung!</b>\n"
                f"Gesamtpunkte: Platz {self._last_rank} → <b>Platz {current_rank}</b> "
                f"({direction} um {abs(diff)})"
            )
            logger.info("Rang-Änderung: %d → %d", self._last_rank, current_rank)
            await self._notify(msg)
        self._last_rank = current_rank

    # ── Multi-Planet-Rotation ─────────────────────────────────────────────────

    # Minimale Pause zwischen zwei Wechseln (verhindert Thrashing)
    _PLANET_MIN_INTERVAL_S: int = 5 * 60        # 5 Minuten
    # Spätestens nach dieser Zeit einen nicht besuchten Planeten anschauen
    _PLANET_IDLE_VISIT_S:   int = 25 * 60       # 25 Minuten

    def _update_planet_list(self, current_city_id: int, colonies: list[dict]) -> None:
        """Aktualisiert die bekannte Planetenliste aus den API-Koloniedaten.

        ``colonies`` ist die Liste der ANDEREN Städte (aus city.colonies).
        Die aktuelle Stadt wird anhand von ``current_city_id`` an der richtigen
        Position in ``_planet_cities`` gehalten.

        Neu erkannte Planeten werden gemeldet.
        """
        # Ausgeschlossene Planeten aus den colonies-Daten herausfiltern
        colonies = [c for c in colonies
                    if isinstance(c, dict) and c.get("id")
                    and c.get("id") not in self._excluded_planet_ids]

        known_ids = {c.get("id") for c in self._planet_cities}
        new_planet_ids = {c.get("id") for c in colonies} - known_ids - {current_city_id}

        # Vollständige Liste: aktuelle Stadt + alle Kolonien
        all_cities: list[dict] = []
        current_entry: dict | None = next(
            (c for c in self._planet_cities if c.get("id") == current_city_id),
            None,
        )
        if current_entry is None:
            current_entry = {"id": current_city_id}
        all_cities.append(current_entry)

        for col in colonies:
            all_cities.append(col)

        # Index der aktuellen Stadt in der neuen Liste ermitteln
        old_current_id = (
            self._planet_cities[self._current_city_idx].get("id")
            if self._planet_cities else current_city_id
        )
        self._planet_cities = all_cities
        # Aktuellen Index auf die Stadt setzen, die gerade aktiv ist
        for i, c in enumerate(self._planet_cities):
            if c.get("id") == current_city_id:
                self._current_city_idx = i
                break

        if new_planet_ids:
            names = [
                next((c.get("name", str(pid)) for c in colonies if c.get("id") == pid), str(pid))
                for pid in new_planet_ids
            ]
            logger.info("Neue Planeten/Kolonien entdeckt: %s", ", ".join(names))
            asyncio.ensure_future(self._notify(
                "🌍 <b>Neue Kolonie entdeckt!</b>\n" +
                "\n".join(f"• {n}" for n in names)
            ))

        planets_store.save(self._planet_cities)

    async def _maybe_switch_planet(self) -> bool:
        """Prüft ob ein Planetenwechsel sinnvoll ist und führt ihn aus.

        Prioritäten (höchste zuerst):
        1. Dashboard-Anfrage (Nutzer klickte "Wechseln"-Button)
        2. Freier Bauplatz auf anderem Planeten (Build-Queue leer oder gerade frei geworden)
        3. Planet wurde länger als _PLANET_IDLE_VISIT_S nicht besucht (Gelegenheitscheck)

        Zwischen zwei Wechseln gilt immer eine Mindestpause von _PLANET_MIN_INTERVAL_S.
        Gibt True zurück wenn gewechselt wurde.
        """
        if len(self._planet_cities) <= 1:
            return False

        now = time.time()
        if now - self._last_planet_switch < self._PLANET_MIN_INTERVAL_S:
            return False

        current_id = self._planet_cities[self._current_city_idx].get("id", 0)

        # ── Priorität 0: explizite Dashboard-Anfrage ──────────────────────────
        requested_city = ts.consume_switch_planet_request()
        if requested_city and requested_city != current_id:
            return await self._do_switch_planet(requested_city, reason="Dashboard-Anfrage")
        elif requested_city:
            return False   # bereits auf dem gewünschten Planeten

        # ── Priorität 1: Build-Queue frei geworden ────────────────────────────
        # "_build_free_at" ist der Timestamp wann die Warteschlange leer wird.
        # 0 bedeutet: Queue war beim letzten Scrape schon leer → Bauplatz frei.
        best_free: tuple[int, dict] | None = None   # (idx, city)
        for i, city in enumerate(self._planet_cities):
            if city.get("id") == current_id:
                continue
            build_free_at = city.get("_build_free_at", 0)
            last_visited  = city.get("_last_visited", 0)
            if build_free_at == 0 and last_visited > 0:
                # Queue war leer als wir zuletzt dort waren — prüfen ob Bausloterr neu
                # Wir besuchen diesen Planeten erst wenn er auch "alt genug" ist
                if now - last_visited >= self._PLANET_IDLE_VISIT_S:
                    best_free = (i, city)
                    break
            elif build_free_at > 0 and build_free_at <= now:
                # Build gerade fertig geworden seit letztem Besuch
                if last_visited < build_free_at:
                    best_free = (i, city)
                    break

        if best_free:
            idx, city = best_free
            reason = "Bauplatz frei"
            return await self._do_switch_planet(int(city.get("id", 0)), reason=reason)

        # ── Priorität 2: Idle-Check — Planet lange nicht besucht ─────────────
        # Wähle den am längsten nicht besuchten Planeten (round-robin als Fallback)
        oldest_visited = now
        oldest_city: dict | None = None
        oldest_idx = -1
        for i, city in enumerate(self._planet_cities):
            if city.get("id") == current_id:
                continue
            last_visited = city.get("_last_visited", 0)
            if last_visited < oldest_visited:
                oldest_visited = last_visited
                oldest_city = city
                oldest_idx = i

        if oldest_city and (now - oldest_visited) >= self._PLANET_IDLE_VISIT_S:
            reason = f"Idle-Check (vor {int((now - oldest_visited) / 60)} min zuletzt besucht)"
            return await self._do_switch_planet(int(oldest_city.get("id", 0)), reason=reason)

        return False

    def _update_next_switch_info(self) -> None:
        """Berechnet den nächsten wahrscheinlichen Planet-Wechsel und schreibt ihn in task_state."""
        if len(self._planet_cities) <= 1:
            ts.set_next_planet_switch(None, "")
            return

        now = time.time()
        current_id = self._planet_cities[self._current_city_idx].get("id", 0)
        earliest_switch = self._last_planet_switch + self._PLANET_MIN_INTERVAL_S

        # Kandidaten in Prioritätsreihenfolge (gleiche Logik wie _maybe_switch_planet)
        target: dict | None = None

        for city in self._planet_cities:
            if city.get("id") == current_id:
                continue
            build_free_at = city.get("_build_free_at", 0)
            last_visited  = city.get("_last_visited", 0)
            if build_free_at == 0 and last_visited > 0 and (now - last_visited) >= self._PLANET_IDLE_VISIT_S:
                target = city; break
            if build_free_at > 0 and build_free_at <= now and last_visited < build_free_at:
                target = city; break

        if target is None:
            oldest_visited = now
            for city in self._planet_cities:
                if city.get("id") == current_id:
                    continue
                lv = city.get("_last_visited", 0)
                if lv < oldest_visited:
                    oldest_visited = lv
                    target = city

        name = ""
        if target:
            n = target.get("name", "")
            c = target.get("coords", "")
            name = f"{n} ({c})" if n and c else (n or f"ID {target.get('id','?')}")

        ts.set_next_planet_switch(earliest_switch, name)

    async def _do_switch_planet(self, city_id: int, reason: str = "") -> bool:
        """Führt den eigentlichen Planetenwechsel durch und aktualisiert Tracking-Daten."""
        from_city = self._planet_cities[self._current_city_idx] if self._planet_cities else {}
        to_city   = next((c for c in self._planet_cities if c.get("id") == city_id), {})

        from_label = f"{from_city.get('name', '?')} ({from_city.get('coords', '?')})"
        to_label   = f"{to_city.get('name', '?')} ({to_city.get('coords', '?')})"

        logger.info("Planet-Wechsel: %s → %s [%s] (ID %d)", from_label, to_label, reason, city_id)

        success = await self._scraper.switch_to_city(city_id)
        if success:
            # Index aktualisieren
            for i, c in enumerate(self._planet_cities):
                if c.get("id") == city_id:
                    self._current_city_idx = i
                    break
            self._last_planet_switch = time.time()
            planets_store.save(self._planet_cities)
            logger.info("Planet-Wechsel erfolgreich → %s", to_label)
            record_activity(
                "planet_switch",
                f"Planet gewechselt → {to_label}",
                f"von {from_label} · Grund: {reason}",
                city_id=city_id,
            )
            return True

        logger.warning("Planet-Wechsel zu %s (ID %d) fehlgeschlagen.", to_label, city_id)
        return False

    def _build_colony_snapshot(self, state: GameState) -> dict:
        """Kompakter Status-Snapshot der aktuellen Stadt für das Dashboard."""
        r = state.resources
        cap = state.capacity
        rt = state.rates

        def fill(res: str) -> float:
            c = getattr(cap, res, 0)
            v = getattr(r, res, 0)
            return round(v / c, 4) if c > 0 else 0.0

        return {
            "city_id":     state.city_id,
            "city_name":   state.city_name,
            "coords":      state.coords,
            "planet_type": state.planet_type,
            "points":      state.points,
            "population_free": state.population_free,
            "population_max":  state.population_max,
            "satisfaction":    round(state.satisfaction, 3),
            "resources": {
                "iron":      round(r.iron),
                "steel":     round(r.steel),
                "chemicals": round(r.chemicals),
                "ice":       round(r.ice),
                "water":     round(r.water),
                "energy":    round(r.energy),
                "vv4a":      round(r.vv4a),
                "credits":   round(r.credits, 1),
                "fp":        round(r.fp),
            },
            "capacity": {
                "iron":      round(cap.iron),
                "steel":     round(cap.steel),
                "chemicals": round(cap.chemicals),
                "ice":       round(cap.ice),
                "water":     round(cap.water),
                "energy":    round(cap.energy),
                "vv4a":      round(cap.vv4a),
            },
            "fill": {
                "iron":      fill("iron"),
                "steel":     fill("steel"),
                "chemicals": fill("chemicals"),
                "ice":       fill("ice"),
                "water":     fill("water"),
                "energy":    fill("energy"),
                "vv4a":      fill("vv4a"),
            },
            "rates": {
                "iron":      round(rt.iron),
                "steel":     round(rt.steel),
                "chemicals": round(rt.chemicals),
                "ice":       round(rt.ice),
                "water":     round(rt.water),
                "energy":    round(rt.energy),
                "vv4a":      round(rt.vv4a),
                "credits":   round(rt.credits, 1),
                "fp":        round(rt.fp),
            },
            "build_queue": [
                {"name": b.name, "finish_time": b.finish_time, "remaining_sec": b.remaining_sec}
                for b in state.build_queue
            ],
            "scraped_at": time.time(),
        }

    def _colony_label(self, state: GameState) -> str:
        """Liefert einen sprechenden Kolonie-Namen für Benachrichtigungen.

        Bevorzugt ``state.city_name`` (liefert das Spiel als "<User>s Kolonie"),
        fällt auf ``<username>s Kolonie`` zurück, wenn die API noch nichts
        gesetzt hat. Coords werden angehängt, falls vorhanden.
        """
        name = (state.city_name or "").strip()
        if not name:
            user = (self._config.auth.username or "").strip()
            name = f"{user}s Kolonie" if user else "deiner Kolonie"
        coords = (state.coords or "").strip()
        if coords:
            return f"{name} ({coords})"
        return name

    async def _check_completed_buildings(self, state: GameState) -> None:
        """Erkennt fertiggestellte Gebäude (Einträge die aus der Queue verschwunden sind).

        Vergleich über finish_time statt Name — so werden auch Gebäude erkannt
        die sofort durch denselben Typ ersetzt werden (gleicher Name, neue Zeit).
        Die Telegram-Nachricht nennt Name + neue Stufe + Kolonie, z. B.:
        "🏗️ Chemielager (Stufe 17) auf admin12345s Kolonie ist fertig".
        """
        if not self._last_queue:
            return  # erste Runde — keine Vergleichsbasis

        # finish_time ist pro Bauslot eindeutig — verschwindet sie, ist das Gebäude fertig
        curr_finish_times = {item.finish_time for item in state.build_queue if item.finish_time}

        # Typ → aktuelle Stufe (nach Fertigstellung zeigt BuildingInfo.level die
        # gerade frisch erreichte Stufe).
        level_by_type: dict[str, int] = {
            (b.type or ""): int(b.level or 0)
            for b in (state.buildings or [])
            if b.type
        }

        colony = self._colony_label(state)

        for item in self._last_queue:
            if not item.finish_time:
                continue
            if item.finish_time in curr_finish_times:
                continue

            btype = item.building_type or ""
            name = (
                item.name
                or building_display_name(btype, btype)
                or "Unbekanntes Gebäude"
            )
            level = level_by_type.get(btype, 0)

            level_part = f" (Stufe {level})" if level > 0 else ""
            logger.info(
                "Gebäude fertig: '%s'%s auf %s (finish_time=%s)",
                name, level_part, colony, item.finish_time,
            )
            record_activity(
                "build_complete", f"{name}{level_part} fertig", colony,
                city_id=state.city_id,
            )
            await self._notify(
                f"🏗️ <b>{name}</b>{level_part} auf {colony} ist fertig"
            )

    async def _detect_new_unlocks(self, state: GameState) -> None:
        """Erkennt neu verfügbare Gebäude (unbekannter Typ) und neu abgeschlossene
        Forschung und meldet sie per Telegram. Jeder Eintrag wird nur einmal
        pro Runtime gemeldet (seen-Sets in task_state).
        """
        # 1) Gebäude-Typen, die nicht in BUILDING_NAMES stehen (z. B. neu freigeschaltet)
        for b in state.buildings or []:
            btype = b.type or ""
            if not btype or btype in BUILDING_NAMES:
                continue
            if ts.mark_building_seen(btype):
                name = b.name or btype
                logger.info("Neuer Gebäude-Typ erkannt: %s (%s)", btype, name)
                try:
                    log_path = f"logs/unknown_buildings.log"
                    import os
                    os.makedirs("logs", exist_ok=True)
                    with open(log_path, "a", encoding="utf-8") as fh:
                        fh.write(f"{datetime.datetime.now().isoformat()} {btype} {name}\n")
                except Exception:
                    pass
                await self._notify(
                    f"🏗 <b>Neues Gebäude entdeckt</b>\n<code>{btype}</code> — {name}"
                )

        # 2) Neu abgeschlossene Forschung (seit dem letzten Turn)
        for r in state.research or []:
            if not r.is_researched:
                continue
            if not r.type:
                continue
            if ts.mark_research_seen(r.type):
                # Erster Turn: seen-Set wird vorher initialisiert → hier kommen
                # wirklich nur neu abgeschlossene Items an.
                unlocks = ""
                if r.unlocks_buildings:
                    unlocks = "\nSchaltet frei: " + ", ".join(r.unlocks_buildings)
                elif r.unlocks_ships:
                    unlocks = "\nSchaltet frei: " + ", ".join(r.unlocks_ships)
                logger.info("Neue Forschung abgeschlossen: %s", r.name)
                record_activity(
                    "research_complete", f"Forschung: {r.name}",
                    unlocks.strip() if unlocks else "",
                    city_id=state.city_id,
                )
                await self._notify(
                    f"✅ <b>Forschung abgeschlossen</b>\n{r.name}{unlocks}"
                )

    @staticmethod
    def _is_night_mode() -> bool:
        """True zwischen 23:00 und 04:00 Uhr — kein Auto-Build."""
        hour = datetime.datetime.now().hour
        return hour >= 23 or hour < 4

    async def _execute_requested_action_if_pending(self) -> None:
        """Führt eine vom Dashboard angeforderte Aktion aus."""
        action_dict = ts.consume_execute_request()
        if action_dict is None:
            return

        action = Action(type=action_dict["type"], params=action_dict.get("params", {}))
        task = _action_to_task(action)
        ts.set_running(task)
        logger.info("Dashboard-Ausführung: %s", action)

        try:
            success = await self._executor.execute(action)
            ts.set_done(task, success)
            ts.set_execute_result("ok" if success else "fehlgeschlagen")

            btype = action.params.get("building_type", "") if action.type in (
                "build_specific", "build_storage"
            ) else ""

            if success:
                self._stats.actions_executed += 1
                self._record_action_event(action)
                if btype:
                    cooldown.record_success(btype)
                ts.set_recommended_action(None)
                record_activity(
                    "bot_action", task.label, (task.reason or "Dashboard-Ausführung"),
                )
            else:
                self._stats.actions_failed += 1
                logger.warning("Dashboard-Aktion fehlgeschlagen: %s", action)
                if btype:
                    cooldown.record_failure(btype, reason=action.params.get("reason", ""))

            await asyncio.sleep(self._config.bot.action_delay_ms / 1000)

        except Exception as e:
            ts.set_execute_result(f"Fehler: {type(e).__name__}")
            logger.error("Dashboard-Ausführung fehlgeschlagen: %s", type(e).__name__)

    async def _execute_donate_request_if_pending(self) -> None:
        """Führt eine vom Dashboard angeforderte Allianz-Spende via Browser aus."""
        donate_req = ts.consume_donate_request()
        if donate_req is None:
            return

        resource = donate_req["resource"]
        amount = donate_req["amount"]
        label = self._MONITORED_RESOURCES.get(resource, resource)
        logger.info("Dashboard-Spende (Browser): %s %d", resource, amount)

        success = await self._executor.donate_to_alliance({resource: amount})
        if success:
            ts.clear_donate_recommended(resource)
            await self._notify(
                f"🤝 <b>Allianz-Spende: {label}</b>\n"
                f"{amount:,} gespendet (via Dashboard)"
            )
        else:
            await self._notify(
                f"⚠️ <b>Spende fehlgeschlagen: {label}</b>\n"
                f"Browser-Aktion gescheitert"
            )

    async def _execute_telegram_donate(self) -> None:
        """Spendet 15% aller Ressourcen mit >85% Füllstand an die Allianz (Telegram /spenden)."""
        state = self._last_state
        if state is None:
            await self._notify("⚠️ Keine Spieldaten — Bot muss erst eine Runde laufen.")
            return

        donations: dict[str, int] = {}
        details: list[str] = []
        for res in ("iron", "steel", "chemicals", "ice", "water", "energy", "vv4a"):
            ratio = state.capacity.fill_ratio(res, state.resources)
            if ratio > 0.85:
                current_val = int(getattr(state.resources, res, 0))
                donate_amount = int(current_val * 0.15)
                if donate_amount > 0:
                    donations[res] = donate_amount
                    label = self._MONITORED_RESOURCES.get(res, res)
                    details.append(f"  {label}: {donate_amount:,} ({int(ratio * 100)}%)")

        if not donations:
            await self._notify("✅ Keine Ressource über 85 % — nichts zu spenden.")
            return

        success = await self._executor.donate_to_alliance(donations)
        summary = "\n".join(details)
        if success:
            await self._notify(f"🤝 <b>Allianz-Spende (/spenden)</b>\n{summary}")
        else:
            await self._notify(f"⚠️ <b>Spende fehlgeschlagen</b>\n{summary}")

    # ── Telegram: Priorität setzen ──────────────────────────────────────

    _PRIORITY_LABELS: dict[str, str] = {
        "balanced":  "⚖️ Ausgewogen",
        "iron":      "⛏ Eisen",
        "steel":     "🔩 Stahl",
        "chemicals": "🧪 Chemikalien",
        "ice":       "🧊 Eis",
        "water":     "💧 Wasser",
        "energy":    "⚡ Energie",
        "vv4a":      "💎 VV4A",
        "fp":        "🔬 Forschungspunkte",
        "credits":   "💰 Credits",
    }

    async def _handle_telegram_priority(self, text: str) -> None:
        """Verarbeitet /priorität [ressource] — zeigt oder setzt die Priorität."""
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            # Keine Ressource angegeben → aktuellen Status anzeigen
            current = G.priority_resource()
            label = self._PRIORITY_LABELS.get(current, current)
            options = "\n".join(
                f"  <code>{k}</code> — {v}" for k, v in self._PRIORITY_LABELS.items()
            )
            await self._notify(
                f"🎯 <b>Aktuelle Priorität:</b> {label}\n\n"
                f"<b>Setzen mit:</b>\n<code>/priorität [option]</code>\n\n"
                f"<b>Optionen:</b>\n{options}"
            )
            return

        value = parts[1].strip().lower()
        # Aliase
        aliases = {
            "ausgewogen": "balanced", "normal": "balanced", "auto": "balanced",
            "eisen": "iron", "stahl": "steel", "chemikalien": "chemicals",
            "chem": "chemicals", "eis": "ice", "wasser": "water",
            "energie": "energy", "forschung": "fp", "forschungspunkte": "fp",
        }
        value = aliases.get(value, value)

        if value not in self._PRIORITY_LABELS:
            await self._notify(
                f"⚠️ Unbekannte Priorität: <code>{value}</code>\n"
                f"Gültige Werte: {', '.join(self._PRIORITY_LABELS.keys())}"
            )
            return

        G.update({"priority_resource": value})
        label = self._PRIORITY_LABELS[value]
        logger.info("Telegram: Priorität gesetzt auf '%s'", value)
        await self._notify(f"🎯 <b>Priorität gesetzt:</b> {label}")

    async def _send_help(self) -> None:
        """Sendet eine Übersicht aller Telegram-Befehle."""
        await self._notify(
            "📖 <b>Verfügbare Befehle</b>\n\n"
            "/start — Bot fortsetzen\n"
            "/stop — Bot pausieren\n"
            "/status — Aktueller Ressourcen-Bericht\n"
            "/highscore — Highscore-Tabelle\n"
            "/spenden — 15% aller Ressourcen >85% an Allianz spenden\n"
            "/priorität [res] — Produktions-Priorität anzeigen/setzen\n"
            "/hilfe — Diese Hilfe anzeigen"
        )

    def _process_planet_removals(self) -> None:
        """Verarbeitet Planet-Entfernungsanfragen vom Dashboard."""
        for city_id in ts.consume_planet_remove_requests():
            self._planet_cities = [c for c in self._planet_cities if c.get("id") != city_id]
            self._excluded_planet_ids.add(city_id)
            planets_store.remove(city_id)
            ts.remove_colony_snapshot(city_id)
            # Index neu setzen falls nötig
            self._current_city_idx = min(self._current_city_idx, max(0, len(self._planet_cities) - 1))
            logger.info("Planet %d aus Rotation entfernt.", city_id)

    async def _run_turn(self) -> None:
        try:
            self._process_planet_removals()
            # Planet-Rotation: Dashboard-Anfragen + smart buildqueue-aware Switching
            switched = await self._maybe_switch_planet()
            if switched:
                await asyncio.sleep(3.0)

            raw = await self._scraper.scrape()
            state = parse_state(raw)
            self._last_state = state
            self._last_raw = raw

            # Kolonieliste pflegen + Snapshot dieser Stadt speichern
            self._update_planet_list(state.city_id, state.colonies)
            ts.set_colony_snapshot(state.city_id, self._build_colony_snapshot(state))
            ts.set_current_city_id(state.city_id)

            # Build-Tracking: wann wird der letzte Bauplatz dieser Stadt frei?
            # 0 = Queue ist leer (Platz sofort frei), >0 = Unix-Timestamp
            max_remaining = max(
                (b.remaining_sec for b in state.build_queue if b.remaining_sec > 0),
                default=0,
            )
            build_free_at = (time.time() + max_remaining) if max_remaining > 0 else 0
            for city in self._planet_cities:
                if city.get("id") == state.city_id:
                    city["_build_free_at"]  = build_free_at
                    city["_last_visited"]   = time.time()
                    # Koordinaten/Name/Typ aus dem echten Scrape aktualisieren
                    if state.coords:
                        city["coords"] = state.coords
                    if state.city_name:
                        city["name"] = state.city_name
                    if state.planet_type:
                        city["planet_type"] = state.planet_type
                    break

            # Initialzustand beim ersten Scrape merken
            if self._stats.initial_resources is None:
                # Colony-Diagnostik: einmalig DOM + JS-Funktionen analysieren
                await self._scraper.dump_colony_diagnostics()

                # seen-Sets initial befüllen, damit beim Boot keine Flut alter
                # 'Forschung abgeschlossen' / 'neues Gebäude'-Meldungen raus geht.
                already_done = [r.type for r in (state.research or []) if r.is_researched and r.type]
                ts.initialize_seen_research(already_done)
                for b in (state.buildings or []):
                    if b.type and b.type not in BUILDING_NAMES:
                        ts.mark_building_seen(b.type)

                self._stats.initial_resources = Resources(
                    iron=state.resources.iron,
                    steel=state.resources.steel,
                    chemicals=state.resources.chemicals,
                    ice=state.resources.ice,
                    water=state.resources.water,
                    energy=state.resources.energy,
                    vv4a=state.resources.vv4a,
                    credits=state.resources.credits,
                    fp=state.resources.fp,
                )
                self._stats.initial_points = state.points
                logger.info(
                    "Initialzustand gespeichert | Punkte=%d | Eisen=%.0f FP=%.0f",
                    state.points, state.resources.iron, state.resources.fp,
                )
                # Ersten Snapshot sofort speichern
                self._record_to_db()

            # Telegram: Rang-Änderung + fertige Gebäude prüfen
            await self._check_rank_change(raw)
            await self._check_completed_buildings(state)

            # Bauwarteschlange für nächste Runde merken
            self._last_queue = list(state.build_queue)

            # Task-State: Warteschlange aus dem Spiel aktualisieren (immer, auch pausiert)
            ts.update_game_queue(state.build_queue, state.active_research)
            ts.tick(self._stats.turns_completed + 1)

            # Dashboard-Ausführungsanfragen IMMER prüfen (auch wenn pausiert)
            await self._execute_requested_action_if_pending()
            await self._execute_donate_request_if_pending()

            # Wenn Bot pausiert: keine Entscheidungen treffen, nur protokollieren
            if ts.is_paused():
                logger.info("Bot pausiert — Runde %d übersprungen.", self._stats.turns_completed + 1)
                self._stats.turns_completed += 1
                return

            actions = self._strategy.decide(state)
            ts.update_planned([_action_to_task(a) for a in actions])

            # Scoring-Transparenz: sortierte Liste aller baubaren Gebäude für Dashboard
            try:
                ts.set_scoring_snapshot(build_scoring_snapshot(state, limit=30))
            except Exception as scoring_err:
                logger.debug("Scoring-Snapshot fehlgeschlagen: %s", scoring_err)

            # Auto-Detection: unbekannte Gebäudetypen + neu abgeschlossene Forschung
            await self._detect_new_unlocks(state)

            # Aktionen nach Typ trennen
            build_actions = [a for a in actions if a.type != "start_research"]
            research_actions = [a for a in actions if a.type == "start_research"]

            # ── Auto-Research: sofort wenn Labor frei ──────────────────────
            if research_actions:
                research = research_actions[0]
                task = _action_to_task(research)
                ts.set_running(task)
                success = await self._executor.execute(research)
                ts.set_done(task, success)
                if success:
                    self._stats.actions_executed += 1
                    self._record_action_event(research)
                    colony = self._colony_label(state)
                    record_activity(
                        "bot_action", task.label,
                        f"{task.reason or 'Auto-Research'} · {colony}",
                        city_id=state.city_id,
                    )
                    logger.info("Auto-Research gestartet: %s", task.label)
                else:
                    self._stats.actions_failed += 1
                await asyncio.sleep(self._config.bot.action_delay_ms / 1000)

            # ── Auto-Build: alle 7 Minuten, nicht nachts ──────────────────
            now = time.time()
            auto_build_due = (now - self._last_auto_build_time) >= 420  # 7 min

            if self._is_night_mode():
                logger.debug("Nachtmodus aktiv (23-04 Uhr) — kein Auto-Build.")
            elif build_actions and auto_build_due:
                build = build_actions[0]
                task = _action_to_task(build)
                ts.set_running(task)
                success = await self._executor.execute(build)
                ts.set_done(task, success)

                btype = build.params.get("building_type", "") if build.type in (
                    "build_specific", "build_storage"
                ) else ""

                if success:
                    self._stats.actions_executed += 1
                    self._record_action_event(build)
                    self._last_auto_build_time = now
                    if btype:
                        cooldown.record_success(btype)
                    colony = self._colony_label(state)
                    record_activity(
                        "bot_action", task.label,
                        f"{task.reason or 'Auto-Build'} · {colony}",
                        city_id=state.city_id,
                    )
                    logger.info("Auto-Build ausgeführt: %s", task.label)
                else:
                    self._stats.actions_failed += 1
                    if btype:
                        cooldown.record_failure(btype, reason=build.params.get("reason", ""))
                await asyncio.sleep(self._config.bot.action_delay_ms / 1000)

            # ── Empfehlung für Dashboard (erste Build-Action) ─────────────
            if build_actions:
                first = build_actions[0]
                first_task = _action_to_task(first)
                ts.set_recommended_action({
                    "type": first.type,
                    "params": dict(first.params),
                    "label": first_task.label,
                    "reason": first_task.reason,
                })
            else:
                ts.set_recommended_action(None)

            self._stats.turns_completed += 1
            self._consecutive_failures = 0
            self._update_next_switch_info()

        except Exception as e:
            self._consecutive_failures += 1
            ts.set_status("error", str(type(e).__name__))
            logger.error("Runde fehlgeschlagen (%d/%d): %s",
                         self._consecutive_failures, self._config.bot.max_retries,
                         type(e).__name__)
            record_activity("error", f"Fehler: {type(e).__name__}", str(e))

            if self._consecutive_failures >= self._config.bot.max_retries:
                logger.warning("Max. Fehler erreicht — Browser-Neustart.")
                self._stats.browser_restarts += 1
                await self._notify(
                    f"⚠️ <b>Bot-Fehler: Browser-Neustart</b>\n"
                    f"Fehler: <code>{type(e).__name__}</code>\n"
                    f"Neustart #{self._stats.browser_restarts}"
                )
                await self._browser.restart()
                await self._auth.ensure_logged_in()
                self._consecutive_failures = 0
            else:
                backoff = 2 ** self._consecutive_failures
                logger.info("Retry in %ds...", backoff)
                await asyncio.sleep(backoff)
