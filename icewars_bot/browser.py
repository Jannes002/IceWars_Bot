from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page, Playwright

from .config import Config

logger = logging.getLogger(__name__)


class BrowserManager:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("Browser not started. Call start() first.")
        return self._page

    async def start(self) -> Page:
        logger.info("Starting browser (headless=%s)", self._config.browser.headless)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self._config.browser.headless,
            slow_mo=self._config.browser.slow_mo_ms,
        )
        self._page = await self._browser.new_page(
            viewport=self._config.browser.viewport,
        )
        return self._page

    async def restart(self) -> Page:
        logger.warning("Restarting browser...")
        await self.stop()
        return await self.start()

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._page = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser stopped.")
