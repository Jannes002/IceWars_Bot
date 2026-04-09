"""Telegram-Benachrichtigungen für den IceWars-Bot.

Sendet Nachrichten über die Telegram Bot API ohne externe Abhängigkeiten
(nur Python-Stdlib urllib). Fehler beim Senden werden nur geloggt —
der Bot läuft immer weiter, auch wenn Telegram nicht erreichbar ist.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.error
import urllib.request
from typing import Optional

# Telegram-API nutzt gültige Zertifikate, aber auf Windows schlägt die
# Verifikation manchmal wegen fehlender/veralteter CA-Ketten fehl.
# Wir deaktivieren die Verifikation nur für diesen einen Endpunkt.
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Sendet Nachrichten an einen Telegram-Chat via Bot-API."""

    def __init__(self, token: str, chat_id: str) -> None:
        self._chat_id = str(chat_id)
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._url = f"{self._base_url}/sendMessage"

    @property
    def chat_id(self) -> str:
        return self._chat_id

    async def send(self, text: str) -> bool:
        """Sendet eine Nachricht. Gibt True zurück wenn erfolgreich.

        Blockiert den Event-Loop nicht (läuft im Thread-Pool).
        """
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._send_sync, text)
            logger.debug("Telegram ✓: %s", text[:60])
            return True
        except urllib.error.HTTPError as e:
            logger.warning("Telegram HTTP-Fehler %s: %s", e.code, e.read()[:200])
        except Exception as e:
            logger.warning("Telegram-Fehler: %s", e)
        return False

    def _send_sync(self, text: str) -> None:
        """Synchroner HTTP-POST (wird im Executor aufgerufen)."""
        payload = json.dumps({
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            resp.read()

    def _get_updates_sync(self, offset: int, timeout: int) -> list:
        """Synchrones long-polling für Telegram-Updates (wird im Executor aufgerufen)."""
        import urllib.parse
        params = urllib.parse.urlencode({
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": '["message"]',
        })
        url = f"{self._base_url}/getUpdates?{params}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout + 10, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        return data.get("result", [])

    async def get_updates(self, offset: int = 0, timeout: int = 30) -> list:
        """Holt neue Updates via long-polling. Gibt eine Liste von Update-Dicts zurück."""
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._get_updates_sync, offset, timeout)
        except urllib.error.HTTPError as e:
            logger.debug("getUpdates HTTP-Fehler %s", e.code)
        except Exception as e:
            logger.debug("getUpdates Fehler: %s", e)
        return []


def make_notifier(config: object) -> Optional[TelegramNotifier]:
    """Erstellt einen TelegramNotifier aus der Config, oder None wenn deaktiviert."""
    tg = getattr(config, "telegram", None)
    if tg is None or not getattr(tg, "enabled", False):
        return None
    token = getattr(tg, "token", "").strip()
    chat_id = getattr(tg, "chat_id", "").strip()
    if not token or not chat_id:
        logger.warning("Telegram aktiviert, aber token/chat_id fehlt in config.toml.")
        return None
    logger.info("Telegram-Benachrichtigungen aktiv (chat_id=%s).", chat_id)
    return TelegramNotifier(token=token, chat_id=chat_id)
