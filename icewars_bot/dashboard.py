"""Standalone Web-Dashboard für Icewars-Bot Statistiken.

Startet einen einfachen HTTP-Server der:
- Eine interaktive Webseite mit Chart.js Graphen ausliefert
- JSON-API-Endpunkte für Snapshot- und Session-Daten bereitstellt

Usage:
    python -m icewars_bot.dashboard [--port 8050] [--db data/icewars_history.db]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .db import (
    get_snapshots, get_sessions, get_latest_snapshot, get_snapshot_count,
    get_highscores, get_highscore_timeline, get_latest_highscore,
    get_build_events,
    DB_PATH,
)
from . import task_state as ts
from . import goals as G

logger = logging.getLogger(__name__)

DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").resolve()


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP-Handler für Dashboard + JSON-API."""

    db_path: Path = DB_PATH

    def log_message(self, format, *args):
        logger.debug(format, *args)

    def _json_response(self, data: dict | list, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html: str, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            data = json.loads(body) if body else {}

            if parsed.path == "/api/goals":
                updated = G.update(data)
                self._json_response(updated)
            elif parsed.path == "/api/goals/reset":
                self._json_response(G.reset())
            elif parsed.path == "/api/pause":
                ts.set_paused(True)
                logger.info("Bot pausiert via Dashboard.")
                self._json_response({"paused": True})
            elif parsed.path == "/api/resume":
                ts.set_paused(False)
                logger.info("Bot fortgesetzt via Dashboard.")
                self._json_response({"paused": False})
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            logger.error("POST error: %s", e)
            self._json_response({"error": str(e)}, 500)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._serve_dashboard()
            elif path == "/api/snapshots":
                self._api_snapshots(qs)
            elif path == "/api/sessions":
                self._api_sessions(qs)
            elif path == "/api/latest":
                self._api_latest()
            elif path == "/api/stats":
                self._api_stats()
            elif path == "/api/highscores":
                self._api_highscores(qs)
            elif path == "/api/highscore/timeline":
                self._api_highscore_timeline(qs)
            elif path == "/api/highscore/latest":
                self._api_highscore_latest(qs)
            elif path == "/api/build-events":
                self._api_build_events(qs)
            elif path == "/api/tasks":
                self._json_response(ts.get())
            elif path == "/api/goals":
                self._json_response(G.get())
            else:
                self.send_error(404, "Not Found")
        except Exception as e:
            logger.error("Request error: %s", e)
            self._json_response({"error": str(e)}, 500)

    def _serve_dashboard(self) -> None:
        if DASHBOARD_HTML.exists():
            html = DASHBOARD_HTML.read_text(encoding="utf-8")
        else:
            html = "<h1>Dashboard HTML not found</h1><p>Expected: {}</p>".format(DASHBOARD_HTML)
        self._html_response(html)

    def _api_snapshots(self, qs: dict) -> None:
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        limit = int(qs.get("limit", [10000])[0])
        data = get_snapshots(from_epoch, to_epoch, limit, self.db_path)
        self._json_response(data)

    def _api_sessions(self, qs: dict) -> None:
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        data = get_sessions(from_epoch, to_epoch, self.db_path)
        self._json_response(data)

    def _api_latest(self) -> None:
        data = get_latest_snapshot(self.db_path)
        self._json_response(data or {})

    def _api_stats(self) -> None:
        count = get_snapshot_count(self.db_path)
        latest = get_latest_snapshot(self.db_path)
        self._json_response({
            "snapshot_count": count,
            "latest_timestamp": latest["timestamp"] if latest else None,
            "db_path": str(self.db_path.resolve()),
        })

    def _api_highscores(self, qs: dict) -> None:
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        category = qs["category"][0] if "category" in qs else None
        username = qs["username"][0] if "username" in qs else None
        data = get_highscores(from_epoch, to_epoch, category, username, self.db_path)
        self._json_response(data)

    def _api_highscore_timeline(self, qs: dict) -> None:
        username = qs.get("username", [""])[0]
        category = qs.get("category", ["points"])[0]
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        if not username:
            self._json_response({"error": "username parameter required"}, 400)
            return
        data = get_highscore_timeline(username, category, from_epoch, to_epoch, self.db_path)
        self._json_response(data)

    def _api_highscore_latest(self, qs: dict) -> None:
        category = qs.get("category", ["points"])[0]
        data = get_latest_highscore(category, self.db_path)
        self._json_response(data)

    def _api_build_events(self, qs: dict) -> None:
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        event_type = qs["type"][0] if "type" in qs else None
        data = get_build_events(from_epoch, to_epoch, event_type, self.db_path)
        self._json_response(data)


def run_dashboard(port: int = 8050, db_path: Path = DB_PATH) -> None:
    """Startet den Dashboard-Webserver.

    Kann standalone oder als Daemon-Thread aus dem Bot heraus gestartet werden.
    """
    DashboardHandler.db_path = db_path

    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    logger.info("Dashboard gestartet: http://localhost:%d  (DB: %s)", port, db_path.resolve())

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        logger.info("Dashboard gestoppt.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Icewars Bot Dashboard")
    parser.add_argument("--port", type=int, default=8050, help="HTTP Port (default: 8050)")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Pfad zur SQLite-DB")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_dashboard(args.port, args.db)


if __name__ == "__main__":
    main()
