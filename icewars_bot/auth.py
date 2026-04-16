from __future__ import annotations

import logging

from playwright.async_api import Page

from .config import Config

logger = logging.getLogger(__name__)


def _normalize_game_url(url: str) -> str:
    """Stellt sicher, dass die Game-URL ein Protokoll hat.

    Playwright weigert sich, URLs ohne Schema anzusteuern
    (``Cannot navigate to invalid URL``). Akzeptiert werden daher neben
    ``https://host/`` auch ``host`` oder ``www.host.de`` aus der Config.
    """
    s = (url or "").strip()
    if not s:
        return s
    lower = s.lower()
    if lower.startswith(("http://", "https://")):
        return s
    # Schemaloses Eingabeformat → https:// voranstellen.
    # Spezialfall ``//host`` (protokollrelativ) ebenfalls zu https aufwerten.
    if s.startswith("//"):
        return "https:" + s
    return "https://" + s


class Authenticator:
    def __init__(self, page: Page, config: Config) -> None:
        self._page = page
        self._config = config

    async def ensure_logged_in(self) -> bool:
        url = _normalize_game_url(self._config.auth.game_url)
        if url != self._config.auth.game_url:
            logger.info(
                "Game-URL ohne Protokoll in config.toml — normalisiert: '%s' → '%s'",
                self._config.auth.game_url, url,
            )
        await self._page.goto(url)

        if await self._is_logged_in():
            logger.info("Already logged in.")
            return True

        logger.info("Not logged in — attempting login...")
        return await self._do_login()

    async def _is_logged_in(self) -> bool:
        try:
            token = await self._page.evaluate("() => localStorage.getItem('icewars_token')")
            return bool(token)
        except Exception:
            return False

    async def _do_login(self) -> bool:
        try:
            logger.info("Login-Versuch mit Benutzer '%s'...", self._config.auth.username)
            await self._page.fill("#login-username", self._config.auth.username)
            await self._page.fill("#login-password", self._config.auth.password)
            await self._page.click("#btn-login")
            await self._page.wait_for_load_state("networkidle")

            if await self._is_logged_in():
                logger.info("Login OK.")
                return True

            # Diagnose: Was sagt die Seite?
            error_text = await self._extract_login_error()
            current_url = self._page.url
            logger.error(
                "Login fehlgeschlagen | User='%s' | URL=%s | Fehler='%s'",
                self._config.auth.username,
                current_url,
                error_text or "(keine Fehlermeldung gefunden)",
            )

            # Screenshot zur Diagnose
            try:
                from pathlib import Path
                shot_dir = Path("logs")
                shot_dir.mkdir(exist_ok=True)
                shot_path = shot_dir / "login_failed.png"
                await self._page.screenshot(path=str(shot_path))
                logger.error("Screenshot: %s", shot_path.resolve())
            except Exception:
                pass

            return False
        except Exception as e:
            logger.error("Login-Exception: %s: %s", type(e).__name__, e)
            return False

    async def _extract_login_error(self) -> str:
        """Versucht, eine sichtbare Fehlermeldung von der Login-Seite zu lesen."""
        selectors = [
            "#login-error",
            ".login-error",
            ".error-message",
            "[class*='error']",
            "#message",
        ]
        for sel in selectors:
            try:
                el = await self._page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text:
                        return text
            except Exception:
                continue
        # Fallback: nach "falsch", "invalid", "wrong" im Body suchen
        try:
            body_text = await self._page.evaluate("() => document.body.innerText")
            for keyword in ("falsch", "invalid", "wrong", "incorrect", "ungültig"):
                if keyword.lower() in body_text.lower():
                    # Zeile mit dem Keyword extrahieren
                    for line in body_text.split("\n"):
                        if keyword.lower() in line.lower():
                            return line.strip()[:200]
        except Exception:
            pass
        return ""
