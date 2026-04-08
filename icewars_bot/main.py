from __future__ import annotations

import argparse
import asyncio
import datetime
import logging
import logging.handlers
import signal
import sys
import threading
from pathlib import Path

from .actions import ActionExecutor
from .auth import Authenticator
from .bot import BotLoop
from .browser import BrowserManager
from .config import Config
from .dashboard import run_dashboard
from .db import DB_PATH
from .scraper import GameScraper
from .strategy import Strategy

logger = logging.getLogger(__name__)


def setup_logging() -> Path:
    """Richtet Console + zwei Log-Dateien ein.

    - icewars_bot.log       — aktueller Lauf (wird bei jedem Start überschrieben)
    - icewars_bot_debug.log — fortlaufendes Debug-Log mit Rotation (10 MB × 5 Dateien)
                              kann direkt in Claude Code hochgeladen werden
    """
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    # Zeitstempel für den Start dieser Session
    session_ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    fmt_normal = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    fmt_debug = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)s (%(filename)s:%(lineno)d): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # 1) Konsole: INFO+
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt_normal)
    root.addHandler(console)

    # 2) icewars_bot.log — aktueller Lauf, INFO+, wird überschrieben
    session_log_path = log_dir / "icewars_bot.log"
    session_handler = logging.FileHandler(session_log_path, mode="w", encoding="utf-8")
    session_handler.setLevel(logging.INFO)
    session_handler.setFormatter(fmt_normal)
    root.addHandler(session_handler)

    # 3) icewars_bot_debug.log — fortlaufend, DEBUG+, rotierend (10 MB × 5)
    debug_log_path = log_dir / "icewars_bot_debug.log"
    debug_handler = logging.handlers.RotatingFileHandler(
        debug_log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    debug_handler.setLevel(logging.DEBUG)
    debug_handler.setFormatter(fmt_debug)
    root.addHandler(debug_handler)

    # Session-Header in Debug-Log schreiben
    sep = "=" * 70
    logging.getLogger(__name__).info(
        "\n%s\n  NEUE SESSION gestartet: %s\n  Debug-Log: %s\n%s",
        sep, session_ts, debug_log_path.resolve(), sep,
    )

    return debug_log_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Icewars Bot")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Path to config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--headless",
        type=lambda x: x.lower() != "false",
        default=None,
        help="Override headless mode (true/false)",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Take a page snapshot after login and exit (useful for debugging selectors)",
    )
    parser.add_argument(
        "--dump-buildings",
        action="store_true",
        help="Dump build view (API + DOM) to logs/build_dump.json and exit. "
             "Used to collect diagnostic data for strategy tuning.",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        default=True,
        help="Start web dashboard alongside the bot (default: on)",
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_false",
        dest="dashboard",
        help="Disable the web dashboard",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8050,
        help="Dashboard HTTP port (default: 8050)",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    if not args.config.exists():
        print(f"Config file not found: {args.config}")
        print("Copy config.example.toml to config.toml and fill in your credentials.")
        sys.exit(1)

    config = Config.load(args.config)

    if args.headless is not None:
        config.browser.headless = args.headless

    browser = BrowserManager(config)
    strategy = Strategy(config)

    # Wire up components after browser starts
    await browser.start()
    try:
        page = browser.page

        auth = Authenticator(page, config)
        scraper = GameScraper(page)
        executor = ActionExecutor(page)

        if args.snapshot:
            if await auth.ensure_logged_in():
                await scraper.snapshot("logs/snapshot.html")
                print("Snapshot saved to logs/snapshot.html")
            return

        if args.dump_buildings:
            if await auth.ensure_logged_in():
                json_path = await scraper.dump_build_view("logs")
                print(f"Build dump saved to {json_path}")
                print("Bitte logs/build_dump.json (und ggf. logs/view_build.html) teilen.")
            else:
                print("Login fehlgeschlagen — siehe logs/login_failed.png")
            return

        bot = BotLoop(browser, scraper, strategy, executor, auth, config)

        # Dashboard im Hintergrund-Thread starten
        if args.dashboard:
            dash_thread = threading.Thread(
                target=run_dashboard,
                kwargs={"port": args.dashboard_port, "db_path": DB_PATH},
                daemon=True,
                name="dashboard",
            )
            dash_thread.start()
            logger.info("Dashboard laeuft auf http://localhost:%d", args.dashboard_port)

        # Handle Ctrl+C gracefully — auf Unix via Loop-Signalhandler,
        # auf Windows läuft asyncio.run() ohnehin sauber durch KeyboardInterrupt.
        main_task = asyncio.current_task()
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda: main_task.cancel() if main_task else None)
                except (NotImplementedError, RuntimeError):
                    pass

        try:
            await bot.run()
        except asyncio.CancelledError:
            logger.info("Bot abgebrochen — fahre herunter.")
    finally:
        # Browser IMMER sauber schließen, sonst gibt's auf Windows
        # ProactorBasePipeTransport-Warnungen beim Interpreter-Shutdown.
        try:
            await browser.stop()
        except Exception as e:
            logger.warning("Browser-Stop-Fehler ignoriert: %s", e)


def _silence_windows_proactor_warnings() -> None:
    """Unterdrückt die harmlosen 'I/O operation on closed pipe'-Warnungen,
    die unter Windows + Python 3.12+ in Kombination mit Playwright/Subprocess
    während des Interpreter-Shutdowns aus dem ProactorBasePipeTransport kommen.
    Sie sind kosmetisch und beeinflussen den Bot-Lauf nicht.
    """
    if sys.platform != "win32":
        return
    import warnings
    warnings.filterwarnings(
        "ignore",
        message=".*I/O operation on closed pipe.*",
        category=ResourceWarning,
    )
    # Auch Asyncio-Loop-Cleanup-Spam dämpfen
    asyncio_logger = logging.getLogger("asyncio")
    asyncio_logger.setLevel(logging.CRITICAL)


def main() -> None:
    debug_log = setup_logging()
    _silence_windows_proactor_warnings()
    args = parse_args()
    log = logging.getLogger(__name__)
    log.info("Debug-Log für Claude Code Upload: %s", debug_log.resolve())
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        log.info("Beendet durch Benutzer (Ctrl+C).")


if __name__ == "__main__":
    main()
