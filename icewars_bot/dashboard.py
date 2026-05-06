"""Standalone Web-Dashboard für Icewars-Bot Statistiken.

Startet einen einfachen HTTP-Server der:
- Eine interaktive Webseite mit Chart.js Graphen ausliefert
- JSON-API-Endpunkte für Snapshot- und Session-Daten bereitstellt

Usage:
    python -m icewars_bot.dashboard [--port 8050] [--db data/icewars_history.db]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

from .db import (
    get_snapshots, get_sessions, get_latest_snapshot, get_snapshot_count,
    get_highscores, get_highscore_timeline, get_latest_highscore,
    get_build_events, get_activity_log, record_activity,
    get_snapshot_planets,
    DB_PATH,
)
from . import task_state as ts
from . import goals as G
from . import strategy as _strategy
from . import credentials as creds
from . import planets_store

logger = logging.getLogger(__name__)

DASHBOARD_HTML = (Path(__file__).parent / "static" / "dashboard.html").resolve()

# ── Session-Store (in-memory; Sessions laufen bei Bot-Neustart ab) ────────────
_sessions: dict[str, float] = {}       # token → expiry (Unix-Timestamp)
_SESSION_MAX_AGE_S: int = 8 * 3600    # 8 Stunden


def _get_dashboard_password() -> str:
    """Liest das Dashboard-Passwort.

    Reihenfolge: Umgebungsvariable → credentials.json.
    Leerer String bedeutet: kein Passwort gesetzt → kein Auth.
    """
    pw = os.environ.get("ICEWARS_DASHBOARD_PASSWORD", "").strip()
    if pw:
        return pw
    return creds.load().get("dashboard_password", "").strip()


def _is_authenticated(cookie_header: str) -> bool:
    """True wenn der Cookie einen gültigen, nicht abgelaufenen Session-Token enthält."""
    if not cookie_header:
        return False
    try:
        c = SimpleCookie(cookie_header)
        morsel = c.get("icewars_session")
        if not morsel:
            return False
        expiry = _sessions.get(morsel.value, 0)
        return expiry > time.time()
    except Exception:
        return False


def _cleanup_sessions() -> None:
    """Entfernt abgelaufene Sessions."""
    now = time.time()
    for token in list(_sessions):
        if _sessions[token] < now:
            del _sessions[token]


_LOGIN_HTML = """\
<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>IceWars Bot — Login</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #080d18;
      color: #c8d0e0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #111827;
      border: 1px solid #1e2d4a;
      border-radius: 14px;
      padding: 2.5rem 2.25rem;
      width: 100%;
      max-width: 360px;
      box-shadow: 0 12px 40px rgba(0,0,0,.5);
    }}
    .logo {{
      text-align: center;
      margin-bottom: 2rem;
    }}
    .logo h1 {{
      font-size: 1.6rem;
      color: #4da6ff;
      font-weight: 700;
      letter-spacing: -.5px;
    }}
    .logo p {{
      margin-top: .3rem;
      font-size: .82rem;
      color: #4a5a70;
    }}
    label {{
      display: block;
      font-size: .82rem;
      color: #7a8fa8;
      margin-bottom: .4rem;
      font-weight: 500;
      letter-spacing: .02em;
    }}
    input[type=password] {{
      width: 100%;
      padding: .7rem 1rem;
      background: #0d1520;
      border: 1px solid #1e2d4a;
      border-radius: 8px;
      color: #c8d0e0;
      font-size: 1rem;
      outline: none;
      transition: border-color .15s;
    }}
    input[type=password]:focus {{ border-color: #4da6ff; }}
    button {{
      width: 100%;
      padding: .72rem;
      background: #4da6ff;
      color: #080d18;
      border: none;
      border-radius: 8px;
      font-size: 1rem;
      font-weight: 700;
      cursor: pointer;
      margin-top: 1.2rem;
      transition: background .15s;
    }}
    button:hover {{ background: #3d96ef; }}
    .error {{
      background: rgba(255,80,80,.1);
      border: 1px solid rgba(255,80,80,.3);
      border-radius: 7px;
      padding: .55rem .9rem;
      color: #ff6868;
      font-size: .82rem;
      margin-bottom: 1.2rem;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <h1>🧊 IceWars Bot</h1>
      <p>Dashboard-Zugang</p>
    </div>
    {error_block}
    <form method="POST" action="/login">
      <input type="hidden" name="next" value="{next_url}">
      <label for="pw">Passwort</label>
      <input type="password" id="pw" name="password"
             autofocus autocomplete="current-password" placeholder="••••••••">
      <button type="submit">Anmelden</button>
    </form>
  </div>
</body>
</html>
"""


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

    # ── Auth-Hilfsmethoden ────────────────────────────────────────────────

    def _check_auth(self) -> bool:
        """True wenn kein Passwort konfiguriert ODER Session gültig ist."""
        if not _get_dashboard_password():
            return True
        return _is_authenticated(self.headers.get("Cookie", ""))

    def _redirect(self, location: str, status: int = 302) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _auth_redirect(self, next_url: str = "/") -> None:
        """Leitet zur Login-Seite um. Merkt sich die ursprüngliche URL."""
        # Nur relative Pfade als next-Ziel erlauben (Open-Redirect verhindern)
        safe_next = next_url if next_url.startswith("/") else "/"
        self._redirect(f"/login?next={quote(safe_next, safe='')}")

    def _make_session_cookie(self, token: str, max_age: int) -> str:
        """Erzeugt den Set-Cookie Header-Wert (Secure nur bei HTTPS)."""
        proto = self.headers.get("X-Forwarded-Proto", "http")
        secure = "; Secure" if proto == "https" else ""
        return (
            f"icewars_session={token}; HttpOnly; SameSite=Lax"
            f"; Max-Age={max_age}; Path=/{secure}"
        )

    def _clear_session_cookie(self) -> str:
        return "icewars_session=; HttpOnly; SameSite=Lax; Max-Age=0; Path=/"

    def _serve_login(self, next_url: str = "/", error: str = "") -> None:
        error_block = (
            f'<div class="error">{error}</div>' if error else ""
        )
        # Sanitize next_url: nur relative Pfade
        safe_next = next_url if next_url.startswith("/") else "/"
        html = _LOGIN_HTML.format(
            error_block=error_block,
            next_url=safe_next,
        )
        self._html_response(html)

    def _handle_login_post(self, body: bytes) -> None:
        """Verarbeitet das Login-Formular."""
        from urllib.parse import parse_qs as _parse_qs
        try:
            form = _parse_qs(body.decode("utf-8", errors="replace"))
            password = form.get("password", [""])[0]
            next_url = form.get("next", ["/"])[0]
            # Nur relative Pfade erlauben
            if not next_url.startswith("/"):
                next_url = "/"
        except Exception:
            password, next_url = "", "/"

        expected = _get_dashboard_password()
        if expected and secrets.compare_digest(
            password.encode("utf-8"), expected.encode("utf-8")
        ):
            token = secrets.token_hex(32)
            _sessions[token] = time.time() + _SESSION_MAX_AGE_S
            _cleanup_sessions()
            logger.info(
                "Dashboard-Login erfolgreich von %s.",
                self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or self.address_string(),
            )
            self.send_response(302)
            self.send_header("Location", next_url)
            self.send_header("Set-Cookie", self._make_session_cookie(token, _SESSION_MAX_AGE_S))
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            logger.warning(
                "Dashboard-Login fehlgeschlagen von %s.",
                self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP") or self.address_string(),
            )
            self._serve_login(next_url, "Falsches Passwort")

    def _handle_logout(self) -> None:
        """Löscht die Session und leitet zur Login-Seite."""
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = c.get("icewars_session")
            if morsel and morsel.value in _sessions:
                del _sessions[morsel.value]
        except Exception:
            pass
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", self._clear_session_cookie())
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)

            # ── Login (kein Auth nötig) ────────────────────────────────────
            if parsed.path == "/login":
                self._handle_login_post(body)
                return

            # ── Auth ──────────────────────────────────────────────────────
            if not self._check_auth():
                self._json_response({"error": "Nicht angemeldet"}, 401)
                return

            # ── Geschützte POST-Routen ─────────────────────────────────────
            data = json.loads(body) if body else {}

            if parsed.path == "/api/goals":
                updated = G.update(data)
                self._json_response(updated)
            elif parsed.path == "/api/setup":
                allowed = {"game_url", "username", "password",
                           "telegram_token", "telegram_chat_id",
                           "dashboard_password"}
                filtered = {k: v for k, v in data.items() if k in allowed and isinstance(v, str)}
                if not filtered:
                    self._json_response({"error": "Keine gültigen Felder"}, 400)
                else:
                    creds.save(filtered)
                    logger.info("Setup: Credentials aktualisiert via Dashboard.")
                    self._json_response(creds.status())
            elif parsed.path == "/api/goals/reset":
                self._json_response(G.reset())
            elif parsed.path == "/api/switch-planet":
                city_id = int(data.get("city_id", 0))
                if not city_id:
                    self._json_response({"error": "city_id erforderlich"}, 400)
                else:
                    ts.request_switch_planet(city_id)
                    logger.info("Planet-Wechsel angefordert via Dashboard: city_id=%d", city_id)
                    self._json_response({"queued": True, "city_id": city_id})
            elif parsed.path == "/api/remove-planet":
                city_id = int(data.get("city_id", 0))
                if not city_id:
                    self._json_response({"error": "city_id erforderlich"}, 400)
                else:
                    ts.request_remove_planet(city_id)
                    logger.info("Planet-Entfernung angefordert via Dashboard: city_id=%d", city_id)
                    self._json_response({"queued": True, "city_id": city_id})
            elif parsed.path == "/api/pause":
                ts.set_paused(True)
                logger.info("Bot pausiert via Dashboard.")
                record_activity("bot_pause", "Bot pausiert", "via Dashboard")
                self._json_response({"paused": True})
            elif parsed.path == "/api/resume":
                ts.set_paused(False)
                logger.info("Bot fortgesetzt via Dashboard.")
                record_activity("bot_resume", "Bot fortgesetzt", "via Dashboard")
                self._json_response({"paused": False})
            elif parsed.path == "/api/execute":
                success = ts.request_execute()
                if success:
                    logger.info("Execute-Anfrage vom Dashboard.")
                    self._json_response({"queued": True})
                else:
                    self._json_response(
                        {"queued": False, "reason": "Keine empfohlene Aktion oder bereits ausstehend"},
                        400,
                    )
            elif parsed.path == "/api/execute/donate":
                resource = data.get("resource", "")
                amount = int(data.get("amount", 0))
                if not resource or amount <= 0:
                    self._json_response({"error": "resource und amount erforderlich"}, 400)
                else:
                    ts.request_donate(resource, amount)
                    logger.info("Donate-Anfrage vom Dashboard: %s=%d", resource, amount)
                    self._json_response({"queued": True})
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
            # ── Auth-freie Routen ─────────────────────────────────────────
            if path == "/login":
                self._serve_login(
                    next_url=qs.get("next", ["/"])[0],
                    error=qs.get("error", [""])[0],
                )
                return
            if path == "/logout":
                self._handle_logout()
                return
            if path == "/api/setup/status":
                # Healthcheck + Dashboard-Konfig → immer frei
                self._json_response(creds.status())
                return

            # ── Auth ──────────────────────────────────────────────────────
            if not self._check_auth():
                if path.startswith("/api/"):
                    self._json_response({"error": "Nicht angemeldet"}, 401)
                else:
                    self._auth_redirect(path)
                return

            # ── Geschützte Routen ─────────────────────────────────────────
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
            elif path == "/api/activity-log":
                self._api_activity_log(qs)
            elif path == "/api/snapshot-planets":
                self._api_snapshot_planets()
            elif path == "/api/tasks":
                self._json_response(ts.get())
            elif path == "/api/scoring":
                self._json_response({
                    "params": {
                        "time_alpha": _strategy.SCORE_TIME_ALPHA,
                        "diversify_k": _strategy.SCORE_DIVERSIFY_K,
                    },
                    "rows": ts.get_scoring_snapshot(),
                })
            elif path == "/api/goals":
                self._json_response(G.get())
            elif path == "/api/colonies":
                snapshots = ts.get_colonies_snapshots()
                self._json_response({
                    "current_city_id": ts.get_current_city_id(),
                    "colonies": sorted(snapshots.values(), key=lambda c: c.get("city_id", 0)),
                })
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
        city_id = int(qs["city_id"][0]) if "city_id" in qs else None
        data = get_snapshots(from_epoch, to_epoch, limit, self.db_path, city_id=city_id)
        self._json_response(data)

    def _api_sessions(self, qs: dict) -> None:
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        data = get_sessions(from_epoch, to_epoch, self.db_path)
        self._json_response(data)

    def _api_latest(self) -> None:
        qs = parse_qs(urlparse(self.path).query)
        city_id = int(qs["city_id"][0]) if "city_id" in qs else None
        data = get_latest_snapshot(self.db_path, city_id=city_id)
        self._json_response(data or {})

    def _api_snapshot_planets(self) -> None:
        planets_db = get_snapshot_planets(self.db_path)
        colonies = ts.get_colonies_snapshots()
        # Namen aus colonies_snapshots anreichern
        for p in planets_db:
            col = colonies.get(p["city_id"], {})
            p["name"]   = col.get("city_name", "")
            p["coords"] = col.get("coords", "")
        self._json_response(planets_db)

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

    def _api_activity_log(self, qs: dict) -> None:
        from_epoch = float(qs["from"][0]) if "from" in qs else None
        to_epoch = float(qs["to"][0]) if "to" in qs else None
        categories = qs["category"][0].split(",") if "category" in qs else None
        limit = int(qs.get("limit", [200])[0])
        offset = int(qs.get("offset", [0])[0])
        data = get_activity_log(from_epoch, to_epoch, categories, limit, offset, self.db_path)
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
