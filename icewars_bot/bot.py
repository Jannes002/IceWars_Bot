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
    start_session, end_session, get_last_stop_epoch, RECORD_INTERVAL_S,
)
from .scraper import GameScraper
from .state import BuildQueueItem, GameState, Resources, parse_state
from .strategy import Action, Strategy, building_display_name
from .telegram import TelegramNotifier, make_notifier
from . import task_state as ts
from . import cooldown

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

    async def run(self) -> None:
        logger.info("Bot startet...")
        ts.set_status("starting")

        # Datenbank initialisieren
        init_db()
        self._session_id = start_session()

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
            status_task = asyncio.create_task(self._status_reporter())
            db_task = asyncio.create_task(self._db_recorder())
            auth_task = asyncio.create_task(self._auth_checker())
            daily_task = asyncio.create_task(self._daily_reporter())
            tg_task = asyncio.create_task(self._telegram_listener())
            res_task = asyncio.create_task(self._resource_monitor())
            try:
                while True:
                    await self._run_turn()
                    await asyncio.sleep(self._config.bot.turn_delay_s)
            finally:
                for t in (status_task, db_task, auth_task, daily_task, tg_task, res_task):
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
                await self._notify(
                    f"📉 <b>Lager fast leer: {label}</b>\n"
                    f"{current_val:,} / {cap_val:,}  ({pct} %)\n"
                    f"Produktion: {rate_str}"
                )

            elif ratio >= self._RES_OK_THRESHOLD and res in self._low_resources:
                # Wieder über 20 % → Entwarnung
                self._low_resources.discard(res)
                logger.info("Ressource erholt: %s %.0f%%", res, ratio * 100)
                await self._notify(
                    f"📈 <b>Lager wieder gefüllt: {label}</b>\n"
                    f"{current_val:,} / {cap_val:,}  ({pct} %)\n"
                    f"Produktion: {rate_str}"
                )

            # ── Hoher Füllstand (>95 %) → Allianz-Spende ─────────────────
            if ratio > self._RES_HIGH_THRESHOLD and res not in self._donated_resources:
                donate_amount = int(current_val * self._RES_DONATE_FRACTION)
                if donate_amount <= 0:
                    continue
                self._donated_resources.add(res)
                logger.info("Allianz-Spende: %s %d (%.0f%% voll)", res, donate_amount, pct)
                result = await self._scraper.donate_to_alliance(res, donate_amount)
                if "_error" in result:
                    logger.warning("Spende fehlgeschlagen (%s): HTTP %s", res, result.get("_error"))
                    await self._notify(
                        f"⚠️ <b>Spende fehlgeschlagen: {label}</b>\n"
                        f"Versuchte Spende: {donate_amount:,}\n"
                        f"API-Fehler: HTTP {result.get('_error')}"
                    )
                else:
                    await self._notify(
                        f"🤝 <b>Allianz-Spende: {label}</b>\n"
                        f"{donate_amount:,} gespendet  (Lager war {pct} % voll)\n"
                        f"Produktion: {rate_str}"
                    )

            elif ratio < self._RES_DONATE_RESET and res in self._donated_resources:
                # Unter 85 % gefallen → Spende wieder erlauben
                self._donated_resources.discard(res)
                logger.debug("Spende-Reset: %s (%.0f%%)", res, ratio * 100)

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
                    await self._notify(
                        "⏸️ <b>Bot pausiert.</b>\n"
                        "Alle Entscheidungen werden ausgesetzt.\n"
                        "Sende /start zum Fortfahren."
                    )
                elif text == "/start":
                    ts.set_paused(False)
                    logger.info("Telegram: Bot fortgesetzt via /start")
                    await self._notify("▶️ <b>Bot läuft wieder.</b>")
                elif text == "/status":
                    logger.info("Telegram: Statusbericht angefordert via /status")
                    await self._send_daily_report()
                elif text == "/highscore":
                    logger.info("Telegram: Highscore angefordert via /highscore")
                    await self._send_highscore()

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

    async def _check_completed_buildings(self, state: GameState) -> None:
        """Erkennt fertiggestellte Gebäude (Einträge die aus der Queue verschwunden sind).

        Vergleich über finish_time statt Name — so werden auch Gebäude erkannt
        die sofort durch denselben Typ ersetzt werden (gleicher Name, neue Zeit).
        """
        if not self._last_queue:
            return  # erste Runde — keine Vergleichsbasis

        # finish_time ist pro Bauslot eindeutig — verschwindet sie, ist das Gebäude fertig
        curr_finish_times = {item.finish_time for item in state.build_queue if item.finish_time}

        for item in self._last_queue:
            if not item.finish_time:
                continue
            if item.finish_time not in curr_finish_times:
                name = item.name or item.building_type or "Unbekanntes Gebäude"
                logger.info("Gebäude fertig: '%s' (finish_time=%s)", name, item.finish_time)
                await self._notify(f"🏗️ <b>Gebäude fertiggestellt!</b>\n{name}")

    async def _run_turn(self) -> None:
        try:
            raw = await self._scraper.scrape()
            state = parse_state(raw)
            self._last_state = state
            self._last_raw = raw

            # Initialzustand beim ersten Scrape merken
            if self._stats.initial_resources is None:
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

            # Wenn Bot pausiert: keine Entscheidungen treffen, nur protokollieren
            if ts.is_paused():
                logger.info("Bot pausiert — Runde %d übersprungen.", self._stats.turns_completed + 1)
                self._stats.turns_completed += 1
                return

            actions = self._strategy.decide(state)
            ts.update_planned([_action_to_task(a) for a in actions])

            for action in actions:
                task = _action_to_task(action)
                ts.set_running(task)
                success = await self._executor.execute(action)
                ts.set_done(task, success)

                # Cooldown-Tracking für gebäudespezifische Aktionen
                btype = action.params.get("building_type", "") if action.type in (
                    "build_specific", "build_storage"
                ) else ""

                if success:
                    self._stats.actions_executed += 1
                    # Bau- und Forschungsstarts in der DB festhalten
                    self._record_action_event(action)
                    if btype:
                        cooldown.record_success(btype)
                else:
                    self._stats.actions_failed += 1
                    logger.warning("Aktion fehlgeschlagen: %s", action)
                    if btype:
                        cooldown.record_failure(btype, reason=action.params.get("reason", ""))
                await asyncio.sleep(self._config.bot.action_delay_ms / 1000)

            self._stats.turns_completed += 1
            self._consecutive_failures = 0

        except Exception as e:
            self._consecutive_failures += 1
            ts.set_status("error", str(type(e).__name__))
            logger.error("Runde fehlgeschlagen (%d/%d): %s",
                         self._consecutive_failures, self._config.bot.max_retries,
                         type(e).__name__)

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
