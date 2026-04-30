"""Sicherer Credential-Store für Spiel-Zugangsdaten und Telegram-Secrets.

Wird in data/credentials.json gespeichert (nie in config.toml committed).
Dashboard kann Credentials einmalig schreiben — lesen gibt nur Metadaten zurück
(ob konfiguriert, welcher Benutzername, welche URL), niemals Passwörter/Tokens.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CREDENTIALS_PATH = Path("data/credentials.json")

# Pflichtfelder die gesetzt sein müssen damit der Bot startet
_REQUIRED = {"game_url", "username", "password"}

_lock = threading.RLock()


def _load_raw() -> dict[str, Any]:
    if not CREDENTIALS_PATH.exists():
        return {}
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("credentials.json Ladefehler: %s", e)
        return {}


def save(data: dict[str, Any]) -> None:
    """Speichert Credentials. Leere Strings für optionale Felder erlaubt."""
    with _lock:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Bestehende Werte laden und mergen — so werden einzelne Felder
        # aktualisiert ohne alle neu eingeben zu müssen.
        existing = _load_raw()
        # Leere Strings NICHT überschreiben (Nutzer hat Feld freigelassen)
        for k, v in data.items():
            if v != "" or k not in existing:
                existing[k] = v
        with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        logger.info("Credentials gespeichert (Felder: %s)", ", ".join(existing.keys()))


def load() -> dict[str, Any]:
    """Gibt alle gespeicherten Credentials zurück (inkl. Passwörter).

    Nur intern vom Config-Loader aufrufen — niemals über API exponieren!
    """
    with _lock:
        return _load_raw()


def is_configured() -> bool:
    """True wenn alle Pflichtfelder (URL, User, Passwort) gesetzt sind."""
    data = load()
    return all(data.get(f, "").strip() for f in _REQUIRED)


def status() -> dict[str, Any]:
    """Gibt sichere Metadaten zurück — niemals Passwörter oder Tokens.

    Wird vom Dashboard-Endpunkt /api/setup/status ausgeliefert.
    """
    data = load()
    return {
        "configured": is_configured(),
        "username":   data.get("username", ""),
        "game_url":   data.get("game_url",  ""),
        # Ob optionale Felder gesetzt sind — aber nicht den Wert selbst
        "telegram_configured": bool(
            data.get("telegram_token", "").strip()
            and data.get("telegram_chat_id", "").strip()
        ),
    }
