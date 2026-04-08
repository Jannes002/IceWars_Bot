from __future__ import annotations

import logging

from playwright.async_api import Page

from .config import Config

logger = logging.getLogger(__name__)


class Authenticator:
    def __init__(self, page: Page, config: Config) -> None:
        self._page = page
        self._config = config

    async def ensure_logged_in(self) -> bool:
        await self._page.goto(self._config.auth.game_url)

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
            await self._page.fill("#login-username", self._config.auth.username)
            await self._page.fill("#login-password", self._config.auth.password)
            await self._page.click("#btn-login")
            await self._page.wait_for_load_state("networkidle")

            if await self._is_logged_in():
                logger.info("Login OK.")
                return True
            else:
                logger.error("Login fehlgeschlagen — Zugangsdaten prüfen.")
                return False
        except Exception as e:
            logger.error("Login: %s", type(e).__name__)
            return False
